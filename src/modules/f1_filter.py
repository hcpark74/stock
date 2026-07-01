"""F1 premarket candidate filter (08:40 ~ 08:58)."""

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src import db, notifier, state
from src.api import kis_rest
from src.utils.logger import log

KST = ZoneInfo("Asia/Seoul")

GAP_MIN = 0.030
GAP_MAX = 0.070
HIGH_GAP_MAX = 0.100
EXTREME_GAP_MAX = 0.150
HIGH_GAP_MIN_EXPECTED_AMOUNT = 2_000_000_000
HIGH_GAP_MIN_VI_GAP = 0.010
LIQUIDITY_TOP_PCT = 0.10
F1_MIN_CANDIDATES = max(1, int(os.getenv("F1_MIN_CANDIDATES", "10")))

F1_DEADLINE_H = 8
F1_DEADLINE_M = 58
F1_RETRY_INTERVAL_SEC = int(os.getenv("F1_RETRY_INTERVAL_SEC", "30"))
F1_SNAPSHOT_DIR = os.getenv("F1_SNAPSHOT_DIR", "data/f1_snapshots")
F1_SNAPSHOT_KEEP = int(os.getenv("F1_SNAPSHOT_KEEP", "20"))
F1_EXPECTED_QUOTE_CONCURRENCY = int(os.getenv("F1_EXPECTED_QUOTE_CONCURRENCY", "2"))
F1_MARKET_INTERVAL_SEC = float(os.getenv("F1_MARKET_INTERVAL_SEC", "2.0"))

# KIS ranking uses J+input market buckets, and expected-quote accepts J for both KOSPI/KOSDAQ.
_PREMARKET_MARKETS = (
    {"label": "J", "ranking_market": "J", "ranking_input": "0001", "quote_market": "J"},
    {"label": "Q", "ranking_market": "J", "ranking_input": "1001", "quote_market": "J"},
)

_EXCLUDED_PRODUCT_KEYWORDS = (
    "ETF",
    "ETN",
    "KODEX",
    "TIGER",
    "ACE",
    "SOL",
    "KBSTAR",
    "KOSEF",
    "HANARO",
    "ARIRANG",
    "TIMEFOLIO",
    "TREX",
    "인버스",
    "레버리지",
    "선물",
)


async def run() -> list[dict]:
    """
    Fetch premarket candidates, apply the gap/liquidity filters, and retry until
    the F2 deadline if the KIS premarket fields are not ready yet.
    """
    if os.getenv("DRY_RUN", "0") == "1":
        result = [_dry_run_candidate()]
        log("DRY_RUN_F1_DONE", level="WARN", total_candidates=len(result), passed=len(result))
        return result

    s = state.get()
    if s.day_skip:
        return []

    attempt = 0
    while True:
        attempt += 1
        raw_candidates = await _fetch_all_premarket()
        gap_filtered = _filter_by_gap(raw_candidates)

        if gap_filtered:
            break

        log(
            "F1_FILTER_EMPTY",
            level="INFO",
            attempt=attempt,
            raw_count=len(raw_candidates),
            filter_count=0,
            reason="GAP_FILTER_EMPTY",
            **_gap_stats(raw_candidates),
        )

        if not _should_retry():
            log(
                "NO_TARGET",
                level="INFO",
                attempt=attempt,
                raw_count=len(raw_candidates),
                filter_count=0,
                reason="GAP_FILTER_EMPTY",
                **_gap_stats(raw_candidates),
            )
            await notifier.send(
                "NO_TARGET",
                level="INFO",
                message="당일 필터 통과 종목 없음. 거래 스킵.",
            )
            s.day_skip = True
            today = datetime.now(KST).strftime("%Y%m%d")
            await db.record_skip(today, "NO_TARGET", f"raw={len(raw_candidates)},gap_filtered=0")
            return []

        sleep_sec = _retry_sleep_seconds()
        log(
            "F1_RETRY_WAIT",
            level="WARN",
            attempt=attempt,
            retry_after_sec=sleep_sec,
            raw_count=len(raw_candidates),
            deadline=f"{F1_DEADLINE_H:02d}:{F1_DEADLINE_M:02d}:00",
            reason="GAP_FILTER_EMPTY",
        )
        await asyncio.sleep(sleep_sec)

    total = len(gap_filtered)
    result = select_liquidity_candidates(gap_filtered)

    log("F1_DONE", level="INFO", total_candidates=total, passed=len(result))
    return result


