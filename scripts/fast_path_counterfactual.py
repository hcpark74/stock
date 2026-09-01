"""Fast Path 하이브리드 전환 판단용 반사실 평가 — 읽기 전용.

Shadow 관측일마다 "Fast 1순위를 09:00에 잡았다면" 과 "레거시 1순위를 약 89초 뒤에
잡았다면" 의 개장 30분 결과를 나란히 놓는다. 후보 일치도(rank1_match)만으로는
전환 여부를 판단할 수 없어서, 불일치가 유리했는지 불리했는지를 가린다.

기본 실행은 외부 API를 호출하지 않고 ``data/paper_fast_probe`` 만 요약한다.
``--with-kis`` 는 ``KIS_MODE=PAPER``, 09:35 이후(09:00~09:11 차단)만 허용하고
분봉 GET만 배경 우선순위로 호출한다. 주문·정정·취소 경로는 없다.

호출 예산은 추정하지 않는다. ``kis_rest.CallBudget`` 을 ``_request`` 재귀 전 분기로
전파해 실제 HTTP 시도마다 1회 차감한다.

분봉은 봉 내부 경로를 모르므로 트레일링은 재현하지 않는다. 승인된 장벽
(+2.5% / -2.0%) 선착과 MFE/MAE까지만 내고, 같은 봉에서 양쪽 장벽에 닿으면
AMBIGUOUS로 남겨 판정에서 제외한다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, time
from pathlib import Path
from time import monotonic
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# 테스트 수집 중에는 개발 머신의 .env가 os.environ을 오염시키지 않게 한다
# (conftest가 STOCK_SKIP_DOTENV=1을 설정). 운영/수동 실행에서는 정상 로드한다.
if os.getenv("STOCK_SKIP_DOTENV", "0") != "1":
    load_dotenv(ROOT / ".env")

from scripts.kis_minute_bar_poc import (  # noqa: E402
    DOWN_BARRIER_PCT,
    UP_BARRIER_PCT,
    PocStop,
    mfe_mae,
    parse_minute_bars,
)
from src.api import auth, kis_rest  # noqa: E402
from src.api.kis_minute_bars import fetch_daily_minute_bars  # noqa: E402

KST = ZoneInfo("Asia/Seoul")

RATE_LIMIT_CODES = {"EGW00201", "HTTP_429"}
MAX_KIS_CALLS = 60
# PAPER는 초당 1건 남짓만 허용한다. 연속 호출은 EGW00201로 그날 표본을 통째로 잃는다.
REQUEST_INTERVAL_SEC = 1.2

# 안전 창: kis_minute_bar_poc와 동일 규약.
FORBIDDEN_START = time(9, 0)
FORBIDDEN_END = time(9, 11)
EARLIEST_LIVE = time(9, 35)

# 진입 시점 모델. Fast는 09:00:00.3 멀티시세 매도호가로 즉시 진입한다.
# 레거시는 관측된 F1 선정 지연(68~89초) 때문에 09:01 봉 종가에 가깝다.
FAST_ENTRY_BAR = "0900"
LEGACY_ENTRY_BAR = "0901"
WINDOW_END = "0930"

# 판정 순위: 익절 장벽 선착 > 미접촉 > 손절 장벽 선착.
_OUTCOME_RANK = {"UP_FIRST": 2, "NONE": 1, "DOWN_FIRST": 0}


# ── 프로브 파일 파싱 (순수 함수) ─────────────────────────────────────────

def read_probe_day(path: Path) -> list[dict]:
    """프로브 JSONL 한 날짜를 읽는다. 깨진 줄은 추정하지 않고 건너뛴다."""
    records: list[dict] = []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return records
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def extract_ask_prices(records: list[dict]) -> dict[str, float]:
    """개장 멀티시세의 종목별 매도1호가. 하이브리드가 실제로 주문했을 가격이다.

    장전(PREOPEN) 응답은 예상체결 구간이라 진입가로 쓸 수 없어 제외한다.
    """
    prices: dict[str, float] = {}
    for record in records:
        if record.get("event") != "PAPER_FAST_PROBE_OPEN_MULTI":
            continue
        if record.get("phase") != "OPEN":
            continue
        response = record.get("response")
        rows = response.get("output") if isinstance(response, dict) else None
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            ticker = str(row.get("inter_shrn_iscd") or "")
            try:
                ask = float(row.get("inter2_askp") or 0)
            except (TypeError, ValueError):
                continue
            if ticker and ask > 0:
                prices[ticker] = ask
    return prices


def extract_rank1(records: list[dict]) -> tuple[str | None, str | None]:
    """(Fast 1순위, 레거시 1순위). 어느 쪽이든 없으면 None."""
    fast: str | None = None
    legacy: str | None = None
    for record in records:
        event = record.get("event")
        if event == "PAPER_FAST_PROBE_OPEN_DONE":
            tickers = record.get("shadow_tickers")
            if isinstance(tickers, list) and tickers:
                fast = str(tickers[0])
        elif event == "PAPER_FAST_SHADOW_COMPARE":
            tickers = record.get("legacy_tickers")
            if isinstance(tickers, list) and tickers:
                legacy = str(tickers[0])
    return fast, legacy


def _record_time(record: dict) -> datetime | None:
    """레코드 시각. 파싱 불가면 None (추정하지 않는다)."""
    try:
        stamped = datetime.fromisoformat(str(record.get("ts") or ""))
    except (TypeError, ValueError):
        return None
    return stamped.replace(tzinfo=KST) if stamped.tzinfo is None else stamped.astimezone(KST)


def has_timely_comparison(records: list[dict], max_delay_sec: float = 180.0) -> bool:
    """완전한 개장 관측 뒤 3분 이내 레거시 비교가 있는지.

    ``paper_fast_probe.shadow_validation_summary`` 와 같은 규칙이다. 늦은 비교는
    수동 재실행이라 개장 시점 선정을 대표하지 않는다.
    """
    opens: list[datetime] = []
    compares: list[datetime] = []
    for record in records:
        stamped = _record_time(record)
        if stamped is None:
            continue
        if record.get("event") == "PAPER_FAST_PROBE_OPEN_DONE":
            quality = record.get("quality")
            if isinstance(quality, dict) and quality.get("ok") is True:
                opens.append(stamped)
        elif record.get("event") == "PAPER_FAST_SHADOW_COMPARE":
            compares.append(stamped)
    return any(
        0.0 <= (compared_at - opened_at).total_seconds() <= max_delay_sec
        for compared_at in compares
        for opened_at in opens
    )


def build_day_case(date: str, records: list[dict]) -> dict | None:
    """하루치 비교 대상. 개장 관측이 불완전·부적시하거나 1순위가 없으면 None.

    ``fast_entry`` 만 개장 멀티시세 매도호가로 채운다. 레거시는 F1 선정에 68~89초가
    걸린 뒤에야 주문하므로 09:00 호가로 값을 매기면 비교의 대상인 지연 페널티가
    사라진다. ``legacy_entry`` 는 항상 None이고 분봉 09:01 종가로 채운다.
    """
    if not has_timely_comparison(records):
        return None

    fast, legacy = extract_rank1(records)
    if not fast or not legacy:
        return None

    return {
        "date": date,
        "fast_ticker": fast,
        "fast_entry": extract_ask_prices(records).get(fast),
        "legacy_ticker": legacy,
        "legacy_entry": None,
    }


# ── 진입가 / 장벽 (순수 함수) ────────────────────────────────────────────

def entry_price_from_bars(
    bars: list[dict], bar_prefix: str, *, field: str = "open"
) -> float | None:
    """``bar_prefix`` (HHMM)로 시작하는 첫 봉의 지정 필드. 없으면 None."""
    for bar in bars:
        if str(bar.get("time") or "")[: len(bar_prefix)] == bar_prefix:
            try:
                value = float(bar[field])
            except (KeyError, TypeError, ValueError):
                return None
            return value if value > 0 else None
    return None


def first_barrier(
    bars: list[dict],
    entry_price: float,
    *,
    start: str = FAST_ENTRY_BAR,
    end: str = WINDOW_END,
    up_pct: float = UP_BARRIER_PCT,
    down_pct: float = DOWN_BARRIER_PCT,
) -> dict:
    """구간 [start, end]에서 시간순 첫 장벽 접촉.

    같은 봉이 양쪽에 닿으면 봉 내부 순서를 알 수 없으므로 AMBIGUOUS로 멈춘다.
    """
    up = entry_price * (1 + up_pct)
    down = entry_price * (1 - down_pct)
    window = [b for b in bars if start <= str(b.get("time") or "")[:4] <= end]
    for bar in sorted(window, key=lambda b: str(b.get("time") or "")):
        hit_up = float(bar["high"]) >= up
        hit_down = float(bar["low"]) <= down
        if hit_up and hit_down:
            return {"outcome": "AMBIGUOUS", "time": bar["time"]}
        if hit_up:
            return {"outcome": "UP_FIRST", "time": bar["time"]}
        if hit_down:
            return {"outcome": "DOWN_FIRST", "time": bar["time"]}
    return {"outcome": "NONE", "time": None}


# ── 판정 / 집계 (순수 함수) ──────────────────────────────────────────────

def verdict(fast: dict | None, legacy: dict | None) -> str:
    """두 결과를 비교한다. 한쪽이라도 모호하거나 없으면 UNDECIDABLE."""
    if not fast or not legacy:
        return "UNDECIDABLE"
    fast_rank = _OUTCOME_RANK.get(str(fast.get("outcome")))
    legacy_rank = _OUTCOME_RANK.get(str(legacy.get("outcome")))
    if fast_rank is None or legacy_rank is None:
        return "UNDECIDABLE"
    if fast_rank > legacy_rank:
        return "FAST_BETTER"
    if fast_rank < legacy_rank:
        return "LEGACY_BETTER"
    return "TIE"


def summarize(rows: list[dict]) -> dict:
    """일자별 판정을 누적한다. decisive는 선택이 결과를 가른 날만 센다."""
    counts = {"FAST_BETTER": 0, "LEGACY_BETTER": 0, "TIE": 0, "UNDECIDABLE": 0}
    for row in rows:
        key = str(row.get("verdict") or "UNDECIDABLE")
        counts[key] = counts.get(key, 0) + 1
    return {
        "evaluated_days": len(rows),
        "fast_better": counts["FAST_BETTER"],
        "legacy_better": counts["LEGACY_BETTER"],
        "tie": counts["TIE"],
        "undecidable": counts["UNDECIDABLE"],
        "decisive_days": counts["FAST_BETTER"] + counts["LEGACY_BETTER"],
    }


class Throttle:
    """호출 간 최소 간격을 지키는 단순 페이서. 시각은 주입받아 순수하게 계산한다."""

    def __init__(self, interval_sec: float = REQUEST_INTERVAL_SEC) -> None:
        self.interval_sec = max(0.0, float(interval_sec))
        self.last_call: float | None = None

    def wait_seconds(self, now: float) -> float:
        if self.last_call is None:
            return 0.0
        return max(0.0, self.interval_sec - (now - self.last_call))

    def mark(self, now: float) -> None:
        self.last_call = now


# ── 안전 게이트 ─────────────────────────────────────────────────────────

def _assert_paper_mode() -> None:
    if os.getenv("KIS_MODE", "PAPER") != "PAPER":
        raise PocStop("PAPER_ONLY")


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


# ── 분봉 조회 (읽기 전용 GET) ────────────────────────────────────────────
# fetch_daily_minute_bars / DAILY_MINUTE_PATH / DAILY_MINUTE_TR은
# src/api/kis_minute_bars.py로 승격되었다. 위에서 import한다.



async def load_bars(
    ticker: str,
    trade_date: str,
    *,
    budget: kis_rest.CallBudget,
    throttle: Throttle | None = None,
) -> list[dict]:
    """해당 거래일 분봉만 남긴다. 오늘자도 일별TR로 읽는다.

    당일TR은 빈 커서에서 장 마감 직전 30봉(15:01~15:30)을 주므로 개장 30분 창에
    쓸 수 없다. 일별TR은 ``FID_INPUT_HOUR_1`` 기준 이전 30봉을 주므로 09:30 기준으로
    09:00~09:30을 정확히 덮는다.

    휴장일을 요청하면 KIS가 가장 가까운 거래일로 조용히 대체하므로, 요청 날짜와
    다른 봉은 전부 버린다. 빈 리스트는 '데이터 없음'이며 호출부가 미판정으로 다룬다.
    """
    if throttle is not None:
        await asyncio.sleep(throttle.wait_seconds(monotonic()))
        throttle.mark(monotonic())
    response = await fetch_daily_minute_bars(ticker, trade_date, budget=budget)
    _assert_success(response)
    bars, _issues = parse_minute_bars(response)
    return [b for b in bars if b["date"] == trade_date]


# ── 한 종목 평가 ────────────────────────────────────────────────────────

def evaluate_side(
    bars: list[dict], *, entry_price: float | None, entry_bar: str, entry_field: str
) -> dict | None:
    """진입가와 분봉으로 장벽/MFE/MAE를 낸다.

    진입가를 못 정하거나 측정 창에 봉이 하나도 없으면 None. 빈 창을 '장벽 미접촉
    (NONE)'으로 보고하면 데이터 없음이 유리한 결과로 둔갑해 판정을 오염시킨다.
    """
    price = entry_price or entry_price_from_bars(bars, entry_bar, field=entry_field)
    if not price or price <= 0:
        return None
    excursion = mfe_mae(bars, price, start=entry_bar, end=WINDOW_END)
    if not excursion or not excursion.get("bar_count"):
        return None
    barrier = first_barrier(bars, price, start=entry_bar, end=WINDOW_END)
    return {
        "entry_price": price,
        "entry_bar": entry_bar,
        "outcome": barrier["outcome"],
        "barrier_time": barrier["time"],
        "mfe_pct": excursion.get("mfe_pct"),
        "mae_pct": excursion.get("mae_pct"),
        "bar_count": excursion.get("bar_count", 0),
    }


async def evaluate_case(
    case: dict, *, budget: kis_rest.CallBudget, throttle: Throttle | None = None
) -> dict:
    """하루치 Fast/레거시 반사실 비교."""
    date = case["date"]
    row: dict = dict(case)
    try:
        fast_bars = await load_bars(
            case["fast_ticker"], date, budget=budget, throttle=throttle
        )
        legacy_bars = await load_bars(
            case["legacy_ticker"], date, budget=budget, throttle=throttle
        )
    except PocStop as exc:
        row.update({"verdict": "UNDECIDABLE", "error": exc.reason})
        return row

    fast = evaluate_side(
        fast_bars,
        entry_price=case.get("fast_entry"),
        entry_bar=FAST_ENTRY_BAR,
        entry_field="open",
    )
    legacy = evaluate_side(
        legacy_bars,
        entry_price=case.get("legacy_entry"),
        entry_bar=LEGACY_ENTRY_BAR,
        entry_field="close",
    )
    row.update({"fast": fast, "legacy": legacy, "verdict": verdict(fast, legacy)})
    return row


# ── CLI ─────────────────────────────────────────────────────────────────

def collect_cases(probe_dir: Path) -> list[dict]:
    """프로브 디렉터리의 모든 날짜에서 비교 가능한 케이스만 날짜순으로 모은다."""
    cases: list[dict] = []
    paths = sorted(probe_dir.glob("*.jsonl")) if probe_dir.exists() else []
    for path in paths:
        case = build_day_case(path.stem, read_probe_day(path))
        if case:
            cases.append(case)
    return cases


def _print_report(rows: list[dict], summary: dict) -> None:
    print(f"{'날짜':<10} {'Fast':<8} {'진입':>9} {'결과':<11} "
          f"{'레거시':<8} {'진입':>9} {'결과':<11} 판정")
    for row in rows:
        fast = row.get("fast") or {}
        legacy = row.get("legacy") or {}
        print(
            f"{row['date']:<10} {row['fast_ticker']:<8} "
            f"{fast.get('entry_price') or 0:>9,.0f} {str(fast.get('outcome') or '-'):<11} "
            f"{row['legacy_ticker']:<8} "
            f"{legacy.get('entry_price') or 0:>9,.0f} {str(legacy.get('outcome') or '-'):<11} "
            f"{row.get('verdict')}"
        )
    print()
    print(json.dumps(summary, ensure_ascii=False, indent=2))


async def _run_with_kis(cases: list[dict]) -> list[dict]:
    _assert_paper_mode()
    _assert_safe_live_window(datetime.now(KST))
    budget = kis_rest.CallBudget(MAX_KIS_CALLS)
    if not await auth.load_or_refresh():
        raise PocStop("TOKEN_UNAVAILABLE")
    throttle = Throttle()
    rows = []
    for case in cases:
        rows.append(await evaluate_case(case, budget=budget, throttle=throttle))
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe-dir", default="data/paper_fast_probe")
    parser.add_argument(
        "--with-kis",
        action="store_true",
        help="분봉 GET 호출로 반사실을 실제 평가한다 (PAPER, 09:35 이후만).",
    )
    parser.add_argument("--out", default="", help="결과 JSON 저장 경로")
    args = parser.parse_args(argv)

    cases = collect_cases(Path(args.probe_dir))
    if not args.with_kis:
        print(json.dumps({"cases": cases, "count": len(cases)}, ensure_ascii=False, indent=2))
        print("\n분봉 평가는 --with-kis 로 실행한다 (읽기 전용 GET).")
        return 0

    try:
        rows = asyncio.run(_run_with_kis(cases))
    except PocStop as exc:
        print(json.dumps({"stopped": exc.reason, "msg_cd": exc.msg_cd}, ensure_ascii=False))
        return 1

    summary = summarize(rows)
    _print_report(rows, summary)
    if args.out:
        Path(args.out).write_text(
            json.dumps({"rows": rows, "summary": summary}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
