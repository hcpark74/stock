# 트랙 B 매매 규칙 선정 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 트랙 B의 09:35 이후 진입 규칙을 세 개의 사전 등록 후보 중에서 고르기 위한 표본(전 세션 분봉)과 판정 하네스를 만들고, 사전에 고정한 관문으로 규칙 하나를 확정한다.

**Architecture:** 순수 함수 계층(`track_b_rules.py` — 진입 축·청산 엔진, I/O 없음)과 I/O 계층(`track_b_backfill.py` — KIS 백필, `track_b_backtest.py` — 유니버스·캐시·관문·CLI)을 분리한다. 지표는 운영 코드 `src.indicators`를 그대로 쓴다. 기존 `strategy_backtest.py`는 09:00~09:30 장벽 모델에 묶여 있어 수정하지 않고 `load_universes`와 캐시 I/O만 import한다.

**Tech Stack:** Python 3.11+ / asyncio / pytest (`asyncio_mode = auto`) / KIS REST (읽기 전용 GET)

**Spec:** [docs/superpowers/specs/2026-08-28-track-b-rule-selection-design.md](../specs/2026-08-28-track-b-rule-selection-design.md)

## Global Constraints

모든 태스크의 요구사항에 아래가 암묵적으로 포함된다.

- **KIS 호출은 PAPER 고정.** `KIS_MODE != "PAPER"`면 즉시 중단한다.
- **KIS 호출은 15:40 KST 이후만.** `strategy_backtest._assert_safe_live_window`는 09:00~09:35만 막으므로 이 가드로 충분하지 않다. [`kis_phase5_historical_poc.py:255`](../../../scripts/kis_phase5_historical_poc.py#L255)와 같이 `time(15, 40)`을 직접 건다.
- **읽기 전용 GET만.** 주문·정정·취소 경로를 구현하지 않는다. `request_priority=kis_rest.REQUEST_PRIORITY_BACKGROUND`, `stop_on_rate_limit=True`.
- **지표를 다시 구현하지 않는다.** `src.indicators.sma` / `.macd`만 쓴다. 하네스가 지표를 따로 구현하면 실시간과 백테스트가 다른 값을 본다.
- **파라미터 스윕 금지, 규칙군 추가 금지** (스펙 §5.6). 값이 결과를 바꾸면 그 사실을 기록하되 값을 고르지 않는다.
- **봉 시각은 `HHMMSS` 문자열**로 비교한다. `data/backtest_bars/*.json`의 봉 dict는 `{"date","time","open","high","low","close","volume"}`이다.
- **트랙 A의 코드를 수정하지 않는다.** `src/modules/f3_entry.py`, `f4_tracking.py`, `f5_timeout.py`, `scripts/strategy_backtest.py`는 읽기만 한다.
- **커밋 메시지는 저장소 관례**를 따른다 — 영문, 무엇을 왜 바꿨는지, 끝에 `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`. 여러 줄 메시지는 파일에 쓰고 `git commit -F`로 넣는다 (PowerShell 따옴표 문제).

## 스펙이 모호했던 지점 — 이 계획에서 확정한다

스펙 §4.1은 "랭크 1~5 중 조건을 만족하는 최상위 랭크 1종목"이라고만 적었다. 랭크 3이 09:40에, 랭크 1이 10:20에 신호를 내면 어느 쪽인지 정해지지 않는다. **실시간에 구현 가능한 해석만 채택한다: 봉을 시간 순으로 훑고, 신호가 난 첫 봉에서 멈춘다. 같은 봉에서 둘 이상이 신호를 내면 그중 최상위 랭크를 고른다.** 미래를 봐야 하는 해석(나중에 랭크 1이 신호를 낼지 기다린다)은 구현 불가이므로 배제한다. Task 8에서 이 확정을 스펙에 반영한다.

---

### Task 1: 전 세션 분봉 백필 스크립트

**Files:**
- Create: `scripts/track_b_backfill.py`
- Test: `tests/test_track_b_backfill.py`

**Interfaces:**
- Consumes: `scripts.fast_path_counterfactual.fetch_daily_minute_bars(ticker, trade_date, *, budget, hour_cursor="093000") -> dict`, `_assert_success(response)`, `Throttle(interval_sec)`, `PocStop(reason, detail=None)`; `src.api.kis_minute_bars.parse_minute_bars(response) -> tuple[list[dict], dict]`; `src.api.kis_rest.CallBudget(max_calls)`; `scripts.strategy_backtest.read_cached_bars/write_cached_bars/load_universes`
- Produces:
  - `merge_bars(existing: list[dict], fetched: list[dict]) -> list[dict]`
  - `next_cursor(bars: list[dict]) -> str | None`
  - `assert_backfill_window(now: datetime) -> None`
  - `assert_paper_mode() -> None`
  - `needed_pairs(depth: int = 5, snapshot_dir: Path | None = None) -> dict[str, set[str]]`
  - `is_session_complete(bars: list[dict] | None, min_bars: int = 300) -> bool`
  - `async fetch_session_bars(ticker, date, *, budget, throttle, max_pages=15) -> list[dict]`
  - `async backfill(needed: dict[str, set[str]], *, cache_dir, budget, throttle) -> dict[str, int]`

**먼저 확인할 것 (설계 §3.3):** 캐시 파일이 31봉에서 약 380봉으로 늘어난다. `data/backtest_bars`를 읽는 호출부 중 봉 **개수**에 의존하는 곳이 있으면 조용히 깨진다.

```bash
grep -rn "backtest_bars\|read_cached_bars\|load_bar_cache" scripts/ src/ tests/
```

`strategy_backtest.py`는 `WINDOW_END = "0930"`으로 시각을 잘라 안전하다(확인됨). 다른 호출부가 나오면 시각으로 자르도록 먼저 고치고 그 커밋을 분리한다.

- [ ] **Step 1: 병합·커서·시간가드의 실패하는 테스트를 쓴다**

```python
# tests/test_track_b_backfill.py
"""전 세션 분봉 백필 검증.

백필이 봉을 중복시키거나 커서를 헛돌면 표본이 조용히 망가진다. 그리고 이
스크립트는 KIS를 1,300번 때리므로 시간 가드가 틀리면 장중에 A의 유량을 먹는다.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from scripts.fast_path_counterfactual import PocStop
from scripts.track_b_backfill import (
    assert_backfill_window,
    merge_bars,
    next_cursor,
)

KST = ZoneInfo("Asia/Seoul")


def _bar(time_: str, close: float = 100.0) -> dict:
    return {
        "date": "20260820",
        "time": time_,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 10.0,
    }


def test_merge_dedupes_and_sorts():
    existing = [_bar("091000"), _bar("090000")]
    fetched = [_bar("090000", close=999.0), _bar("085900")]
    merged = merge_bars(existing, fetched)
    assert [b["time"] for b in merged] == ["085900", "090000", "091000"]
    # 기존 값을 유지한다. 같은 봉을 다시 받아도 캐시가 흔들리면 안 된다.
    assert merged[1]["close"] == 100.0


def test_next_cursor_is_earliest_bar():
    assert next_cursor([_bar("091000"), _bar("090500")]) == "090500"


def test_next_cursor_none_when_empty():
    assert next_cursor([]) is None


def test_window_rejects_before_1540():
    with pytest.raises(PocStop) as exc:
        assert_backfill_window(datetime(2026, 8, 28, 15, 39, 59, tzinfo=KST))
    assert exc.value.reason == "AFTER_1540_ONLY"


def test_window_allows_after_1540():
    assert_backfill_window(datetime(2026, 8, 28, 15, 40, tzinfo=KST)) is None
```

- [ ] **Step 2: 실패를 확인한다**

Run: `python -m pytest tests/test_track_b_backfill.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.track_b_backfill'`

- [ ] **Step 3: 순수 함수와 가드를 구현한다**

```python
# scripts/track_b_backfill.py
"""전 세션 분봉 백필 — 읽기 전용.

``data/backtest_bars`` 의 09:00~09:30 31봉을 09:00~15:30 전 세션으로 늘린다.
트랙 B는 09:35 이후에 판정하므로 기존 캐시로는 규칙을 고를 수 없다.

일별분봉 TR(``FHKST03010230``)은 ``FID_INPUT_HOUR_1`` 기준 이전 30봉을 준다.
커서를 가장 이른 봉으로 밀어 09:00까지 역방향으로 채운다.

KIS를 1,300번 때리므로 가드가 전부다. PAPER 고정, 15:40 이후, GET만, 예산
객체로 상한. 캐시가 남으므로 중단해도 재실행하면 이어진다.
"""

from __future__ import annotations

import argparse
import asyncio
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
if os.getenv("STOCK_SKIP_DOTENV", "0") != "1":
    load_dotenv(ROOT / ".env")

from scripts.fast_path_counterfactual import (  # noqa: E402
    PocStop,
    Throttle,
    _assert_success,
    fetch_daily_minute_bars,
)
from scripts.strategy_backtest import (  # noqa: E402
    BAR_CACHE_DIR,
    load_universes,
    read_cached_bars,
    write_cached_bars,
)
from src.api import kis_rest  # noqa: E402
from src.api.kis_minute_bars import parse_minute_bars  # noqa: E402
from src.modules import f1_selector  # noqa: E402

KST = ZoneInfo("Asia/Seoul")

# 과거 데이터 조회는 15:40 이후. DEV_ENV.md 규약이며 각 스크립트가 직접 건다 —
# strategy_backtest 의 안전창은 09:00~09:35만 막는다.
EARLIEST_BACKFILL = time(15, 40)

SESSION_START = "090000"
# 09:00~15:30 = 391봉. 페이지당 30봉이라 14페이지면 닿는다. 한 장 여유.
MAX_PAGES = 15


def assert_backfill_window(now: datetime) -> None:
    if now.timetz().replace(tzinfo=None) < EARLIEST_BACKFILL:
        raise PocStop("AFTER_1540_ONLY")


def assert_paper_mode() -> None:
    if os.getenv("KIS_MODE", "").upper() != "PAPER":
        raise PocStop("NOT_PAPER_MODE")


def merge_bars(existing: list[dict], fetched: list[dict]) -> list[dict]:
    """시각 기준으로 합치고 정렬한다. 기존 봉을 새 봉으로 덮지 않는다.

    같은 봉을 다시 받는 것은 정상(커서가 겹친다)이고, 그때 값이 흔들리면
    캐시가 재현 불가능해진다.
    """
    merged: dict[tuple[str, str], dict] = {}
    for bar in fetched:
        merged[(bar["date"], bar["time"])] = bar
    for bar in existing:
        merged[(bar["date"], bar["time"])] = bar
    return [merged[k] for k in sorted(merged)]


def next_cursor(bars: list[dict]) -> str | None:
    """다음 페이지 커서 — 이번 페이지에서 가장 이른 봉."""
    if not bars:
        return None
    return min(b["time"] for b in bars)
```

- [ ] **Step 4: 순수 함수 테스트가 통과하는지 확인한다**

Run: `python -m pytest tests/test_track_b_backfill.py -v`
Expected: PASS (5개)

- [ ] **Step 5: 페이지네이션의 실패하는 테스트를 쓴다**

```python
# tests/test_track_b_backfill.py 에 추가
from unittest.mock import AsyncMock, patch

from scripts.track_b_backfill import fetch_session_bars
from src.api import kis_rest


def _response(times: list[str]) -> dict:
    return {
        "rt_cd": "0",
        "output2": [
            {
                "stck_bsop_date": "20260820",
                "stck_cntg_hour": t,
                "stck_oprc": "100",
                "stck_hgpr": "101",
                "stck_lwpr": "99",
                "stck_prpr": "100",
                "cntg_vol": "10",
            }
            for t in times
        ],
    }


async def test_fetch_session_pages_backwards_until_session_start():
    pages = [
        _response(["093000", "092900"]),
        _response(["092800", "090000"]),
    ]
    calls: list[str] = []

    async def fake_fetch(ticker, trade_date, *, budget, hour_cursor="093000"):
        calls.append(hour_cursor)
        return pages[len(calls) - 1]

    with patch("scripts.track_b_backfill.fetch_daily_minute_bars", fake_fetch):
        bars = await fetch_session_bars(
            "005930", "20260820",
            budget=kis_rest.CallBudget(10),
            throttle=Throttle(0.0),
        )

    # 첫 커서는 장 마감, 그다음은 직전 페이지의 가장 이른 봉이다.
    assert calls == ["153000", "092900"]
    # 09:00에 닿으면 멈춘다 — 더 밀면 전일 봉이 섞인다.
    assert [b["time"] for b in bars] == ["090000", "092800", "092900", "093000"]


async def test_fetch_session_drops_other_dates():
    page = _response(["090000"])
    page["output2"].append({
        "stck_bsop_date": "20260819",
        "stck_cntg_hour": "151900",
        "stck_oprc": "1", "stck_hgpr": "1", "stck_lwpr": "1",
        "stck_prpr": "1", "cntg_vol": "1",
    })

    async def fake_fetch(ticker, trade_date, *, budget, hour_cursor="093000"):
        return page

    with patch("scripts.track_b_backfill.fetch_daily_minute_bars", fake_fetch):
        bars = await fetch_session_bars(
            "005930", "20260820",
            budget=kis_rest.CallBudget(10),
            throttle=Throttle(0.0),
        )

    assert {b["date"] for b in bars} == {"20260820"}


async def test_fetch_session_stops_when_cursor_stalls():
    """같은 페이지가 계속 오면 멈춘다. 안 멈추면 예산을 다 태운다."""
    calls = {"n": 0}

    async def fake_fetch(ticker, trade_date, *, budget, hour_cursor="093000"):
        calls["n"] += 1
        return _response(["093000"])

    with patch("scripts.track_b_backfill.fetch_daily_minute_bars", fake_fetch):
        bars = await fetch_session_bars(
            "005930", "20260820",
            budget=kis_rest.CallBudget(10),
            throttle=Throttle(0.0),
        )

    assert calls["n"] == 2
    assert [b["time"] for b in bars] == ["093000"]
```

- [ ] **Step 6: 실패를 확인한다**

Run: `python -m pytest tests/test_track_b_backfill.py -v`
Expected: FAIL — `ImportError: cannot import name 'fetch_session_bars'`

- [ ] **Step 7: 페이지네이션을 구현한다**

```python
# scripts/track_b_backfill.py 에 추가
async def fetch_session_bars(
    ticker: str,
    date: str,
    *,
    budget: kis_rest.CallBudget,
    throttle: Throttle,
    max_pages: int = MAX_PAGES,
) -> list[dict]:
    """전 세션 분봉. 장 마감 커서에서 09:00까지 역방향으로 페이지를 민다.

    요청 날짜와 다른 봉은 버린다 — KIS가 휴장일을 가장 가까운 거래일로 조용히
    대체하고, 커서가 09:00을 넘어가면 전일 봉이 섞인다.
    """
    bars: list[dict] = []
    seen: set[str] = set()
    cursor = "153000"

    for _ in range(max_pages):
        await asyncio.sleep(throttle.wait_seconds(monotonic()))
        throttle.mark(monotonic())
        response = await fetch_daily_minute_bars(
            ticker, date, budget=budget, hour_cursor=cursor
        )
        _assert_success(response)
        page, _issues = parse_minute_bars(response)
        page = [b for b in page if b["date"] == date]
        fresh = [b for b in page if b["time"] not in seen]
        if not fresh:
            break
        for bar in fresh:
            seen.add(bar["time"])
            bars.append(bar)
        earliest = min(b["time"] for b in fresh)
        if earliest <= SESSION_START:
            break
        cursor = earliest

    bars.sort(key=lambda b: b["time"])
    return bars
```

- [ ] **Step 8: 테스트 통과를 확인한다**

Run: `python -m pytest tests/test_track_b_backfill.py -v`
Expected: PASS (8개)

- [ ] **Step 9: 필요 쌍 계산과 CLI를 붙인다**

```python
# scripts/track_b_backfill.py 에 추가
def needed_pairs(depth: int = 5, snapshot_dir: Path | None = None) -> dict[str, set[str]]:
    """날짜별 F1 랭크 1~depth 종목. 운영 랭킹 함수를 그대로 쓴다."""
    universes = (
        load_universes(snapshot_dir) if snapshot_dir is not None else load_universes()
    )
    needed: dict[str, set[str]] = {}
    for date, rows in universes.items():
        ranked = f1_selector.rank_candidates(rows)[:depth]
        tickers = {str(r["ticker"]) for r in ranked if r.get("ticker")}
        if tickers:
            needed[date] = tickers
    return needed


def is_session_complete(bars: list[dict] | None, min_bars: int = 300) -> bool:
    """이미 전 세션이 채워진 쌍은 건너뛴다. 31봉짜리는 채운다."""
    return bool(bars) and len(bars) >= min_bars


async def backfill(
    needed: dict[str, set[str]],
    *,
    cache_dir: Path = BAR_CACHE_DIR,
    budget: kis_rest.CallBudget,
    throttle: Throttle,
) -> dict[str, int]:
    stats = {"skipped": 0, "filled": 0, "empty": 0, "failed": 0}
    for date in sorted(needed):
        for ticker in sorted(needed[date]):
            existing = read_cached_bars(date, ticker, cache_dir) or []
            if is_session_complete(existing):
                stats["skipped"] += 1
                continue
            try:
                fetched = await fetch_session_bars(
                    ticker, date, budget=budget, throttle=throttle
                )
            except PocStop:
                raise
            except Exception:
                stats["failed"] += 1
                continue
            if not fetched:
                stats["empty"] += 1
                continue
            write_cached_bars(date, ticker, merge_bars(existing, fetched), cache_dir)
            stats["filled"] += 1
            print(f"  {date} {ticker}: {len(existing)} -> "
                  f"{len(merge_bars(existing, fetched))}봉", flush=True)
    return stats


async def main_async(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="트랙 B 전 세션 분봉 백필")
    parser.add_argument("--depth", type=int, default=5, help="F1 랭크 상위 N종목")
    parser.add_argument("--max-calls", type=int, default=1600)
    parser.add_argument("--interval", type=float, default=1.2)
    parser.add_argument("--dry-run", action="store_true", help="호출 없이 계획만 출력")
    args = parser.parse_args(argv)

    needed = needed_pairs(args.depth)
    pairs = sum(len(v) for v in needed.values())
    print(f"대상 {len(needed)}거래일 / {pairs}쌍 (랭크 1~{args.depth})")
    if args.dry_run:
        return 0

    assert_paper_mode()
    assert_backfill_window(datetime.now(KST))
    from src.api import auth

    if not await auth.load_or_refresh():
        raise PocStop("TOKEN_UNAVAILABLE")

    stats = await backfill(
        needed,
        budget=kis_rest.CallBudget(args.max_calls),
        throttle=Throttle(args.interval),
    )
    print(stats)
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(main_async(argv))
    except PocStop as exc:
        print(f"중단: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 10: 필요 쌍 계산 테스트를 쓰고 통과시킨다**

```python
# tests/test_track_b_backfill.py 에 추가
import json

from scripts.track_b_backfill import is_session_complete, needed_pairs


def test_needed_pairs_uses_operational_ranking(tmp_path):
    # f1_selector 의 바닥 조건: gap_pct 는 [0.025, 0.100), expected_amount 는
    # 1억 이상. expected_amount 는 expected_price×volume 이 아니라 그 이름의
    # 필드(없으면 avg_amount_5d)를 읽는다.
    rows = [
        {"ticker": "000001", "gap_pct": 0.05, "prev_close": 950,
         "expected_amount": 5_000_000_000, "avg_amount_5d": 1_000_000_000},
        {"ticker": "000002", "gap_pct": 0.04, "prev_close": 960,
         "expected_amount": 3_000_000_000, "avg_amount_5d": 1_000_000_000},
    ]
    # load_universes 는 MIN_UNIVERSE_ROWS(30) 미만을 버린다. 같은 행을 늘려 채운다.
    padded = []
    for i in range(30):
        row = dict(rows[i % 2])
        row["ticker"] = f"{i:06d}"
        padded.append(row)
    path = tmp_path / "20260820_090100.jsonl"
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in padded), encoding="utf-8"
    )

    needed = needed_pairs(depth=5, snapshot_dir=tmp_path)
    assert set(needed) == {"20260820"}
    assert len(needed["20260820"]) <= 5


