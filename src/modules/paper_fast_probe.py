"""PAPER-only observation probe for the proposed pre-open fast path.

The probe is deliberately read-only: it records public market-data responses
and never mutates trading state, selects the live target, or submits orders.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.api import kis_rest
from src.modules import f1_selector
from src.utils.logger import log

KST = ZoneInfo("Asia/Seoul")

RANKING_PATH = "/uapi/domestic-stock/v1/ranking/fluctuation"
RANKING_TR_ID = "FHPST01710000"
MULTI_PRICE_PATH = "/uapi/domestic-stock/v1/quotations/intstock-multprice"
MULTI_PRICE_TR_ID = "FHKST11300006"

PREOPEN_MARKETS = (
    {"label": "J", "ranking_input": "0001"},
    {"label": "Q", "ranking_input": "1001"},
)

_prepared_tickers: list[str] = []


def enabled() -> bool:
    """Return True only for an explicitly enabled PAPER session."""
    return (
        os.getenv("KIS_MODE", "PAPER").upper() == "PAPER"
        and os.getenv("PAPER_FAST_PROBE", "0") == "1"
        and os.getenv("DRY_RUN", "0") != "1"
    )


def _probe_dir() -> Path:
    return Path(os.getenv("PAPER_FAST_PROBE_DIR", "data/paper_fast_probe"))


def _probe_path(now: datetime | None = None) -> Path:
    current = now or datetime.now(KST)
    return _probe_dir() / f"{current.strftime('%Y%m%d')}.jsonl"


def _to_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _to_int(value: Any) -> int:
    return int(_to_float(value))


def _rows(resp: dict, key: str = "output") -> list[dict]:
    value = resp.get(key) or []
    if isinstance(value, dict):
        return [value]
    return [row for row in value if isinstance(row, dict)]


def _public_response(resp: dict, key: str = "output") -> dict:
    """Keep only public response data; never persist headers or credentials."""
    rows = _rows(resp, key)
    return {
        "rt_cd": resp.get("rt_cd"),
        "msg_cd": resp.get("msg_cd"),
        "msg1": resp.get("msg1"),
        "output_count": len(rows),
        "output": rows,
    }


def _append_record(event: str, **fields: Any) -> None:
    path = _probe_path()
    record = {
        "ts": datetime.now(KST).isoformat(),
        "event": event,
        "mode": os.getenv("KIS_MODE", "PAPER"),
        **fields,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        log(
            "PAPER_FAST_PROBE_ERROR",
            level="WARN",
            phase=fields.get("phase"),
            reason="WRITE_FAILED",
            error=str(exc)[:300],
        )


def _ranking_params(ranking_input: str) -> dict:
    return {
        "fid_cond_mrkt_div_code": "J",
        "fid_cond_scr_div_code": "20171",
        "fid_input_iscd": ranking_input,
        "fid_rank_sort_cls_code": "0",
        "fid_input_cnt_1": "0",
        "fid_prc_cls_code": "0",
        "fid_input_price_1": "",
        "fid_input_price_2": "",
        "fid_vol_cnt": "",
        "fid_trgt_cls_code": "0",
        "fid_trgt_exls_cls_code": "0",
        "fid_div_cls_code": "0",
        "fid_rsfl_rate1": f"{f1_selector.GAP_MIN * 100:.1f}",
        "fid_rsfl_rate2": f"{f1_selector.GAP_HARD_MAX * 100:.1f}",
    }


def _valid_tickers(rows: list[dict]) -> list[str]:
    tickers: list[str] = []
    for row in rows:
        ticker = str(row.get("stck_shrn_iscd") or row.get("mksc_shrn_iscd") or "")
        if len(ticker) == 6 and ticker.isdigit() and ticker not in tickers:
            tickers.append(ticker)
        if len(tickers) >= 30:
            break
    return tickers


def _multi_params(tickers: list[str]) -> dict:
    params: dict[str, str] = {}
    for index, ticker in enumerate(tickers[:30], start=1):
        params[f"FID_COND_MRKT_DIV_CODE_{index}"] = "J"
        params[f"FID_INPUT_ISCD_{index}"] = ticker
    return params


def _estimate_timing_before_call() -> tuple[float, int]:
    started = time.monotonic()
    last_call = float(getattr(kis_rest, "_last_call_at", 0.0) or 0.0)
    interval = float(getattr(kis_rest, "_RATE_INTERVAL", 0.0) or 0.0)
    estimated_wait_ms = max(0, round((interval - (started - last_call)) * 1000))
    return started, estimated_wait_ms


async def _get_and_record(
    *,
    event: str,
    phase: str,
    market: str,
    path: str,
    tr_id: str,
    params: dict,
) -> dict:
    started, estimated_wait_ms = _estimate_timing_before_call()
    try:
        resp = await kis_rest.get(path, tr_id=tr_id, params=params)
    except Exception as exc:
        total_ms = max(0, round((time.monotonic() - started) * 1000))
        _append_record(
            event,
            phase=phase,
            market=market,
            path=path,
            tr_id=tr_id,
            request=params,
            timing={
                "total_ms": total_ms,
                "estimated_rate_wait_ms": estimated_wait_ms,
                "estimated_non_rate_ms": max(0, total_ms - estimated_wait_ms),
            },
            error={"type": type(exc).__name__, "message": str(exc)[:300]},
        )
        log(
            "PAPER_FAST_PROBE_ERROR",
            level="WARN",
            phase=phase,
            market=market,
            path=path,
            reason=type(exc).__name__,
        )
        return {}

    total_ms = max(0, round((time.monotonic() - started) * 1000))
    response = _public_response(resp)
    _append_record(
        event,
        phase=phase,
        market=market,
        path=path,
        tr_id=tr_id,
        request=params,
        timing={
            "total_ms": total_ms,
            "estimated_rate_wait_ms": estimated_wait_ms,
            "estimated_non_rate_ms": max(0, total_ms - estimated_wait_ms),
        },
        response=response,
    )
    log(
        event,
        level="INFO" if str(resp.get("rt_cd", "")) == "0" else "WARN",
        phase=phase,
        market=market,
        output_count=response["output_count"],
        total_ms=total_ms,
        estimated_rate_wait_ms=estimated_wait_ms,
        rt_cd=resp.get("rt_cd"),
        msg_cd=resp.get("msg_cd"),
    )
    return resp


def _candidate_from_multi(
    row: dict,
    market: str,
    ranking_by_ticker: dict[str, dict],
) -> dict | None:
    ticker = str(row.get("inter_shrn_iscd") or "")
    if len(ticker) != 6 or not ticker.isdigit():
        return None

    price = _to_float(row.get("inter2_prpr"))
    prev_close = _to_float(row.get("inter2_prdy_clpr") or row.get("inter2_sdpr"))
    expected_qty = _to_int(row.get("intr_antc_vol"))
    gap_pct = (
        _to_float(row.get("intr_antc_cntg_prdy_ctrt"))
        if expected_qty > 0
        else _to_float(row.get("prdy_ctrt"))
    )
    qty = expected_qty if expected_qty > 0 else _to_int(row.get("acml_vol"))
    expected_amount = (
        price * qty
        if expected_qty > 0
        else _to_float(row.get("acml_tr_pbmn"))
    )
    ranking = ranking_by_ticker.get(ticker, {})
    ranking_price = _to_float(ranking.get("stck_prpr")) or price
    avg_amount_5d = _to_float(ranking.get("avrg_vol")) * ranking_price
    vi_gap = (
        ((prev_close * 1.10) - price) / price
        if price > 0 and prev_close > 0
        else None
    )
    return {
        "ticker": ticker,
        "name": row.get("inter_kor_isnm") or ranking.get("hts_kor_isnm") or "",
        "market": market,
        "expected_price": price,
        "prev_close": prev_close,
        "gap_pct": gap_pct / 100,
        "gap_source": (
            "multi.intr_antc_cntg_prdy_ctrt"
            if expected_qty > 0
            else "multi.prdy_ctrt"
        ),
        "avg_amount_5d": avg_amount_5d,
        "expected_amount": expected_amount,
        "expected_qty": qty,
        "vi_gap": vi_gap,
        "buy_sell_ratio": 0.0,
    }


def _select_probe_tickers(
    ranking_rows: dict[str, list[dict]],
    multi_rows: dict[str, list[dict]],
    limit: int = 3,
) -> list[str]:
    candidates: list[dict] = []
    fallback: list[dict] = []
    for market in ("J", "Q"):
        ranking_by_ticker = {
            str(row.get("stck_shrn_iscd") or row.get("mksc_shrn_iscd") or ""): row
            for row in ranking_rows.get(market, [])
        }
        for row in multi_rows.get(market, []):
            candidate = _candidate_from_multi(row, market, ranking_by_ticker)
            if candidate is None:
                continue
            fallback.append(candidate)
            if candidate["expected_price"] > 0 and candidate["prev_close"] > 0:
                candidates.append(candidate)

    ranked = f1_selector.rank_candidates(candidates)
    selected = [str(candidate["ticker"]) for candidate in ranked[:limit]]
    if len(selected) >= limit:
        return selected

    fallback.sort(
        key=lambda item: (
            item.get("expected_amount", 0.0),
            item.get("gap_pct", 0.0),
        ),
        reverse=True,
    )
    for candidate in fallback:
        ticker = str(candidate["ticker"])
        if ticker not in selected:
            selected.append(ticker)
        if len(selected) >= limit:
            break
    return selected


async def prepare() -> list[str]:
    """Collect the 08:59 ranking and multi-price evidence."""
    global _prepared_tickers
    if not enabled():
        return []

    _prepared_tickers = []
    now = datetime.now(KST)
    market_open = now.replace(hour=9, minute=0, second=0, microsecond=0)
    if now >= market_open:
        _append_record(
            "PAPER_FAST_PROBE_PREOPEN_SKIPPED",
            phase="PREOPEN",
            scheduled_before=market_open.isoformat(),
            actual_ts=now.isoformat(),
            reason="MARKET_OPEN",
        )
        log(
            "PAPER_FAST_PROBE_PREOPEN_SKIPPED",
            level="WARN",
            actual_ts=now.isoformat(),
            reason="MARKET_OPEN",
        )
        return []

    started = time.monotonic()
    _append_record("PAPER_FAST_PROBE_PREOPEN_START", phase="PREOPEN")
    log("PAPER_FAST_PROBE_PREOPEN_START", level="INFO")

    ranking_rows: dict[str, list[dict]] = {}
    multi_rows: dict[str, list[dict]] = {}
    market_summaries: list[dict] = []

    for market_cfg in PREOPEN_MARKETS:
        market = market_cfg["label"]
        ranking_params = _ranking_params(market_cfg["ranking_input"])
        ranking_resp = await _get_and_record(
            event="PAPER_FAST_PROBE_RANKING",
            phase="PREOPEN",
            market=market,
            path=RANKING_PATH,
            tr_id=RANKING_TR_ID,
            params=ranking_params,
        )
        rows = _rows(ranking_resp)
        ranking_rows[market] = rows
        requested_tickers = _valid_tickers(rows)

        multi_resp: dict = {}
        if requested_tickers:
            multi_resp = await _get_and_record(
                event="PAPER_FAST_PROBE_MULTI",
                phase="PREOPEN",
                market=market,
                path=MULTI_PRICE_PATH,
                tr_id=MULTI_PRICE_TR_ID,
                params=_multi_params(requested_tickers),
            )
        returned_rows = _rows(multi_resp)
        multi_rows[market] = returned_rows
        returned_tickers = [
            str(row.get("inter_shrn_iscd") or "")
            for row in returned_rows
            if row.get("inter_shrn_iscd")
        ]
        market_summaries.append(
            {
                "market": market,
                "ranking_count": len(rows),
                "requested_count": len(requested_tickers),
                "returned_count": len(returned_rows),
                "requested_tickers": requested_tickers,
                "returned_tickers": returned_tickers,
                "missing_tickers": [
                    ticker for ticker in requested_tickers if ticker not in returned_tickers
                ],
                "unexpected_tickers": [
                    ticker for ticker in returned_tickers if ticker not in requested_tickers
                ],
            }
        )

    _prepared_tickers = _select_probe_tickers(ranking_rows, multi_rows)
    elapsed_ms = max(0, round((time.monotonic() - started) * 1000))
    _append_record(
        "PAPER_FAST_PROBE_PREOPEN_DONE",
        phase="PREOPEN",
        elapsed_ms=elapsed_ms,
        selected_tickers=_prepared_tickers,
        markets=market_summaries,
    )
    log(
        "PAPER_FAST_PROBE_PREOPEN_DONE",
        level="INFO",
        elapsed_ms=elapsed_ms,
        selected_tickers=_prepared_tickers,
        market_count=len(market_summaries),
    )
    return list(_prepared_tickers)


def _load_prepared_tickers() -> list[str]:
    path = _probe_path()
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except (TypeError, ValueError):
            continue
        if record.get("event") != "PAPER_FAST_PROBE_PREOPEN_DONE":
            continue
        return [
            str(ticker)
            for ticker in record.get("selected_tickers", [])
            if len(str(ticker)) == 6 and str(ticker).isdigit()
        ][:3]
    return []


async def observe_open_boundary() -> None:
    """Observe the prepared top-three once near 09:00 without changing F1."""
    global _prepared_tickers
    if not enabled():
        return

    now = datetime.now(KST)
    target = now.replace(hour=9, minute=0, second=0, microsecond=0)
    offset_ms = max(0, int(os.getenv("PAPER_FAST_PROBE_OPEN_OFFSET_MS", "300")))
    target += timedelta(milliseconds=offset_ms)
    if now < target:
        await asyncio.sleep((target - now).total_seconds())
        now = datetime.now(KST)

    lateness_ms = max(0, round((now - target).total_seconds() * 1000))
    max_lateness_ms = max(
        0,
        int(os.getenv("PAPER_FAST_PROBE_OPEN_MAX_LATENESS_MS", "2500")),
    )
    if lateness_ms > max_lateness_ms:
        _append_record(
            "PAPER_FAST_PROBE_OPEN_SKIPPED",
            phase="OPEN",
            target_ts=target.isoformat(),
            actual_ts=now.isoformat(),
            lateness_ms=lateness_ms,
            max_lateness_ms=max_lateness_ms,
            reason="TOO_LATE",
        )
        log(
            "PAPER_FAST_PROBE_OPEN_SKIPPED",
            level="WARN",
            lateness_ms=lateness_ms,
            max_lateness_ms=max_lateness_ms,
            reason="TOO_LATE",
        )
        return

    tickers = list(_prepared_tickers) or _load_prepared_tickers()
    if not tickers:
        _append_record(
            "PAPER_FAST_PROBE_OPEN_SKIPPED",
            phase="OPEN",
            target_ts=target.isoformat(),
            actual_ts=now.isoformat(),
            lateness_ms=lateness_ms,
            reason="NO_PREOPEN_TICKERS",
        )
        log(
            "PAPER_FAST_PROBE_OPEN_SKIPPED",
            level="WARN",
            lateness_ms=lateness_ms,
            reason="NO_PREOPEN_TICKERS",
        )
        return

    resp = await _get_and_record(
        event="PAPER_FAST_PROBE_OPEN_MULTI",
        phase="OPEN",
        market="TOP3",
        path=MULTI_PRICE_PATH,
        tr_id=MULTI_PRICE_TR_ID,
        params=_multi_params(tickers),
    )
    rows = _rows(resp)
    valid_ask_count = sum(1 for row in rows if _to_float(row.get("inter2_askp")) > 0)
    _append_record(
        "PAPER_FAST_PROBE_OPEN_DONE",
        phase="OPEN",
        target_ts=target.isoformat(),
        actual_ts=now.isoformat(),
        lateness_ms=lateness_ms,
        requested_tickers=tickers,
        returned_tickers=[
            str(row.get("inter_shrn_iscd") or "")
            for row in rows
            if row.get("inter_shrn_iscd")
        ],
        valid_ask_count=valid_ask_count,
        output_count=len(rows),
    )
    log(
        "PAPER_FAST_PROBE_OPEN_DONE",
        level="INFO" if rows else "WARN",
        lateness_ms=lateness_ms,
        requested_count=len(tickers),
        output_count=len(rows),
        valid_ask_count=valid_ask_count,
    )
