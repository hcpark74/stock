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
from src.modules.f1_filter import F1_SNAPSHOT_DIR, GAP_MAX, GAP_MIN, select_liquidity_candidates

KST = ZoneInfo("Asia/Seoul")
_MODE = os.getenv("KIS_MODE", "PAPER")
_LOG_DIR = Path(os.getenv("LOG_DIR", "data/logs"))
_F1_SNAPSHOT_DIR = Path(F1_SNAPSHOT_DIR)
_HTML_DIR = Path(__file__).parent.parent.parent / "docs" / "html"

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
    if not _F1_SNAPSHOT_DIR.exists():
        return None
    today_files = list(_F1_SNAPSHOT_DIR.glob(f"{_today()}_*.jsonl"))
    files = today_files or list(_F1_SNAPSHOT_DIR.glob("*.jsonl"))
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


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


def _pipeline_from_logs(logs: list[dict], position_status: str) -> dict:
    stage = 0
    failed = False

    if position_status == "ENTERING":
        return {"pipeline_stage": 2, "pipeline_failed": False}
    if position_status == "HOLDING":
        return {"pipeline_stage": 3, "pipeline_failed": False}
    if position_status == "CLOSED":
        return {"pipeline_stage": 4, "pipeline_failed": False}

    for entry in logs:
        event = entry.get("event")
        if event in {"F1_DONE", "F1_FILTER_EMPTY", "F1_SNAPSHOT_SAVED", "NO_TARGET"}:
            stage = max(stage, 1)
        elif event in {"TARGET_LOCKED", "F2_SKIPPED"}:
            stage = max(stage, 2)
        elif event in {
            "F3_RECHECK",
            "ENTRY_ORDER_SENT",
            "ENTRY_FILL_POLL_TIMEOUT",
            "ENTRY_CANCEL_SENT",
            "ENTRY_RETRY_START",
            "ENTRY_RETRY_SKIPPED",
            "ENTRY_FAIL",
            "F3_SKIPPED",
            "GAP_CHANGED",
        }:
            stage = max(stage, 2)
        elif event in {"ENTRY_EXECUTED", "DRY_RUN_ENTRY_EXECUTED"}:
            stage = max(stage, 3)
            failed = False

        if event in {"ENTRY_FAIL", "F3_SKIPPED", "GAP_CHANGED"}:
            failed = True

    return {"pipeline_stage": stage, "pipeline_failed": failed}


def _f1_verdict(candidate: dict) -> str:
    reason = candidate.get("gap_reason")
    labels = {
        "CORE_GAP": "통과",
        "HIGH_GAP_ALLOWED": "고갭통과",
        "GAP_BELOW_2": "갭미달",
        "GAP_BELOW_CORE": "약한갭",
        "HIGH_GAP_AMOUNT_LOW": "대금부족",
        "HIGH_GAP_VI_UNKNOWN": "VI미확인",
        "HIGH_GAP_VI_NEAR": "VI근접",
        "EXTREME_GAP_RISK": "초고갭",
        "GAP_TOO_HIGH": "갭과열",
        "NEGATIVE_GAP": "음수갭",
    }
    return labels.get(str(reason), "통과" if candidate.get("gap_allowed") else "제외")


def _f1_allowed(candidate: dict) -> bool:
    if "gap_allowed" in candidate:
        return candidate.get("gap_allowed") is True
    gap = float(candidate.get("gap_pct") or 0)
    return GAP_MIN <= gap < GAP_MAX


def _candidate_amount(candidate: dict) -> float:
    return float(candidate.get("expected_amount") or candidate.get("avg_amount_5d") or 0)


def _sort_f1_candidates_for_display(rows: list[dict], selected: list[dict]) -> list[dict]:
    selected_tickers = {c.get("ticker") for c in selected}

    def key(candidate: dict) -> tuple:
        ticker = candidate.get("ticker")
        expected_gap = float(candidate.get("expected_api_gap_pct") or 0)
        ranking_gap = float(candidate.get("ranking_gap_pct") or 0)
        expected_valid = (
            float(candidate.get("expected_api_price") or 0) > 0
            and int(candidate.get("expected_api_qty") or 0) > 0
        )
        return (
            ticker in selected_tickers,
            _f1_allowed(candidate),
            GAP_MIN <= expected_gap < GAP_MAX,
            GAP_MIN <= ranking_gap < GAP_MAX,
            expected_valid,
            _candidate_amount(candidate),
            float(candidate.get("gap_pct") or 0),
        )

    return sorted(rows, key=key, reverse=True)


def _f1_summary_from_rows(rows: list[dict]) -> dict:
    gap_pass = [c for c in rows if _f1_allowed(c)]
    selected = select_liquidity_candidates(gap_pass)
    display_rows = _sort_f1_candidates_for_display(rows, selected)

    expected_valid = [
        c for c in rows
        if float(c.get("expected_api_price") or 0) > 0
        and int(c.get("expected_api_qty") or 0) > 0
    ]
    ranking_pass = [
        c for c in rows
        if GAP_MIN <= float(c.get("ranking_gap_pct") or 0) < GAP_MAX
    ]
    expected_pass = [
        c for c in rows
        if GAP_MIN <= float(c.get("expected_api_gap_pct") or 0) < GAP_MAX
    ]

    return {
        "raw_count": len(rows),
        "expected_valid": len(expected_valid),
        "ranking_pass": len(ranking_pass),
        "expected_pass": len(expected_pass),
        "gap_pass": len(gap_pass),
        "core_gap": sum(1 for c in rows if c.get("gap_band") == "CORE_GAP"),
        "high_gap_allowed": sum(1 for c in rows if c.get("gap_reason") == "HIGH_GAP_ALLOWED"),
        "high_gap_rejected": sum(
            1
            for c in rows
            if c.get("gap_band") == "HIGH_GAP" and c.get("gap_allowed") is not True
        ),
        "extreme_gap": sum(1 for c in rows if c.get("gap_band") == "EXTREME_GAP"),
        "liquidity_pass": len(selected),
        "selected": selected[0] if selected else None,
        "candidates": [
            {**c, "verdict": _f1_verdict(c)}
            for c in display_rows[:50]
        ],
    }


# ── /api/status ──────────────────────────────────────────────────────

@app.get("/api/status")
async def api_status() -> JSONResponse:
    s = state.get()
    logs = _read_today_logs(limit=500)
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
        **_pipeline_from_logs(logs, s.position_status),
    })


# ── /api/logs ────────────────────────────────────────────────────────

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