def test_session_complete_skips_full_days_only():
    assert is_session_complete([{"time": "090000"}] * 380) is True
    assert is_session_complete([{"time": "090000"}] * 31) is False
    assert is_session_complete(None) is False
```

Run: `python -m pytest tests/test_track_b_backfill.py -v`
Expected: PASS (10개)

- [ ] **Step 11: dry-run으로 실제 대상 규모를 확인한다 (KIS 호출 없음)**

Run: `python scripts/track_b_backfill.py --dry-run`
Expected: `대상 22거래일 / 약 100~110쌍 (랭크 1~5)` 형태의 출력. 0쌍이면 `load_universes`가 스냅샷을 못 읽은 것이므로 멈추고 원인을 본다.

- [ ] **Step 12: 커밋**

```bash
git add scripts/track_b_backfill.py tests/test_track_b_backfill.py
git commit -F <메시지 파일>
```

메시지 본문: 왜 기존 캐시로는 안 되는지(09:35 이후가 창 밖), 왜 15:40 가드를 직접 거는지(기존 안전창은 09:00~09:35만 막는다)를 적는다.

---

### Task 2: 청산 엔진 — 봉으로 재현한 스텝 트레일링

**Files:**
- Create: `scripts/track_b_rules.py`
- Test: `tests/test_track_b_rules.py`

**Interfaces:**
- Consumes: 없음 (순수 함수)
- Produces:
  - `STEP_SIZE = 0.025`, `STEP_TRAIL = 0.020`, `HARD_STOP = 0.020`, `TIMEOUT_TIME = "151500"`
  - `simulate_exit(bars, entry_idx, entry_price, *, order) -> dict` — `{"exit_idx","exit_time","exit_price","reason","pct"}`
  - `resolve_exit(bars, entry_idx, entry_price) -> dict` — 양쪽 순서를 돌려 `{"ambiguous": bool, "high_first": dict, "low_first": dict, "pct": float | None}`

- [ ] **Step 1: 청산 엔진의 실패하는 테스트를 쓴다**

```python
# tests/test_track_b_rules.py
"""트랙 B 규칙 후보의 순수 함수 검증.

청산은 트랙 A와 같은 한 벌(하드스탑·스텝 트레일링·15:15)이어야 진입 시각만
비교된다. 봉은 내부 경로를 모르므로 고가·저가 순서를 양쪽으로 돌리고, 답이
갈리는 날은 AMBIGUOUS로 판정에서 뺀다 — STRATEGY_BACKTEST_20260820.md 관례.
"""

