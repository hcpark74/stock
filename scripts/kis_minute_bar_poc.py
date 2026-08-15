"""당일 분봉 API 읽기 전용 PoC — 가용성·호출 예산·분봉 장벽 모호성.

phase5 PoC의 안전 규약을 계승한다. 기본 실행은 외부 API를 호출하지 않고 로컬 F1
스냅샷만 요약한다. ``--with-kis``는 ``KIS_MODE=PAPER``, 09:35 이후(또는 15:40 이후)만
허용하고 09:00~09:11에는 차단하며, 당일 분봉 GET만 배경 우선순위로 호출한다.

호출 예산은 추정하지 않는다. ``kis_rest.CallBudget``을 ``_request`` 재귀 전 분기로
전파해 실제 HTTP 시도(transient/token/rate-limit 재시도 포함)마다 1회 차감하고,
예산을 넘기는 시도(예: 61번째)는 전송 전에 거절한다. 주문·정정·취소 경로는 없다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# 테스트 수집 중에는 개발 머신의 .env가 os.environ을 오염시키지 않게 한다
# (conftest가 STOCK_SKIP_DOTENV=1을 설정). 운영/수동 실행에서는 정상 로드한다.
if os.getenv("STOCK_SKIP_DOTENV", "0") != "1":
    load_dotenv(ROOT / ".env")

from scripts.kis_phase5_historical_poc import read_market_closed_dates  # noqa: E402
from src.api import auth, kis_rest  # noqa: E402
from src.modules import f1_snapshot_selector as f1_sel  # noqa: E402

KST = ZoneInfo("Asia/Seoul")

MINUTE_PATH = "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
MINUTE_TR = "FHKST03010200"
MAX_KIS_CALLS = 60
RATE_LIMIT_CODES = {"EGW00201", "HTTP_429"}

# 안전 창: 개장 동시호가·최초 변동 구간 차단, 후보 수집 계약(09:35 이후)과 정렬.
FORBIDDEN_START = time(9, 0)
FORBIDDEN_END = time(9, 11)
EARLIEST_LIVE = time(9, 35)

MFE_WINDOW_START = "0900"
MFE_WINDOW_END = "0930"

# 승인된 장벽: 첫 스텝 +2.5% / 하드스탑 -2.0% (임계값 변경 아님, 분석용 상수).
UP_BARRIER_PCT = 0.025
DOWN_BARRIER_PCT = 0.020

# 페이지네이션 상한(호출 예산과 별개의 보호막).
MAX_PAGES = 20


class PocStop(RuntimeError):
    def __init__(self, reason: str, msg_cd: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.msg_cd = msg_cd


# ── 순수 함수 (테스트 대상) ─────────────────────────────────────────────

def parse_minute_bars(response: dict) -> tuple[list[dict], dict]:
    """분봉 응답을 (정렬된 bar 리스트, 이슈 카운트)로 파싱한다.

    시각/OHLC 필드가 없는 봉은 추정하지 않고 이슈로 세고 제외한다.
    """
    rows = response.get("output2")
    if rows is None:
        rows = response.get("output")
    if not isinstance(rows, list):
        raise PocStop("MINUTE_OUTPUT_MISSING")

    issues = {"empty_bar": 0, "field_missing": 0}
    bars: list[dict] = []
    for row in rows:
        if not isinstance(row, dict) or not row:
            issues["empty_bar"] += 1
            continue
        try:
            bar = {
                "date": str(row["stck_bsop_date"]),
                "time": str(row["stck_cntg_hour"]),
                "open": float(row["stck_oprc"]),
                "high": float(row["stck_hgpr"]),
                "low": float(row["stck_lwpr"]),
                "close": float(row["stck_prpr"]),
                "volume": float(row.get("cntg_vol") or 0),
            }
        except (KeyError, TypeError, ValueError):
            issues["field_missing"] += 1
            continue
        bars.append(bar)

    bars.sort(key=lambda b: (b["date"], b["time"]))
    return bars, issues


def mfe_mae(
    bars: list[dict],
    ref_price: float,
    *,
    start: str = MFE_WINDOW_START,
    end: str = MFE_WINDOW_END,
) -> dict | None:
    """구간 [start, end] KST의 MFE/MAE(%)와 발생 시각. 봉이 없으면 None."""
    if not ref_price or ref_price <= 0:
        return None
    window = [b for b in bars if start <= b["time"][:4] <= end]
    if not window:
        return None
    hi = max(window, key=lambda b: b["high"])
    lo = min(window, key=lambda b: b["low"])
    return {
        "mfe_pct": round((hi["high"] / ref_price - 1) * 100, 6),
        "mfe_time": hi["time"],
        "mae_pct": round((lo["low"] / ref_price - 1) * 100, 6),
        "mae_time": lo["time"],
        "bar_count": len(window),
    }


def barrier_order(bar: dict, up: float, down: float) -> str:
    """분봉 하나의 장벽 도달. 같은 봉에서 상·하 장벽 모두 닿으면 AMBIGUOUS."""
    hit_up = bar["high"] >= up
    hit_down = bar["low"] <= down
    if hit_up and hit_down:
        return "AMBIGUOUS"
    if hit_up:
        return "UP_FIRST"
    if hit_down:
        return "DOWN_FIRST"
    return "NONE"


def ambiguous_ratio(bars: list[dict], up: float, down: float) -> dict:
    """모호 봉 / 접촉 봉 비율. 접촉이 없으면 ratio=None."""
    ambiguous = touched = 0
    for bar in bars:
        order = barrier_order(bar, up, down)
        if order == "AMBIGUOUS":
            ambiguous += 1
            touched += 1
        elif order in ("UP_FIRST", "DOWN_FIRST"):
            touched += 1
    return {
        "ambiguous": ambiguous,
        "touched": touched,
        "ratio": (ambiguous / touched) if touched else None,
    }


# ── 안전 게이트 ─────────────────────────────────────────────────────────

def _assert_safe_live_window(now: datetime) -> None:
    current = now.timetz().replace(tzinfo=None)
    if FORBIDDEN_START <= current < FORBIDDEN_END:
        raise PocStop("FORBIDDEN_0900_0911")
    if current < EARLIEST_LIVE:
        raise PocStop("AFTER_0935_ONLY")


def _assert_success(response: dict) -> None:
    msg_cd = str(response.get("msg_cd") or "")
    if msg_cd in RATE_LIMIT_CODES:
        raise PocStop("RATE_LIMIT", msg_cd)
    if str(response.get("rt_cd") or "") != "0":
        raise PocStop("MINUTE_PRICE_FAILED", msg_cd or None)


async def fetch_minute_bars(
    ticker: str,
    *,
    budget: kis_rest.CallBudget,
    hour_cursor: str = "",
) -> dict:
    """당일 분봉 한 페이지 조회 (배경 우선순위·rate-limit 즉시 중단)."""
    return await kis_rest.get(
        MINUTE_PATH,
        tr_id=MINUTE_TR,
        params={
            "FID_ETC_CLS_CODE": "",
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": ticker,
            "FID_INPUT_HOUR_1": hour_cursor,
            "FID_PW_DATA_INCU_YN": "N",
        },
        stop_on_rate_limit=True,
        request_priority=kis_rest.REQUEST_PRIORITY_BACKGROUND,
        budget=budget,
    )


def select_target(args: argparse.Namespace) -> dict:
    """가장 최근(오늘) 최초 정상 스냅샷 하나에서 첫 유효 종목만 선택한다.

    과거 전 날짜의 종목을 합치지 않는다 — preflight 예산이 의미를 가지려면 대상이
    한 종목이어야 한다.
    """
    snapshot_dir = Path(args.snapshot_dir)
    db_path = Path(args.db_path)
    paths = sorted(snapshot_dir.glob("*.jsonl")) if snapshot_dir.exists() else []
    market_closed = read_market_closed_dates(db_path)
    selected, report = f1_sel.select_earliest_normal_snapshots(
        paths,
        market_closed_dates=market_closed,
        min_coverage=args.min_coverage,
        log_dir=Path(args.log_dir) if args.log_dir else None,
    )
    target: dict = {"selector": report, "trade_date": None, "ticker": None, "ref_price": None}
    if not selected:
        return target
    latest_date = max(selected)
    for line in selected[latest_date].read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        ticker = str(row.get("ticker") or "")
        ref = row.get("expected_price") or row.get("prev_close")
        if ticker and ref:
            target.update(
                {"trade_date": latest_date, "ticker": ticker, "ref_price": float(ref)}
            )
            break
    return target


def local_summary(args: argparse.Namespace) -> dict:
    target = select_target(args)
    # 대상은 1종목. 재시도까지 반영한 최악 호출 수가 예산 안이어야 시작한다.
    planned = args.max_pages if target["ticker"] else 0
    worst_case = planned * (1 + int(os.getenv("KIS_MAX_TRANSIENT_RETRIES", "2")))
    return {
        "selector": target["selector"],
        "trade_date": target["trade_date"],
        "ticker": target["ticker"],
        "ref_price": target["ref_price"],
        "max_pages": args.max_pages,
        "worst_case_calls_incl_retries": worst_case,
        "max_kis_calls": args.max_kis_calls,
        "budget_ok_to_start": bool(target["ticker"]) and worst_case <= args.max_kis_calls,
    }


async def fetch_all_minute_bars(
    ticker: str,
    trade_date: str,
    *,
    budget: kis_rest.CallBudget,
    max_pages: int = MAX_PAGES,
) -> tuple[list[dict], dict]:
    """시간 커서로 당일 분봉을 역방향 페이지네이션한다.

    진전 없음(같은 커서)·중복(새 봉 0)에서 멈추고, max_pages와 CallBudget으로도
    상한을 건다. 예산 초과 시도는 `_request`가 전송 전에 거절한다.
    """
    bars: list[dict] = []
    seen: set[tuple[str, str]] = set()
    issues = {"empty_bar": 0, "field_missing": 0}
    cursor = ""
    pages = 0
    for _ in range(max_pages):
        resp = await fetch_minute_bars(ticker, budget=budget, hour_cursor=cursor)
        _assert_success(resp)
        page, page_issues = parse_minute_bars(resp)
        issues["empty_bar"] += page_issues["empty_bar"]
        issues["field_missing"] += page_issues["field_missing"]
        page = [b for b in page if b["date"] == trade_date]
        new = [b for b in page if (b["date"], b["time"]) not in seen]
        pages += 1
        if not new:
            break  # 중복/진전 없음 → 종료
        for b in new:
            seen.add((b["date"], b["time"]))
            bars.append(b)
        earliest = min(b["time"] for b in page)
        if earliest == cursor:
            break  # 커서 진전 없음 → 종료
        cursor = earliest
    bars.sort(key=lambda b: (b["date"], b["time"]))
    issues["pages_fetched"] = pages
    return bars, issues


def analyze_minute_bars(bars: list[dict], ref_price: float) -> dict:
    """09:00~09:30 MFE/MAE와 +2.5%/-2.0% 장벽 AMBIGUOUS 개수·비율."""
    up = ref_price * (1 + UP_BARRIER_PCT)
    down = ref_price * (1 - DOWN_BARRIER_PCT)
    window = [b for b in bars if MFE_WINDOW_START <= b["time"][:4] <= MFE_WINDOW_END]
    return {
        "ref_price": ref_price,
        "up_barrier": round(up, 4),
        "down_barrier": round(down, 4),
        "window_bar_count": len(window),
        "mfe_mae_0900_0930": mfe_mae(bars, ref_price),
        "ambiguous": ambiguous_ratio(window, up, down),
    }


async def run_live(args: argparse.Namespace, summary: dict) -> dict:
    result: dict = {
        "status": "NOT_RUN",
        "read_only": True,
        "mode": os.getenv("KIS_MODE", "PAPER"),
        "endpoint": MINUTE_PATH,
        "tr_id": MINUTE_TR,
        "ticker": summary.get("ticker"),
        "trade_date": summary.get("trade_date"),
    }
    try:
        if result["mode"] != "PAPER":
            raise PocStop("PAPER_ONLY")
        _assert_safe_live_window(datetime.now(KST))
        if not summary["budget_ok_to_start"]:
            raise PocStop("BUDGET_WOULD_EXCEED_60")
        if not summary.get("ticker"):
            raise PocStop("NO_TARGET_SNAPSHOT")
        if not await auth.load_or_refresh():
            raise PocStop("AUTH_FAILED")

        budget = kis_rest.CallBudget(args.max_kis_calls)
        bars, issues = await fetch_all_minute_bars(
            summary["ticker"],
            summary["trade_date"],
            budget=budget,
            max_pages=args.max_pages,
        )
        result.update(
            {
                "status": "SUCCESS",
                "calls_used": budget.used,
                "bar_count": len(bars),
                "issues": issues,
                "analysis": analyze_minute_bars(bars, summary["ref_price"]),
            }
        )
    except kis_rest.RequestBudgetExceeded:
        result["status"] = "STOPPED"
        result["stop_reason"] = "BUDGET_EXCEEDED"
    except PocStop as exc:
        result["status"] = "STOPPED"
        result["stop_reason"] = exc.reason
        if exc.msg_cd:
            result["msg_cd"] = exc.msg_cd
    except Exception as exc:  # noqa: BLE001 — read-only PoC, never raise to shell
        result["status"] = "STOPPED"
        result["stop_reason"] = "UNEXPECTED_ERROR"
        result["error_type"] = type(exc).__name__
    return result


async def run(args: argparse.Namespace) -> dict:
    summary = local_summary(args)
    result = {
        "checked_at": datetime.now(KST).isoformat(timespec="seconds"),
        "read_only": True,
        "local_summary": summary,
    }
    result["kis_minute_comparison"] = (
        await run_live(args, summary)
        if args.with_kis
        else {"status": "NOT_RUN"}
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="당일 분봉 읽기 전용 PoC")
    parser.add_argument("--snapshot-dir", default=str(ROOT / "data" / "f1_snapshots"))
    parser.add_argument(
        "--db-path", default=str(ROOT / os.getenv("DB_DIR", "data/db") / "trading.db")
    )
    parser.add_argument("--log-dir", default=str(ROOT / os.getenv("LOG_DIR", "data/logs")))
    parser.add_argument("--min-coverage", type=int, default=10)
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES)
    parser.add_argument("--max-kis-calls", type=int, default=MAX_KIS_CALLS)
    parser.add_argument("--with-kis", action="store_true", help="장중(09:35+) PAPER 분봉 GET")
    args = parser.parse_args()
    result = asyncio.run(run(args))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["kis_minute_comparison"].get("status") != "STOPPED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
