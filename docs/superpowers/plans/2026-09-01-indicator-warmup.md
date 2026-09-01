# 지표 워밍업 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 전 거래일 분봉을 지표 입력 앞에 붙여 09:00부터 증권사 차트와 같은 SMA·EMA·MACD 값을 내되, 세션 누적값과 기존 표본의 재현성은 그대로 지킨다.

**Architecture:** `src/indicators.py`는 봉 리스트를 받는 순수 함수이므로 **한 줄도 고치지 않는다.** 새 순수 모듈 `src/warmup.py`가 "앞에 붙이고 잘라내는" 계약만 담고, 실제 전일 봉을 읽는 일은 데이터가 있는 곳(백테스트는 `data/backtest_bars/`, 실시간은 분봉 API)이 맡는다. 워밍업 여부는 `warmed` 플래그로 항상 표시한다.

**Tech Stack:** Python 3.12, pytest, 기존 KIS REST 클라이언트(`src/api/kis_minute_bars.py`), FastAPI(`src/api/server.py`)

**Spec:** [docs/superpowers/specs/2026-09-01-indicator-warmup-design.md](../specs/2026-09-01-indicator-warmup-design.md)

> **정정 (2026-09-01, 실행 중 발견).** 이 계획서 곳곳의 `WARMUP_MIN_BARS = 391`과
> "`warmed`는 `warmup_bars >= 391`일 때 참"은 틀렸다. 15:20~15:30은 단일가라 한 세션은
> 381봉이고, 391은 어떤 날도 넘지 못해 워밍업이 아예 적용되지 않았다. 완결성은 개수가
> 아니라 개장~마감 도달과 중간 공백으로 판정한다(`warmup.covers_session`), 수렴 하한은
> 228봉이다. 계획서 본문은 그때의 지시로 남긴다 — 실제 코드는 `src/warmup.py`와
> `docs/INDICATOR_WARMUP_VERIFY_20260901.md`를 본다.

## Global Constraints

- **`src/indicators.py`는 수정 금지.** 이 계획의 어떤 태스크도 이 파일을 건드리지 않는다
- **기본 워밍업 = 1거래일**, `--warmup-days 0`이면 구 동작과 **완전히 동일**해야 한다
- **`warmed`는 `warmup_bars >= 391`일 때만 참** (`src/warmup.py`의 `WARMUP_MIN_BARS`)
- 워밍업은 **SMA·EMA·MACD에만** 적용한다. VWAP·`run_high`·`gap_block`은 당일 봉만으로 계산한다 (Task 2 참고)
- 백필 가드는 그대로다 — PAPER 고정 · 15:40 이후 · GET만 · `--max-calls` 상한
- 커밋 메시지는 저장소 관례대로 영어, 끝에 `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`
- 전체 스위트(`./.venv/Scripts/python.exe -m pytest -q`)가 매 태스크 끝에서 통과해야 한다

---

### Task 1: `src/warmup.py` — 순수 결합 계약

전일 봉을 앞에 붙이고 당일 시작 인덱스를 돌려주는 순수 함수. I/O 없음.

**Files:**
- Create: `src/warmup.py`
- Test: `tests/test_warmup.py`

**Interfaces:**
- Consumes: 없음
- Produces:
  - `WARMUP_MIN_BARS: int = 391`
  - `combine(warm: list[dict], day: list[dict]) -> tuple[list[dict], int]`
  - `meta(warm: list[dict], days: int) -> dict` → `{"warmup_days": int, "warmup_bars": int, "warmed": bool}`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
# tests/test_warmup.py
from src import warmup


def _bars(n, start=0):
    return [{"time": f"{9 + i // 60:02d}{i % 60:02d}00", "close": float(start + i)}
            for i in range(n)]


def test_combine_prepends_and_reports_offset():
    warm, day = _bars(3), _bars(2, start=100)

    merged, offset = warmup.combine(warm, day)

    assert offset == 3
    assert merged[offset:] == day
    assert merged[:offset] == warm


def test_combine_with_no_warmup_is_the_day_itself():
    day = _bars(2)

    merged, offset = warmup.combine([], day)

    assert offset == 0
    assert merged == day


def test_combine_does_not_alias_the_inputs():
    """호출부가 반환값을 고쳐도 원본 리스트가 바뀌면 안 된다."""
    warm, day = _bars(1), _bars(1)

    merged, _ = warmup.combine(warm, day)
    merged.append({"time": "150000"})

    assert len(warm) == 1
    assert len(day) == 1


def test_meta_is_not_warmed_below_the_minimum():
    assert warmup.meta(_bars(390), days=1) == {
        "warmup_days": 1, "warmup_bars": 390, "warmed": False,
    }


def test_meta_is_warmed_at_the_minimum():
    assert warmup.meta(_bars(warmup.WARMUP_MIN_BARS), days=1)["warmed"] is True


def test_meta_reports_zero_days_when_no_bars_were_prepended():
    """요청은 1일이었어도 실제로 붙은 봉이 없으면 0일로 정직하게 남긴다."""
    assert warmup.meta([], days=1) == {
        "warmup_days": 0, "warmup_bars": 0, "warmed": False,
    }
```

- [ ] **Step 2: 실패를 확인한다**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_warmup.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.warmup'`

- [ ] **Step 3: 최소 구현을 쓴다**

```python
# src/warmup.py
"""지표 워밍업 — 전 거래일 봉을 지표 입력 앞에 붙이는 순수 계약.

봉을 읽어오는 일은 여기 없다. 백테스트는 캐시 파일에서, 실시간은 분봉
API에서 각자 가져와 이 함수에 넘긴다. 여기 I/O를 두면 테스트가 파일과
네트워크에 묶인다.

설계: docs/superpowers/specs/2026-09-01-indicator-warmup-design.md
"""

from __future__ import annotations

# 전 거래일 한 세션(09:00~15:30). EMA26 평활계수 2/27로 391봉이면 시드의
# 잔존 영향이 8.5e-14라 표시 정밀도 어디에서도 증권사와 갈라지지 않는다.
# 이보다 적게 붙으면 데운 셈 치지 않는다 (스펙 §4).
WARMUP_MIN_BARS = 391


def combine(warm: list[dict], day: list[dict]) -> tuple[list[dict], int]:
    """워밍업 봉을 앞에 붙이고 당일 첫 봉의 인덱스를 함께 돌려준다.

    새 리스트를 만든다 — 호출부가 결과를 고쳐도 원본이 바뀌면 안 된다.
    """
    merged = list(warm) + list(day)
    return merged, len(warm)


def meta(warm: list[dict], days: int) -> dict:
    """워밍업 상태. 실제로 붙은 봉이 없으면 요청 일수와 무관하게 0일이다."""
    count = len(warm)
    return {
        "warmup_days": days if count else 0,
        "warmup_bars": count,
        "warmed": count >= WARMUP_MIN_BARS,
    }
```