import pytest

from scripts.track_b_rules import (
    HARD_STOP,
    STEP_SIZE,
    STEP_TRAIL,
    resolve_exit,
    simulate_exit,
)


def _bar(time_: str, *, open_: float, high: float, low: float, close: float) -> dict:
    return {
        "date": "20260820", "time": time_,
        "open": open_, "high": high, "low": low, "close": close, "volume": 1000.0,
    }


def test_hard_stop_before_trailing_activates():
    bars = [
        _bar("093600", open_=100, high=101, low=100, close=100),
        _bar("093700", open_=100, high=100, low=97.9, close=98),
    ]
    result = simulate_exit(bars, 0, 100.0, order="low_first")
    assert result["reason"] == "HARD_STOP"
    assert result["exit_price"] == pytest.approx(100.0 * (1 - HARD_STOP))
    assert result["pct"] == pytest.approx(-HARD_STOP * 100)


def test_trailing_stop_uses_highest_step_not_high_price():
    """스텝 +2.5% 도달 후 청산선은 진입가*(1+0.025-0.020)이다. 고가가 아니다."""
    bars = [
        _bar("093600", open_=100, high=100, low=100, close=100),
        _bar("093700", open_=100, high=104, low=103, close=103),
        _bar("093800", open_=103, high=103, low=100.4, close=100.4),
    ]
    result = simulate_exit(bars, 0, 100.0, order="high_first")
    assert result["reason"] == "TRAILING"
    assert result["exit_price"] == pytest.approx(100.0 * (1 + STEP_SIZE - STEP_TRAIL))
    assert result["exit_time"] == "093800"


def test_hard_stop_disarms_once_trailing_active():
    """A와 같다 — 트레일링이 켜지면 하드스탑은 더 이상 보지 않는다."""
    bars = [
        _bar("093600", open_=100, high=100, low=100, close=100),
        _bar("093700", open_=100, high=106, low=100, close=106),
        _bar("093800", open_=106, high=106, low=97, close=97),
    ]
    result = simulate_exit(bars, 0, 100.0, order="high_first")
    # 스텝 0.05 → 청산선 100*(1+0.05-0.02) = 103. 하드스탑 98이 아니다.
    assert result["reason"] == "TRAILING"
    assert result["exit_price"] == pytest.approx(103.0)


def test_timeout_closes_at_1515_close():
    bars = [
        _bar("093600", open_=100, high=100, low=100, close=100),
        _bar("151500", open_=100, high=101, low=99.5, close=100.5),
    ]
    result = simulate_exit(bars, 0, 100.0, order="high_first")
    assert result["reason"] == "TIMEOUT"
    assert result["exit_price"] == pytest.approx(100.5)


def test_same_bar_touches_both_is_ambiguous():
    """같은 봉이 스텝과 손절에 모두 닿으면 순서에 따라 답이 갈린다."""
    bars = [
        _bar("093600", open_=100, high=100, low=100, close=100),
        _bar("093700", open_=100, high=104, low=97.5, close=98),
        _bar("151500", open_=98, high=98, low=98, close=98),
    ]
    resolved = resolve_exit(bars, 0, 100.0)
    assert resolved["ambiguous"] is True
    assert resolved["pct"] is None
    assert resolved["high_first"]["reason"] == "TRAILING"
    assert resolved["low_first"]["reason"] == "HARD_STOP"


def test_unambiguous_day_reports_single_pct():
    bars = [
        _bar("093600", open_=100, high=100, low=100, close=100),
        _bar("093700", open_=100, high=100.5, low=99.8, close=100.2),
        _bar("151500", open_=100.2, high=100.3, low=100.1, close=100.3),
    ]
    resolved = resolve_exit(bars, 0, 100.0)
    assert resolved["ambiguous"] is False
    assert resolved["pct"] == pytest.approx(0.3)


def test_exit_measures_from_entry_bar():
    """진입 봉 이전의 저가로 손절 판정을 받으면 안 된다."""
    bars = [
        _bar("093500", open_=100, high=100, low=90, close=100),
        _bar("093600", open_=100, high=101, low=100, close=101),
        _bar("151500", open_=101, high=101, low=101, close=101),
    ]
    result = simulate_exit(bars, 1, 100.0, order="low_first")
    assert result["reason"] == "TIMEOUT"
```

- [ ] **Step 2: 실패를 확인한다**

Run: `python -m pytest tests/test_track_b_rules.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.track_b_rules'`

- [ ] **Step 3: 청산 엔진을 구현한다**

