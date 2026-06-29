"""FastAPI 웹 서버 — UI에 실시간 데이터 제공."""

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import StreamingResponse

from src import db, live, state
from src.modules.f4_tracking import HARD_STOP_RATIO, STEP_TRAIL

KST = ZoneInfo("Asia/Seoul")
_MODE = os.getenv("KIS_MODE", "PAPER")
_LOG_DIR = Path(os.getenv("LOG_DIR", "data/logs"))
_HTML_DIR = Path(__file__).parent.parent.parent / "docs" / "html"

app = FastAPI(title="Daily1 Trading UI", docs_url=None, redoc_url=None)


# ── 정적 파일 / 인덱스 ────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory=str(_HTML_DIR)), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(str(_HTML_DIR / "index.html"))


# ── /api/status ──────────────────────────────────────────────────────

@app.get("/api/status")
async def api_status() -> JSONResponse:
    s = state.get()
    entry = s.entry_price or 0.0
    cur = live.last_tick_price
    pnl_pct = round((cur / entry - 1) * 100, 2) if (cur and entry) else None

    # 하드스탑 가격 계산
    hard_stop = round(entry * (1 - HARD_STOP_RATIO)) if entry else None
    trail_stop: float | None = None
    if s.trailing_active and entry and s.highest_step:
        trail_stop = round(entry * (1 + s.highest_step - STEP_TRAIL))

    return JSONResponse({
        "mode": _MODE,
        "position_status": s.position_status,
        "ticker": s.target_ticker,
        "entry_price": s.entry_price,
        "entry_qty": s.entry_qty,
        "remaining_qty": s.remaining_qty,
        "high_price": s.high_price,
        "current_price": cur,
        "pnl_pct": pnl_pct,
        "trailing_active": s.trailing_active,
        "highest_step": s.highest_step,
        "hard_stop": hard_stop,
        "trail_stop": trail_stop,
        "ws_connected": live.ws_connected,
        "ntp_offset_ms": live.ntp_offset_ms,
        "ntp_level": live.ntp_level,
        "close_reason": s.close_reason,
    })


# ── /api/logs ────────────────────────────────────────────────────────

@app.get("/api/logs")
async def api_logs(n: int = 60) -> JSONResponse:
    today = datetime.now(KST).strftime("%Y%m%d")
    path = _LOG_DIR / f"{today}.jsonl"
    lines: list[dict] = []
    if path.exists():
        raw = path.read_text(encoding="utf-8").strip().splitlines()
        for line in raw[-n:]:
            try:
                lines.append(json.loads(line))
            except Exception:
                pass
    lines.reverse()
    return JSONResponse(lines)


# ── /api/history ─────────────────────────────────────────────────────

@app.get("/api/history")
async def api_history(limit: int = 60) -> JSONResponse:
    try:
        conn = db.get()
        async with conn.execute(
            """SELECT date, ticker, entry_price, exit_price,
                      pnl_pct, close_reason, highest_step, pyramided, status
               FROM trades
               ORDER BY date DESC
               LIMIT ?""",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        result = [dict(r) for r in rows]
    except Exception:
        result = []
    return JSONResponse(result)


# ── /api/stats ───────────────────────────────────────────────────────

@app.get("/api/stats")
async def api_stats() -> JSONResponse:
    try:
        conn = db.get()

        # 전체 집계
        async with conn.execute(
            """SELECT
                COUNT(*) as total,
                SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) as wins,
                AVG(pnl_pct) as avg_pnl,
                MIN(pnl_pct) as max_loss,
                MAX(pnl_pct) as max_gain
               FROM trades WHERE status='CLOSED'"""
        ) as cur:
            agg = dict(await cur.fetchone())

        # 청산 사유별 평균 손익
        async with conn.execute(
            """SELECT close_reason,
                      COUNT(*) as n,
                      AVG(pnl_pct) as avg_pnl
               FROM trades WHERE status='CLOSED'
               GROUP BY close_reason"""
        ) as cur:
            rows = await cur.fetchall()
        by_reason = {r["close_reason"]: {"n": r["n"], "avg_pnl": round(r["avg_pnl"] or 0, 2)}
                     for r in rows}

        # 월별 누적 손익
        async with conn.execute(
            """SELECT substr(date,1,6) as ym,
                      COUNT(*) as n,
                      SUM(pnl_pct) as sum_pnl
               FROM trades WHERE status='CLOSED'
               GROUP BY ym ORDER BY ym"""
        ) as cur:
            rows = await cur.fetchall()
        monthly = [{"ym": r["ym"], "n": r["n"], "sum_pnl": round(r["sum_pnl"] or 0, 2)}
                   for r in rows]

        total = agg.get("total") or 0
        wins = agg.get("wins") or 0
        return JSONResponse({
            "total": total,
            "wins": wins,
            "losses": total - wins,
            "win_rate": round(wins / total * 100, 1) if total else 0,
            "avg_pnl": round(agg.get("avg_pnl") or 0, 2),
            "max_loss": round(agg.get("max_loss") or 0, 2),
            "max_gain": round(agg.get("max_gain") or 0, 2),
            "by_reason": by_reason,
            "monthly": monthly,
        })
    except Exception:
        return JSONResponse({"total": 0, "wins": 0, "losses": 0, "win_rate": 0,
                             "avg_pnl": 0, "max_loss": 0, "max_gain": 0,
                             "by_reason": {}, "monthly": []})


# ── /api/stream (SSE) ────────────────────────────────────────────────

@app.get("/api/stream")
async def api_stream(request: Request) -> StreamingResponse:
    queue = live.subscribe()

    async def event_gen():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            live.unsubscribe(queue)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