- [ ] **Step 4: 통과를 확인한다**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_warmup.py -q`
Expected: PASS (6 passed)

- [ ] **Step 5: 커밋**

```bash
git add src/warmup.py tests/test_warmup.py
git commit -m "feat(warmup): add the pure prepend-and-offset contract

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `build_context`에 워밍업을 먹인다 — 세션 누적값은 제외

**이 계획에서 가장 틀리기 쉬운 태스크다.** `build_context`는 지표 말고도 세 가지를 계산하는데, 그것들은 워밍업을 먹으면 **안 된다.**

| 값 | 워밍업 적용 | 이유 |
|---|---|---|
| `sma` · `macd` | **적용** | 이 계획의 목적 |
| `vwap` | **제외** | 정의상 세션 누적이다. 전일을 섞으면 R2의 기준선이 어긋난다 |
| `run_high` | **제외** | "그날 직전까지의 고가"다. 전일 고가를 섞으면 R1이 거의 발화하지 않는다 |
| `gap_block` | **제외** | 15:30→09:00 경계가 거대한 봉 간격으로 잡혀 당일 첫 봉들이 차단된다 |

`first_hist_idx`는 워밍업이 있으면 0이 된다 — 당일 첫 봉부터 히스토그램이 정의되기 때문이다. 이는 의도된 결과이며 `hist_maturity_bars` 가드를 사실상 무력화한다. R3의 발화 시점이 빨라진다.

**Files:**
- Modify: `scripts/track_b_rules.py:126-175` (`build_context`)
- Test: `tests/test_track_b_rules.py`

**Interfaces:**
- Consumes: `src.warmup.combine` (Task 1)
- Produces: `build_context(bars: list[dict], params: dict, warmup: list[dict] | None = None) -> dict` — 반환 dict의 모든 배열은 **여전히 `bars`와 같은 길이**다. 호출부(`find_signal`, 규칙 함수 `r1_high_reclaim`/`r2_vwap_reclaim`/`r3_indicator`)는 무변경이다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_track_b_rules.py` 끝에 추가한다. 이 파일은 이미 `from scripts import track_b_rules`를 import하고 있다.

```python
def _seq_bars(closes, start_hhmm=900, volume=100.0):
    """분 단위로 이어지는 봉. close만 의미가 있다."""
    out = []
    hh, mm = divmod(start_hhmm, 100)
    for c in closes:
        out.append({
            "time": f"{hh:02d}{mm:02d}00",
            "open": float(c), "high": float(c), "low": float(c), "close": float(c),
            "volume": volume,
        })
        mm += 1
        if mm == 60:
            mm = 0
            hh += 1
    return out


def test_warmup_defines_macd_from_the_first_bar_of_the_day():
    """워밍업이 없으면 당일 초반 MACD는 None이다. 붙이면 첫 봉부터 값이 선다."""
    warm = _seq_bars([1000 + i for i in range(60)])
    day = _seq_bars([1060 + i for i in range(5)], start_hhmm=1000)

    cold = track_b_rules.build_context(day, track_b_rules.DEFAULT_PARAMS)
    hot = track_b_rules.build_context(day, track_b_rules.DEFAULT_PARAMS, warmup=warm)

    assert cold["macd"][0]["macd"] is None
    assert hot["macd"][0]["macd"] is not None
    assert len(hot["macd"]) == len(day)
    assert len(hot["sma"]) == len(day)


def test_warmup_does_not_leak_into_session_accumulators():
    """VWAP·당일 고가·봉 간격은 세션 값이다 — 워밍업이 섞이면 안 된다."""
    warm = _seq_bars([9999.0] * 60)          # 당일보다 훨씬 높은 전일 고가
    day = _seq_bars([100.0, 110.0, 120.0], start_hhmm=1000)

    cold = track_b_rules.build_context(day, track_b_rules.DEFAULT_PARAMS)
    hot = track_b_rules.build_context(day, track_b_rules.DEFAULT_PARAMS, warmup=warm)

    assert hot["vwap"] == cold["vwap"]
    assert hot["run_high"] == cold["run_high"]
    assert hot["gap_block"] == cold["gap_block"]


def test_warmup_none_reproduces_the_old_context_exactly():
    """--warmup-days 0 의 회귀선. 기존 문서의 숫자가 재현 가능해야 한다."""
    day = _seq_bars([100 + i for i in range(40)])

    assert (track_b_rules.build_context(day, track_b_rules.DEFAULT_PARAMS)
            == track_b_rules.build_context(day, track_b_rules.DEFAULT_PARAMS,
                                           warmup=[]))
```

- [ ] **Step 2: 실패를 확인한다**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_track_b_rules.py -q -k warmup`
Expected: FAIL — `build_context() got an unexpected keyword argument 'warmup'`

- [ ] **Step 3: 구현한다**

`scripts/track_b_rules.py`의 import 블록(기존 `from src import indicators` 아래)에 추가한다.

```python
from src import warmup as warmup_mod  # noqa: E402
```

`build_context`를 아래로 통째로 바꾼다.

```python
def build_context(
    bars: list[dict], params: dict, warmup: list[dict] | None = None
) -> dict:
    """하루치 파생 계열을 한 번만 계산한다.

    지표는 반드시 운영 코드(src.indicators)를 쓴다. 여기서 다시 구현하면
    백테스트와 실시간이 다른 값을 보게 된다.

    ``warmup``은 전 거래일 봉이다. **SMA·MACD에만 먹인다** — VWAP은 정의상
    세션 누적이고, ``run_high``는 "그날 직전까지의 고가"이며, ``gap_block``은
    15:30→09:00 경계를 거대한 봉 간격으로 읽는다. 셋 중 하나라도 전일을
    섞으면 R1·R2가 조용히 다른 규칙이 된다.
    """
    period = params.get("sma_period", DEFAULT_PARAMS["sma_period"])
    warmed_bars, offset = warmup_mod.combine(warmup or [], bars)

    macd_rows = indicators.macd(
        warmed_bars,
        params.get("macd_fast", DEFAULT_PARAMS["macd_fast"]),
        params.get("macd_slow", DEFAULT_PARAMS["macd_slow"]),
        params.get("macd_signal", DEFAULT_PARAMS["macd_signal"]),
    )[offset:]
    sma_rows = indicators.sma(warmed_bars, period)[offset:]

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
        "sma": sma_rows,
        "macd": macd_rows,
        "vwap": vwap,
        "run_high": run_high,
        "first_hist_idx": first_hist_idx,
        "gap_block": gap_block,
    }
```