```python
# scripts/track_b_rules.py
"""트랙 B 규칙 후보 — 순수 함수만 둔다.

진입 축 세 개와 청산 한 벌이 들어 있다. I/O도 상태도 없어서 실시간 신호
엔진(2단계)과 백테스트가 같은 코드를 탈 수 있다.

청산은 트랙 A와 같다 — 하드스탑 -2.0%, 스텝 트레일링 +2.5%/-2.0%, 15:15.
B가 A와 다른 점을 진입 시각 하나로 줄여야 비교가 통제된다.
"""

from __future__ import annotations

import math

from src import indicators

# 트랙 A와 같은 값. f4_tracking.STEP_SIZE / STEP_TRAIL / HARD_STOP_RATIO 와 일치한다.
STEP_SIZE = 0.025
STEP_TRAIL = 0.020
HARD_STOP = 0.020
TIMEOUT_TIME = "151500"


def _step_of(pnl: float) -> float:
    return max(math.floor(pnl / STEP_SIZE) * STEP_SIZE, 0.0)


def simulate_exit(
    bars: list[dict],
    entry_idx: int,
    entry_price: float,
    *,
    order: str,
) -> dict:
    """진입 봉부터 청산까지 봉으로 훑는다.

    봉 안의 가격 경로는 모른다. ``order`` 로 고가·저가 중 어느 쪽을 먼저 본
    것으로 가정할지 정하고, 호출부가 양쪽을 돌려 답이 갈리는지 본다.
    """
    if entry_price <= 0:
        raise ValueError(f"entry_price must be positive: {entry_price}")
    highest_step = 0.0
    trailing_active = False

    # bars.index(bar) 를 쓰지 않는다 — 값이 같은 봉이 둘이면 앞의 것을 돌려준다.
    for idx in range(entry_idx, len(bars)):
        bar = bars[idx]
        if bar["time"] >= TIMEOUT_TIME:
            return {
                "exit_idx": idx, "exit_time": bar["time"],
                "exit_price": bar["close"], "reason": "TIMEOUT",
                "pct": (bar["close"] / entry_price - 1) * 100,
            }
        prices = (
            [bar["high"], bar["low"]] if order == "high_first"
            else [bar["low"], bar["high"]]
        )
        for price in prices:
            step = _step_of(price / entry_price - 1)
            if step > highest_step:
                highest_step = step
            if highest_step >= STEP_SIZE:
                trailing_active = True

            if not trailing_active and price <= entry_price * (1 - HARD_STOP):
                stop = entry_price * (1 - HARD_STOP)
                return {
                    "exit_idx": idx, "exit_time": bar["time"],
                    "exit_price": stop, "reason": "HARD_STOP",
                    "pct": -HARD_STOP * 100,
                }
            if trailing_active:
                stop = entry_price * (1 + highest_step - STEP_TRAIL)
                if price <= stop:
                    return {
                        "exit_idx": idx, "exit_time": bar["time"],
                        "exit_price": stop, "reason": "TRAILING",
                        "pct": (stop / entry_price - 1) * 100,
                    }

    last = bars[-1]
    return {
        "exit_idx": len(bars) - 1, "exit_time": last["time"],
        "exit_price": last["close"], "reason": "DATA_END",
        "pct": (last["close"] / entry_price - 1) * 100,
    }


def resolve_exit(bars: list[dict], entry_idx: int, entry_price: float) -> dict:
    """양쪽 순서로 돌려 답이 갈리면 AMBIGUOUS.

    갈린 날을 한쪽 답으로 적으면 봉 내부를 안다고 주장하는 것이 된다. 판정에서
    빼되 몇 건이 빠졌는지는 항상 보고한다 — 많이 빠지면 결론 자체가 약하다.
    """
    high_first = simulate_exit(bars, entry_idx, entry_price, order="high_first")
    low_first = simulate_exit(bars, entry_idx, entry_price, order="low_first")
    ambiguous = (
        high_first["reason"] != low_first["reason"]
        or high_first["exit_time"] != low_first["exit_time"]
    )
    return {
        "ambiguous": ambiguous,
        "high_first": high_first,
        "low_first": low_first,
        "pct": None if ambiguous else high_first["pct"],
    }
```

- [ ] **Step 4: 테스트 통과를 확인한다**

Run: `python -m pytest tests/test_track_b_rules.py -v`
Expected: PASS (7개)

- [ ] **Step 5: 운영 상수와의 동치를 고정하는 테스트를 추가한다**

```python
# tests/test_track_b_rules.py 에 추가
def test_exit_constants_match_track_a():
    """A와 다른 값이 되면 '진입 시각만 다르다'는 전제가 깨진다."""
    from src.modules import f4_tracking

    assert STEP_SIZE == f4_tracking.STEP_SIZE
    assert STEP_TRAIL == f4_tracking.STEP_TRAIL
    assert HARD_STOP == f4_tracking.HARD_STOP_RATIO
```

Run: `python -m pytest tests/test_track_b_rules.py -v`
Expected: PASS (8개)

- [ ] **Step 6: 커밋**

```bash
git add scripts/track_b_rules.py tests/test_track_b_rules.py
git commit -F <메시지 파일>
```

---

### Task 3: 진입 축 R1·R2·R3

**Files:**
- Modify: `scripts/track_b_rules.py`
- Test: `tests/test_track_b_rules.py`

**Interfaces:**
- Consumes: Task 2의 `scripts/track_b_rules.py`; `src.indicators.sma(bars, period)`, `src.indicators.macd(bars, fast, slow, signal)`
- Produces:
  - `DEFAULT_PARAMS: dict` — `{"sma_period":20,"macd_fast":12,"macd_slow":26,"macd_signal":9,"vol_window":5,"hist_maturity_bars":3,"min_bars_after_gap":2}`
  - `build_context(bars, params) -> dict` — `{"sma","macd","vwap","run_high","first_hist_idx","gap_block"}`
  - `r1_high_reclaim(bars, i, ctx, params) -> bool`
  - `r2_vwap_reclaim(bars, i, ctx, params) -> bool`
  - `r3_indicator(bars, i, ctx, params) -> bool`
  - `RULES: dict[str, callable]` — `{"R1":…, "R2":…, "R3":…}`

- [ ] **Step 1: 진입 축의 실패하는 테스트를 쓴다**

```python
# tests/test_track_b_rules.py 에 추가
from scripts.track_b_rules import (
    DEFAULT_PARAMS,
    RULES,
    build_context,
    r1_high_reclaim,
    r2_vwap_reclaim,
    r3_indicator,
)


def _series(closes: list[float], *, volumes: list[float] | None = None,
            start_min: int = 0) -> list[dict]:
    """09:00부터 1분씩. 고가·저가는 종가에 붙여 단순하게 둔다."""
    bars = []
    for i, c in enumerate(closes):
        minute = start_min + i
        hour, mm = 9 + minute // 60, minute % 60
        bars.append({
            "date": "20260820",
            "time": f"{hour:02d}{mm:02d}00",
            "open": c, "high": c, "low": c, "close": c,
            "volume": (volumes[i] if volumes else 1000.0),
        })
    return bars


def test_r1_fires_only_when_prior_high_is_reclaimed():
    bars = _series([100, 110, 105, 109, 111])
    ctx = build_context(bars, DEFAULT_PARAMS)
    assert r1_high_reclaim(bars, 3, ctx, DEFAULT_PARAMS) is False  # 109 < 110
    assert r1_high_reclaim(bars, 4, ctx, DEFAULT_PARAMS) is True   # 111 > 110


def test_r1_needs_no_parameters():
    """파라미터가 0개라는 것이 R1의 근거다. 값이 늘면 그 근거가 사라진다."""
    bars = _series([100, 110, 111])
    ctx = build_context(bars, DEFAULT_PARAMS)
    assert r1_high_reclaim(bars, 2, ctx, {}) is True


def test_r2_requires_crossing_and_volume_expansion():
    # VWAP(직전) 95.0 아래에서 90으로 닫혔다가 105로 올라선다.
    closes = [100, 98, 96, 94, 92, 90, 105]
    volumes = [1000] * 6 + [5000]
    bars = _series(closes, volumes=volumes)
    ctx = build_context(bars, DEFAULT_PARAMS)
    # 마지막 봉에서 VWAP 위로 올라서고 거래량이 직전 5봉 평균을 넘는다.
    assert r2_vwap_reclaim(bars, 6, ctx, DEFAULT_PARAMS) is True


def test_r2_rejects_crossing_without_volume():
    closes = [100, 98, 96, 94, 92, 90, 105]
    volumes = [1000] * 7
    bars = _series(closes, volumes=volumes)
    ctx = build_context(bars, DEFAULT_PARAMS)
    assert r2_vwap_reclaim(bars, 6, ctx, DEFAULT_PARAMS) is False


def test_r3_waits_for_histogram_maturity():
    """히스토그램이 막 정의된 구간에서는 신호를 내지 않는다.

    전일 시드를 안 쓰므로 34번째 봉에서야 첫 값이 선다. 값 두세 개의 부호로
    판정하는 것이 v0의 결함이었다.
    """
    closes = [100 + (i % 7) - 3 for i in range(60)]
    bars = _series(closes)
    ctx = build_context(bars, DEFAULT_PARAMS)
    first = ctx["first_hist_idx"]
    assert first is not None
    for i in range(first, first + DEFAULT_PARAMS["hist_maturity_bars"]):
        assert r3_indicator(bars, i, ctx, DEFAULT_PARAMS) is False


def test_rules_registry_has_exactly_three_axes():
    """등록은 닫혀 있다. 축을 추가하면 그리드 서치가 된다 (스펙 §4.2)."""
    assert sorted(RULES) == ["R1", "R2", "R3"]


def test_gap_block_suppresses_signals_after_missing_minutes():
    bars = _series([100, 101, 102])
    # 09:02 다음이 09:06 — 3분이 빠졌다.
    bars.append({"date": "20260820", "time": "090600", "open": 120,
                 "high": 120, "low": 120, "close": 120, "volume": 1000.0})
    bars.append({"date": "20260820", "time": "090700", "open": 121,
                 "high": 121, "low": 121, "close": 121, "volume": 1000.0})
    ctx = build_context(bars, DEFAULT_PARAMS)
    assert ctx["gap_block"][3] is True
    assert ctx["gap_block"][4] is True   # min_bars_after_gap=2
    assert ctx["gap_block"][2] is False
```

- [ ] **Step 2: 실패를 확인한다**

Run: `python -m pytest tests/test_track_b_rules.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_context'`

- [ ] **Step 3: 컨텍스트와 세 축을 구현한다**

