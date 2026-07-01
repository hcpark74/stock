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
from src.api import kis_rest
from src.api.status_logic import (
    f1_summary_from_rows as _f1_summary_from_rows,
    f1_verdict as _f1_verdict,
    f3_detail_from_event as _f3_detail_from_event,
    latest_today_snapshot_path as _latest_today_snapshot_path,
    parse_asset_snapshot_response as _parse_asset_snapshot_response,
    pipeline_from_logs as _pipeline_from_logs,
    sort_f1_candidates_for_display as _sort_f1_candidates_for_display,
)
from src.modules.f4_tracking import HARD_STOP_RATIO, STEP_TRAIL
from src.modules.f1_filter import F1_SNAPSHOT_DIR

KST = ZoneInfo("Asia/Seoul")
_MODE = os.getenv("KIS_MODE", "PAPER")
_LOG_DIR = Path(os.getenv("LOG_DIR", "data/logs"))
_F1_SNAPSHOT_DIR = Path(F1_SNAPSHOT_DIR)
_HTML_DIR = Path(__file__).parent.parent.parent / "docs" / "html"
_STATUS_LOG_LIMIT = 50
_ASSET_CACHE_TTL_SEC = float(os.getenv("ASSET_CACHE_TTL_SEC", "60"))
_ASSET_CACHE: dict | None = None
_ASSET_CACHE_AT: float = 0.0
_ASSET_CACHE_LOCK = asyncio.Lock()
_BAL_TR = {"REAL": "TTTC8434R", "PAPER": "VTTC8434R"}

app = FastAPI(title="Daily1 Trading UI", docs_url=None, redoc_url=None)


# ── 정적 파일 / 인덱스 ────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory=str(_HTML_DIR)), name="static")
app.mount("/assets", StaticFiles(directory=str(_HTML_DIR / "assets")), name="assets")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(str(_HTML_DIR / "index.html"))


def _today() -> str:
    return datetime.now(KST).strftime("%Y%m%d")


def _read_today_logs(limit: int | None = None) -> list[dict]:
    path = _LOG_DIR / f"{_today()}.jsonl"
    if not path.exists():
        return []

    raw_lines = path.read_text(encoding="utf-8").splitlines()
    if limit is not None and limit > 0:
        raw_lines = raw_lines[-limit:]

    result: list[dict] = []
    for line in raw_lines:
        try:
            result.append(json.loads(line))
        except Exception:
            pass
    return result


def _latest_f1_snapshot_path() -> Path | None:
    return _latest_today_snapshot_path(_F1_SNAPSHOT_DIR, _today())


def _read_f1_snapshot() -> tuple[Path | None, list[dict]]:
    path = _latest_f1_snapshot_path()
    if path is None:
        return None, []

    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    return path, rows


def _f1_status_from_logs(logs: list[dict]) -> tuple[str, dict | None]:
    status = "IDLE"
    last_event: dict | None = None
    for entry in logs:
        event = entry.get("event")
        if not str(event).startswith("F1") and event != "NO_TARGET":
            continue
        last_event = entry
        if event == "F1_API_ERROR":
            status = "FAILED"
        elif event == "F1_RETRY_WAIT":
            status = "RETRYING"
        elif event in {"F1_FETCH_DONE", "F1_FILTER_EMPTY", "F1_EXPECTED_COMPARE"}:
            status = "RUNNING"
        elif event == "F1_DONE":
            status = "DONE"
        elif event == "NO_TARGET":
            status = "NO_TARGET"
        # F1_SNAPSHOT_SAVED is a weak completion signal. It should only mark
        # DONE when no fetch/filter/done/no-target event has been seen yet.
        elif event == "F1_SNAPSHOT_SAVED" and status == "IDLE":
            status = "DONE"
    return status, last_event