- [ ] **Step 4: 통과를 확인한다**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_track_b_rules.py -q`
Expected: PASS — 기존 16개 + 신규 3개

- [ ] **Step 5: 전체 스위트로 회귀를 확인한다**

Run: `./.venv/Scripts/python.exe -m pytest -q`
Expected: PASS, 실패 0

- [ ] **Step 6: 커밋**

```bash
git add scripts/track_b_rules.py tests/test_track_b_rules.py
git commit -m "feat(track-b): warm the indicators without warming the session values

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: 백테스트 경로 — 전 거래일 로더와 `--warmup-days`

**Files:**
- Modify: `scripts/track_b_backtest.py` (`find_signal`·`simulate_day`·`run_axis`·`sign_stability`·`main`)
- Test: `tests/test_track_b_backtest.py`

**Interfaces:**
- Consumes: `build_context(bars, params, warmup=...)` (Task 2), `scripts.strategy_backtest.read_cached_bars`, `src.warmup.WARMUP_MIN_BARS`
- Produces:
  - `previous_trading_date(dates: list[str], date: str) -> str | None`
  - `load_warmup(date: str, ticker: str, dates: list[str], days: int, cache_dir: Path = BAR_CACHE_DIR) -> list[dict]`
  - `find_signal(..., warmup_by_ticker: dict[str, list[dict]] | None = None)`
  - `simulate_day(..., warmup_by_ticker: dict[str, list[dict]] | None = None)`
  - `run_axis(..., warmup: dict[str, dict[str, list[dict]]] | None = None)`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_track_b_backtest.py` 끝에 추가한다.

```python
def test_previous_trading_date_uses_the_universe_not_the_calendar():
    """08-17은 대체공휴일이라 유니버스에 없다. 달력을 쓰면 안 된다."""
    dates = ["20260814", "20260818", "20260819"]

    assert track_b_backtest.previous_trading_date(dates, "20260818") == "20260814"
    assert track_b_backtest.previous_trading_date(dates, "20260814") is None
    assert track_b_backtest.previous_trading_date(dates, "20260901") is None


def test_load_warmup_returns_empty_when_the_previous_day_is_missing(tmp_path):
    dates = ["20260814", "20260818"]

    assert track_b_backtest.load_warmup(
        "20260818", "005930", dates, days=1, cache_dir=tmp_path
    ) == []