```python
# scripts/track_b_rules.py 에 추가
DEFAULT_PARAMS = {
    "sma_period": 20,
    "macd_fast": 12,
    "macd_slow": 26,
    "macd_signal": 9,
    "vol_window": 5,
    # 히스토그램이 정의된 뒤 이만큼 지나야 판정한다. v0가 값 2~3개로 부호를
    # 판정하던 것을 막는다.
    "hist_maturity_bars": 3,
    # 봉이 빠진 직후 이만큼은 신호를 내지 않는다 (그림자 스펙 §15.3).
    "min_bars_after_gap": 2,
}


def _minutes(time_: str) -> int:
    return int(time_[:2]) * 60 + int(time_[2:4])


def build_context(bars: list[dict], params: dict) -> dict:
    """하루치 파생 계열을 한 번만 계산한다.

    지표는 반드시 운영 코드(src.indicators)를 쓴다. 여기서 다시 구현하면
    백테스트와 실시간이 다른 값을 보게 된다.
    """
    period = params.get("sma_period", DEFAULT_PARAMS["sma_period"])
    macd_rows = indicators.macd(
        bars,
        params.get("macd_fast", DEFAULT_PARAMS["macd_fast"]),
        params.get("macd_slow", DEFAULT_PARAMS["macd_slow"]),
        params.get("macd_signal", DEFAULT_PARAMS["macd_signal"]),
    )

    run_high: list[float] = []
    vwap: list[float] = []
    cum_pv = 0.0
    cum_v = 0.0
    highest = float("-inf")
    for bar in bars:
        run_high.append(highest)          # 직전 봉까지의 고가
        highest = max(highest, bar["high"])
        cum_pv += bar["close"] * bar["volume"]
        cum_v += bar["volume"]
        vwap.append(cum_pv / cum_v if cum_v > 0 else None)

    first_hist_idx = next(
        (i for i, r in enumerate(macd_rows) if r["hist"] is not None), None
    )

    block_for = params.get("min_bars_after_gap", DEFAULT_PARAMS["min_bars_after_gap"])
    gap_block = [False] * len(bars)
    remaining = 0
    for i, bar in enumerate(bars):
        if i > 0 and _minutes(bar["time"]) - _minutes(bars[i - 1]["time"]) > 1:
            remaining = block_for
        if remaining > 0:
            gap_block[i] = True
            remaining -= 1

    return {
        "sma": indicators.sma(bars, period),
        "macd": macd_rows,
        "vwap": vwap,
        "run_high": run_high,
        "first_hist_idx": first_hist_idx,
        "gap_block": gap_block,
    }


def r1_high_reclaim(bars: list[dict], i: int, ctx: dict, params: dict) -> bool:
    """확정 봉 종가가 그날 직전까지의 고가를 넘는다. 파라미터 0개.

    30분 고가의 49%가 09:00~09:02에 만들어진다. 그 고가를 되찾는 사건은 드물고
    상태 전환이 분명하다 — 설계 §2.3.
    """
    prior_high = ctx["run_high"][i]
    if prior_high == float("-inf"):
        return False
    return bars[i]["close"] > prior_high


def r2_vwap_reclaim(bars: list[dict], i: int, ctx: dict, params: dict) -> bool:
    """VWAP 아래에 있던 종가가 위로 올라서고, 그 봉 거래량이 직전 N봉 평균을 넘는다."""
    window = params.get("vol_window", DEFAULT_PARAMS["vol_window"])
    if i < 1 or i < window:
        return False
    vwap_now, vwap_prev = ctx["vwap"][i], ctx["vwap"][i - 1]
    if vwap_now is None or vwap_prev is None:
        return False
    if not (bars[i - 1]["close"] <= vwap_prev < bars[i]["close"]):
        return False
    prior = [b["volume"] for b in bars[i - window:i]]
    if not prior or sum(prior) <= 0:
        return False
    return bars[i]["volume"] > sum(prior) / len(prior)


def r3_indicator(bars: list[dict], i: int, ctx: dict, params: dict) -> bool:
    """v0 개정판 — 종가 > SMA + 히스토그램 음→양. 단 성숙 대기를 둔다.

    청산에 'SMA 이탈'이 없다는 점이 v0와 다르다. v0는 진입 조건과 청산 조건이
    같은 가격에서 무장해 3~6분 만에 끝났다 (설계 §2.1).
    """
    first = ctx["first_hist_idx"]
    maturity = params.get("hist_maturity_bars", DEFAULT_PARAMS["hist_maturity_bars"])
    if first is None or i < first + maturity or i < 1:
        return False
    sma_now = ctx["sma"][i]
    hist_now, hist_prev = ctx["macd"][i]["hist"], ctx["macd"][i - 1]["hist"]
    if sma_now is None or hist_now is None or hist_prev is None:
        return False
    return bars[i]["close"] > sma_now and hist_prev < 0 <= hist_now


RULES = {
    "R1": r1_high_reclaim,
    "R2": r2_vwap_reclaim,
    "R3": r3_indicator,
}
```

- [ ] **Step 4: 테스트 통과를 확인한다**

Run: `python -m pytest tests/test_track_b_rules.py -v`
Expected: PASS (15개)

- [ ] **Step 5: 커밋**

```bash
git add scripts/track_b_rules.py tests/test_track_b_rules.py
git commit -F <메시지 파일>
```

---

### Task 4: 하루 시뮬레이션 — 종목 선택과 진입

**Files:**
- Create: `scripts/track_b_backtest.py`
- Test: `tests/test_track_b_backtest.py`

**Interfaces:**
- Consumes: `scripts.track_b_rules.{RULES, DEFAULT_PARAMS, build_context, resolve_exit}`; `scripts.strategy_backtest.{load_universes, read_cached_bars, BAR_CACHE_DIR}`; `src.modules.f1_selector.rank_candidates`
- Produces:
  - `SIGNAL_START = "093500"`, `ENTRY_DEADLINE = "140000"`
  - `find_signal(bars_by_ticker, ranked_tickers, rule_key, params) -> dict | None` — `{"ticker","signal_idx","signal_time","rank"}`
  - `simulate_day(date, universe, bars_by_ticker, rule_key, params, *, slippage=0.0, depth=5) -> dict | None`

- [ ] **Step 1: 종목 선택과 진입가의 실패하는 테스트를 쓴다**

```python
# tests/test_track_b_backtest.py
"""트랙 B 규칙 비교 하네스 검증.

선택 규칙이 미래를 보면 백테스트가 실시간에 없는 정보를 쓴다. 진입가가 신호 봉
종가면 봉이 닫히는 순간을 미리 안 것이 된다. 두 가지가 이 파일의 핵심이다.
"""

import pytest

from scripts.track_b_backtest import (
    ENTRY_DEADLINE,
    SIGNAL_START,
    find_signal,
    simulate_day,
)
from scripts.track_b_rules import DEFAULT_PARAMS


def _bars(prices: list[tuple[str, float]]) -> list[dict]:
    return [
        {"date": "20260820", "time": t, "open": p, "high": p, "low": p,
         "close": p, "volume": 1000.0}
        for t, p in prices
    ]


def test_signal_takes_earliest_bar_not_best_rank():
    """랭크 1이 나중에 신호를 내도 기다리지 않는다. 실시간에 불가능하다."""
    bars_by_ticker = {
        "AAA": _bars([("093500", 100), ("093600", 100), ("094000", 100),
                      ("100000", 130)]),   # 랭크 1 — 10:00에 고가 돌파
        "BBB": _bars([("093500", 100), ("093600", 100), ("094000", 130),
                      ("100000", 100)]),   # 랭크 2 — 09:40에 돌파
    }
    signal = find_signal(bars_by_ticker, ["AAA", "BBB"], "R1", DEFAULT_PARAMS)
    assert signal["ticker"] == "BBB"
    assert signal["signal_time"] == "094000"


def test_same_bar_tie_goes_to_higher_rank():
    bars_by_ticker = {
        "AAA": _bars([("093500", 100), ("093600", 100), ("094000", 130)]),
        "BBB": _bars([("093500", 100), ("093600", 100), ("094000", 130)]),
    }
    signal = find_signal(bars_by_ticker, ["AAA", "BBB"], "R1", DEFAULT_PARAMS)
    assert signal["ticker"] == "AAA"
    assert signal["rank"] == 1


def test_signal_ignores_bars_before_0935_and_after_1400():
    early = _bars([("091000", 100), ("091100", 130)])
    late = _bars([("093500", 100), ("140100", 130)])
    assert find_signal({"AAA": early}, ["AAA"], "R1", DEFAULT_PARAMS) is None
    assert find_signal({"AAA": late}, ["AAA"], "R1", DEFAULT_PARAMS) is None


def test_entry_price_is_next_bar_open_not_signal_close():
    """신호 봉 종가에 샀다고 하면 봉이 닫히는 순간을 미리 안 것이 된다."""
    bars = _bars([("093500", 100), ("093600", 130)])
    bars.append({"date": "20260820", "time": "093700", "open": 125,
                 "high": 125, "low": 125, "close": 125, "volume": 1000.0})
    bars.append({"date": "20260820", "time": "151500", "open": 125,
                 "high": 125, "low": 125, "close": 125, "volume": 1000.0})
    universe = [{"ticker": "AAA", "gap_pct": 0.05, "prev_close": 95,
                 "expected_amount": 5_000_000_000,
                 "avg_amount_5d": 1_000_000_000}]
    result = simulate_day("20260820", universe, {"AAA": bars}, "R1",
                          DEFAULT_PARAMS)
    assert result["entry_price"] == 125.0
    assert result["entry_time"] == "093700"


def test_no_entry_when_signal_is_the_last_bar():
    """다음 봉이 없으면 진입가가 없다. 종가로 대체하지 않는다."""
    bars = _bars([("093500", 100), ("093600", 130)])
    universe = [{"ticker": "AAA", "gap_pct": 0.05, "prev_close": 95,
                 "expected_amount": 5_000_000_000,
                 "avg_amount_5d": 1_000_000_000}]
    assert simulate_day("20260820", universe, {"AAA": bars}, "R1",
                        DEFAULT_PARAMS) is None


def test_slippage_raises_entry_price_only():
    bars = _bars([("093500", 100), ("093600", 130)])
    bars.append({"date": "20260820", "time": "093700", "open": 100,
                 "high": 100, "low": 100, "close": 100, "volume": 1000.0})
    bars.append({"date": "20260820", "time": "151500", "open": 100,
                 "high": 100, "low": 100, "close": 100, "volume": 1000.0})
    universe = [{"ticker": "AAA", "gap_pct": 0.05, "prev_close": 95,
                 "expected_amount": 5_000_000_000,
                 "avg_amount_5d": 1_000_000_000}]
    result = simulate_day("20260820", universe, {"AAA": bars}, "R1",
                          DEFAULT_PARAMS, slippage=0.004)
    assert result["entry_price"] == pytest.approx(100.4)
    assert result["pct"] == pytest.approx((100 / 100.4 - 1) * 100)
```

