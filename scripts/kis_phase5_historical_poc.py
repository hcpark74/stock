"""Phase 5 F1 과거 데이터 읽기 전용 PoC.

로컬 F1 스냅샷과 거래 DB를 변경하지 않고 품질을 집계한다. ``--with-kis``를
지정하면 장 종료 후 PAPER 시세 일봉 GET만 최대 3회 호출해 예상 갭과 실제 시가
갭을 대조한다. 주문·정정·취소 API는 호출하지 않는다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from src.api import auth, kis_rest  # noqa: E402

KST = ZoneInfo("Asia/Seoul")
SNAPSHOT_NAME_RE = re.compile(r"^(?P<date>\d{8})_(?P<time>\d{6})\.jsonl$")
CAPTURE_START = time(9, 0)
CAPTURE_END = time(9, 11)
DAILY_PATH = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
DAILY_TR = "FHKST03010100"
RATE_LIMIT_CODES = {"EGW00201", "HTTP_429"}
REQUIRED_SNAPSHOT_FIELDS = {
    "ticker",
    "expected_price",
    "prev_close",
    "gap_pct",
    "expected_amount",
    "vi_gap",
    "gap_allowed",
    "gap_band",
}


class PocStop(RuntimeError):
    def __init__(self, reason: str, msg_cd: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.msg_cd = msg_cd


def parse_snapshot_time(path: Path) -> datetime | None:
    """파일명 시각을 프로젝트 계약에 따라 Asia/Seoul aware datetime으로 해석한다."""
    match = SNAPSHOT_NAME_RE.match(path.name)
    if not match:
        return None
    try:
        naive = datetime.strptime(
            match.group("date") + match.group("time"), "%Y%m%d%H%M%S"
        )
    except ValueError:
        return None
    return naive.replace(tzinfo=KST)


def read_market_closed_dates(db_path: Path) -> set[str]:
    if not db_path.exists():
        return set()
    with sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True) as conn:
        try:
            return {
                str(row[0]).replace("-", "")
                for row in conn.execute(
                    "SELECT date FROM daily_skips WHERE reason='MARKET_CLOSED'"
                )
            }
        except sqlite3.Error:
            return set()


def select_representative_snapshots(
    paths: list[Path], market_closed_dates: set[str]
) -> tuple[list[Path], dict]:
    """09:00~09:10 KST의 거래일 후보에서 날짜별 최초 파일 하나를 선택한다."""
    eligible: list[tuple[datetime, Path]] = []
    invalid_name_n = outside_window_n = weekend_n = market_closed_n = 0
    for path in sorted(paths):
        captured_at = parse_snapshot_time(path)
        if captured_at is None:
            invalid_name_n += 1
            continue
        compact_date = captured_at.strftime("%Y%m%d")
        local_time = captured_at.timetz().replace(tzinfo=None)
        if not (CAPTURE_START <= local_time < CAPTURE_END):
            outside_window_n += 1
            continue
        if captured_at.weekday() >= 5:
            weekend_n += 1
            continue
        if compact_date in market_closed_dates:
            market_closed_n += 1
            continue
        eligible.append((captured_at, path))

    representatives: dict[str, Path] = {}
    for captured_at, path in eligible:
        representatives.setdefault(captured_at.strftime("%Y%m%d"), path)
    selected = [representatives[key] for key in sorted(representatives)]
    inventory = {
        "total_files": len(paths),
        "invalid_filename_count": invalid_name_n,
        "outside_capture_window_count": outside_window_n,
        "weekend_count": weekend_n,
        "known_market_closed_count": market_closed_n,
        "eligible_file_count": len(eligible),
        "duplicate_eligible_count": len(eligible) - len(selected),
        "selected_file_count": len(selected),
        "selected_date_start": selected[0].name[:8] if selected else None,
        "selected_date_end": selected[-1].name[:8] if selected else None,
        "timezone": "Asia/Seoul",
        "timezone_source": "filename_contract",
        "timezone_embedded_in_record": False,
    }
    return selected, inventory


def _missing(value: object) -> bool:
    return value is None or value == ""


def analyze_snapshots(paths: list[Path]) -> tuple[dict, dict[tuple[str, str], dict]]:
    missing_fields: Counter[str] = Counter()
    invalid_json_n = duplicate_ticker_n = formula_mismatch_n = 0
    row_n = core_n = conditional_n = outside_n = allowed_n = conditional_allowed_n = 0
    by_date_ticker: dict[tuple[str, str], dict] = {}

    for path in paths:
        compact_date = path.name[:8]
        seen: set[str] = set()
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            if not raw_line.strip():
                continue
            try:
                row = json.loads(raw_line)
            except (TypeError, json.JSONDecodeError):
                invalid_json_n += 1
                continue
            row_n += 1
            for field in REQUIRED_SNAPSHOT_FIELDS:
                if _missing(row.get(field)):
                    missing_fields[field] += 1
            ticker = str(row.get("ticker") or "")
            if ticker in seen:
                duplicate_ticker_n += 1
            seen.add(ticker)
            if ticker:
                by_date_ticker[(compact_date, ticker)] = row

            try:
                gap = float(row["gap_pct"])
                expected_price = float(row["expected_price"])
                prev_close = float(row["prev_close"])
            except (KeyError, TypeError, ValueError):
                continue
            if 0.03 <= gap < 0.08:
                core_n += 1
            elif 0.08 <= gap < 0.10:
                conditional_n += 1
                conditional_allowed_n += bool(row.get("gap_allowed"))
            else:
                outside_n += 1
            allowed_n += bool(row.get("gap_allowed"))
            if prev_close <= 0 or abs((expected_price / prev_close - 1) - gap) > 0.00011:
                formula_mismatch_n += 1

    return {
        "row_count": row_n,
        "invalid_json_count": invalid_json_n,
        "duplicate_ticker_count": duplicate_ticker_n,
        "missing_required_row_count": sum(
            1
            for path in paths
            for raw_line in path.read_text(encoding="utf-8").splitlines()
            if raw_line.strip()
            and _row_has_missing_required(raw_line)
        ),
        "missing_fields": dict(sorted(missing_fields.items())),
        "gap_formula_mismatch_count": formula_mismatch_n,
        "gap_unit": "ratio",
        "core_3_to_8_count": core_n,
        "conditional_8_to_10_count": conditional_n,
        "conditional_allowed_count": conditional_allowed_n,
        "outside_3_to_10_count": outside_n,
        "gap_allowed_count": allowed_n,
    }, by_date_ticker


def _row_has_missing_required(raw_line: str) -> bool:
    try:
        row = json.loads(raw_line)
    except (TypeError, json.JSONDecodeError):
        return False
    return any(_missing(row.get(field)) for field in REQUIRED_SNAPSHOT_FIELDS)


def read_improve_summary(db_path: Path) -> tuple[dict, list[tuple[str, str]]]:
    if not db_path.exists():
        return {"status": "DB_NOT_FOUND"}, []
    with sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        trades = [
            dict(row)
            for row in conn.execute(
                """SELECT date, ticker, name, entry_price, high_price, highest_step,
                          pnl_pct, close_reason, entry_at, exit_at
                   FROM trades WHERE status='CLOSED' ORDER BY date"""
            )
        ]
        orders = [
            dict(row)
            for row in conn.execute(
                """SELECT o.order_phase, o.order_price, o.fill_price, o.fill_latency_ms
                   FROM orders o JOIN trades t ON t.id=o.trade_id
                   WHERE o.status IN ('FILLED','PARTIAL_FILL')
                     AND COALESCE(t.close_reason, '') != 'MANUAL'"""
            )
        ]
        skip_rows = [
            dict(row)
            for row in conn.execute("SELECT date, reason FROM daily_skips ORDER BY date, id")
        ]
    skips = dict(Counter(str(row["reason"]) for row in skip_rows))
    from src.api.server import _improve_from_rows

    improve = _improve_from_rows(trades, orders, skips, skip_rows=skip_rows)
    trade_pairs = [
        (str(row["date"]).replace("-", ""), str(row["ticker"]))
        for row in trades
        if row.get("close_reason") != "MANUAL"
    ]
    return {
        "status": "SUCCESS",
        "overall": improve["overall"],
        "candidate_supply": improve["candidate_supply"],
        "data_quality": improve["data_quality"],
    }, trade_pairs


def select_daily_samples(
    trade_pairs: list[tuple[str, str]], snapshot_rows: dict[tuple[str, str], dict], limit: int
) -> list[tuple[str, str, dict]]:
    matched = [
        (date, ticker, snapshot_rows[(date, ticker)])
        for date, ticker in trade_pairs
        if (date, ticker) in snapshot_rows
    ]
    return sorted(matched, reverse=True)[:limit]


def _assert_safe_live_window(now: datetime) -> None:
    current = now.timetz().replace(tzinfo=None)
    if CAPTURE_START <= current < CAPTURE_END:
        raise PocStop("FORBIDDEN_0900_0911")
    if current < time(15, 40):
        raise PocStop("AFTER_CLOSE_ONLY")


def _assert_success(response: dict) -> None:
    msg_cd = str(response.get("msg_cd") or "")
    if msg_cd in RATE_LIMIT_CODES:
        raise PocStop("RATE_LIMIT", msg_cd)
    if str(response.get("rt_cd") or "") != "0":
        raise PocStop("DAILY_PRICE_FAILED", msg_cd or None)


async def compare_kis_daily(samples: list[tuple[str, str, dict]]) -> dict:
    result: dict = {
        "status": "NOT_RUN",
        "read_only": True,
        "mode": os.getenv("KIS_MODE", "PAPER"),
        "endpoint": DAILY_PATH,
        "tr_id": DAILY_TR,
        "adjustment_flag": "0",
        "adjustment_flag_note": "endpoint-specific; do not reuse for inquire-daily-price",
        "requested_sample_count": len(samples),
    }
    try:
        if result["mode"] != "PAPER":
            raise PocStop("PAPER_ONLY")
        _assert_safe_live_window(datetime.now(KST))
        if not await auth.load_or_refresh():
            raise PocStop("AUTH_FAILED")

        comparisons = []
        for compact_date, ticker, snapshot in samples:
            target = datetime.strptime(compact_date, "%Y%m%d")
            response = await kis_rest.get(
                DAILY_PATH,
                tr_id=DAILY_TR,
                params={
                    "FID_COND_MRKT_DIV_CODE": "J",
                    "FID_INPUT_ISCD": ticker,
                    "FID_INPUT_DATE_1": (target - timedelta(days=10)).strftime("%Y%m%d"),
                    "FID_INPUT_DATE_2": compact_date,
                    "FID_PERIOD_DIV_CODE": "D",
                    "FID_ORG_ADJ_PRC": "0",
                },
                stop_on_rate_limit=True,
            )
            _assert_success(response)
            bars = response.get("output2")
            if not isinstance(bars, list):
                raise PocStop("DAILY_OUTPUT2_MISSING")
            by_date = {
                str(row.get("stck_bsop_date")): row for row in bars if isinstance(row, dict)
            }
            dates = sorted(date for date in by_date if date <= compact_date)
            if compact_date not in by_date or len(dates) < 2:
                comparisons.append({"date": compact_date, "status": "BAR_MISSING"})
                continue
            index = dates.index(compact_date)
            if index == 0:
                comparisons.append({"date": compact_date, "status": "PREVIOUS_BAR_MISSING"})
                continue
            target_bar, previous_bar = by_date[compact_date], by_date[dates[index - 1]]
            try:
                open_price = float(target_bar["stck_oprc"])
                previous_close = float(previous_bar["stck_clpr"])
                expected_gap = float(snapshot["gap_pct"]) * 100
                open_gap = (open_price / previous_close - 1) * 100
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                comparisons.append({"date": compact_date, "status": "BAR_FIELD_MISSING"})
                continue
            comparisons.append(
                {
                    "date": compact_date,
                    "status": "COMPARED",
                    "expected_gap_pct": round(expected_gap, 3),
                    "official_open_gap_pct": round(open_gap, 3),
                    "absolute_difference_pp": round(abs(expected_gap - open_gap), 3),
                }
            )
        compared = [row for row in comparisons if row["status"] == "COMPARED"]
        result.update(
            {
                "status": "SUCCESS",
                "call_count": len(samples),
                "compared_count": len(compared),
                "missing_or_invalid_count": len(comparisons) - len(compared),
                "avg_absolute_difference_pp": round(
                    sum(row["absolute_difference_pp"] for row in compared) / len(compared), 3
                )
                if compared
                else None,
                "comparisons": comparisons,
            }
        )
    except PocStop as exc:
        result["status"] = "STOPPED"
        result["stop_reason"] = exc.reason
        if exc.msg_cd:
            result["msg_cd"] = exc.msg_cd
    except Exception as exc:
        result["status"] = "STOPPED"
        result["stop_reason"] = "UNEXPECTED_ERROR"
        result["error_type"] = type(exc).__name__
    return result


async def run(args: argparse.Namespace) -> dict:
    snapshot_dir = Path(args.snapshot_dir)
    db_path = Path(args.db_path)
    paths = sorted(snapshot_dir.glob("*.jsonl")) if snapshot_dir.exists() else []
    market_closed_dates = read_market_closed_dates(db_path)
    selected, inventory = select_representative_snapshots(paths, market_closed_dates)
    snapshot_quality, snapshot_rows = analyze_snapshots(selected)
    improve, trade_pairs = read_improve_summary(db_path)
    result = {
        "checked_at": datetime.now(KST).isoformat(timespec="seconds"),
        "read_only": True,
        "snapshot_inventory": inventory,
        "snapshot_quality": snapshot_quality,
        "improve_menu": improve,
        "scope_conclusion": {
            "exact_observed_f1_source": "local_f1_snapshot",
            "gap_threshold_replay_source": "official_daily_open_and_previous_close",
            "conditional_eligibility_source": "local_f1_snapshot_only",
            "daily_api_role": "opening_gap_and_OHLC_validation",
            "historical_expected_auction_available": False,
            "improve_relationship": "complementary_not_replacement",
        },
    }
    samples = select_daily_samples(trade_pairs, snapshot_rows, args.max_kis_samples)
    result["kis_daily_comparison"] = (
        await compare_kis_daily(samples)
        if args.with_kis
        else {"status": "NOT_RUN", "selected_sample_count": len(samples)}
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 5 F1 과거 데이터 읽기 전용 PoC")
    parser.add_argument("--snapshot-dir", default=str(ROOT / "data" / "f1_snapshots"))
    parser.add_argument(
        "--db-path", default=str(ROOT / os.getenv("DB_DIR", "data/db") / "trading.db")
    )
    parser.add_argument("--with-kis", action="store_true", help="장 종료 후 PAPER 일봉 GET 대조")
    parser.add_argument("--max-kis-samples", type=int, default=3, choices=range(1, 4))
    args = parser.parse_args()
    result = asyncio.run(run(args))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["kis_daily_comparison"].get("status") != "STOPPED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