def test_load_warmup_reads_the_previous_day_in_time_order(tmp_path):
    import json
    (tmp_path / "20260814_005930.json").write_text(json.dumps([
        {"time": "091000", "open": 2, "high": 2, "low": 2, "close": 2, "volume": 1},
        {"time": "090000", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
    ]), encoding="utf-8")
    dates = ["20260814", "20260818"]

    rows = track_b_backtest.load_warmup(
        "20260818", "005930", dates, days=1, cache_dir=tmp_path
    )

    assert [r["time"] for r in rows] == ["090000", "091000"]


def test_load_warmup_zero_days_reads_nothing(tmp_path):
    import json
    (tmp_path / "20260814_005930.json").write_text(json.dumps([
        {"time": "090000", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
    ]), encoding="utf-8")

    assert track_b_backtest.load_warmup(
        "20260818", "005930", ["20260814", "20260818"], days=0, cache_dir=tmp_path
    ) == []
```

- [ ] **Step 2: 실패를 확인한다**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_track_b_backtest.py -q -k "previous_trading_date or load_warmup"`
Expected: FAIL — `AttributeError: module 'scripts.track_b_backtest' has no attribute 'previous_trading_date'`

- [ ] **Step 3: 로더를 구현한다**

`scripts/track_b_backtest.py`의 `load_bars_for` 정의 바로 위에 추가한다. `read_cached_bars`·`BAR_CACHE_DIR`·`Path`는 이미 import되어 있다.

```python
def previous_trading_date(dates: list[str], date: str) -> str | None:
    """실제 거래일 목록에서 바로 앞 날짜.

    달력을 쓰지 않는다 — 대체공휴일(20260817)처럼 유니버스에 없는 날을
    자동으로 건너뛴다.
    """
    ordered = sorted(dates)
    if date not in ordered:
        return None
    i = ordered.index(date)
    return ordered[i - 1] if i > 0 else None


def load_warmup(
    date: str, ticker: str, dates: list[str], days: int,
    cache_dir: Path = BAR_CACHE_DIR,
) -> list[dict]:
    """전 거래일 봉을 시간 순으로 이어 붙인다.

    없으면 빈 리스트다 — 워밍업 실패를 조용히 채우지 않고 warmed=False로
    드러낸다(스펙 §4.3).
    """
    if days <= 0:
        return []
    out: list[dict] = []
    cursor = date
    for _ in range(days):
        cursor = previous_trading_date(dates, cursor)
        if cursor is None:
            break
        cached = read_cached_bars(cursor, ticker, cache_dir)
        if not cached:
            break
        out = sorted(cached, key=lambda r: r["time"]) + out
    return out
```

- [ ] **Step 4: 통과를 확인한다**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_track_b_backtest.py -q -k "previous_trading_date or load_warmup"`
Expected: PASS (4 passed)

- [ ] **Step 5: 시뮬레이션 경로에 배선한다**

`find_signal`의 시그니처와 컨텍스트 생성부를 바꾼다.

```python
def find_signal(
    bars_by_ticker: dict[str, list[dict]],
    ranked_tickers: list[str],
    rule_key: str,
    params: dict,
    warmup_by_ticker: dict[str, list[dict]] | None = None,
) -> dict | None:
    """봉을 시간 순으로 훑어 첫 신호에서 멈춘다.

    같은 봉에서 둘 이상이 신호를 내면 최상위 랭크를 고른다. 랭크 1이 나중에
    신호를 낼지 기다리는 해석은 실시간에 구현 불가라 쓰지 않는다.
    """
    rule = RULES[rule_key]
    warm = warmup_by_ticker or {}
    contexts = {
        t: build_context(bars, params, warmup=warm.get(t))
        for t, bars in bars_by_ticker.items()
        if bars
    }
```

`simulate_day`의 시그니처와 `find_signal` 호출을 바꾼다.

```python
def simulate_day(
    date: str,
    universe: list[dict],
    bars_by_ticker: dict[str, list[dict]],
    rule_key: str,
    params: dict,
    *,
    slippage: float = 0.0,
    depth: int = DEPTH,
    warmup_by_ticker: dict[str, list[dict]] | None = None,
) -> dict | None:
    """하루 한 건. 신호가 없거나 진입가가 없으면 None(미진입)이다."""
    ranked = f1_selector.rank_candidates(universe)[:depth]
    ranked_tickers = [str(r["ticker"]) for r in ranked if r.get("ticker")]
    signal = find_signal(
        bars_by_ticker, ranked_tickers, rule_key, params,
        warmup_by_ticker=warmup_by_ticker,
    )
```

`run_axis`와 `sign_stability`가 워밍업을 실어 나르게 한다.

```python
def run_axis(
    universes: dict[str, list[dict]],
    bars: dict[str, dict[str, list[dict]]],
    rule_key: str,
    params: dict,
    *,
    slippage: float = 0.0,
    warmup: dict[str, dict[str, list[dict]]] | None = None,
) -> list[dict]:
    rows = []
    warm = warmup or {}
    for date in sorted(universes):
        result = simulate_day(
            date, universes[date], bars.get(date, {}), rule_key, params,
            slippage=slippage, warmup_by_ticker=warm.get(date),
        )
        if result is not None:
            rows.append(result)
    return rows


def sign_stability(
    universes: dict[str, list[dict]],
    bars: dict[str, dict[str, list[dict]]],
    rule_key: str,
    params: dict,
    warmup: dict[str, dict[str, list[dict]]] | None = None,
) -> list[int]:
    """체결 가정 셋에서의 합계 부호. 하나라도 다르면 관문 2 탈락이다."""
    signs = []
    for slip in SLIPPAGES:
        rows = run_axis(universes, bars, rule_key, params,
                        slippage=slip, warmup=warmup)
        total = sum(r["pct"] for r in rows if r["pct"] is not None)
        signs.append(0 if total == 0 else (1 if total > 0 else -1))
    return signs
```

- [ ] **Step 6: CLI에 `--warmup-days`를 붙인다**

import 블록에 추가한다.

```python
from src import warmup as warmup_mod  # noqa: E402
```

`main`의 파서와 본문 앞부분을 바꾼다.

```python
    parser.add_argument("--depth", type=int, default=DEPTH)
    parser.add_argument(
        "--warmup-days", type=int, default=1,
        help="지표에 먹일 전 거래일 수. 0이면 구 동작(일 단위 초기화)",
    )
    parser.add_argument("--out", default="", help="결과 JSON 경로")
    args = parser.parse_args(argv)

    universes = load_universes()
    bars, stats = load_bars_for(universes, depth=args.depth)
    dates = sorted(universes)
    warmup = {
        date: {
            ticker: load_warmup(date, ticker, dates, args.warmup_days)
            for ticker in day
        }
        for date, day in bars.items()
    }
    warmed_pairs = sum(
        1 for day in warmup.values() for rows in day.values()
        if len(rows) >= warmup_mod.WARMUP_MIN_BARS
    )
    total_pairs = sum(len(day) for day in warmup.values())
    print(f"표본: {len(bars)}거래일 / 쌍 {stats['pairs']} "
          f"(없음 {stats['missing']}, 09:00~09:30만 {stats['partial']})")
    print(f"워밍업: {args.warmup_days}일 요청 / 실제 데운 쌍 "
          f"{warmed_pairs}/{total_pairs}")
```

축 루프에 워밍업을 넘긴다.

```python
        rows = run_axis(universes, bars, key, DEFAULT_PARAMS, warmup=warmup)
        axis_results[key] = {
            "rows": rows,
            "slippage_signs": sign_stability(
                universes, bars, key, DEFAULT_PARAMS, warmup=warmup
            ),
        }
```

- [ ] **Step 7: 구 동작 재현을 확인한다**

Run: `./.venv/Scripts/python.exe scripts/track_b_backtest.py --depth 1 --warmup-days 0`

**판정 기준은 고정된 숫자가 아니라 불변식이다** — `--warmup-days 0`의 출력이 이 변경 **이전 코드**를 같은 캐시로 돌린 출력과 같아야 한다. 유니버스 재분석 §3의 8/13/22는 캐시가 22거래일이던 시점의 값이고, 그 뒤 백필로 20260831이 들어와 캐시가 23거래일이 됐다. **캐시는 계속 자라므로 리터럴을 회귀선으로 쓸 수 없다.**

확인 방법: 변경분을 stash하고 같은 명령을 돌려 숫자를 적어둔 뒤, stash를 되살려 다시 돌려 두 출력이 같은지 본다. 2026-09-01 시점 23거래일 캐시에서의 값은 **R1 9일 / R2 14일 / R3 23일**이다. 숫자가 이와 달라도 stash 대조가 일치하면 통과다 — 그 사이 캐시가 또 자란 것이다. stash 대조가 **어긋나면** 배선이 틀린 것이므로 숫자를 맞추지 말고 BLOCKED로 보고한다.

- [ ] **Step 8: 전체 스위트**

Run: `./.venv/Scripts/python.exe -m pytest -q`
Expected: PASS, 실패 0

- [ ] **Step 9: 커밋**

```bash
git add scripts/track_b_backtest.py tests/test_track_b_backtest.py
git commit -m "feat(track-b): feed the backtest a previous-session warm-up

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: 백필이 워밍업 대상을 함께 채운다

**Files:**
- Modify: `scripts/track_b_backfill.py` (import·`needed_pairs`·`main_async`)
- Test: `tests/test_track_b_backfill.py`

**Interfaces:**
- Consumes: `scripts.track_b_backtest.previous_trading_date` (Task 3)
- Produces: `needed_pairs(depth: int = 5, snapshot_dir: Path | None = None, warmup_days: int = 0) -> dict[str, set[str]]`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_track_b_backfill.py` 끝에 추가한다.

```python
def _snapshot(tmp_path, date, tickers):
    """스냅샷 한 장을 만든다.

    load_universes 는 MIN_UNIVERSE_ROWS(30) 미만인 스냅샷을 통째로 버린다
    (scripts/strategy_backtest.py:70). 기존 테스트가 쓰는 패딩 방식을 그대로
    따라 30행을 채운다.
    """
    rows = [{
        "ticker": t, "gap_pct": 0.05, "prev_close": 1000,
        "expected_amount": 5_000_000_000, "avg_amount_5d": 1_000_000_000,
    } for t in tickers]
    while len(rows) < 30:
        filler = dict(rows[0])
        filler["ticker"] = f"9{len(rows):05d}"
        rows.append(filler)
    (tmp_path / f"{date}_090100.jsonl").write_text(
        "
".join(json.dumps(r, ensure_ascii=False) for r in rows),
        encoding="utf-8",
    )


def test_needed_pairs_adds_the_previous_session_for_warmup(tmp_path):
    """워밍업 1일이면 각 날짜의 종목이 전 거래일 쌍에도 들어간다.

    두 날의 스냅샷을 같게 만들어, 랭킹 내부 구현에 기대지 않고 집합 관계만
    본다 — 어느 종목이 랭크 1인지는 이 테스트의 관심사가 아니다.
    """
    for date in ("20260814", "20260818"):
        _snapshot(tmp_path, date, ["005930", "000660"])

    cold = track_b_backfill.needed_pairs(depth=5, snapshot_dir=tmp_path,
                                         warmup_days=0)
    hot = track_b_backfill.needed_pairs(depth=5, snapshot_dir=tmp_path,
                                        warmup_days=1)

    assert set(cold) == {"20260814", "20260818"}
    # 20260818의 워밍업은 20260814다. 그날 쌍이 08-18의 종목을 흡수한다.
    assert hot["20260814"] >= cold["20260818"]
    assert hot["20260818"] == cold["20260818"]


def test_needed_pairs_warmup_does_not_invent_days_outside_the_universe(tmp_path):
    """유니버스의 첫 날은 그 앞이 없다 — 없는 날짜를 만들어내면 안 된다."""
    _snapshot(tmp_path, "20260814", ["005930"])

    pairs = track_b_backfill.needed_pairs(depth=5, snapshot_dir=tmp_path,
                                          warmup_days=1)

    assert set(pairs) == {"20260814"}
```

- [ ] **Step 2: 실패를 확인한다**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_track_b_backfill.py -q -k warmup`
Expected: FAIL — `needed_pairs() got an unexpected keyword argument 'warmup_days'`

- [ ] **Step 3: 구현한다**

`scripts/track_b_backfill.py`의 import 블록에 추가한다.

```python
from scripts.track_b_backtest import previous_trading_date  # noqa: E402
```

`needed_pairs`를 아래로 바꾼다.

```python
def needed_pairs(
    depth: int = 5, snapshot_dir: Path | None = None, warmup_days: int = 0
) -> dict[str, set[str]]:
    """날짜별 F1 랭크 1~depth 종목. 운영 랭킹 함수를 그대로 쓴다.

    ``warmup_days``가 0보다 크면 각 종목의 전 거래일 쌍을 함께 대상에 넣는다.
    지표 워밍업이 그 봉을 필요로 하는데, 그 종목이 그날 F1 상위에 없었으면
    캐시에 없기 때문이다(스펙 §5.1).
    """
    universes = (
        load_universes(snapshot_dir) if snapshot_dir is not None else load_universes()
    )
    needed: dict[str, set[str]] = {}
    for date, rows in universes.items():
        ranked = f1_selector.rank_candidates(rows)[:depth]
        tickers = {str(r["ticker"]) for r in ranked if r.get("ticker")}
        if tickers:
            needed[date] = tickers

    if warmup_days > 0:
        dates = sorted(universes)
        for date in list(needed):
            cursor = date
            for _ in range(warmup_days):
                cursor = previous_trading_date(dates, cursor)
                if cursor is None:
                    break
                needed.setdefault(cursor, set()).update(needed[date])
    return needed
```

`main_async`의 파서와 호출을 바꾼다.

```python
    parser.add_argument("--dry-run", action="store_true", help="호출 없이 계획만 출력")
    parser.add_argument(
        "--warmup-days", type=int, default=1,
        help="지표 워밍업에 필요한 전 거래일도 함께 채운다. 0이면 채우지 않는다",
    )
    args = parser.parse_args(argv)

    needed = needed_pairs(args.depth, warmup_days=args.warmup_days)
```

- [ ] **Step 4: 통과를 확인한다**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_track_b_backfill.py -q`
Expected: PASS — 기존 11개 + 신규 2개

- [ ] **Step 5: 계획을 눈으로 확인한다**

Run:
```bash
./.venv/Scripts/python.exe scripts/track_b_backfill.py --dry-run --depth 1 --warmup-days 0
./.venv/Scripts/python.exe scripts/track_b_backfill.py --dry-run --depth 1 --warmup-days 1
```
Expected: 두 번째 쪽 쌍 수가 더 크다. 랭크 1 기준이므로 늘어난 폭이 거래일 수 근처여야 한다.

- [ ] **Step 6: 커밋**

```bash
git add scripts/track_b_backfill.py tests/test_track_b_backfill.py
git commit -m "feat(backfill): fetch the previous session the warm-up needs

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: `/api/bars`가 워밍업 값과 상태를 함께 낸다

**Files:**
- Modify: `src/api/server.py` (import·`api_bars`)
- Test: `tests/test_api_bars.py`

**Interfaces:**
- Consumes: `src.warmup.combine`·`src.warmup.meta` (Task 1), `src.bars.bars_path`
- Produces: `/api/bars`가 쿼리 파라미터 `prev`(전 거래일 `YYYYMMDD`, 기본 `""`)를 받고, 응답에 `"warmup": {"warmup_days": int, "warmup_bars": int, "warmed": bool}`을 싣는다. `bars` 배열 길이와 `indicators` 각 배열 길이는 **여전히 같다.**

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_api_bars.py` 끝에 추가한다. 이 파일이 쓰는 클라이언트 픽스처와 `bars` import 이름을 먼저 확인하고 그대로 따른다.

```python
def test_api_bars_reports_warmup_state_when_no_previous_day_exists(
    client, tmp_path, monkeypatch
):
    """전일 파일이 없으면 warmed=False로 정직하게 남긴다."""
    monkeypatch.setattr(bars, "_BARS_DIR", tmp_path)
    (tmp_path / "20260901_005930.json").write_text(json.dumps([
        {"time": "090000", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
    ]), encoding="utf-8")

    body = client.get("/api/bars?date=20260901&ticker=005930").json()

    assert body["warmup"] == {"warmup_days": 0, "warmup_bars": 0, "warmed": False}
    assert len(body["indicators"]["sma"]) == len(body["bars"])


def test_api_bars_indicator_arrays_stay_aligned_with_the_day(
    client, tmp_path, monkeypatch
):
    """워밍업이 있어도 지표 배열은 당일 봉 길이로 잘려 나오고, 캔들은 당일 것뿐이다."""
    monkeypatch.setattr(bars, "_BARS_DIR", tmp_path)
    prev = [{"time": f"09{m:02d}00", "open": 10, "high": 10, "low": 10,
             "close": 10.0 + m, "volume": 5} for m in range(60)]
    today = [{"time": f"09{m:02d}00", "open": 20, "high": 20, "low": 20,
              "close": 20.0 + m, "volume": 5} for m in range(3)]
    (tmp_path / "20260831_005930.json").write_text(json.dumps(prev), encoding="utf-8")
    (tmp_path / "20260901_005930.json").write_text(json.dumps(today), encoding="utf-8")

    body = client.get(
        "/api/bars?date=20260901&ticker=005930&prev=20260831"
    ).json()

    assert body["warmup"]["warmup_bars"] == 60
    assert body["warmup"]["warmed"] is False      # 60 < WARMUP_MIN_BARS
    assert len(body["bars"]) == 3
    assert len(body["indicators"]["macd"]) == 3
    assert len(body["indicators"]["ma"]["5"]) == 3
```

- [ ] **Step 2: 실패를 확인한다**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_api_bars.py -q -k warmup`
Expected: FAIL — `KeyError: 'warmup'`

- [ ] **Step 3: 구현한다**

`src/api/server.py`의 import에 추가한다.

```python
from src import warmup as warmup_mod
```

`api_bars`의 시그니처에 `prev: str = ""`를 넣는다.

```python
@app.get("/api/bars")
async def api_bars(
    track: str = "B",
    date: str = "",
    ticker: str = "",
    prev: str = "",
    sma: int = 20,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> JSONResponse:
```

`rows`를 채우는 기존 블록이 끝난 뒤, `return JSONResponse(` 앞에 넣는다.

```python
    # 지표 워밍업 — 전 거래일 봉을 앞에 붙여 09:00부터 증권사와 같은 값을 낸다.
    # 캔들은 당일 것만 돌려준다. 화면이 전일 봉을 그리면 안 된다.
    warm: list = []
    if rows and prev and _BARS_DATE_RE.match(prev) and target:
        try:
            warm = json.loads(
                bars.bars_path(prev, target).read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            warm = []
    warmed_rows, offset = warmup_mod.combine(warm, rows)

    def _sma(period: int, field: str = "close") -> list:
        if not rows:
            return []
        return indicators.sma(warmed_rows, period, field=field)[offset:]
```

지표 블록과 응답을 아래로 바꾼다.

```python
        "indicators": {
            "sma": _sma(sma),
            "macd": (
                indicators.macd(warmed_rows, fast, slow, signal)[offset:]
                if rows else []
            ),
            "ma": {str(p): _sma(p) for p in _BARS_MA_PERIODS} if rows else {},
            "vol_ma": {
                str(p): _sma(p, field="volume") for p in _BARS_VOL_MA_PERIODS
            } if rows else {},
        },
        "warmup": warmup_mod.meta(warm, days=1 if warm else 0),
```

- [ ] **Step 4: 통과를 확인한다**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_api_bars.py -q`
Expected: PASS — 기존 23개 + 신규 2개

- [ ] **Step 5: 전체 스위트**

Run: `./.venv/Scripts/python.exe -m pytest -q`
Expected: PASS, 실패 0

- [ ] **Step 6: 커밋**

```bash
git add src/api/server.py tests/test_api_bars.py
git commit -m "feat(api): serve warmed indicators and say whether they are warmed

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: 실시간 — 금지창이 끝난 뒤 전 거래일 봉을 한 번 받는다

스펙 §6.1·§6.2. 09:00~09:11은 `kis_minute_bars.in_forbidden_window()`가 막으므로 **선취하지 않는다.** 금지창이 끝난 뒤 종목당 한 번만 받아 디스크에 남긴다. 그 전까지 `/api/bars`는 `warmed=False`다. 트랙 B의 판정이 빨라도 09:35이라 지연 로드가 판정을 늦추지 않는다.

**Files:**
- Modify: `src/api/kis_minute_bars.py` (`fetch_session` 추가)
- Modify: `src/bars.py` (`ensure_warmup` 추가)
- Test: `tests/test_bars_warmup_fetch.py` (Create)

**Interfaces:**
- Consumes: `src.api.kis_minute_bars.in_forbidden_window`, `src.bars.bars_path`
- Produces:
  - `kis_minute_bars.fetch_session(date: str, ticker: str, max_pages: int = 20) -> list[dict]`
  - `bars.ensure_warmup(date: str, ticker: str, prev_date: str, now: datetime | None = None) -> bool` — 이미 파일이 있으면 호출 없이 `True`, 금지창이면 `False`

- [ ] **Step 1: 먼저 의존 함수의 위치를 확인한다**

`src/api/kis_minute_bars.py`에 페이지 요청 함수와 공식 봉 파서가 있는지 확인한다.

Run: `grep -n "def " src/api/kis_minute_bars.py`

`scripts/fast_path_counterfactual.py`에만 있다면 **먼저 운영 모듈로 옮기고 그 이동만 별도 커밋한 뒤** 이 태스크를 이어간다. 운영 코드(`src/`)가 `scripts/`를 import하면 안 된다.

- [ ] **Step 2: 실패하는 테스트를 쓴다**

```python
# tests/test_bars_warmup_fetch.py
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src import bars

KST = ZoneInfo("Asia/Seoul")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(bars, "_BARS_DIR", tmp_path)
    yield


async def test_warmup_fetch_is_refused_inside_the_forbidden_window(monkeypatch):
    """09:00~09:11은 A의 진입 창이다. 여기서 분봉 API를 부르지 않는다."""
    called = []

    async def fake_session(date, ticker, max_pages=20):
        called.append((date, ticker))
        return []

    monkeypatch.setattr(bars.kis_minute_bars, "fetch_session", fake_session)

    ok = await bars.ensure_warmup(
        "20260901", "005930", "20260831",
        now=datetime(2026, 9, 1, 9, 5, tzinfo=KST),
    )

    assert ok is False
    assert called == []


async def test_warmup_fetch_is_skipped_when_the_file_already_exists(
    monkeypatch, tmp_path
):
    (tmp_path / "20260831_005930.json").write_text(json.dumps([
        {"time": "090000", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
    ]), encoding="utf-8")
    called = []

    async def fake_session(date, ticker, max_pages=20):
        called.append((date, ticker))
        return []

    monkeypatch.setattr(bars.kis_minute_bars, "fetch_session", fake_session)

    ok = await bars.ensure_warmup(
        "20260901", "005930", "20260831",
        now=datetime(2026, 9, 1, 9, 30, tzinfo=KST),
    )

    assert ok is True
    assert called == []


async def test_warmup_fetch_writes_the_previous_session_to_disk(
    monkeypatch, tmp_path
):
    async def fake_session(date, ticker, max_pages=20):
        return [{"time": "090000", "open": 1, "high": 1, "low": 1,
                 "close": 1, "volume": 1}]

    monkeypatch.setattr(bars.kis_minute_bars, "fetch_session", fake_session)

    ok = await bars.ensure_warmup(
        "20260901", "005930", "20260831",
        now=datetime(2026, 9, 1, 9, 30, tzinfo=KST),
    )

    assert ok is True
    written = json.loads(
        (tmp_path / "20260831_005930.json").read_text(encoding="utf-8")
    )
    assert [r["time"] for r in written] == ["090000"]


async def test_warmup_fetch_reports_failure_without_raising(monkeypatch):
    """워밍업 실패가 판정 경로를 막으면 안 된다."""
    async def boom(date, ticker, max_pages=20):
        raise RuntimeError("network down")

    monkeypatch.setattr(bars.kis_minute_bars, "fetch_session", boom)

    ok = await bars.ensure_warmup(
        "20260901", "005930", "20260831",
        now=datetime(2026, 9, 1, 9, 30, tzinfo=KST),
    )

    assert ok is False
```

- [ ] **Step 3: 실패를 확인한다**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_bars_warmup_fetch.py -q`
Expected: FAIL — `AttributeError: module 'src.bars' has no attribute 'ensure_warmup'`

- [ ] **Step 4: `fetch_session`을 구현한다**

`src/api/kis_minute_bars.py`에 추가한다. 페이지 밀기 방식은 `scripts/track_b_backfill.py`의 `fetch_session_bars`와 같다 — 마감 커서에서 09:00까지 역방향으로 민다. 그 스크립트의 구현을 읽고 같은 파서·같은 종료 조건을 쓴다.

```python
async def fetch_session(date: str, ticker: str, max_pages: int = 20) -> list[dict]:
    """지정 날짜의 09:00~15:30 분봉. 마감 커서에서 09:00까지 역방향으로 민다.

    한 페이지가 30봉이므로 전 세션은 약 14페이지다. 같은 분이 두 페이지에
    걸쳐 오므로 시각을 키로 중복을 제거한다.
    """
    seen: dict[str, dict] = {}
    cursor = "153000"
    for _ in range(max_pages):
        page = await fetch_minute_bars(ticker, trade_date=date, hour_cursor=cursor)
        rows = page.get("output2") or []
        if not rows:
            break
        for row in rows:
            bar = parse_official(row)
            if bar:
                seen[bar["time"]] = bar
        if not seen:
            break
        earliest = min(seen)
        if earliest <= "090000":
            break
        cursor = earliest
    return [seen[t] for t in sorted(seen)]
```

> 구현자 주의: `fetch_minute_bars`의 실제 시그니처(날짜 인자 이름, 커서 인자 이름)와 공식 봉 파서의 이름을 Step 1에서 확인한 것으로 맞춘다. 위 코드는 이름을 가정하고 있으므로 **그대로 붙여넣지 말고 실제 이름으로 고친다.**

- [ ] **Step 5: `ensure_warmup`을 구현한다**

`src/bars.py`의 `_merge_official` 근처에 추가한다 — 정정 경로와 같은 어휘를 쓰는 자리다.

```python
async def ensure_warmup(
    date: str, ticker: str, prev_date: str, now: datetime | None = None
) -> bool:
    """전 거래일 봉을 디스크에 확보한다. 지표 워밍업이 이 파일을 읽는다.

    금지창(09:00~09:11)에는 부르지 않는다 — A의 진입 창을 지키는 가드가
    워밍업보다 우선한다. 트랙 B의 판정이 빨라도 09:35이라 지연 로드가 판정을
    늦추지 않는다(스펙 §6.2).

    ``date``는 로깅용이다. 읽고 쓰는 것은 ``prev_date``의 파일이다.
    """
    now = now or datetime.now(KST)
    if bars_path(prev_date, ticker).exists():
        return True
    if kis_minute_bars.in_forbidden_window(now):
        return False
    try:
        rows = await kis_minute_bars.fetch_session(prev_date, ticker)
    except Exception as exc:  # noqa: BLE001 — 워밍업 실패는 판정을 막지 않는다
        log("TRACK_B_WARMUP_FAILED", level="WARN",
            ticker=ticker, date=prev_date, error=repr(exc))
        return False
    if not rows:
        log("TRACK_B_WARMUP_EMPTY", level="INFO", ticker=ticker, date=prev_date)
        return False
    bars_path(prev_date, ticker).write_text(
        json.dumps(rows, ensure_ascii=False), encoding="utf-8"
    )
    log("TRACK_B_WARMUP_READY", level="INFO",
        ticker=ticker, date=prev_date, bars=len(rows))
    return True
```

- [ ] **Step 6: 통과를 확인한다**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_bars_warmup_fetch.py -q`
Expected: PASS (4 passed)

- [ ] **Step 7: 전체 스위트**

Run: `./.venv/Scripts/python.exe -m pytest -q`
Expected: PASS, 실패 0

- [ ] **Step 8: 커밋**

```bash
git add src/bars.py src/api/kis_minute_bars.py tests/test_bars_warmup_fetch.py
git commit -m "feat(bars): fetch the warm-up session once the entry window closes

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: 수용 기준 — 실측으로 증권사 값과 맞춘다

스펙 §11. **이 태스크는 KIS를 호출하므로 15:40 이후에만 실행한다.**

**Files:**
- Create: `docs/INDICATOR_WARMUP_VERIFY_20260901.md`
- Modify: `docs/INDICATOR_EXTERNAL_CHECK_20260901.md` (§3에 해소 표시와 링크)

- [ ] **Step 1: 워밍업 봉을 백필한다**

Run:
```bash
./.venv/Scripts/python.exe scripts/track_b_backfill.py --depth 1 --warmup-days 1
```
Expected: `failed`가 0이고, 20260831의 전 거래일인 20260828의 443670이 채워진다.

> 채워지지 않으면 **여기서 멈춘다.** 일별분봉 TR의 과거 가용 범위는 미검증이다(`fetch_daily_minute_bars` docstring: "가용성은 미검증이다"). 며칠까지 주는지 실측 결과를 Step 5 문서에 기록하고, 스펙 §5.1의 백필 범위를 다시 잡는다.

- [ ] **Step 2: 09:17 MACD를 대조한다**

Run:
```bash
./.venv/Scripts/python.exe -c "
import json, sys; sys.path.insert(0, '.')
from scripts.track_b_rules import build_context, DEFAULT_PARAMS
from scripts.strategy_backtest import read_cached_bars
day = sorted(read_cached_bars('20260831','443670'), key=lambda r: r['time'])
warm = sorted(read_cached_bars('20260828','443670') or [], key=lambda r: r['time'])
ctx = build_context(day, DEFAULT_PARAMS, warmup=warm)
print('워밍업 봉:', len(warm))
for t in ('0917', '1248'):
    i = [k for k,x in enumerate(day) if x['time'][:4]==t][0]
    r = ctx['macd'][i]
    print(t, {k: (None if v is None else round(v,2)) for k,v in r.items()})
"
```
Expected:
- `0917` → `macd 49.42 · signal 47.32 · hist 2.10` (알파스퀘어 값과 일치)
- `1248` → `macd 26.80 · signal 14.39 · hist 12.41` (워밍업과 무관하게 유지 — 수렴 대조군)

- [ ] **Step 3: 구 동작 재현을 다시 확인한다**

Run: `./.venv/Scripts/python.exe scripts/track_b_backtest.py --depth 1 --warmup-days 0`
Expected: Task 3 Step 7이 기록한 값과 같다. 캐시가 그 사이 자랐다면 값이 커질 수 있으므로, 리터럴이 아니라 **같은 캐시에서 변경 전 코드와 일치하는가**로 판정한다

- [ ] **Step 4: 워밍업 기준으로 재계산한다**

Run: `./.venv/Scripts/python.exe scripts/track_b_backtest.py --depth 1 --warmup-days 1`
Expected: 진입일 수가 나온다. **이 숫자를 보고 관문을 고치지 않는다** — 스펙 §7의 관문(진입율 25~55%, 손익 제외)을 그대로 적용해 통과·탈락만 기록한다.

- [ ] **Step 5: 결과를 문서로 남긴다**

`docs/INDICATOR_WARMUP_VERIFY_20260901.md`에 다음 다섯 절을 쓴다.

1. 백필 결과 — 채운 쌍 수, **일별분봉 API의 실제 과거 가용 범위**(Step 1에서 실측한 것)
2. 09:17·12:48 대조표 — 알파스퀘어 값과 우리 값을 나란히
3. `--warmup-days 0` 재현 결과 — 유니버스 재분석 §3의 랭크 1 열과 일치하는지
4. 워밍업 기준 R1·R2·R3 진입일과 스펙 §7 관문 적용 결과(통과·탈락만)
5. 유니버스 재분석 §9의 재판정 조건을 새 발화율로 다시 계산한 값

`docs/INDICATOR_EXTERNAL_CHECK_20260901.md` §3 "값이 다른 구간 — 설계상 의도된 차이"에 해소 표시와 이 문서 링크를 추가한다. 본문은 지우지 않는다 — 그때의 판단이 무엇이었는지 남아야 한다.

- [ ] **Step 6: 커밋**

```bash
git add docs/INDICATOR_WARMUP_VERIFY_20260901.md docs/INDICATOR_EXTERNAL_CHECK_20260901.md
git commit -m "docs: verify the warmed indicators against the broker chart

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## 스펙 커버리지 자기 점검

| 스펙 절 | 태스크 |
|---|---|
| §3 `indicators.py` 무변경 | Global Constraints — 어떤 태스크도 이 파일을 열지 않는다 |
| §4.1 하루면 충분 · `WARMUP_MIN_BARS` | Task 1 |
| §4.2 결합 계약과 offset | Task 1, Task 2 |
| §4.3 `warmed` 표시 | Task 1(meta), Task 5(API 노출), Task 6(실패 시 로그) |
| §5 백테스트 경로 · 전 거래일 = 유니버스 기준 | Task 3 |
| §5.1 추가 백필 | Task 4 |
| §6.1 금지창 · 지연 로드 | Task 6 |
| §6.2 유량 — 종목당 1회 | Task 6 (`ensure_warmup`이 파일 존재를 먼저 본다) |
| §7 관문 먼저 고정 후 재계산 | Task 7 Step 4 |
| §8 구 모드 보존 | Task 2 Step 1(회귀 테스트), Task 3 Step 7, Task 7 Step 3 |
| §9 재판정 조건 재수립 | Task 7 Step 5 |
| §11 수용 기준 1 | Task 3 Step 8, Task 5 Step 5, Task 6 Step 7 |
| §11 수용 기준 2·3 | Task 7 Step 2 |
| §11 수용 기준 4 | Task 3 Step 3(빈 리스트 반환), Task 5 Step 1(첫 테스트) |
| §11 수용 기준 5 | Task 5 |

**스펙 §10(범위 밖)** — RSI 등 신규 지표, 진입 창 변경, 우리 차트 육안 확인, 15:30 봉 문제는 어떤 태스크에도 없다. 의도대로다.

## 알려진 위험

- **일별분봉 API의 과거 가용 범위가 미검증이다.** Task 7 Step 1이 이것을 실측하는 첫 지점이고, 여기서 막히면 §7 재계산 전체가 막힌다. Task 1~6은 이 위험과 무관하게 완료 가능하다.
- **Task 6의 함수 이름이 가정이다.** `fetch_minute_bars`·공식 봉 파서의 실제 이름을 Step 1에서 확인하고 맞춘다. 확인 없이 붙여넣으면 실패한다.