- [ ] **Step 2: 실패를 확인한다**

Run: `python -m pytest tests/test_track_b_backtest.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.track_b_backtest'`

- [ ] **Step 3: 하루 시뮬레이션을 구현한다**

```python
# scripts/track_b_backtest.py
"""트랙 B 진입 규칙 비교 하네스 — 읽기 전용.

사전 등록한 세 축(R1·R2·R3)을 같은 표본에 돌려 스펙 §5의 관문으로 판정한다.
청산은 세 축 모두 트랙 A와 같은 한 벌이라 진입 시각만 비교된다.

strategy_backtest.py 는 09:00~09:30 장벽 모델에 묶여 있어 재사용하지 않는다.
유니버스 로딩과 봉 캐시 I/O만 가져다 쓴다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.strategy_backtest import (  # noqa: E402
    BAR_CACHE_DIR,
    load_universes,
    read_cached_bars,
)
from scripts.track_b_rules import (  # noqa: E402
    DEFAULT_PARAMS,
    RULES,
    build_context,
    resolve_exit,
)
from src.modules import f1_selector  # noqa: E402

SIGNAL_START = "093500"
ENTRY_DEADLINE = "140000"
DEPTH = 5


def find_signal(
    bars_by_ticker: dict[str, list[dict]],
    ranked_tickers: list[str],
    rule_key: str,
    params: dict,
) -> dict | None:
    """봉을 시간 순으로 훑어 첫 신호에서 멈춘다.

    같은 봉에서 둘 이상이 신호를 내면 최상위 랭크를 고른다. 랭크 1이 나중에
    신호를 낼지 기다리는 해석은 실시간에 구현 불가라 쓰지 않는다.
    """
    rule = RULES[rule_key]
    contexts = {
        t: build_context(bars, params)
        for t, bars in bars_by_ticker.items()
        if bars
    }
    times = sorted({
        b["time"]
        for t in ranked_tickers
        for b in bars_by_ticker.get(t, [])
        if SIGNAL_START <= b["time"] <= ENTRY_DEADLINE
    })
    for time_ in times:
        for rank, ticker in enumerate(ranked_tickers, start=1):
            bars = bars_by_ticker.get(ticker)
            ctx = contexts.get(ticker)
            if not bars or ctx is None:
                continue
            idx = next((i for i, b in enumerate(bars) if b["time"] == time_), None)
            if idx is None or ctx["gap_block"][idx]:
                continue
            if rule(bars, idx, ctx, params):
                return {
                    "ticker": ticker, "signal_idx": idx,
                    "signal_time": time_, "rank": rank,
                }
    return None


def simulate_day(
    date: str,
    universe: list[dict],
    bars_by_ticker: dict[str, list[dict]],
    rule_key: str,
    params: dict,
    *,
    slippage: float = 0.0,
    depth: int = DEPTH,
) -> dict | None:
    """하루 한 건. 신호가 없거나 진입가가 없으면 None(미진입)이다."""
    ranked = f1_selector.rank_candidates(universe)[:depth]
    ranked_tickers = [str(r["ticker"]) for r in ranked if r.get("ticker")]
    signal = find_signal(bars_by_ticker, ranked_tickers, rule_key, params)
    if signal is None:
        return None

    bars = bars_by_ticker[signal["ticker"]]
    entry_idx = signal["signal_idx"] + 1
    if entry_idx >= len(bars):
        return None
    entry_price = bars[entry_idx]["open"] * (1 + slippage)
    if entry_price <= 0:
        return None

    exit_ = resolve_exit(bars, entry_idx, entry_price)
    return {
        "date": date,
        "ticker": signal["ticker"],
        "rank": signal["rank"],
        "signal_time": signal["signal_time"],
        "entry_time": bars[entry_idx]["time"],
        "entry_price": entry_price,
        "ambiguous": exit_["ambiguous"],
        "reason": exit_["high_first"]["reason"],
        "exit_time": exit_["high_first"]["exit_time"],
        "pct": exit_["pct"],
    }
```

- [ ] **Step 4: 테스트 통과를 확인한다**

Run: `python -m pytest tests/test_track_b_backtest.py -v`
Expected: PASS (6개)

- [ ] **Step 5: 커밋**

```bash
git add scripts/track_b_backtest.py tests/test_track_b_backtest.py
git commit -F <메시지 파일>
```

---

### Task 5: 관문 계산과 리포트 CLI

**Files:**
- Modify: `scripts/track_b_backtest.py`
- Test: `tests/test_track_b_backtest.py`

**Interfaces:**
- Consumes: Task 4의 `simulate_day`
- Produces:
  - `bootstrap_ci(values, *, seed=20260828, resamples=10000, alpha=0.05) -> tuple[float, float]`
  - `correlation(xs, ys) -> float | None`
  - `run_axis(universes, bars, rule_key, params, *, slippage=0.0) -> list[dict]`
  - `sign_stability(universes, bars, rule_key, params) -> list[int]`
  - `load_bars_for(universes, *, depth=5, cache_dir) -> tuple[dict[str, dict[str, list[dict]]], dict[str, int]]`
  - `a_daily_from_baseline() -> dict[str, float | None]`
  - `gate_report(axis_results, a_daily) -> dict`
  - `main(argv=None) -> int`

- [ ] **Step 1: 관문 계산의 실패하는 테스트를 쓴다**

```python
# tests/test_track_b_backtest.py 에 추가
from scripts.track_b_backtest import bootstrap_ci, correlation, gate_report


def test_bootstrap_ci_is_deterministic_and_brackets_the_mean():
    values = [1.0, -2.0, 3.0, -1.0, 2.0]
    low, high = bootstrap_ci(values)
    assert low < sum(values) / len(values) < high
    assert (low, high) == bootstrap_ci(values)   # 시드 고정


def test_bootstrap_ci_of_empty_is_none():
    assert bootstrap_ci([]) == (None, None)


def test_correlation_of_identical_series_is_one():
    assert correlation([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0)


def test_correlation_needs_two_points():
    assert correlation([1.0], [1.0]) is None


def test_gate1_rejects_axis_with_fewer_than_three_entry_days():
    axis = {
        "R1": {
            "rows": [{"date": "20260820", "pct": 1.0, "ambiguous": False}] * 2,
            "slippage_signs": [1, 1, 1],
        }
    }
    report = gate_report(axis, a_daily={})
    assert report["R1"]["gate1_pass"] is False
    assert "진입일" in report["R1"]["gate1_reason"]


def test_gate2_rejects_axis_whose_sign_flips_under_slippage():
    axis = {
        "R1": {
            "rows": [{"date": f"2026082{i}", "pct": 1.0, "ambiguous": False}
                     for i in range(4)],
            "slippage_signs": [1, 1, -1],
        }
    }
    report = gate_report(axis, a_daily={})
    assert report["R1"]["gate2_pass"] is False


def test_ambiguous_days_are_excluded_but_counted():
    axis = {
        "R1": {
            "rows": [
                {"date": "20260818", "pct": 1.0, "ambiguous": False},
                {"date": "20260819", "pct": None, "ambiguous": True},
                {"date": "20260820", "pct": 2.0, "ambiguous": False},
                {"date": "20260821", "pct": 3.0, "ambiguous": False},
            ],
            "slippage_signs": [1, 1, 1],
        }
    }
    report = gate_report(axis, a_daily={})
    assert report["R1"]["ambiguous_days"] == 1
    assert report["R1"]["judged_days"] == 3
    assert report["R1"]["total_pct"] == pytest.approx(6.0)
```

- [ ] **Step 2: 실패를 확인한다**

Run: `python -m pytest tests/test_track_b_backtest.py -v`
Expected: FAIL — `ImportError: cannot import name 'bootstrap_ci'`

- [ ] **Step 3: 관문 계산을 구현한다**