def _filter_by_gap(candidates: list[dict]) -> list[dict]:
    return [c for c in candidates if _is_gap_candidate(c)]


def _is_gap_candidate(candidate: dict) -> bool:
    return candidate.get("gap_allowed") is True


def select_liquidity_candidates(candidates: list[dict]) -> list[dict]:
    if not candidates:
        return []
    sorted_candidates = sorted(
        candidates,
        key=lambda c: c.get("avg_amount_5d", 0.0),
        reverse=True,
    )
    threshold = max(F1_MIN_CANDIDATES, int(len(sorted_candidates) * LIQUIDITY_TOP_PCT))
    return sorted_candidates[:threshold]


def _classify_gap_candidate(candidate: dict) -> dict:
    gap = candidate.get("gap_pct", 0.0)
    amount = candidate.get("expected_amount", 0.0)
    vi_gap = candidate.get("vi_gap")

    if gap < 0:
        return {"gap_band": "NEGATIVE_GAP", "gap_allowed": False, "gap_reason": "NEGATIVE_GAP"}
    if gap < 0.020:
        return {"gap_band": "LOW_GAP", "gap_allowed": False, "gap_reason": "GAP_BELOW_2"}
    if gap < GAP_MIN:
        return {"gap_band": "WEAK_GAP", "gap_allowed": False, "gap_reason": "GAP_BELOW_CORE"}
    if gap < GAP_MAX:
        return {"gap_band": "CORE_GAP", "gap_allowed": True, "gap_reason": "CORE_GAP"}
    if gap < HIGH_GAP_MAX:
        if amount >= HIGH_GAP_MIN_EXPECTED_AMOUNT and vi_gap is not None and vi_gap >= HIGH_GAP_MIN_VI_GAP:
            return {
                "gap_band": "HIGH_GAP",
                "gap_allowed": True,
                "gap_reason": "HIGH_GAP_ALLOWED",
            }
        if amount < HIGH_GAP_MIN_EXPECTED_AMOUNT:
            reason = "HIGH_GAP_AMOUNT_LOW"
        elif vi_gap is None:
            reason = "HIGH_GAP_VI_UNKNOWN"
        else:
            reason = "HIGH_GAP_VI_NEAR"
        return {"gap_band": "HIGH_GAP", "gap_allowed": False, "gap_reason": reason}
    if gap < EXTREME_GAP_MAX:
        return {"gap_band": "EXTREME_GAP", "gap_allowed": False, "gap_reason": "EXTREME_GAP_RISK"}
    return {"gap_band": "EXCLUDED_GAP", "gap_allowed": False, "gap_reason": "GAP_TOO_HIGH"}


def _gap_stats(candidates: list[dict]) -> dict:
    gaps = [c.get("gap_pct", 0.0) * 100 for c in candidates]
    if not gaps:
        return {"gap_min_pct": None, "gap_max_pct": None, "zero_gap_count": 0}
    return {
        "gap_min_pct": round(min(gaps), 3),
        "gap_max_pct": round(max(gaps), 3),
        "zero_gap_count": sum(1 for g in gaps if abs(g) < 0.0001),
    }


def _should_retry() -> bool:
    if os.getenv("PYTEST_CURRENT_TEST"):
        return False
    if os.getenv("F1_ENABLE_RETRY", "1") != "1":
        return False
    now = datetime.now(KST)
    deadline = now.replace(hour=F1_DEADLINE_H, minute=F1_DEADLINE_M, second=0, microsecond=0)
    return now < deadline


def _retry_sleep_seconds() -> int:
    now = datetime.now(KST)
    deadline = now.replace(hour=F1_DEADLINE_H, minute=F1_DEADLINE_M, second=0, microsecond=0)
    remaining = max(1, int((deadline - now).total_seconds()))
    return max(1, min(F1_RETRY_INTERVAL_SEC, remaining))