def _selection_process_from_logs(summary: dict, logs: list[dict]) -> list[dict]:
    selected = summary.get("selected") or {}
    f1_ticker = selected.get("ticker")
    f1_count = summary.get("liquidity_pass") or summary.get("gap_pass") or 0
    candidate_tickers = {str(c.get("ticker")) for c in summary.get("candidates", []) if c.get("ticker")}
    steps = [{
        "key": "f1",
        "phase": "F1 선정",
        "tickers": summary.get("selected_tickers") or ([f1_ticker] if f1_ticker else []),
        "ticker": f1_ticker,
        "name": selected.get("name"),
        "gap_pct": selected.get("gap_pct"),
        "expected_amount": selected.get("expected_amount"),
        "status": "완료" if f1_ticker else "대기",
        "detail": f"{f1_count}개 후보",
    }]

    f2_event = next((e for e in reversed(logs) if e.get("event") == "TARGET_LOCKED"), None)
    f2_tickers = []
    if f2_event:
        f2_tickers = list(f2_event.get("target_tickers") or [])
        if not f2_tickers and f2_event.get("ticker"):
            f2_tickers = [f2_event.get("ticker")]
        if candidate_tickers and not (set(map(str, f2_tickers)) & candidate_tickers):
            f2_event = None
            f2_tickers = []
    steps.append({
        "key": "f2",
        "phase": "F2 선정",
        "tickers": f2_tickers,
        "ticker": f2_event.get("ticker") if f2_event else None,
        "gap_pct": (float(f2_event.get("gap_pct")) / 100) if f2_event and f2_event.get("gap_pct") is not None else None,
        "expected_price": f2_event.get("expected_price") if f2_event else None,
        "expected_amount": f2_event.get("expected_amount") if f2_event else None,
        "status": "잠금" if f2_event else "대기",
        "detail": ", ".join(str(t) for t in f2_tickers) if f2_tickers else "최대 3개 lock",
    })

    f3_events = {"F3_FINAL_PICK", "ENTRY_ORDER_SENT", "ENTRY_EXECUTED", "F3_ENTRY_BLOCKED", "F3_SKIPPED", "GAP_CHANGED"}
    f3_event = next((e for e in reversed(logs) if e.get("event") in f3_events), None) if f2_event else None
    f3_status = {
        "F3_FINAL_PICK": "최종",
        "ENTRY_ORDER_SENT": "주문전송",
        "ENTRY_EXECUTED": "체결",
        "F3_ENTRY_BLOCKED": "차단",
        "F3_SKIPPED": "생략",
        "GAP_CHANGED": "제외",
    }.get(f3_event.get("event") if f3_event else None, "대기")
    steps.append({
        "key": "f3",
        "phase": "F3 최종",
        "tickers": [f3_event.get("ticker")] if f3_event and f3_event.get("ticker") else [],
        "ticker": f3_event.get("ticker") if f3_event else None,
        "expected_price": f3_event.get("expected_price") if f3_event else None,
        "status": f3_status,
        "detail": _f3_detail_from_event(f3_event),
    })
    return steps


async def _fetch_asset_snapshot() -> dict:
    mode = os.getenv("KIS_MODE", "PAPER")
    resp = await kis_rest.get(
        "/uapi/domestic-stock/v1/trading/inquire-balance",
        tr_id=_BAL_TR[mode],
        params=kis_rest.balance_inquiry_params(),
    )
    return _parse_asset_snapshot_response(resp)


async def _asset_snapshot_safe() -> dict | None:
    global _ASSET_CACHE, _ASSET_CACHE_AT
    now = asyncio.get_running_loop().time()
    if _ASSET_CACHE is not None and now - _ASSET_CACHE_AT < _ASSET_CACHE_TTL_SEC:
        return _ASSET_CACHE
    # With a stale cache, concurrent refreshes return the stale value immediately.
    # On first load there is no safe value to show, so concurrent callers wait for
    # the in-flight request and then reuse its newly populated cache.
    if _ASSET_CACHE is not None and _ASSET_CACHE_LOCK.locked():
        return _ASSET_CACHE
    async with _ASSET_CACHE_LOCK:
        now = asyncio.get_running_loop().time()
        if _ASSET_CACHE is not None and now - _ASSET_CACHE_AT < _ASSET_CACHE_TTL_SEC:
            return _ASSET_CACHE
        try:
            _ASSET_CACHE = await _fetch_asset_snapshot()
            _ASSET_CACHE_AT = now
            return _ASSET_CACHE
        except Exception:
            return _ASSET_CACHE


def _asset_snapshot_cached() -> dict | None:
    return _ASSET_CACHE


# ── /api/status ──────────────────────────────────────────────────────

@app.get("/api/status")
async def api_status() -> JSONResponse:
    s = state.get()
    logs = _read_today_logs(limit=_STATUS_LOG_LIMIT)
    assets = _asset_snapshot_cached()
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
        "assets": assets,
        **_pipeline_from_logs(logs, s.position_status),
    })


# ── /api/logs ────────────────────────────────────────────────────────

@app.get("/api/assets")
async def api_assets(refresh: int = 0) -> JSONResponse:
    assets = await _asset_snapshot_safe() if refresh else _asset_snapshot_cached()
    return JSONResponse({"assets": assets})


@app.get("/api/logs")
async def api_logs(n: int = 60) -> JSONResponse:
    lines = _read_today_logs(limit=n)
    lines.reverse()
    return JSONResponse(lines)


# ── /api/history ─────────────────────────────────────────────────────

@app.get("/api/f1")
async def api_f1() -> JSONResponse:
    logs = _read_today_logs(limit=500)
    status, last_event = _f1_status_from_logs(logs)
    snapshot_path, rows = _read_f1_snapshot()
    summary = _f1_summary_from_rows(rows)
    if rows and status in {"IDLE", "NO_TARGET"}:
        status = "DONE" if summary["gap_pass"] else "NO_TARGET"

    return JSONResponse({
        "status": status,
        "last_event": last_event,
        "snapshot_name": snapshot_path.name if snapshot_path else None,
        "updated_at": (
            datetime.fromtimestamp(snapshot_path.stat().st_mtime, tz=KST).isoformat()
            if snapshot_path else None
        ),
        "selection_process": _selection_process_from_logs(summary, logs),
        **summary,
    })


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