```python
# scripts/track_b_backtest.py 에 추가
import random
import statistics

# 스펙 §5.2 — 체결 가정을 셋 얹어 부호가 유지되는지만 본다. 최댓값을 고르지 않는다.
SLIPPAGES = (0.000, 0.002, 0.004)
MIN_ENTRY_DAYS = 3


def bootstrap_ci(
    values: list[float],
    *,
    seed: int = 20260828,
    resamples: int = 10000,
    alpha: float = 0.05,
) -> tuple[float | None, float | None]:
    """일별 손익 평균의 백분위 부트스트랩 CI. 시드를 고정해 재현 가능하게 둔다."""
    if not values:
        return (None, None)
    rng = random.Random(seed)
    n = len(values)
    means = sorted(
        sum(rng.choice(values) for _ in range(n)) / n for _ in range(resamples)
    )
    lo = means[int(resamples * alpha / 2)]
    hi = means[min(resamples - 1, int(resamples * (1 - alpha / 2)))]
    return (lo, hi)


def correlation(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    try:
        return statistics.correlation(xs, ys)
    except statistics.StatisticsError:
        return None


def run_axis(
    universes: dict[str, list[dict]],
    bars: dict[str, dict[str, list[dict]]],
    rule_key: str,
    params: dict,
    *,
    slippage: float = 0.0,
) -> list[dict]:
    rows = []
    for date in sorted(universes):
        result = simulate_day(
            date, universes[date], bars.get(date, {}), rule_key, params,
            slippage=slippage,
        )
        if result is not None:
            rows.append(result)
    return rows


def sign_stability(
    universes: dict[str, list[dict]],
    bars: dict[str, dict[str, list[dict]]],
    rule_key: str,
    params: dict,
) -> list[int]:
    """체결 가정 셋에서의 합계 부호. 하나라도 다르면 관문 2 탈락이다."""
    signs = []
    for slip in SLIPPAGES:
        rows = run_axis(universes, bars, rule_key, params, slippage=slip)
        total = sum(r["pct"] for r in rows if r["pct"] is not None)
        signs.append(0 if total == 0 else (1 if total > 0 else -1))
    return signs


def gate_report(axis_results: dict[str, dict], a_daily: dict[str, float]) -> dict:
    """스펙 §5의 관문을 그대로 적용한다. 통과·탈락과 사유를 함께 남긴다."""
    report = {}
    for key, data in axis_results.items():
        rows = data["rows"]
        judged = [r for r in rows if not r["ambiguous"] and r["pct"] is not None]
        pcts = [r["pct"] for r in judged]
        entry_days = len(rows)

        gate1_pass = entry_days >= MIN_ENTRY_DAYS
        gate1_reason = (
            "" if gate1_pass
            else f"진입일 {entry_days}일 < {MIN_ENTRY_DAYS}일 — 판정 불가"
        )

        signs = [s for s in data["slippage_signs"] if s != 0]
        gate2_pass = len(set(signs)) <= 1
        gate2_reason = (
            "" if gate2_pass
            else f"체결 가정에 따라 부호가 뒤집힌다: {data['slippage_signs']}"
        )

        paired_b, paired_a = [], []
        for row in judged:
            a_pct = a_daily.get(row["date"])
            if a_pct is not None:      # None 을 상관계수에 넣으면 TypeError 다
                paired_b.append(row["pct"])
                paired_a.append(a_pct)
        # A가 미진입이거나 AMBIGUOUS라 판정 못 한 날. 둘을 섞어 세므로 문서에
        # 적을 때 "A의 손익이 없는 날"이라고 쓴다.
        a_missing_days = [d for d, v in a_daily.items() if v is None]
        covered = sum(1 for r in rows if r["date"] in a_missing_days)

        lo, hi = bootstrap_ci(pcts)
        report[key] = {
            "entry_days": entry_days,
            "judged_days": len(judged),
            "ambiguous_days": entry_days - len(judged),
            "total_pct": sum(pcts),
            "win_rate": (sum(1 for p in pcts if p > 0) / len(pcts)) if pcts else None,
            "ci_low": lo,
            "ci_high": hi,
            "ci_includes_zero": (lo is not None and lo <= 0 <= hi),
            "corr_with_a": correlation(paired_b, paired_a),
            "a_missing_day_coverage": covered,
            "gate1_pass": gate1_pass,
            "gate1_reason": gate1_reason,
            "gate2_pass": gate2_pass,
            "gate2_reason": gate2_reason,
        }
    return report
```

- [ ] **Step 4: 테스트 통과를 확인한다**

Run: `python -m pytest tests/test_track_b_backtest.py -v`
Expected: PASS (13개)

- [ ] **Step 5: CLI를 붙인다**