async def _fetch_all_premarket() -> list[dict]:
    """
    Fetch KOSPI/KOSDAQ fluctuation rankings and enrich each row with the
    quote API's expected execution price/volume when available.
    """
    results: list[dict] = []
    for index, market_cfg in enumerate(_PREMARKET_MARKETS):
        market = market_cfg["label"]
        if index > 0 and F1_MARKET_INTERVAL_SEC > 0:
            log("F1_MARKET_INTERVAL", level="INFO", market=market, sleep_sec=F1_MARKET_INTERVAL_SEC)
            await asyncio.sleep(F1_MARKET_INTERVAL_SEC)

        try:
            resp = await kis_rest.get(
                "/uapi/domestic-stock/v1/ranking/fluctuation",
                tr_id="FHPST01710000",
                params={
                    "fid_cond_mrkt_div_code": market_cfg["ranking_market"],
                    "fid_cond_scr_div_code": "20171",
                    "fid_input_iscd": market_cfg["ranking_input"],
                    "fid_rank_sort_cls_code": "0",
                    "fid_input_cnt_1": "0",
                    "fid_prc_cls_code": "0",
                    "fid_input_price_1": "",
                    "fid_input_price_2": "",
                    "fid_vol_cnt": "",
                    "fid_trgt_cls_code": "0",
                    "fid_trgt_exls_cls_code": "0",
                    "fid_div_cls_code": "0",
                    "fid_rsfl_rate1": f"{GAP_MIN * 100:.1f}",
                    "fid_rsfl_rate2": f"{HIGH_GAP_MAX * 100:.1f}",
                },
            )
        except Exception as e:
            log("F1_API_ERROR", level="WARN", market=market, error=repr(e))
            continue

        output = resp.get("output", [])
        candidates = await _parse_candidates_concurrently(
            output,
            market,
            market_cfg["quote_market"],
        )
        parsed_count = len(candidates)
        zero_gap_count = sum(1 for c in candidates if abs(c.get("gap_pct", 0.0)) < 0.000001)
        results.extend(candidates)

        log(
            "F1_FETCH_DONE",
            level="INFO",
            market=market,
            rt_cd=resp.get("rt_cd"),
            msg_cd=resp.get("msg_cd"),
            msg1=resp.get("msg1"),
            output_count=len(output),
            parsed_count=parsed_count,
            zero_gap_count=zero_gap_count,
        )

    _log_expected_comparison(results)
    _save_candidate_snapshot(results)
    return results


async def _parse_candidates_concurrently(
    items: list[dict],
    market: str,
    quote_market: str = "J",
) -> list[dict]:
    concurrency = max(1, F1_EXPECTED_QUOTE_CONCURRENCY)
    semaphore = asyncio.Semaphore(concurrency)

    async def parse_one(item: dict) -> dict | None:
        async with semaphore:
            try:
                return await _parse_candidate(item, market, quote_market)
            except (KeyError, ValueError, ZeroDivisionError):
                return None

    parsed = await asyncio.gather(*(parse_one(item) for item in items))
    return [candidate for candidate in parsed if candidate is not None]