```python
# scripts/track_b_backtest.py 에 추가
def load_bars_for(
    universes: dict[str, list[dict]], *, depth: int = DEPTH,
    cache_dir: Path = BAR_CACHE_DIR,
) -> tuple[dict[str, dict[str, list[dict]]], dict[str, int]]:
    """캐시에서만 읽는다. 없는 쌍은 세어서 보고한다 — 조용히 빠지면 안 된다."""
    bars: dict[str, dict[str, list[dict]]] = {}
    stats = {"pairs": 0, "missing": 0, "partial": 0}
    for date, rows in universes.items():
        ranked = f1_selector.rank_candidates(rows)[:depth]
        day: dict[str, list[dict]] = {}
        for row in ranked:
            ticker = str(row.get("ticker") or "")
            if not ticker:
                continue
            stats["pairs"] += 1
            cached = read_cached_bars(date, ticker, cache_dir)
            if not cached:
                stats["missing"] += 1
                continue
            if max(b["time"] for b in cached) < "140000":
                stats["partial"] += 1
                continue
            day[ticker] = cached
        if day:
            bars[date] = day
    return bars, stats


def a_daily_from_baseline() -> dict[str, float | None]:
    """트랙 A의 일별 손익 — 기존 하네스의 현행 정책(BASELINE)으로 낸다.

    실계좌 체결이 아니라 같은 봉 위의 시뮬레이션이라 B와 대칭이다. 미진입일은
    None으로 두고 커버리지 계산에 쓴다.
    """
    from scripts.strategy_backtest import (
        BASELINE, load_bar_cache, load_universes as _lu,
        simulate_day as a_simulate_day, tickers_needed,
    )

    universes = _lu()
    bars = load_bar_cache(tickers_needed(universes, [BASELINE]))
    daily: dict[str, float | None] = {}
    for date in sorted(universes):
        # 인자 순서에 주의한다 — (date, universe, policy, bars) 다.
        row = a_simulate_day(date, universes[date], BASELINE, bars.get(date, {}))
        daily[date] = row.get("realized_pct") if row.get("entered") else None
    return daily


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="트랙 B 진입 규칙 비교")
    parser.add_argument("--depth", type=int, default=DEPTH)
    parser.add_argument("--out", default="", help="결과 JSON 경로")
    args = parser.parse_args(argv)

    universes = load_universes()
    bars, stats = load_bars_for(universes, depth=args.depth)
    print(f"표본: {len(bars)}거래일 / 쌍 {stats['pairs']} "
          f"(없음 {stats['missing']}, 09:00~09:30만 {stats['partial']})")
    if stats["missing"] + stats["partial"] > 0:
        print("  ! 전 세션 봉이 없는 쌍이 있다. track_b_backfill.py 를 먼저 돌린다.")

    axis_results = {}
    for key in sorted(RULES):
        rows = run_axis(universes, bars, key, DEFAULT_PARAMS)
        axis_results[key] = {
            "rows": rows,
            "slippage_signs": sign_stability(universes, bars, key, DEFAULT_PARAMS),
        }

    report = gate_report(axis_results, a_daily_from_baseline())
    for key in sorted(report):
        r = report[key]
        print(f"\n[{key}] 진입 {r['entry_days']}일 / 판정 {r['judged_days']}일 "
              f"(AMBIGUOUS {r['ambiguous_days']})")
        print(f"  합계 {r['total_pct']:+.2f}%  승률 "
              f"{'-' if r['win_rate'] is None else format(r['win_rate'] * 100, '.1f')}%")
        print(f"  일평균 95% CI [{r['ci_low']}, {r['ci_high']}] "
              f"{'— 0을 포함한다 (차이 없음)' if r['ci_includes_zero'] else ''}")
        print(f"  A와의 상관 {r['corr_with_a']}  A 미진입일 커버 "
              f"{r['a_missing_day_coverage']}일")
        print(f"  관문1 {'통과' if r['gate1_pass'] else '탈락 — ' + r['gate1_reason']}")
        print(f"  관문2 {'통과' if r['gate2_pass'] else '탈락 — ' + r['gate2_reason']}")

    if args.out:
        Path(args.out).write_text(
            json.dumps({"report": report, "axes": axis_results},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n결과: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 6: 백필 전 상태로 CLI가 도는지 확인한다 (KIS 호출 없음)**

Run: `python scripts/track_b_backtest.py`
Expected: 표본 줄에 `09:00~09:30만 <큰 수>`가 찍히고 세 축 모두 진입 0~소수일. **이 단계에서 결론을 읽지 않는다.** 전 세션 봉이 없으므로 09:35 이후가 비어 있는 것이 정상이다.

- [ ] **Step 7: 전체 테스트 스위트가 깨지지 않았는지 확인한다**

Run: `python -m pytest -q`
Expected: 기존 테스트 전부 통과. 특히 `tests/test_strategy_backtest.py`가 통과해야 한다 — A의 하네스를 건드리지 않았다는 증거다.

- [ ] **Step 8: 커밋**

```bash
git add scripts/track_b_backtest.py tests/test_track_b_backtest.py
git commit -F <메시지 파일>
```

---

### Task 6: 백필 실행 (15:40 이후, 사람 승인 필요)

**Files:**
- Modify: `data/backtest_bars/*.json` (데이터, 커밋하지 않는다 — `.gitignore` 확인)

**Interfaces:**
- Consumes: Task 1의 `scripts/track_b_backfill.py`
- Produces: 전 세션 봉이 채워진 캐시

- [ ] **Step 1: 대상 규모를 먼저 확인한다**

Run: `python scripts/track_b_backfill.py --dry-run`
Expected: 거래일 수와 쌍 수. 쌍 × 13 ≈ 총 호출 수. 1,600을 넘으면 `--max-calls`를 올리지 말고 `--depth`를 줄여 사람에게 묻는다.

- [ ] **Step 2: 15:40이 지났는지, PAPER인지 확인한다**

```bash
python -c "from datetime import datetime; from zoneinfo import ZoneInfo; print(datetime.now(ZoneInfo('Asia/Seoul')))"
python -c "import os; from dotenv import load_dotenv; load_dotenv(); print(os.getenv('KIS_MODE'))"
```

Expected: 15:40 이후, `PAPER`. 아니면 **실행하지 않는다.**

- [ ] **Step 3: 사람에게 실행 승인을 받는다**

KIS를 약 1,300회 호출한다. 승인 없이 실행하지 않는다.

- [ ] **Step 4: 백필을 실행한다**

Run: `python scripts/track_b_backfill.py --depth 5`
Expected: `{date} {ticker}: 31 -> 380봉` 형태가 이어지고 마지막에 `{'skipped':…,'filled':…}`. `EGW00201`로 중단되면 다시 실행한다 — 캐시가 남아 이어진다.

- [ ] **Step 5: 채워진 표본을 검증한다**

```bash
python - <<'PY'
import json, glob, statistics
lens = []
for f in glob.glob('data/backtest_bars/*.json'):
    bars = json.load(open(f))
    lens.append(len(bars))
full = [n for n in lens if n >= 300]
print(f'파일 {len(lens)} / 전 세션 {len(full)} / 중앙 봉수 {statistics.median(lens)}')
PY
```

Expected: 전 세션 파일이 100개 이상. 크게 못 미치면 원인을 찾고 멈춘다 — 표본이 반쪽이면 뒤의 판정이 전부 무의미하다.

---

### Task 7: 비교 실행과 결과 문서

**Files:**
- Create: `docs/TRACK_B_RULE_SELECTION_20260828.md` (실행일에 맞춰 이름 조정)

**Interfaces:**
- Consumes: Task 5의 CLI, Task 6의 표본
- Produces: 관문별 판정이 적힌 문서

- [ ] **Step 1: 비교를 실행하고 결과를 저장한다**

Run: `python scripts/track_b_backtest.py --out data/track_b_selection.json`

- [ ] **Step 2: 결과 문서를 쓴다**

문서에 반드시 포함할 것:

- 표본 규모 — 거래일, 쌍, AMBIGUOUS로 빠진 날 수
- 축별 관문 1~4 판정과 사유. **탈락도 그대로 적는다**
- 합계·승률은 **CI와 함께만**. CI가 0을 포함하면 "차이 없음"이라고 적는다
- A와의 상관, A 미진입일 커버리지
- 랭크 4~5가 기여한 진입 건수 (스펙 §9 미결 사항)
- `min_bars_after_gap`·`hist_maturity_bars`·`vol_window`가 결과를 바꾸는지 — **바꾼다면 그 사실을 기록하고 값을 고르지 않는다** (스펙 §5.6)

문서에 쓰지 말 것: 관문을 통과하지 못한 축을 "그래도 유망하다"고 적는 것, 점추정으로 순위를 매기는 것.

- [ ] **Step 3: 확정 규칙을 결정한다**

관문을 통과한 축이 하나면 그것. 둘 이상이면 관문 3(A와의 상관이 낮고 미진입일 커버리지가 큰 쪽). **하나도 없으면 스펙 §5.5대로 R1을 채택하고, 그 사실을 문서에 적는다.**

- [ ] **Step 4: 커밋**

```bash
git add docs/TRACK_B_RULE_SELECTION_20260828.md
git commit -F <메시지 파일>
```

---

### Task 8: 확정 규칙을 스펙과 설정에 반영

**Files:**
- Modify: `docs/superpowers/specs/2026-08-27-track-b-shadow-design.md` (§8, §16)
- Modify: `docs/superpowers/specs/2026-08-25-multi-track-strategy-design.md` (§2)
- Modify: `docs/superpowers/specs/2026-08-28-track-b-rule-selection-design.md` (§4.1 선택 규칙 확정)
- Create: `scripts/register_track_b_config.py`
- Test: `tests/test_register_track_b_config.py`

**Interfaces:**
- Consumes: `src.db.upsert_strategy_config(config: dict, *, kind: str, code_fingerprint: str | None = None, parent_config_id: str | None = None) -> str`
- Produces: `build_config(rule_key: str, params: dict) -> dict`

- [ ] **Step 1: config 조립의 실패하는 테스트를 쓴다**

```python
# tests/test_register_track_b_config.py
"""확정 규칙의 설정 등록 검증.

config 는 해시로 고정된다. 값이 하나 달라지면 새 config_id 가 나와야 하고,
같으면 재사용돼야 한다 — 그래야 그림자 기록을 나중에 규칙별로 갈라 읽는다.
"""

from scripts.register_track_b_config import build_config


def test_config_carries_rule_key_and_exit_constants():
    config = build_config("R1", {"min_bars_after_gap": 2})
    assert config["b_rule"] == "R1"
    assert config["b_signal_start"] == "09:35"
    assert config["b_entry_deadline"] == "14:00"
    # 청산은 A와 같은 한 벌이다. 설계 §4.1.
    assert config["b_step_size"] == 0.025
    assert config["b_step_trail"] == 0.020
    assert config["b_hard_stop_ratio"] == 0.020


def test_config_is_stable_for_same_inputs():
    assert build_config("R1", {"min_bars_after_gap": 2}) == build_config(
        "R1", {"min_bars_after_gap": 2}
    )


def test_unknown_rule_is_rejected():
    import pytest

    with pytest.raises(ValueError):
        build_config("R9", {})
```

- [ ] **Step 2: 실패를 확인한다**

Run: `python -m pytest tests/test_register_track_b_config.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 등록 스크립트를 구현한다**

```python
# scripts/register_track_b_config.py
"""확정된 트랙 B 규칙을 strategy_configs 에 EXPLORATORY 로 등록한다.

값이 하나라도 다르면 새 해시 → 새 config_id 다. 기존 결과를 덮지 않으므로
규칙을 바꿔도 과거 그림자 기록의 해석이 살아 있다.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.track_b_rules import (  # noqa: E402
    DEFAULT_PARAMS, HARD_STOP, RULES, STEP_SIZE, STEP_TRAIL,
)


def build_config(rule_key: str, params: dict) -> dict:
    if rule_key not in RULES:
        raise ValueError(f"unknown rule: {rule_key}")
    merged = {**DEFAULT_PARAMS, **params}
    return {
        "b_rule": rule_key,
        "b_signal_start": "09:35",
        "b_entry_deadline": "14:00",
        "b_sma_period": merged["sma_period"],
        "b_macd_fast": merged["macd_fast"],
        "b_macd_slow": merged["macd_slow"],
        "b_macd_signal": merged["macd_signal"],
        "b_vol_window": merged["vol_window"],
        "b_hist_maturity_bars": merged["hist_maturity_bars"],
        "b_min_bars_after_gap": merged["min_bars_after_gap"],
        "b_step_size": STEP_SIZE,
        "b_step_trail": STEP_TRAIL,
        "b_hard_stop_ratio": HARD_STOP,
    }


async def main_async(rule_key: str) -> int:
    from src import db

    await db.connect()
    config_id = await db.upsert_strategy_config(
        build_config(rule_key, {}), kind="EXPLORATORY"
    )
    print(f"config_id = {config_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async(sys.argv[1])))
```

- [ ] **Step 4: 테스트 통과를 확인한다**

Run: `python -m pytest tests/test_register_track_b_config.py -v`
Expected: PASS (3개)

- [ ] **Step 5: 확정 규칙을 등록한다**

Run: `python scripts/register_track_b_config.py <확정된 축>`
Expected: `config_id = cfg-…` 출력. 이 값을 Task 7의 결과 문서에 적는다.

- [ ] **Step 6: 그림자 스펙 §8을 확정 규칙으로 고쳐 쓴다**

`docs/superpowers/specs/2026-08-27-track-b-shadow-design.md`에서:

- §8.1 진입 규칙을 확정 축의 조건으로 교체한다
- §8.1 청산 표에서 `INDICATOR_STOP`(종가 < SMA)을 **뺀다.** 진입과 청산이 같은 가격에서 무장하던 결함이다. 청산은 `HARD_STOP` / `TRAILING` / `TIMEOUT` 세 가지가 된다
- §8.2 파라미터를 `build_config`가 내는 키로 교체하고 `config_id`를 적는다
- §16의 "v0 규칙의 숫자" 항목을 해소로 표시하고 근거 문서를 링크한다

- [ ] **Step 7: 모스펙 §2에 종목 이탈을 기록한다**

`docs/superpowers/specs/2026-08-25-multi-track-strategy-design.md` §2의 "종목" 행을 고친다 — "두 트랙이 같은 종목" → "A는 F3 확정 종목, B는 09:35에 F1 랭크 1~5 중 직접 선택". 사유와 대가(호가 스프레드 기록 불가, 후보 봉은 REST로)를 함께 적고 이 계획의 설계 문서 §6을 링크한다.

- [ ] **Step 8: 선택 규칙 확정을 이 계획의 설계 문서에 반영한다**

`docs/superpowers/specs/2026-08-28-track-b-rule-selection-design.md` §4.1에 "봉을 시간 순으로 훑어 첫 신호에서 멈춘다. 같은 봉에서 둘 이상이면 최상위 랭크" 를 명시한다 (이 계획 앞머리의 확정).

- [ ] **Step 9: 전체 테스트를 돌린다**

Run: `python -m pytest -q`
Expected: 전부 통과.

- [ ] **Step 10: 커밋**

```bash
git add scripts/register_track_b_config.py tests/test_register_track_b_config.py docs/superpowers/specs/
git commit -F <메시지 파일>
```

---

## 이 계획이 끝나면

트랙 B의 진입 규칙 하나가 근거와 함께 확정되고, `strategy_configs`에 `config_id`로 박히고, 그림자 스펙 §8이 그 규칙으로 갱신된다. 그림자 스펙 §17.2의 2단계 착수 조건이 해소된다.

**이 계획에 없는 것**: 신호 엔진 구현, `shadow_trades` 기록, 실시간 종목 선택 경로(설계 §6.1의 09:35 REST 조회), 자본 분배. 전부 그림자 스펙 2단계다.