async def _parse_candidate(item: dict, market: str = "J", quote_market: str = "J") -> dict | None:
    ticker = item.get("stck_shrn_iscd") or item.get("mksc_shrn_iscd")
    name = item.get("hts_kor_isnm", "")
    if not _is_common_stock_candidate(ticker, name):
        return None

    ranking_gap_pct = _to_float(item.get("prdy_ctrt"))
    ranking_price = _to_float(item.get("stck_prpr"))
    if ranking_price <= 0:
        return None

    prev_close = ranking_price / (1 + ranking_gap_pct / 100)
    expected_price = ranking_price
    expected_qty = _to_int(item.get("acml_vol"))
    expected_amount = _to_float(item.get("acml_tr_pbmn"))
    final_gap_pct = ranking_gap_pct
    gap_source = "ranking.prdy_ctrt"

    expected_quote = await _fetch_expected_quote(ticker, quote_market)
    if expected_quote and expected_quote["expected_price"] > 0 and expected_quote["expected_qty"] > 0:
        expected_price = expected_quote["expected_price"]
        expected_qty = expected_quote["expected_qty"]
        expected_amount = expected_quote["expected_amount"]
        final_gap_pct = expected_quote["expected_gap_pct"]
        if abs(expected_quote.get("prev_close", 0.0)) > 0.0001:
            prev_close = expected_quote["prev_close"]
        elif abs(final_gap_pct) > 0.0001:
            prev_close = expected_price / (1 + final_gap_pct / 100)
        gap_source = "expected.antc_cnpr"

    avrg_vol = _to_float(item.get("avrg_vol"))
    vi_gap = _calc_vi_gap(expected_price, prev_close)
    candidate = {
        "ticker": ticker,
        "name": name,
        "market": market,
        "expected_price": expected_price,
        "prev_close": prev_close,
        "gap_pct": final_gap_pct / 100,
        "gap_source": gap_source,
        "avg_amount_5d": avrg_vol * ranking_price,
        "expected_amount": expected_amount,
        "expected_qty": expected_qty,
        "ranking_gap_pct": ranking_gap_pct / 100,
        "ranking_price": ranking_price,
        "ranking_qty": _to_int(item.get("acml_vol")),
        "ranking_amount": _to_float(item.get("acml_tr_pbmn")),
        "expected_api_gap_pct": (
            expected_quote["expected_gap_pct"] / 100 if expected_quote else None
        ),
        "expected_api_price": expected_quote["expected_price"] if expected_quote else None,
        "expected_api_qty": expected_quote["expected_qty"] if expected_quote else None,
        "expected_api_amount": expected_quote["expected_amount"] if expected_quote else None,
        "vi_gap": vi_gap,
        "buy_sell_ratio": 0.0,
    }
    candidate.update(_classify_gap_candidate(candidate))
    return candidate


async def _fetch_expected_quote(ticker: str, market: str = "J") -> dict | None:
    try:
        resp = await kis_rest.get(
            "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
            tr_id="FHKST01010200",
            params={"FID_COND_MRKT_DIV_CODE": market, "FID_INPUT_ISCD": ticker},
        )
    except Exception as e:
        log("F1_EXPECTED_QUOTE_ERROR", level="WARN", ticker=ticker, market=market, error=repr(e))
        return None

    out = resp.get("output2", {})
    expected = _to_float(out.get("antc_cnpr"))
    qty = _to_int(out.get("antc_vol"))
    gap_pct = _to_float(out.get("antc_cntg_prdy_ctrt"))
    diff = _to_float(out.get("antc_cntg_vrss"))

    if expected <= 0:
        return None

    prev_close = expected - diff if diff else 0.0
    return {
        "expected_price": expected,
        "expected_qty": qty,
        "expected_amount": expected * qty,
        "expected_gap_pct": gap_pct,
        "prev_close": prev_close,
        "rt_cd": resp.get("rt_cd"),
        "msg_cd": resp.get("msg_cd"),
        "msg1": resp.get("msg1"),
    }


def _log_expected_comparison(candidates: list[dict]) -> None:
    ranking_pass = sum(1 for c in candidates if GAP_MIN <= c.get("ranking_gap_pct", 0.0) < GAP_MAX)
    expected_pass = sum(1 for c in candidates if GAP_MIN <= (c.get("expected_api_gap_pct") or 0.0) < GAP_MAX)
    final_pass = sum(1 for c in candidates if _is_gap_candidate(c))
    expected_valid = sum(1 for c in candidates if (c.get("expected_api_price") or 0) > 0)
    gap_source_expected = sum(1 for c in candidates if c.get("gap_source") == "expected.antc_cnpr")
    mismatch = sum(
        1
        for c in candidates
        if (GAP_MIN <= c.get("ranking_gap_pct", 0.0) < GAP_MAX)
        != (GAP_MIN <= (c.get("expected_api_gap_pct") or 0.0) < GAP_MAX)
    )
    log(
        "F1_EXPECTED_COMPARE",
        level="INFO",
        total=len(candidates),
        ranking_pass=ranking_pass,
        expected_valid=expected_valid,
        expected_pass=expected_pass,
        final_pass=final_pass,
        expected_source_count=gap_source_expected,
        mismatch_count=mismatch,
        core_gap_count=sum(1 for c in candidates if c.get("gap_band") == "CORE_GAP"),
        high_gap_allowed_count=sum(
            1 for c in candidates if c.get("gap_reason") == "HIGH_GAP_ALLOWED"
        ),
    )


def _save_candidate_snapshot(candidates: list[dict]) -> None:
    if os.getenv("PYTEST_CURRENT_TEST") or os.getenv("F1_SAVE_SNAPSHOT", "1") != "1":
        return
    if not candidates:
        return

    try:
        snapshot_dir = Path(F1_SNAPSHOT_DIR)
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(KST)
        path = snapshot_dir / f"{now.strftime('%Y%m%d_%H%M%S')}.jsonl"
        with path.open("w", encoding="utf-8") as f:
            for candidate in candidates:
                f.write(json.dumps(candidate, ensure_ascii=False) + "\n")
        _rotate_candidate_snapshots(snapshot_dir, keep=F1_SNAPSHOT_KEEP)
        log("F1_SNAPSHOT_SAVED", level="INFO", path=str(path), count=len(candidates))
    except Exception as e:
        log("F1_SNAPSHOT_SAVE_ERROR", level="WARN", error=repr(e))


def _rotate_candidate_snapshots(snapshot_dir: Path, keep: int = F1_SNAPSHOT_KEEP) -> None:
    if keep <= 0:
        return
    files = sorted(
        snapshot_dir.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in files[keep:]:
        try:
            old.unlink()
        except OSError as e:
            log("F1_SNAPSHOT_ROTATE_ERROR", level="WARN", path=str(old), error=repr(e))


def _to_float(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _to_int(value: object) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _calc_vi_gap(expected_price: float, prev_close: float) -> float | None:
    if expected_price <= 0 or prev_close <= 0:
        return None
    # Assumes the standard +/-10% static VI band. Some KOSDAQ names can use
    # +/-15%; model that explicitly before using this helper for those cases.
    static_vi_upper = prev_close * 1.10
    return (static_vi_upper - expected_price) / expected_price


def _is_common_stock_candidate(ticker: str | None, name: str = "") -> bool:
    """Exclude ETF/ETN/leveraged/inverse products from F1 candidates."""
    if not ticker or len(ticker) != 6 or not ticker.isdigit():
        return False

    upper_name = name.upper()
    return not any(keyword in upper_name for keyword in _EXCLUDED_PRODUCT_KEYWORDS)


def _dry_run_candidate() -> dict:
    prev_close = float(os.getenv("DRY_RUN_PREV_CLOSE", "10000"))
    expected_price = float(os.getenv("DRY_RUN_EXPECTED_PRICE", "10300"))
    expected_qty = int(os.getenv("DRY_RUN_EXPECTED_QTY", "500000"))
    gap_pct = (expected_price / prev_close) - 1
    candidate = {
        "ticker": os.getenv("DRY_RUN_TICKER", "005930"),
        "name": "DRY RUN",
        "market": "J",
        "expected_price": expected_price,
        "prev_close": prev_close,
        "gap_pct": gap_pct,
        "gap_source": "dry_run",
        "avg_amount_5d": expected_price * expected_qty,
        "expected_amount": expected_price * expected_qty,
        "expected_qty": expected_qty,
        "ranking_gap_pct": gap_pct,
        "ranking_price": expected_price,
        "ranking_qty": expected_qty,
        "ranking_amount": expected_price * expected_qty,
        "expected_api_gap_pct": gap_pct,
        "expected_api_price": expected_price,
        "expected_api_qty": expected_qty,
        "expected_api_amount": expected_price * expected_qty,
        "vi_gap": ((prev_close * 1.10) - expected_price) / expected_price,
        "buy_sell_ratio": 2.0,
    }
    candidate.update(_classify_gap_candidate(candidate))
    return candidate
