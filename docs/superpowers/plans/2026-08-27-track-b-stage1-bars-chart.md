# 트랙 B 1단계 — 봉/지표 계층과 차트 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 트랙 B가 판단할 1분 봉과 지표가 매일 쌓이고, 그것이 화면에 캔들·MACD로 보이게 한다. 신호는 내지 않는다.

**Architecture:** 틱은 `tick_capture.enqueue`의 신규 팬아웃 훅으로 새 모듈 `src/bars.py`에 흘러든다(`live.push_tick`은 거래량이 없어 쓸 수 없다). `bars`는 논블로킹 deque로 받아 별도 태스크에서 1분 OHLCV를 확정하고, 09:11 이후 분봉 API로 확정 봉을 정정한 뒤 `data/bars/`에 write-through 한다. 지표는 `src/indicators.py`의 순수 함수가 계산하고, `/api/bars`가 서버에서 계산한 값을 내려주며, `bars_chart.js`가 Canvas 2D로 그린다.

**Tech Stack:** Python 3.12 · asyncio · aiosqlite(이 단계에서는 미사용) · FastAPI · httpx(`kis_rest` 경유) · pytest(`asyncio_mode=auto`) · Vanilla JS + Canvas 2D · Node(JS 테스트 하네스)

**Spec:** [docs/superpowers/specs/2026-08-27-track-b-shadow-design.md](../specs/2026-08-27-track-b-shadow-design.md)

## Global Constraints

- **기존 테스트 스위트를 한 줄도 고치지 않는다.** 고쳐야 한다면 실행 계층을 건드렸다는 뜻이고 전제가 무너진 것이다. JS 쪽에서는 `node tests/js/price_flow_checks.js`가 같은 기준이다. (스펙 §13.①)
- **`_STRATEGY_FILES`에 있는 파일을 1단계에서 하나도 수정하지 않는다.** 목록: `main.py`, `src/state.py`, `src/live.py`, `src/db.py`, `src/api/kis_rest.py`, `src/api/kis_ws.py`, `src/modules/f1_filter.py`, `src/modules/f1_selector.py`, `src/modules/f2_lockup.py`, `src/modules/f3_entry.py`, `src/modules/f4_tracking.py`, `src/modules/f5_timeout.py`, `src/modules/exit_recovery.py`, `src/modules/paper_fast_probe.py`, `src/modules/vi_watch.py`, `src/schedule_times.py`, `src/scheduler.py`, `src/utils/number.py`, `src/utils/spike_filter.py` ([src/release.py:16-37](../../../src/release.py#L16-L37)). (스펙 §3.2·§4)
- **환경변수 이름은 `TRACK_B_`로 시작한다.** `F1_`~`F5_`, `PAPER_FAST_`, `TRAILING_SHADOW_`, `STRATEGY_TICK_`, `VI_`, `BALANCE_SNAPSHOT_`, `EXIT_RECONCILE_`, `KIS_RATE_`, `KIS_MAX_TRANSIENT_`, `KIS_TRANSIENT_`, `KIS_LOW_PRIORITY_` 접두사는 전략 지문 환경 스냅샷에 들어가므로 **절대 쓰지 않는다** ([src/release.py:42-58](../../../src/release.py#L42-L58)).
- **`src/modules/` 코드는 `src/api/`를 직접 import 하지 않는다** ([docs/CODING_GUIDELINES.md](../../CODING_GUIDELINES.md) §2). 그래서 `bars.py`는 `src/modules/`가 아니라 `src/` 최상위에 둔다.
- **`time.sleep()`·`threading` 금지, `asyncio.sleep()`·asyncio 태스크만 쓴다** (CODING_GUIDELINES §3).
- **분봉 API는 09:00~09:11에 호출하지 않는다.** A의 F1 선정(09:00)부터 F3 체결 마감(09:11)까지의 창이다. (스펙 §6.3)
- **B가 A의 주문 경로에서 하는 일은 `deque.append` 하나뿐이다.** 팬아웃 지점에서 봉 확정·정정·지표 계산을 하지 않는다. (스펙 §12.1)
- 시각은 전부 `ZoneInfo("Asia/Seoul")` 기준. 저장 문자열은 `date="YYYYMMDD"`, `time="HHMMSS"`.

---

## File Structure

| 파일 | 상태 | 책임 |
|---|---|---|
| `src/indicators.py` | 생성 | `sma`/`ema`/`macd` 순수 함수. I/O·상태 없음 |
| `src/api/kis_minute_bars.py` | 생성 | 분봉 API 호출·응답 파싱·금지창 판정 |
| `src/bars.py` | 생성 | 틱 → 1분 OHLCV 집계, 정정 조율, `data/bars/` write-through |
| `src/modules/tick_capture.py` | 수정 | `register_tick_listener` + `enqueue` 팬아웃 |
| `src/api/server.py` | 수정 | `GET /api/bars` |
| `docs/html/assets/bars_chart.js` | 생성 | 봉·지표 차트 (순수 헬퍼 + Canvas 드로잉) |
| `docs/html/index.html` | 수정 | 캔버스 2개 + `<script>` 한 줄 |
| `docs/html/assets/app.css` | 수정 | 신규 캔버스 레이아웃 |
| `tests/test_indicators.py` | 생성 | 지표 골든값 |
| `tests/test_kis_minute_bars.py` | 생성 | 파싱·금지창 |
| `tests/test_tick_fanout.py` | 생성 | 팬아웃 격리 |
| `tests/test_bars.py` | 생성 | 집계·write-through·스파이크 |
| `tests/test_bars_correction.py` | 생성 | 정정·유량 가드 |
| `tests/test_bars_restore.py` | 생성 | 재시작 복구 |
| `tests/test_api_bars.py` | 생성 | `/api/bars` |
| `tests/test_a_noninterference.py` | 생성 | 틱→손절 판정 지연 벤치 |
| `tests/js/bars_chart_checks.js` | 생성 | 차트 순수 헬퍼 |

---

### Task 0: 2026-08-26 캡처 공백 원인 확인

**Files:**
- 조사 전용. 코드 변경 없음. 결과는 스펙 §15에 기록

**Interfaces:**
- Consumes: 없음
- Produces: 없음 (조사 결론만)

1단계의 산출물이 "봉이 매일 쌓인다"인데, 절반의 날에 틱이 안 들어오면 산출물 자체가 성립하지 않는다. 착수 전에 확인한다.

- [ ] **Step 1: 두 날의 캡처 산출물 비교**

```bash
ls -la data/strategy_ticks/20260826 data/strategy_ticks/20260827
python -c "
import sqlite3
c = sqlite3.connect('data/db/trading.db')
cols = [r[1] for r in c.execute('PRAGMA table_info(price_path_manifests)')]
for r in c.execute(\"select * from price_path_manifests where trade_date in ('20260826','20260827')\"):
    d = dict(zip(cols, r))
    print(d['trade_date'], 'ws_disconnects=', d['ws_disconnects'],
          'first_received=', d['first_received_at'], 'reason=', d['missing_reason'])
"
```

기대: 08-26은 `first_received_at=None`(틱 0건), 08-27은 값이 있다.

- [ ] **Step 2: 08-26 로그에서 WS 구독과 종목 잠금 흔적을 찾는다**

```bash
ls data/logs/ | grep 2026-08-26
grep -E "F2_LOCK|F4_WS|WS_CONNECT|WS_DISCONNECT|F4_OBSERVATION|TICK_CAPTURE" data/logs/*2026-08-26* | head -40
```

찾는 것은 셋이다. **(a)** F2가 종목을 잠갔는가, **(b)** F4가 구독을 시작했는가, **(c)** 프로세스가 09:01 이후에도 살아 있었는가.

- [ ] **Step 3: 결론을 스펙 §15에 기록하고 커밋**

세 갈래로 갈린다.

| 발견 | 판정 |
|---|---|
| 그날 프로세스가 죽어 있었다 / 휴장이었다 | 1단계 진행에 영향 없음. §15에 기록하고 넘어간다 |
| 구독은 했는데 틱이 0건 | **1단계 착수 전에 고쳐야 한다.** 원인 조사 태스크를 이 계획 앞에 추가한다 |
| 종목 잠금이 없었다 (F1이 후보를 못 찾음) | 정상 동작. 그런 날은 B도 쉰다. §15에 기록 |

```bash
git add docs/superpowers/specs/2026-08-27-track-b-shadow-design.md
git commit -m "docs: record the 2026-08-26 capture gap finding"
```

---

### Task 1: 지표 엔진 — `src/indicators.py`

**Files:**
- Create: `src/indicators.py`
- Test: `tests/test_indicators.py`

**Interfaces:**
- Consumes: 없음 (완전 독립)
- Produces:
  - `sma(bars: list[dict], period: int) -> list[float | None]`
  - `ema(bars: list[dict], period: int) -> list[float | None]`
  - `macd(bars: list[dict], fast: int = 12, slow: int = 26, signal: int = 9) -> list[dict]`
  - `bars`는 `{"close": float}` 키를 가진 dict의 리스트. 반환 리스트 길이는 항상 `len(bars)`와 같고, 값이 설 수 없는 앞 구간은 `None`이다.
  - `macd`의 각 원소는 `{"macd": float | None, "signal": float | None, "hist": float | None}`

값이 설 수 없는 구간에 `0`을 넣지 않는다. MACD 히스토그램의 부호 판정이 개장 직후 거짓 신호를 낸다 (스펙 §7).

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_indicators.py`:

```python
import pytest

from src import indicators


def _bars(closes):
    return [{"close": float(c)} for c in closes]


def test_sma_is_none_until_period_is_filled():
    result = indicators.sma(_bars([1, 2, 3, 4]), 3)

    assert result[0] is None
    assert result[1] is None
    assert result[2] == pytest.approx(2.0)
    assert result[3] == pytest.approx(3.0)


def test_sma_returns_same_length_as_input():
    assert len(indicators.sma(_bars([1, 2, 3]), 5)) == 3
    assert indicators.sma(_bars([1, 2, 3]), 5) == [None, None, None]


def test_ema_seeds_from_sma_then_applies_smoothing():
    # period=3 → alpha = 2/4 = 0.5, seed = SMA(1,2,3) = 2.0
    result = indicators.ema(_bars([1, 2, 3, 4, 5]), 3)

    assert result[:2] == [None, None]
    assert result[2] == pytest.approx(2.0)
    assert result[3] == pytest.approx(3.0)   # 2.0 + 0.5 * (4 - 2.0)
    assert result[4] == pytest.approx(4.0)   # 3.0 + 0.5 * (5 - 3.0)


def test_macd_is_none_while_slow_ema_is_undefined():
    closes = list(range(1, 30))
    rows = indicators.macd(_bars(closes), fast=12, slow=26, signal=9)

    assert len(rows) == len(closes)
    assert all(r["macd"] is None for r in rows[:25])
    assert rows[25]["macd"] is not None


def test_macd_signal_needs_nine_defined_macd_values():
    closes = list(range(1, 40))
    rows = indicators.macd(_bars(closes), fast=12, slow=26, signal=9)

    defined = [i for i, r in enumerate(rows) if r["macd"] is not None]
    first_signal = defined[0] + 8

    assert rows[first_signal - 1]["signal"] is None
    assert rows[first_signal]["signal"] is not None
    assert rows[first_signal]["hist"] == pytest.approx(
        rows[first_signal]["macd"] - rows[first_signal]["signal"]
    )


def test_macd_hist_is_none_when_signal_is_none():
    rows = indicators.macd(_bars(list(range(1, 30))), fast=12, slow=26, signal=9)

    assert all(r["hist"] is None for r in rows if r["signal"] is None)


def test_indicators_do_not_mutate_input_bars():
    bars = _bars([1, 2, 3, 4, 5])
    snapshot = [dict(b) for b in bars]

    indicators.sma(bars, 3)
    indicators.ema(bars, 3)
    indicators.macd(bars, fast=2, slow=3, signal=2)

    assert bars == snapshot


def test_period_must_be_positive():
    with pytest.raises(ValueError):
        indicators.sma(_bars([1, 2, 3]), 0)
    with pytest.raises(ValueError):
        indicators.ema(_bars([1, 2, 3]), -1)
```

- [ ] **Step 2: 실패를 확인한다**

Run: `python -m pytest tests/test_indicators.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.indicators'`

- [ ] **Step 3: 최소 구현을 쓴다**

`src/indicators.py`:

```python
"""지표 엔진 — 순수 함수만 둔다.

상태도 I/O도 없으므로 테스트가 결정적이고, 오프라인 재생 검증과 실시간이
같은 코드를 탄다. 값이 설 수 없는 구간은 0이 아니라 None을 돌려준다 —
0으로 채우면 MACD 히스토그램의 부호 판정이 개장 직후 거짓 신호를 낸다.
"""


def _closes(bars: list[dict]) -> list[float]:
    return [float(b["close"]) for b in bars]


def sma(bars: list[dict], period: int) -> list[float | None]:
    """단순이동평균. 앞의 period-1개는 None."""
    if period <= 0:
        raise ValueError(f"period must be positive: {period}")
    closes = _closes(bars)
    out: list[float | None] = [None] * len(closes)
    running = 0.0
    for i, c in enumerate(closes):
        running += c
        if i >= period:
            running -= closes[i - period]
        if i >= period - 1:
            out[i] = running / period
    return out


def ema(bars: list[dict], period: int) -> list[float | None]:
    """지수이동평균. 첫 값은 SMA(period)로 시드한다."""
    if period <= 0:
        raise ValueError(f"period must be positive: {period}")
    closes = _closes(bars)
    out: list[float | None] = [None] * len(closes)
    if len(closes) < period:
        return out
    alpha = 2.0 / (period + 1)
    prev = sum(closes[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(closes)):
        prev = prev + alpha * (closes[i] - prev)
        out[i] = prev
    return out


def macd(
    bars: list[dict],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> list[dict]:
    """MACD선·시그널선·히스토그램.

    시그널선은 'MACD가 정의된 구간'만 모아 EMA를 걸고 원래 자리로 되돌린다.
    None을 0으로 채워 EMA에 넣으면 시그널선이 0 쪽으로 끌려간다.
    """
    fast_ema = ema(bars, fast)
    slow_ema = ema(bars, slow)
    macd_line: list[float | None] = [
        (f - s) if (f is not None and s is not None) else None
        for f, s in zip(fast_ema, slow_ema)
    ]

    defined_idx = [i for i, v in enumerate(macd_line) if v is not None]
    signal_line: list[float | None] = [None] * len(macd_line)
    if defined_idx:
        packed = [{"close": macd_line[i]} for i in defined_idx]
        for pos, value in enumerate(ema(packed, signal)):
            signal_line[defined_idx[pos]] = value

    rows = []
    for m, s in zip(macd_line, signal_line):
        hist = (m - s) if (m is not None and s is not None) else None
        rows.append({"macd": m, "signal": s, "hist": hist})
    return rows
```

- [ ] **Step 4: 통과를 확인한다**

Run: `python -m pytest tests/test_indicators.py -v`
Expected: PASS (9 passed)

- [ ] **Step 5: 기존 스위트가 그대로인지 확인한다**

Run: `python -m pytest -q`
Expected: 기존 테스트 전부 PASS, 실패 0

- [ ] **Step 6: 커밋**

```bash
git add src/indicators.py tests/test_indicators.py
git commit -m "feat(indicators): add pure sma/ema/macd for track B"
```

---

### Task 2: 분봉 API — `src/api/kis_minute_bars.py`

**Files:**
- Create: `src/api/kis_minute_bars.py`
- Test: `tests/test_kis_minute_bars.py`
- Read for reference: `scripts/kis_minute_bar_poc.py:39-46`, `:69-101`, `:177-196`, `:254-275`

**Interfaces:**
- Consumes: 없음
- Produces:
  - `MINUTE_PATH: str`, `MINUTE_TR: str`
  - `FORBIDDEN_START: datetime.time`, `FORBIDDEN_END: datetime.time`
  - `parse_minute_bars(response: dict) -> tuple[list[dict], dict]` — `({date,time,open,high,low,close,volume} 정렬 리스트, {"empty_bar": n, "field_missing": n})`
  - `in_forbidden_window(now: datetime) -> bool`
  - `async fetch_minute_bars(ticker: str, *, hour_cursor: str = "") -> dict`
  - `async fetch_day_bars(ticker: str, *, max_pages: int = 20) -> tuple[list[dict], dict]`
  - `MinuteBarError(Exception)`

POC의 `PocStop`·`CallBudget`·MFE 계산은 가져오지 않는다. 조사 스크립트 전용이다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_kis_minute_bars.py`:

```python
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.api import kis_minute_bars as mb

KST = ZoneInfo("Asia/Seoul")


def _row(time_, o, h, low, c, v):
    return {
        "stck_bsop_date": "20260827",
        "stck_cntg_hour": time_,
        "stck_oprc": str(o),
        "stck_hgpr": str(h),
        "stck_lwpr": str(low),
        "stck_prpr": str(c),
        "cntg_vol": str(v),
    }


def test_parse_sorts_bars_by_date_and_time():
    resp = {"output2": [_row("093500", 1, 2, 1, 2, 10), _row("093400", 1, 2, 1, 1, 5)]}

    bars, issues = mb.parse_minute_bars(resp)

    assert [b["time"] for b in bars] == ["093400", "093500"]
    assert issues == {"empty_bar": 0, "field_missing": 0}


def test_parse_counts_rows_with_missing_fields_and_excludes_them():
    resp = {"output2": [_row("093500", 1, 2, 1, 2, 10), {"stck_cntg_hour": "093600"}, {}]}

    bars, issues = mb.parse_minute_bars(resp)

    assert len(bars) == 1
    assert issues["field_missing"] == 1
    assert issues["empty_bar"] == 1


def test_parse_falls_back_to_output_key():
    bars, _ = mb.parse_minute_bars({"output": [_row("093500", 1, 2, 1, 2, 10)]})

    assert len(bars) == 1


def test_parse_raises_when_no_row_container_exists():
    with pytest.raises(mb.MinuteBarError):
        mb.parse_minute_bars({"rt_cd": "0"})


def test_forbidden_window_covers_0900_to_0911():
    assert mb.in_forbidden_window(datetime(2026, 8, 27, 9, 0, 0, tzinfo=KST))
    assert mb.in_forbidden_window(datetime(2026, 8, 27, 9, 10, 59, tzinfo=KST))
    assert not mb.in_forbidden_window(datetime(2026, 8, 27, 9, 11, 0, tzinfo=KST))
    assert not mb.in_forbidden_window(datetime(2026, 8, 27, 8, 59, 59, tzinfo=KST))


async def test_fetch_uses_background_priority_and_stops_on_rate_limit(monkeypatch):
    seen = {}

    async def fake_get(path, **kwargs):
        seen["path"] = path
        seen.update(kwargs)
        return {"rt_cd": "0", "output2": []}

    monkeypatch.setattr(mb.kis_rest, "get", fake_get)

    await mb.fetch_minute_bars("006340")

    assert seen["path"] == mb.MINUTE_PATH
    assert seen["tr_id"] == mb.MINUTE_TR
    assert seen["request_priority"] == mb.kis_rest.REQUEST_PRIORITY_BACKGROUND
    assert seen["stop_on_rate_limit"] is True
    assert seen["params"]["FID_INPUT_ISCD"] == "006340"


async def test_fetch_raises_on_nonzero_rt_cd(monkeypatch):
    async def fake_get(path, **kwargs):
        return {"rt_cd": "1", "msg_cd": "EGW00123"}

    monkeypatch.setattr(mb.kis_rest, "get", fake_get)

    with pytest.raises(mb.MinuteBarError):
        await mb.fetch_minute_bars("006340")


async def test_fetch_day_bars_stops_when_cursor_makes_no_progress(monkeypatch):
    calls = {"n": 0}

    async def fake_get(path, **kwargs):
        calls["n"] += 1
        return {"rt_cd": "0", "output2": [_row("093500", 1, 2, 1, 2, 10)]}

    monkeypatch.setattr(mb.kis_rest, "get", fake_get)

    bars, issues = await mb.fetch_day_bars("006340", max_pages=10)

    assert len(bars) == 1          # 중복 제거
    assert calls["n"] == 2         # 두 번째 페이지에서 새 봉 0 → 중단
```

- [ ] **Step 2: 실패를 확인한다**

Run: `python -m pytest tests/test_kis_minute_bars.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.api.kis_minute_bars'`

- [ ] **Step 3: 최소 구현을 쓴다**

`src/api/kis_minute_bars.py`:

```python
"""당일 분봉 API — scripts/kis_minute_bar_poc.py에서 승격.

트랙 B의 확정 봉 정정과 재시작 복구에 쓴다. 주문 경로 뒤에 서도록
BACKGROUND 우선순위를 쓰고, A의 진입 창(09:00~09:11)에는 호출하지 않는다.
"""

from datetime import datetime, time

from src.api import kis_rest

MINUTE_PATH = "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
MINUTE_TR = "FHKST03010200"

# A의 F1 선정(09:00)부터 F3 체결 마감(09:11)까지. B는 이 구간에 지표가 없어
# 잃는 것이 없고, PAPER 초당 1건 예산을 A의 진입과 다투면 안 된다.
FORBIDDEN_START = time(9, 0)
FORBIDDEN_END = time(9, 11)

_MAX_PAGES = 20


class MinuteBarError(Exception):
    """분봉 조회·파싱 실패. 호출부가 그날 정정을 건너뛰게 한다."""


def in_forbidden_window(now: datetime) -> bool:
    current = now.timetz().replace(tzinfo=None)
    return FORBIDDEN_START <= current < FORBIDDEN_END


def parse_minute_bars(response: dict) -> tuple[list[dict], dict]:
    """분봉 응답을 (정렬된 bar 리스트, 이슈 카운트)로 파싱한다.

    시각/OHLC 필드가 없는 봉은 추정하지 않고 이슈로 세고 제외한다.
    """
    rows = response.get("output2")
    if rows is None:
        rows = response.get("output")
    if not isinstance(rows, list):
        raise MinuteBarError("MINUTE_OUTPUT_MISSING")

    issues = {"empty_bar": 0, "field_missing": 0}
    bars: list[dict] = []
    for row in rows:
        if not isinstance(row, dict) or not row:
            issues["empty_bar"] += 1
            continue
        try:
            bars.append({
                "date": str(row["stck_bsop_date"]),
                "time": str(row["stck_cntg_hour"]),
                "open": float(row["stck_oprc"]),
                "high": float(row["stck_hgpr"]),
                "low": float(row["stck_lwpr"]),
                "close": float(row["stck_prpr"]),
                "volume": float(row.get("cntg_vol") or 0),
            })
        except (KeyError, TypeError, ValueError):
            issues["field_missing"] += 1
            continue

    bars.sort(key=lambda b: (b["date"], b["time"]))
    return bars, issues


async def fetch_minute_bars(ticker: str, *, hour_cursor: str = "") -> dict:
    """당일 분봉 한 페이지. 1페이지가 최근 약 30봉을 준다."""
    response = await kis_rest.get(
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
    )
    if str(response.get("rt_cd") or "") != "0":
        raise MinuteBarError(
            f"MINUTE_PRICE_FAILED msg_cd={response.get('msg_cd')!r}"
        )
    return response


async def fetch_day_bars(
    ticker: str,
    *,
    max_pages: int = _MAX_PAGES,
) -> tuple[list[dict], dict]:
    """시간 커서로 당일 분봉을 역방향 페이지네이션한다.

    새 봉이 0인 페이지에서 멈춘다 — 커서가 진전하지 않는다는 뜻이다.
    """
    bars: list[dict] = []
    seen: set[tuple[str, str]] = set()
    issues = {"empty_bar": 0, "field_missing": 0}
    cursor = ""

    for _ in range(max_pages):
        response = await fetch_minute_bars(ticker, hour_cursor=cursor)
        page, page_issues = parse_minute_bars(response)
        issues["empty_bar"] += page_issues["empty_bar"]
        issues["field_missing"] += page_issues["field_missing"]

        fresh = [b for b in page if (b["date"], b["time"]) not in seen]
        if not fresh:
            break
        for bar in fresh:
            seen.add((bar["date"], bar["time"]))
        bars.extend(fresh)
        cursor = min(b["time"] for b in fresh)

    bars.sort(key=lambda b: (b["date"], b["time"]))
    return bars, issues
```

- [ ] **Step 4: 통과를 확인한다**

Run: `python -m pytest tests/test_kis_minute_bars.py -v`
Expected: PASS (8 passed)

- [ ] **Step 5: 커밋**

```bash
git add src/api/kis_minute_bars.py tests/test_kis_minute_bars.py
git commit -m "feat(api): promote the minute bar client out of the POC script"
```

---

### Task 3: 틱 팬아웃 훅 — `tick_capture.register_tick_listener`

**Files:**
- Modify: `src/modules/tick_capture.py` (모듈 수준 `enqueue`, 파일 끝 근처 `:733`)
- Test: `tests/test_tick_fanout.py`

**Interfaces:**
- Consumes: 없음
- Produces:
  - `tick_capture.register_tick_listener(fn: Callable[[dict], None]) -> None`
  - `tick_capture.clear_tick_listeners() -> None` (테스트 전용)
  - 리스너는 f4가 만든 tick dict을 그대로 받는다 —
    `{"source_ts": str | None, "received_at": str, "price": float, "qty": int | None, "source": "ws" | "rest", "valid": bool, "ticker": str, "raw": list[str] | None}`

`src/modules/tick_capture.py`는 `_STRATEGY_FILES`에 없다 ([release.py:12](../../../src/release.py#L12)의 "관측 전용 모듈은 제외한다"). 이 수정은 A의 지문을 돌리지 않는다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_tick_fanout.py`:

```python
import pytest

from src.modules import tick_capture


@pytest.fixture(autouse=True)
def clean_listeners():
    tick_capture.clear_tick_listeners()
    yield
    tick_capture.clear_tick_listeners()


def _tick(ticker="006340", price=14570.0):
    return {
        "source_ts": "2026-08-27T09:35:00+09:00",
        "received_at": "2026-08-27T09:35:00.100000+09:00",
        "price": price,
        "qty": 100,
        "source": "ws",
        "valid": True,
        "ticker": ticker,
        "raw": ["006340", "093500", str(price)] + [""] * 43,
    }


def test_listener_receives_the_full_tick_dict():
    seen = []
    tick_capture.register_tick_listener(seen.append)

    tick_capture.enqueue(_tick())

    assert len(seen) == 1
    assert seen[0]["qty"] == 100
    assert len(seen[0]["raw"]) == 46


def test_listener_runs_even_when_no_capture_is_attached():
    # 캡처가 붙지 않은 날에도 트랙 B는 봉을 만들어야 한다.
    assert tick_capture._capture is None
    seen = []
    tick_capture.register_tick_listener(seen.append)

    tick_capture.enqueue(_tick())

    assert len(seen) == 1


def test_a_raising_listener_does_not_break_the_others_or_the_caller():
    seen = []

    def boom(_tick):
        raise RuntimeError("listener exploded")

    tick_capture.register_tick_listener(boom)
    tick_capture.register_tick_listener(seen.append)

    tick_capture.enqueue(_tick())   # 예외가 새어 나오면 실패

    assert len(seen) == 1


def test_clear_removes_every_listener():
    seen = []
    tick_capture.register_tick_listener(seen.append)
    tick_capture.clear_tick_listeners()

    tick_capture.enqueue(_tick())

    assert seen == []
```

- [ ] **Step 2: 실패를 확인한다**

Run: `python -m pytest tests/test_tick_fanout.py -v`
Expected: FAIL — `AttributeError: module 'src.modules.tick_capture' has no attribute 'clear_tick_listeners'`

- [ ] **Step 3: 최소 구현을 쓴다**

`src/modules/tick_capture.py`의 모듈 수준 `enqueue`(`:733`)를 다음으로 바꾼다.

```python
# 관측 팬아웃 — 트랙 B의 봉 집계기가 여기 붙는다. live.push_tick은 가격과
# 종목만 받아 OHLCV를 만들 수 없으므로(거래량 없음) 팬아웃은 여기에 둔다.
_tick_listeners: list = []


def register_tick_listener(fn) -> None:
    """틱 스트림 구독자를 등록한다. 중복 등록은 무시한다."""
    if fn not in _tick_listeners:
        _tick_listeners.append(fn)


def clear_tick_listeners() -> None:
    """테스트 전용 — 등록된 구독자를 모두 제거한다."""
    _tick_listeners.clear()


def enqueue(tick: dict) -> None:
    """논블로킹. 활성 캡처가 있고 종목이 일치할 때만 적재한다.

    구독자 호출을 캡처 활성 검사보다 **앞에** 둔다 — 캡처가 붙지 않은
    순간에도 트랙 B는 봉을 만들어야 한다.
    """
    for fn in _tick_listeners:
        try:
            fn(tick)
        except Exception:  # noqa: BLE001 — 구독자 실패가 캡처·주문 경로를 흔들면 안 된다
            pass
    cap = _capture
    if cap is not None and tick.get("ticker") in (None, cap.ticker):
        cap.enqueue(tick)
```

- [ ] **Step 4: 통과를 확인한다**

Run: `python -m pytest tests/test_tick_fanout.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: 기존 캡처 테스트가 무수정으로 통과하는지 확인한다**

Run: `python -m pytest tests/test_tick_capture.py tests/test_tick_capture_target_switch.py tests/test_f4_capture_wiring.py -v`
Expected: 전부 PASS, 파일 수정 없음

- [ ] **Step 6: 전략 지문이 돌지 않았는지 확인한다**

```bash
python -c "
from src.release import strategy_fingerprint
print(strategy_fingerprint())
"
```

Expected: `39cf806f8eac` — 오늘 DB의 `experiment_registry.strategy_fingerprint`와 같아야 한다. 다르면 `_STRATEGY_FILES`에 있는 파일을 건드린 것이므로 되돌린다.

- [ ] **Step 7: 커밋**

```bash
git add src/modules/tick_capture.py tests/test_tick_fanout.py
git commit -m "feat(capture): fan out ticks to registered observers"
```

---

### Task 4: 봉 집계 — `src/bars.py`

**Files:**
- Create: `src/bars.py`
- Test: `tests/test_bars.py`

**Interfaces:**
- Consumes: `tick_capture.register_tick_listener` (Task 3), `SpikeFilter` (기존 `src/utils/spike_filter.py`)
- Produces:
  - `on_tick(tick: dict) -> None` — 논블로킹, 어떤 예외도 전파하지 않는다
  - `install() -> None` — `tick_capture`에 자신을 등록한다 (idempotent)
  - `drain() -> None` — 큐를 비우며 봉을 갱신한다. 워커가 부르고, 테스트가 직접 부른다
  - `series(date: str, ticker: str) -> list[dict]` — 확정·미확정 봉 전부, 시각 오름차순
  - `reset() -> None` — 테스트 전용
  - `bars_path(date: str, ticker: str) -> pathlib.Path`
  - 봉 dict 형식:
    ```python
    {"date": "20260827", "time": "093500",
     "open": 14570.0, "high": 15180.0, "low": 14410.0, "close": 15080.0,
     "volume": 1194689.0,
     "confirmed": False,          # 분봉 API 정정 전
     "tick_count": 812, "spike_dropped": 3,
     "tick_derived": {"cttr": 121.4, "askp1": 15090.0, "bidp1": 15080.0,
                      "total_askp_rsqn": 48210.0, "total_bidp_rsqn": 51330.0,
                      "vol_by_ccld": {"1": 720401.0, "5": 474288.0},
                      "corrected": False}}
    ```

`vol_by_ccld`는 체결구분 코드를 **해석하지 않고** 코드별 체결량을 그대로 쌓는다. 코드 의미(매수/매도 주도)가 확인되지 않았으므로 여기서 단정하지 않는다.

**`tick_derived.corrected`는 항상 `false`다.** 분봉 API는 OHLCV만 주므로 이 값들은 정정 대상이 아니다 (스펙 §6.1).

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_bars.py`:

```python
import json

import pytest

from src import bars
from src.modules import tick_capture

_RAW_LEN = 46


def _raw(price, *, cttr="120.5", ccld="1", askp1="15090", bidp1="15080",
         total_ask="48210", total_bid="51330"):
    fields = [""] * _RAW_LEN
    fields[0] = "006340"
    fields[2] = str(price)
    fields[10] = askp1
    fields[11] = bidp1
    fields[18] = cttr
    fields[21] = ccld
    fields[38] = total_ask
    fields[39] = total_bid
    return fields


def _tick(price, *, minute="0935", second="00", qty=100, ticker="006340", **raw_kw):
    ts = f"2026-08-27T09:{minute[2:]}:{second}+09:00"
    return {
        "source_ts": ts,
        "received_at": ts,
        "price": float(price),
        "qty": qty,
        "source": "ws",
        "valid": True,
        "ticker": ticker,
        "raw": _raw(price, **raw_kw),
    }


@pytest.fixture(autouse=True)
def isolated_bars(tmp_path, monkeypatch):
    monkeypatch.setattr(bars, "_BARS_DIR", tmp_path)
    tick_capture.clear_tick_listeners()
    bars.reset()
    yield
    bars.reset()
    tick_capture.clear_tick_listeners()


def test_ohlc_comes_from_the_ticks_in_that_minute():
    for price in (14570, 15180, 14410, 15080):
        bars.on_tick(_tick(price, qty=10))
    bars.drain()

    rows = bars.series("20260827", "006340")

    assert len(rows) == 1
    assert rows[0]["open"] == 14570.0
    assert rows[0]["high"] == 15180.0
    assert rows[0]["low"] == 14410.0
    assert rows[0]["close"] == 15080.0
    assert rows[0]["volume"] == 40.0
    assert rows[0]["tick_count"] == 4


def test_a_new_minute_opens_a_new_bar():
    bars.on_tick(_tick(14570, minute="0935"))
    bars.on_tick(_tick(15080, minute="0936"))
    bars.drain()

    rows = bars.series("20260827", "006340")

    assert [r["time"] for r in rows] == ["093500", "093600"]


def test_tick_derived_values_are_read_from_the_raw_frame():
    bars.on_tick(_tick(14570, qty=30, ccld="1"))
    bars.on_tick(_tick(14600, qty=20, ccld="5", cttr="131.2"))
    bars.drain()

    derived = bars.series("20260827", "006340")[0]["tick_derived"]

    assert derived["cttr"] == 131.2          # 봉 구간 마지막 값
    assert derived["askp1"] == 15090.0
    assert derived["total_bidp_rsqn"] == 51330.0
    assert derived["vol_by_ccld"] == {"1": 30.0, "5": 20.0}
    assert derived["corrected"] is False


def test_spike_ticks_are_dropped_from_the_bar_but_counted():
    bars.on_tick(_tick(14570))
    bars.on_tick(_tick(30000))          # +105% 단일 틱 → 스파이크
    bars.drain()

    row = bars.series("20260827", "006340")[0]

    assert row["high"] == 14570.0
    assert row["spike_dropped"] == 1
    assert row["tick_count"] == 1


def test_track_b_uses_its_own_spike_filter_instance():
    a_filter = bars._spike_filter_for("006340")
    other = bars._spike_filter_for("005930")

    assert a_filter is not other
    assert bars._spike_filter_for("006340") is a_filter


def test_a_closed_bar_is_written_through_to_disk():
    bars.on_tick(_tick(14570, minute="0935"))
    bars.on_tick(_tick(15080, minute="0936"))
    bars.drain()

    path = bars.bars_path("20260827", "006340")
    written = json.loads(path.read_text(encoding="utf-8"))

    assert [b["time"] for b in written] == ["093500", "093600"]
    assert written[0]["confirmed"] is False


def test_a_ticker_switch_starts_a_separate_series():
    bars.on_tick(_tick(14570, ticker="006340"))
    bars.on_tick(_tick(70000, ticker="005930"))
    bars.drain()

    assert len(bars.series("20260827", "006340")) == 1
    assert len(bars.series("20260827", "005930")) == 1


def test_on_tick_never_raises_on_a_malformed_tick():
    bars.on_tick({"garbage": True})
    bars.on_tick({"ticker": "006340", "price": "not-a-number", "raw": None})
    bars.drain()          # 예외가 새어 나오면 실패

    assert bars.series("20260827", "006340") == []


def test_a_tick_without_a_valid_source_ts_falls_back_to_received_at():
    tick = _tick(14570)
    tick["source_ts"] = None
    tick["valid"] = False
    bars.on_tick(tick)
    bars.drain()

    assert len(bars.series("20260827", "006340")) == 1


def test_install_registers_exactly_one_listener():
    bars.install()
    bars.install()

    assert tick_capture._tick_listeners.count(bars.on_tick) == 1
```

- [ ] **Step 2: 실패를 확인한다**

Run: `python -m pytest tests/test_bars.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.bars'`

- [ ] **Step 3: 최소 구현을 쓴다**

`src/bars.py`:

```python
"""트랙 B 봉 집계 — 틱 스트림에서 1분 OHLCV를 만든다.

`src/modules/`가 아니라 최상위에 두는 이유는 CODING_GUIDELINES §2의
"modules/ 코드는 api/를 직접 import하지 않는다" 규칙 때문이다. live.py와
같은 층위의 관측 인프라로 본다.

기동·종료 배선이 없다. 첫 틱이 그 (날짜, 종목)의 계열을 시작하고, 봉이
닫힐 때마다 파일로 write-through 한다 — main.py를 건드리지 않기 위해서다
(main.py는 _STRATEGY_FILES에 있어 수정하면 트랙 A의 지문이 돈다).
"""

import json
import os
from collections import deque
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src.modules import tick_capture
from src.utils.logger import log
from src.utils.spike_filter import SpikeFilter

KST = ZoneInfo("Asia/Seoul")

_BARS_DIR = Path(os.getenv("TRACK_B_BARS_DIR", "data/bars"))

# 모스펙 §11.1이 확정한 H0STCNT0 필드 배치
_IDX_CTTR = 18            # 체결강도
_IDX_CCLD_DVSN = 21       # 체결구분 (코드 의미는 해석하지 않는다)
_IDX_ASKP1 = 10           # 최우선 매도호가
_IDX_BIDP1 = 11           # 최우선 매수호가
_IDX_TOTAL_ASKP = 38      # 총 매도호가잔량
_IDX_TOTAL_BIDP = 39      # 총 매수호가잔량

_queue: deque[dict] = deque()
_series: dict[tuple[str, str], dict[str, dict]] = {}
_filters: dict[str, SpikeFilter] = {}
_dirty: set[tuple[str, str]] = set()


def bars_path(date: str, ticker: str) -> Path:
    return _BARS_DIR / f"{date}_{ticker}.json"


def reset() -> None:
    """테스트 전용 — 모든 인메모리 상태를 비운다."""
    _queue.clear()
    _series.clear()
    _filters.clear()
    _dirty.clear()


def install() -> None:
    """틱 스트림에 자신을 등록한다. 여러 번 불러도 한 번만 붙는다."""
    tick_capture.register_tick_listener(on_tick)


def on_tick(tick: dict) -> None:
    """논블로킹. 어떤 예외도 전파하지 않는다(주문 경로 격리).

    A의 손절 판정 경로에서 트랙 B가 하는 일은 이 append 하나뿐이다.
    봉 확정·정정·지표는 전부 drain()에서, 별도 태스크로 돈다.
    """
    try:
        _queue.append(tick)
    except Exception:  # noqa: BLE001 — 관측 실패가 호출부를 흔들면 안 된다
        pass


def _spike_filter_for(ticker: str) -> SpikeFilter:
    """트랙 B 전용 인스턴스. A의 필터를 공유하면 내부 상태가 오염된다."""
    flt = _filters.get(ticker)
    if flt is None:
        flt = SpikeFilter()
        _filters[ticker] = flt
    return flt


def _minute_of(tick: dict) -> datetime | None:
    raw_ts = tick.get("source_ts") if tick.get("valid") else None
    for candidate in (raw_ts, tick.get("received_at")):
        if not candidate:
            continue
        try:
            return datetime.fromisoformat(str(candidate)).astimezone(KST)
        except (TypeError, ValueError):
            continue
    return None


def _float_at(raw, index: int) -> float | None:
    try:
        return float(raw[index])
    except (TypeError, ValueError, IndexError):
        return None


def _new_bar(when: datetime, price: float) -> dict:
    return {
        "date": when.strftime("%Y%m%d"),
        "time": when.strftime("%H%M%S"),
        "open": price, "high": price, "low": price, "close": price,
        "volume": 0.0,
        "confirmed": False,
        "tick_count": 0,
        "spike_dropped": 0,
        "tick_derived": {
            "cttr": None, "askp1": None, "bidp1": None,
            "total_askp_rsqn": None, "total_bidp_rsqn": None,
            "vol_by_ccld": {},
            # 분봉 API는 OHLCV만 준다. 틱 파생값은 정정 대상이 아니다.
            "corrected": False,
        },
    }


def _apply(bar: dict, tick: dict, price: float) -> None:
    bar["high"] = max(bar["high"], price)
    bar["low"] = min(bar["low"], price)
    bar["close"] = price
    bar["tick_count"] += 1

    qty = tick.get("qty")
    volume = float(qty) if isinstance(qty, (int, float)) else 0.0
    bar["volume"] += volume

    raw = tick.get("raw")
    if not isinstance(raw, list):
        return
    derived = bar["tick_derived"]
    for key, index in (
        ("cttr", _IDX_CTTR), ("askp1", _IDX_ASKP1), ("bidp1", _IDX_BIDP1),
        ("total_askp_rsqn", _IDX_TOTAL_ASKP), ("total_bidp_rsqn", _IDX_TOTAL_BIDP),
    ):
        value = _float_at(raw, index)
        if value is not None:
            derived[key] = value        # 봉 구간 마지막 값
    try:
        code = str(raw[_IDX_CCLD_DVSN]).strip()
    except IndexError:
        code = ""
    if code:
        derived["vol_by_ccld"][code] = derived["vol_by_ccld"].get(code, 0.0) + volume


def drain() -> None:
    """큐를 비우며 봉을 갱신하고, 바뀐 계열을 디스크에 쓴다."""
    while _queue:
        tick = _queue.popleft()
        try:
            _consume(tick)
        except Exception as exc:  # noqa: BLE001 — 개별 틱 실패 격리
            log("TRACK_B_BAR_TICK_ERROR", level="WARN", error=repr(exc))
    _flush()


def _consume(tick: dict) -> None:
    ticker = tick.get("ticker")
    if not ticker:
        return
    when = _minute_of(tick)
    if when is None:
        return
    try:
        price = float(tick["price"])
    except (KeyError, TypeError, ValueError):
        return
    if price <= 0:
        return

    key = (when.strftime("%Y%m%d"), str(ticker))
    minute = when.strftime("%H%M%S")[:4] + "00"
    minutes = _series.setdefault(key, {})
    bar = minutes.get(minute)

    if not _spike_filter_for(str(ticker)).is_valid(price, str(ticker)):
        if bar is not None:
            bar["spike_dropped"] += 1
            _dirty.add(key)
        return

    if bar is None:
        bar = _new_bar(when.replace(second=0, microsecond=0), price)
        minutes[minute] = bar
    _apply(bar, tick, price)
    _dirty.add(key)


def series(date: str, ticker: str) -> list[dict]:
    minutes = _series.get((date, ticker), {})
    return [minutes[m] for m in sorted(minutes)]


def _flush() -> None:
    while _dirty:
        date, ticker = _dirty.pop()
        rows = series(date, ticker)
        if not rows:
            continue
        path = bars_path(date, ticker)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(rows, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001 — 쓰기 실패가 집계를 멈추면 안 된다
            log(
                "TRACK_B_BAR_WRITE_ERROR", level="WARN",
                ticker=ticker, date=date, error=repr(exc),
            )
```

- [ ] **Step 4: 통과를 확인한다**

Run: `python -m pytest tests/test_bars.py -v`
Expected: PASS (10 passed)

- [ ] **Step 5: 기존 스위트가 그대로인지 확인한다**

Run: `python -m pytest -q`
Expected: 실패 0

- [ ] **Step 6: 커밋**

```bash
git add src/bars.py tests/test_bars.py
git commit -m "feat(bars): aggregate ticks into one-minute OHLCV for track B"
```

---

### Task 5: 확정 봉 정정과 유량 가드

**Files:**
- Modify: `src/bars.py`
- Test: `tests/test_bars_correction.py`

**Interfaces:**
- Consumes: `kis_minute_bars.fetch_minute_bars`·`in_forbidden_window`·`MinuteBarError` (Task 2), `bars.series`·`bars.drain` (Task 4)
- Produces:
  - `async correct_once(date: str, ticker: str, *, now: datetime | None = None) -> int` — 정정한 봉 개수. 건너뛰면 `0`
  - `should_correct(now: datetime, *, a_holding: bool, ws_stale: bool) -> bool`
  - `async worker(date: str, ticker: str) -> None` — 1분 주기로 `drain()`과 `correct_once()`를 돈다
  - `ensure_worker(date: str, ticker: str) -> None` — 실행 중인 루프가 있으면 지연 생성 (idempotent)
  - 정정된 봉은 `confirmed=True`, `tick_derived.corrected`는 그대로 `False`

정정이 도착하면 OHLCV를 **대체**한다. 틱 파생값·`tick_count`·`spike_dropped`는 보존한다 — 분봉 API가 주지 않는 값이고, "이 봉에 이상치가 몇 개 있었나"는 사후 분석의 자료다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_bars_correction.py`:

```python
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src import bars
from src.api import kis_minute_bars as mb
from src.modules import tick_capture

KST = ZoneInfo("Asia/Seoul")


def _tick(price, minute="0935", qty=10):
    ts = f"2026-08-27T09:{minute[2:]}:00+09:00"
    raw = [""] * 46
    raw[0], raw[2], raw[18] = "006340", str(price), "120.5"
    return {
        "source_ts": ts, "received_at": ts, "price": float(price),
        "qty": qty, "source": "ws", "valid": True, "ticker": "006340", "raw": raw,
    }


@pytest.fixture(autouse=True)
def isolated_bars(tmp_path, monkeypatch):
    monkeypatch.setattr(bars, "_BARS_DIR", tmp_path)
    tick_capture.clear_tick_listeners()
    bars.reset()
    yield
    bars.reset()
    tick_capture.clear_tick_listeners()


def _official(time_, o, h, low, c, v):
    return {
        "stck_bsop_date": "20260827", "stck_cntg_hour": time_,
        "stck_oprc": str(o), "stck_hgpr": str(h), "stck_lwpr": str(low),
        "stck_prpr": str(c), "cntg_vol": str(v),
    }


def test_no_correction_inside_the_0900_0911_window():
    now = datetime(2026, 8, 27, 9, 5, tzinfo=KST)

    assert bars.should_correct(now, a_holding=False, ws_stale=False) is False


def test_no_correction_while_a_holds_and_the_socket_is_stale():
    now = datetime(2026, 8, 27, 10, 0, tzinfo=KST)

    assert bars.should_correct(now, a_holding=True, ws_stale=True) is False
    assert bars.should_correct(now, a_holding=True, ws_stale=False) is True
    assert bars.should_correct(now, a_holding=False, ws_stale=True) is True


async def test_correction_replaces_ohlcv_and_marks_the_bar_confirmed(monkeypatch):
    bars.on_tick(_tick(14570, qty=10))
    bars.drain()

    async def fake_fetch(ticker, *, hour_cursor=""):
        return {"rt_cd": "0", "output2": [_official("093500", 14500, 15200, 14400, 15100, 900)]}

    monkeypatch.setattr(bars.kis_minute_bars, "fetch_minute_bars", fake_fetch)

    corrected = await bars.correct_once(
        "20260827", "006340", now=datetime(2026, 8, 27, 9, 40, tzinfo=KST)
    )

    row = bars.series("20260827", "006340")[0]
    assert corrected == 1
    assert (row["open"], row["high"], row["low"], row["close"]) == (14500.0, 15200.0, 14400.0, 15100.0)
    assert row["volume"] == 900.0
    assert row["confirmed"] is True


async def test_correction_preserves_tick_derived_and_counters(monkeypatch):
    bars.on_tick(_tick(14570, qty=10))
    bars.on_tick(_tick(14580, qty=10))
    bars.drain()

    async def fake_fetch(ticker, *, hour_cursor=""):
        return {"rt_cd": "0", "output2": [_official("093500", 14500, 15200, 14400, 15100, 900)]}

    monkeypatch.setattr(bars.kis_minute_bars, "fetch_minute_bars", fake_fetch)
    await bars.correct_once("20260827", "006340", now=datetime(2026, 8, 27, 9, 40, tzinfo=KST))

    row = bars.series("20260827", "006340")[0]
    assert row["tick_count"] == 2
    assert row["tick_derived"]["cttr"] == 120.5
    assert row["tick_derived"]["corrected"] is False


async def test_correction_creates_bars_the_tick_stream_missed(monkeypatch):
    bars.on_tick(_tick(14570, minute="0935"))
    bars.drain()

    async def fake_fetch(ticker, *, hour_cursor=""):
        return {"rt_cd": "0", "output2": [
            _official("093500", 14500, 15200, 14400, 15100, 900),
            _official("093600", 15100, 15300, 15000, 15250, 400),
        ]}

    monkeypatch.setattr(bars.kis_minute_bars, "fetch_minute_bars", fake_fetch)
    await bars.correct_once("20260827", "006340", now=datetime(2026, 8, 27, 9, 40, tzinfo=KST))

    rows = bars.series("20260827", "006340")
    assert [r["time"] for r in rows] == ["093500", "093600"]
    assert rows[1]["tick_derived"] is None      # 틱이 없던 봉 — 파생값 없음
    assert rows[1]["confirmed"] is True


async def test_a_failed_fetch_leaves_the_bars_untouched(monkeypatch):
    bars.on_tick(_tick(14570))
    bars.drain()

    async def boom(ticker, *, hour_cursor=""):
        raise mb.MinuteBarError("MINUTE_PRICE_FAILED")

    monkeypatch.setattr(bars.kis_minute_bars, "fetch_minute_bars", boom)

    corrected = await bars.correct_once(
        "20260827", "006340", now=datetime(2026, 8, 27, 9, 40, tzinfo=KST)
    )

    assert corrected == 0
    assert bars.series("20260827", "006340")[0]["confirmed"] is False


async def test_correction_is_skipped_inside_the_forbidden_window(monkeypatch):
    called = {"n": 0}

    async def counting_fetch(ticker, *, hour_cursor=""):
        called["n"] += 1
        return {"rt_cd": "0", "output2": []}

    monkeypatch.setattr(bars.kis_minute_bars, "fetch_minute_bars", counting_fetch)

    await bars.correct_once(
        "20260827", "006340", now=datetime(2026, 8, 27, 9, 5, tzinfo=KST)
    )

    assert called["n"] == 0
```

- [ ] **Step 2: 실패를 확인한다**

Run: `python -m pytest tests/test_bars_correction.py -v`
Expected: FAIL — `AttributeError: module 'src.bars' has no attribute 'should_correct'`

- [ ] **Step 3: 최소 구현을 쓴다**

`src/bars.py` 상단 import에 추가:

```python
import asyncio

from src.api import kis_minute_bars
from src import live, state
```

파일 끝에 추가:

```python
_CORRECT_INTERVAL_SEC = 60.0
_IDLE_STOP_SEC = 600.0
_workers: dict[tuple[str, str], asyncio.Task] = {}


def should_correct(now: datetime, *, a_holding: bool, ws_stale: bool) -> bool:
    """분봉 API를 지금 호출해도 되는가.

    두 창에서 막는다. 09:00~09:11은 A의 진입 창이고, A가 보유 중인데 WS가
    끊긴 구간은 A의 REST 백업이 PAPER 초당 1건 예산을 쓰고 있는 때다.
    """
    if kis_minute_bars.in_forbidden_window(now):
        return False
    return not (a_holding and ws_stale)


def _merge_official(bar: dict | None, official: dict) -> dict:
    """공식 분봉으로 OHLCV를 대체한다. 틱 파생값과 카운터는 보존한다."""
    if bar is None:
        bar = {
            "date": official["date"], "time": official["time"],
            "tick_count": 0, "spike_dropped": 0,
            # 분봉 API는 틱 파생값을 주지 않는다. 없음을 없음으로 남긴다.
            "tick_derived": None,
        }
    bar["open"] = official["open"]
    bar["high"] = official["high"]
    bar["low"] = official["low"]
    bar["close"] = official["close"]
    bar["volume"] = official["volume"]
    bar["confirmed"] = True
    return bar


async def correct_once(
    date: str,
    ticker: str,
    *,
    now: datetime | None = None,
) -> int:
    """공식 분봉 한 페이지로 당일 봉을 정정한다. 정정한 봉 수를 돌려준다."""
    when = now or datetime.now(KST)
    a_holding = state.get().position_status == "HOLDING"
    if not should_correct(when, a_holding=a_holding, ws_stale=not live.ws_connected):
        return 0

    try:
        response = await kis_minute_bars.fetch_minute_bars(ticker)
        official, issues = kis_minute_bars.parse_minute_bars(response)
    except kis_minute_bars.MinuteBarError as exc:
        log("TRACK_B_CORRECTION_FAILED", level="WARN", ticker=ticker, error=repr(exc))
        return 0
    except Exception as exc:  # noqa: BLE001 — 정정 실패가 집계를 멈추면 안 된다
        log("TRACK_B_CORRECTION_ERROR", level="WARN", ticker=ticker, error=repr(exc))
        return 0

    minutes = _series.setdefault((date, ticker), {})
    corrected = 0
    for row in official:
        if row["date"] != date:
            continue
        minute = row["time"][:4] + "00"
        minutes[minute] = _merge_official(minutes.get(minute), {**row, "time": minute})
        corrected += 1

    if corrected:
        _dirty.add((date, ticker))
        _flush()
        log(
            "TRACK_B_BARS_CORRECTED", level="INFO",
            ticker=ticker, date=date, corrected=corrected, issues=issues,
        )
    return corrected


async def worker(date: str, ticker: str) -> None:
    """1분 주기로 봉을 확정하고 정정한다. 틱이 끊기면 스스로 끝난다."""
    idle = 0.0
    try:
        while idle < _IDLE_STOP_SEC:
            await asyncio.sleep(_CORRECT_INTERVAL_SEC)
            pending = len(_queue)
            drain()
            idle = 0.0 if pending else idle + _CORRECT_INTERVAL_SEC
            await correct_once(date, ticker)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        log("TRACK_B_WORKER_DEAD", level="CRIT", ticker=ticker, error=repr(exc))
    finally:
        # 마지막 배출도 실패할 수 있다. 워커 종료 경로에서 예외를 올리면
        # 태스크가 unretrieved exception으로 남는다.
        try:
            drain()
        except Exception:  # noqa: BLE001
            pass
        _workers.pop((date, ticker), None)


def ensure_worker(date: str, ticker: str) -> None:
    """실행 중인 이벤트 루프가 있으면 워커를 지연 생성한다.

    main.py에 기동 배선을 넣지 않기 위해서다 — main.py는 _STRATEGY_FILES에
    있어 수정하면 트랙 A의 전략 지문이 돈다.
    """
    key = (date, ticker)
    task = _workers.get(key)
    if task is not None and not task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _workers[key] = loop.create_task(worker(date, ticker), name=f"track_b_bars_{ticker}")
```

`_consume()`의 마지막 줄 `_dirty.add(key)` 바로 앞에 워커 지연 생성을 넣는다.

```python
    _apply(bar, tick, price)
    ensure_worker(key[0], key[1])
    _dirty.add(key)
```

`reset()`에 워커 정리를 넣는다.

```python
def reset() -> None:
    """테스트 전용 — 모든 인메모리 상태를 비운다."""
    for task in _workers.values():
        task.cancel()
    _workers.clear()
    _queue.clear()
    _series.clear()
    _filters.clear()
    _dirty.clear()
```

- [ ] **Step 4: 통과를 확인한다**

Run: `python -m pytest tests/test_bars_correction.py tests/test_bars.py -v`
Expected: PASS (17 passed)

- [ ] **Step 5: 기존 스위트가 그대로인지 확인한다**

Run: `python -m pytest -q`
Expected: 실패 0

- [ ] **Step 6: 커밋**

```bash
git add src/bars.py tests/test_bars_correction.py
git commit -m "feat(bars): correct aggregated bars with the official minute API"
```

---

### Task 6: 재시작 복구

**Files:**
- Modify: `src/bars.py`
- Test: `tests/test_bars_restore.py`

**Interfaces:**
- Consumes: `kis_minute_bars.fetch_day_bars` (Task 2), `bars.should_correct` (Task 5)
- Produces:
  - `async restore_day(date: str, ticker: str, *, now: datetime | None = None) -> int` — 복원한 봉 개수
  - 복원된 봉은 `confirmed=True`, `tick_derived=None`, `tick_count=0`

봉은 인메모리 누적이라 장중 재시작하면 그날 봉이 통째로 사라진다. MACD는 26봉을 요구하므로 복구가 없으면 재시작한 날의 B가 26분간 눈이 먼다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_bars_restore.py`:

```python
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src import bars
from src.api import kis_minute_bars as mb
from src.modules import tick_capture

KST = ZoneInfo("Asia/Seoul")


@pytest.fixture(autouse=True)
def isolated_bars(tmp_path, monkeypatch):
    monkeypatch.setattr(bars, "_BARS_DIR", tmp_path)
    tick_capture.clear_tick_listeners()
    bars.reset()
    yield
    bars.reset()
    tick_capture.clear_tick_listeners()


def _day_bars(count):
    return [
        {
            "date": "20260827", "time": f"09{m:02d}00",
            "open": 14500.0 + m, "high": 14600.0 + m,
            "low": 14400.0 + m, "close": 14550.0 + m, "volume": 100.0 + m,
        }
        for m in range(count)
    ]


async def test_restore_fills_the_day_from_the_official_api(monkeypatch):
    async def fake_day(ticker, *, max_pages=20):
        return _day_bars(30), {"empty_bar": 0, "field_missing": 0}

    monkeypatch.setattr(bars.kis_minute_bars, "fetch_day_bars", fake_day)

    restored = await bars.restore_day(
        "20260827", "006340", now=datetime(2026, 8, 27, 10, 30, tzinfo=KST)
    )

    rows = bars.series("20260827", "006340")
    assert restored == 30
    assert len(rows) == 30
    assert all(r["confirmed"] is True for r in rows)
    assert all(r["tick_derived"] is None for r in rows)
    assert all(r["tick_count"] == 0 for r in rows)


async def test_restore_is_deferred_inside_the_forbidden_window(monkeypatch):
    called = {"n": 0}

    async def counting_day(ticker, *, max_pages=20):
        called["n"] += 1
        return _day_bars(3), {"empty_bar": 0, "field_missing": 0}

    monkeypatch.setattr(bars.kis_minute_bars, "fetch_day_bars", counting_day)

    restored = await bars.restore_day(
        "20260827", "006340", now=datetime(2026, 8, 27, 9, 5, tzinfo=KST)
    )

    assert restored == 0
    assert called["n"] == 0


async def test_restore_does_not_clobber_bars_that_already_have_ticks(monkeypatch):
    ts = "2026-08-27T09:35:00+09:00"
    raw = [""] * 46
    raw[0], raw[2], raw[18] = "006340", "15080", "133.7"
    bars.on_tick({
        "source_ts": ts, "received_at": ts, "price": 15080.0, "qty": 55,
        "source": "ws", "valid": True, "ticker": "006340", "raw": raw,
    })
    bars.drain()

    async def fake_day(ticker, *, max_pages=20):
        return [{
            "date": "20260827", "time": "093500",
            "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 9.0,
        }], {"empty_bar": 0, "field_missing": 0}

    monkeypatch.setattr(bars.kis_minute_bars, "fetch_day_bars", fake_day)
    await bars.restore_day(
        "20260827", "006340", now=datetime(2026, 8, 27, 10, 30, tzinfo=KST)
    )

    row = bars.series("20260827", "006340")[0]
    assert row["tick_derived"]["cttr"] == 133.7     # 틱 파생값 보존
    assert row["tick_count"] == 1
    assert row["close"] == 1.5                      # OHLCV는 공식값으로 정정


async def test_a_failed_restore_returns_zero_and_does_not_raise(monkeypatch):
    async def boom(ticker, *, max_pages=20):
        raise mb.MinuteBarError("MINUTE_OUTPUT_MISSING")

    monkeypatch.setattr(bars.kis_minute_bars, "fetch_day_bars", boom)

    restored = await bars.restore_day(
        "20260827", "006340", now=datetime(2026, 8, 27, 10, 30, tzinfo=KST)
    )

    assert restored == 0
```

- [ ] **Step 2: 실패를 확인한다**

Run: `python -m pytest tests/test_bars_restore.py -v`
Expected: FAIL — `AttributeError: module 'src.bars' has no attribute 'restore_day'`

- [ ] **Step 3: 최소 구현을 쓴다**

`src/bars.py` 파일 끝에 추가:

```python
async def restore_day(
    date: str,
    ticker: str,
    *,
    now: datetime | None = None,
) -> int:
    """장중 재시작 후 당일 봉을 공식 분봉으로 복원한다.

    봉은 인메모리 누적이라 재시작하면 그날 봉이 사라진다. MACD가 26봉을
    요구하므로(모스펙 §7.1) 복원이 없으면 재시작한 날의 B는 26분간 눈이 먼다.

    복원된 봉의 틱 파생값은 None이다 — 분봉 API가 주지 않는다. 이 값을 쓰는
    규칙을 나중에 만들면 그때 복원 구간을 신호 대상에서 제외해야 한다.
    """
    when = now or datetime.now(KST)
    a_holding = state.get().position_status == "HOLDING"
    if not should_correct(when, a_holding=a_holding, ws_stale=not live.ws_connected):
        log("TRACK_B_RESTORE_DEFERRED", level="INFO", ticker=ticker, date=date)
        return 0

    try:
        official, issues = await kis_minute_bars.fetch_day_bars(ticker)
    except kis_minute_bars.MinuteBarError as exc:
        log("TRACK_B_RESTORE_FAILED", level="WARN", ticker=ticker, error=repr(exc))
        return 0
    except Exception as exc:  # noqa: BLE001
        log("TRACK_B_RESTORE_ERROR", level="WARN", ticker=ticker, error=repr(exc))
        return 0

    minutes = _series.setdefault((date, ticker), {})
    restored = 0
    for row in official:
        if row["date"] != date:
            continue
        minute = row["time"][:4] + "00"
        minutes[minute] = _merge_official(minutes.get(minute), {**row, "time": minute})
        restored += 1

    if restored:
        _dirty.add((date, ticker))
        _flush()
        log(
            "TRACK_B_BARS_RESTORED", level="INFO",
            ticker=ticker, date=date, restored=restored, issues=issues,
        )
    return restored
```

- [ ] **Step 4: 통과를 확인한다**

Run: `python -m pytest tests/test_bars_restore.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: 커밋**

```bash
git add src/bars.py tests/test_bars_restore.py
git commit -m "feat(bars): restore the day's bars after an intraday restart"
```

---

### Task 7: `GET /api/bars`

**Files:**
- Modify: `src/api/server.py` (`@app.get("/api/history")` 정의 바로 앞, `:954` 부근)
- Test: `tests/test_api_bars.py`

**Interfaces:**
- Consumes: `bars.series`·`bars.bars_path` (Task 4), `indicators.sma`·`indicators.macd` (Task 1)
- Produces:
  - `GET /api/bars?track=B&date=YYYYMMDD&ticker=006340&sma=20&fast=12&slow=26&signal=9`
  - 응답:
    ```json
    {"date": "20260827", "ticker": "006340", "track": "B",
     "bars": [...], "indicators": {"sma": [...], "macd": [...]},
     "meta": {"bar_count": 31, "confirmed_count": 30,
              "spike_dropped": 4, "tick_derived_missing": 1, "source": "memory"}}
    ```
  - `date`·`ticker` 생략 시 오늘 날짜와 `state.get().target_ticker`를 쓴다
  - 인메모리 계열이 비어 있으면 `data/bars/`의 파일에서 읽는다 (`meta.source="file"`)

**지표는 서버에서 계산한다.** 전략 판정과 차트가 같은 함수를 타야 "차트는 매수 신호인데 봇은 안 샀다"는 혼란이 없다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_api_bars.py`:

```python
import json

import pytest
from fastapi.testclient import TestClient

from src import bars
from src.api.server import app
from src.modules import tick_capture

client = TestClient(app)


@pytest.fixture(autouse=True)
def isolated_bars(tmp_path, monkeypatch):
    monkeypatch.setattr(bars, "_BARS_DIR", tmp_path)
    tick_capture.clear_tick_listeners()
    bars.reset()
    yield
    bars.reset()
    tick_capture.clear_tick_listeners()


def _row(minute, close, *, confirmed=True, derived=True):
    return {
        "date": "20260827", "time": f"09{minute:02d}00",
        "open": close - 5, "high": close + 10, "low": close - 10, "close": close,
        "volume": 100.0, "confirmed": confirmed,
        "tick_count": 12, "spike_dropped": 1 if minute == 0 else 0,
        "tick_derived": ({"cttr": 120.0, "askp1": None, "bidp1": None,
                          "total_askp_rsqn": None, "total_bidp_rsqn": None,
                          "vol_by_ccld": {}, "corrected": False} if derived else None),
    }


def test_bars_endpoint_returns_bars_indicators_and_meta(tmp_path):
    rows = [_row(m, 14500 + m * 10) for m in range(30)]
    (tmp_path / "20260827_006340.json").write_text(json.dumps(rows), encoding="utf-8")

    res = client.get("/api/bars", params={"date": "20260827", "ticker": "006340"})
    body = res.json()

    assert res.status_code == 200
    assert body["ticker"] == "006340"
    assert body["track"] == "B"
    assert len(body["bars"]) == 30
    assert len(body["indicators"]["sma"]) == 30
    assert len(body["indicators"]["macd"]) == 30
    assert body["meta"]["bar_count"] == 30
    assert body["meta"]["source"] == "file"


def test_indicator_arrays_align_with_the_bar_array():
    for m in range(30):
        bars._series.setdefault(("20260827", "006340"), {})[f"09{m:02d}00"] = _row(m, 14500 + m * 10)

    body = client.get("/api/bars", params={"date": "20260827", "ticker": "006340"}).json()

    assert body["indicators"]["sma"][:19] == [None] * 19
    assert body["indicators"]["sma"][19] is not None
    assert body["meta"]["source"] == "memory"


def test_meta_counts_unconfirmed_bars_and_missing_tick_derived():
    minutes = bars._series.setdefault(("20260827", "006340"), {})
    minutes["090000"] = _row(0, 14500, confirmed=True)
    minutes["090100"] = _row(1, 14510, confirmed=False)
    minutes["090200"] = _row(2, 14520, confirmed=True, derived=False)

    body = client.get("/api/bars", params={"date": "20260827", "ticker": "006340"}).json()

    assert body["meta"]["bar_count"] == 3
    assert body["meta"]["confirmed_count"] == 2
    assert body["meta"]["tick_derived_missing"] == 1
    assert body["meta"]["spike_dropped"] == 1


def test_unknown_series_returns_empty_arrays_not_an_error():
    res = client.get("/api/bars", params={"date": "20991231", "ticker": "000000"})
    body = res.json()

    assert res.status_code == 200
    assert body["bars"] == []
    assert body["indicators"]["sma"] == []
    assert body["meta"]["bar_count"] == 0


def test_indicator_periods_are_configurable():
    for m in range(10):
        bars._series.setdefault(("20260827", "006340"), {})[f"09{m:02d}00"] = _row(m, 14500 + m * 10)

    body = client.get(
        "/api/bars",
        params={"date": "20260827", "ticker": "006340", "sma": 3, "fast": 2, "slow": 4, "signal": 2},
    ).json()

    assert body["indicators"]["sma"][2] is not None
    assert body["indicators"]["macd"][3]["macd"] is not None
```

- [ ] **Step 2: 실패를 확인한다**

Run: `python -m pytest tests/test_api_bars.py -v`
Expected: FAIL — 404 (엔드포인트 없음)

- [ ] **Step 3: 최소 구현을 쓴다**

`src/api/server.py` import 블록에 추가:

```python
from src import bars, indicators
```

`@app.get("/api/history")` 정의 바로 앞에 추가:

```python
@app.get("/api/bars")
async def api_bars(
    track: str = "B",
    date: str = "",
    ticker: str = "",
    sma: int = 20,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> JSONResponse:
    """트랙 B의 1분 봉과 지표.

    지표를 브라우저가 아니라 여기서 계산한다 — 전략 판정과 차트가 같은 순수
    함수를 타야 "차트는 신호인데 봇은 안 샀다"는 혼란이 없다.
    """
    trade_date = date or datetime.now(KST).strftime("%Y%m%d")
    target = ticker or (state.get().target_ticker or "")

    rows = bars.series(trade_date, target) if target else []
    source = "memory"
    if not rows and target:
        path = bars.bars_path(trade_date, target)
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
            source = "file"
        except (OSError, ValueError):
            rows = []

    return JSONResponse({
        "date": trade_date,
        "ticker": target,
        "track": track,
        "bars": rows,
        "indicators": {
            "sma": indicators.sma(rows, sma) if rows else [],
            "macd": indicators.macd(rows, fast, slow, signal) if rows else [],
        },
        "meta": {
            "bar_count": len(rows),
            "confirmed_count": sum(1 for r in rows if r.get("confirmed")),
            "spike_dropped": sum(int(r.get("spike_dropped") or 0) for r in rows),
            "tick_derived_missing": sum(1 for r in rows if r.get("tick_derived") is None),
            "sma_period": sma,
            "macd": {"fast": fast, "slow": slow, "signal": signal},
            "source": source,
        },
    })
```

`json`·`datetime`·`KST`·`state`가 이미 import 되어 있는지 확인하고, 없으면 추가한다.

- [ ] **Step 4: 통과를 확인한다**

Run: `python -m pytest tests/test_api_bars.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: 기존 API 테스트가 그대로인지 확인한다**

Run: `python -m pytest tests/test_api_server.py tests/test_api_track_scope.py -q`
Expected: 실패 0

- [ ] **Step 6: 커밋**

```bash
git add src/api/server.py tests/test_api_bars.py
git commit -m "feat(api): serve track B bars with server-computed indicators"
```

---

### Task 8: 차트 순수 헬퍼 — `bars_chart.js`

**Files:**
- Create: `docs/html/assets/bars_chart.js`
- Create: `tests/js/bars_chart_checks.js`

**Interfaces:**
- Consumes: `/api/bars` 응답 (Task 7)
- Produces (전부 `function` 선언 — Node 하네스가 이름으로 추출한다):
  - `barsPriceDomain(bars, smaSeries)` → `{min, max}`
  - `barsMacdDomain(macdRows)` → `{min, max}`
  - `barsTimeIndex(bars, i, chartW, padLeft)` → `number` (봉 중심 x좌표)
  - `barsCandleWidth(count, chartW)` → `number`
  - `barsYAt(value, domain, padTop, chartH)` → `number`

`tests/js/price_flow_checks.js`와 같은 추출·eval 방식으로 검증한다. 이 파일이 무수정으로 통과하는 것이 "`drawPriceFlow`를 건드리지 않았다"의 회귀 방지선이다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/js/bars_chart_checks.js`:

```javascript
// 봉·지표 차트 순수 로직 검증 — bars_chart.js에서 함수를 추출해 실제 실행한다.
// 실행: node tests\js\bars_chart_checks.js
const fs = require('fs');
const path = require('path');
const src = fs.readFileSync(
  path.join(__dirname, '..', '..', 'docs', 'html', 'assets', 'bars_chart.js'), 'utf8');

function extract(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`${name} not found`);
  let depth = 0;
  const open = src.indexOf('{', start);
  for (let j = open; j < src.length; j++) {
    if (src[j] === '{') depth++;
    else if (src[j] === '}') { depth--; if (depth === 0) return src.slice(start, j + 1); }
  }
  throw new Error(`${name} unbalanced`);
}

eval(extract('barsPriceDomain'));
eval(extract('barsMacdDomain'));
eval(extract('barsTimeIndex'));
eval(extract('barsCandleWidth'));
eval(extract('barsYAt'));

let failures = 0;
const check = (label, cond) => {
  if (!cond) { console.error(`FAIL ${label}`); failures++; }
  else console.log(`ok   ${label}`);
};
const near = (a, b, eps = 1e-6) => Math.abs(a - b) < eps;

const BARS = [
  {high: 110, low: 90, open: 95, close: 105},
  {high: 130, low: 100, open: 105, close: 125},
  {high: 120, low: 80, open: 125, close: 85},
];

// 가격 도메인은 고가·저가를 모두 담고 이동평균선도 담는다
{
  const d = barsPriceDomain(BARS, [null, null, 150]);
  check('price domain covers every low', d.min <= 80);
  check('price domain covers the sma above every high', d.max >= 150);
}

// 이동평균이 전부 null이어도 도메인이 성립한다
{
  const d = barsPriceDomain(BARS, [null, null, null]);
  check('null sma does not poison the domain', d.min <= 80 && d.max >= 130);
}

// 봉이 없으면 도메인이 무너지지 않는다
{
  const d = barsPriceDomain([], []);
  check('empty bars give a finite domain', Number.isFinite(d.min) && Number.isFinite(d.max));
  check('empty domain is not inverted', d.max > d.min);
}

// 모든 값이 같아도 도메인이 0폭이 되지 않는다 (0으로 나누기 방지)
{
  const flat = [{high: 100, low: 100, open: 100, close: 100}];
  const d = barsPriceDomain(flat, [100]);
  check('flat bars get a padded domain', d.max > d.min);
}

// MACD 도메인은 0을 반드시 포함한다 — 0 기준선이 화면 밖으로 나가면 안 된다
{
  const d = barsMacdDomain([{macd: 5, signal: 4, hist: 1}, {macd: 8, signal: 6, hist: 2}]);
  check('macd domain includes zero', d.min <= 0 && d.max >= 8);
}
{
  const d = barsMacdDomain([{macd: -5, signal: -4, hist: -1}]);
  check('negative macd domain still includes zero', d.min <= -5 && d.max >= 0);
}
{
  const d = barsMacdDomain([{macd: null, signal: null, hist: null}]);
  check('all-null macd gives a finite domain', Number.isFinite(d.min) && Number.isFinite(d.max));
}

// x 좌표는 봉 중심이고 좌에서 우로 단조 증가한다
{
  const xs = [0, 1, 2].map(i => barsTimeIndex(BARS, i, 300, 40));
  check('x is monotonically increasing', xs[0] < xs[1] && xs[1] < xs[2]);
  check('first bar sits inside the chart area', xs[0] > 40);
  check('last bar stays inside the chart area', xs[2] < 340);
}

// 캔들 폭은 양수이고 봉이 많아질수록 좁아진다
{
  const wide = barsCandleWidth(10, 300);
  const narrow = barsCandleWidth(200, 300);
  check('candle width is positive', wide > 0 && narrow > 0);
  check('more bars means narrower candles', narrow < wide);
  check('candle width never collapses to zero', barsCandleWidth(5000, 300) >= 1);
}

// y 매핑은 뒤집혀 있다 — 큰 값이 위(작은 y)
{
  const domain = {min: 0, max: 100};
  check('max maps to the top', near(barsYAt(100, domain, 10, 200), 10));
  check('min maps to the bottom', near(barsYAt(0, domain, 10, 200), 210));
  check('mid maps to the middle', near(barsYAt(50, domain, 10, 200), 110));
}

// 0폭 도메인이 들어와도 NaN을 내지 않는다
{
  const y = barsYAt(5, {min: 5, max: 5}, 10, 200);
  check('zero-width domain does not produce NaN', Number.isFinite(y));
}

if (failures) { console.error(`\n${failures} check(s) failed`); process.exit(1); }
console.log('\nall bars_chart checks passed');
```

- [ ] **Step 2: 실패를 확인한다**

Run: `node tests/js/bars_chart_checks.js`
Expected: FAIL — `ENOENT: no such file or directory ... bars_chart.js`

- [ ] **Step 3: 최소 구현을 쓴다**

`docs/html/assets/bars_chart.js`:

```javascript
// 트랙 B 봉·지표 차트.
// app.js의 drawPriceFlow(트랙 A 틱 차트)는 한 줄도 건드리지 않는다 — 요구사항이
// 다르다(원시 틱 vs 1분 봉, 20분 창 vs 60분+, 1패널 vs 2패널).
// 아래 bars* 함수들은 순수 함수이며 tests/js/bars_chart_checks.js가 Node에서
// 이름으로 추출해 실행한다. 외부 의존을 넣지 말 것.

const BARS_MIN_CANDLE_PX = 1;
const BARS_DOMAIN_PAD = 0.02;

function barsPriceDomain(bars, smaSeries) {
  let min = Infinity;
  let max = -Infinity;
  for (const b of (bars || [])) {
    if (Number.isFinite(b.low)) min = Math.min(min, b.low);
    if (Number.isFinite(b.high)) max = Math.max(max, b.high);
  }
  for (const v of (smaSeries || [])) {
    if (Number.isFinite(v)) { min = Math.min(min, v); max = Math.max(max, v); }
  }
  if (!Number.isFinite(min) || !Number.isFinite(max)) return {min: 0, max: 1};
  if (max === min) { const pad = Math.abs(max) * BARS_DOMAIN_PAD || 1; return {min: min - pad, max: max + pad}; }
  const pad = (max - min) * BARS_DOMAIN_PAD;
  return {min: min - pad, max: max + pad};
}

function barsMacdDomain(macdRows) {
  // 0을 반드시 담는다 — 0 기준선이 화면 밖으로 나가면 히스토그램 부호를 못 읽는다.
  let min = 0;
  let max = 0;
  for (const r of (macdRows || [])) {
    for (const key of ['macd', 'signal', 'hist']) {
      const v = r ? r[key] : null;
      if (Number.isFinite(v)) { min = Math.min(min, v); max = Math.max(max, v); }
    }
  }
  if (max === min) return {min: min - 1, max: max + 1};
  const pad = (max - min) * BARS_DOMAIN_PAD;
  return {min: min - pad, max: max + pad};
}

function barsCandleWidth(count, chartW) {
  const n = Math.max(1, count || 1);
  const slot = chartW / n;
  return Math.max(BARS_MIN_CANDLE_PX, slot * 0.7);
}

function barsTimeIndex(bars, i, chartW, padLeft) {
  const n = Math.max(1, (bars || []).length);
  const slot = chartW / n;
  return padLeft + slot * (i + 0.5);
}

function barsYAt(value, domain, padTop, chartH) {
  const span = (domain.max - domain.min) || 1;
  const ratio = (value - domain.min) / span;
  return padTop + chartH * (1 - ratio);
}
```

- [ ] **Step 4: 통과를 확인한다**

Run: `node tests/js/bars_chart_checks.js`
Expected: `all bars_chart checks passed`

- [ ] **Step 5: A의 차트 테스트가 무수정으로 통과하는지 확인한다**

Run: `node tests/js/price_flow_checks.js`
Expected: 전부 통과, 파일 수정 없음

- [ ] **Step 6: 커밋**

```bash
git add docs/html/assets/bars_chart.js tests/js/bars_chart_checks.js
git commit -m "feat(ui): add pure scale helpers for the track B bar chart"
```

---

### Task 9: 차트 드로잉과 화면 배선

**Files:**
- Modify: `docs/html/assets/bars_chart.js` (드로잉 추가)
- Modify: `docs/html/index.html` (`:148` `<canvas id="price-flow">` 다음)
- Modify: `docs/html/assets/app.css`

**Interfaces:**
- Consumes: Task 8의 순수 헬퍼, `/api/bars` (Task 7)
- Produces:
  - `drawBarsChart(payload)` — `{bars, indicators, meta}`를 받아 두 캔버스를 그린다
  - `refreshBarsChart()` — `/api/bars`를 호출하고 `drawBarsChart`를 부른다
  - DOM: `<canvas id="bars-price">`, `<canvas id="bars-macd">`, `<div id="bars-sub">`

**1단계에는 마커가 없다.** 그림자 진입·청산 마커는 신호가 있어야 찍히므로 2단계다.

- [ ] **Step 1: 마크업을 넣는다**

`docs/html/index.html`의 `<canvas id="price-flow" width="760" height="180"></canvas>` 다음 줄에 추가:

```html
          <div class="bars-chart">
            <div class="bars-sub" id="bars-sub">봉 수집 대기</div>
            <canvas id="bars-price" width="760" height="240"></canvas>
            <canvas id="bars-macd" width="760" height="120"></canvas>
          </div>
```

`</body>` 앞 `<script src="assets/app.js?v=...">` **다음** 줄에 추가:

```html
<script src="assets/bars_chart.js?v=20260827-track-b"></script>
```

- [ ] **Step 2: 스타일을 넣는다**

`docs/html/assets/app.css` 끝에 추가:

```css
/* 트랙 B 봉·지표 차트 — 트랙 A 틱 차트 아래에 세로로 쌓는다 */
.bars-chart { margin-top: 10px; }
.bars-chart canvas { width: 100%; display: block; }
#bars-macd { margin-top: 4px; }
.bars-sub { font-size: 11px; color: #787b86; margin-bottom: 4px; }
```

- [ ] **Step 3: 드로잉을 구현한다**

`docs/html/assets/bars_chart.js` 끝에 추가:

```javascript
// ── 드로잉 ────────────────────────────────────────────────────────────
// 아래는 캔버스에 의존하므로 Node 하네스가 추출하지 않는다.

const BARS_UP = '#ef5350';
const BARS_DOWN = '#1e88e5';
const BARS_SMA = '#f7a600';
const BARS_SIGNAL = '#9b59b6';
const BARS_PAD = {l: 52, r: 14, t: 12, b: 22};

function barsResize(canvas) {
  const ratio = window.devicePixelRatio || 1;
  const displayW = Math.max(320, Math.round(canvas.clientWidth || canvas.width));
  const displayH = Math.max(80, Math.round(canvas.clientHeight || canvas.height));
  const pw = Math.round(displayW * ratio);
  const ph = Math.round(displayH * ratio);
  if (canvas.width !== pw || canvas.height !== ph) { canvas.width = pw; canvas.height = ph; }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  return {ctx, W: displayW, H: displayH};
}

function barsGrid(ctx, W, H, chartH) {
  ctx.strokeStyle = 'rgba(120,123,134,.18)';
  ctx.lineWidth = 1;
  for (let i = 0; i < 4; i++) {
    const y = BARS_PAD.t + chartH * i / 3;
    ctx.beginPath(); ctx.moveTo(BARS_PAD.l, y); ctx.lineTo(W - BARS_PAD.r, y); ctx.stroke();
  }
}

function drawBarsPricePanel(payload) {
  const canvas = document.getElementById('bars-price');
  if (!canvas) return;
  const {ctx, W, H} = barsResize(canvas);
  ctx.clearRect(0, 0, W, H);
  const chartW = W - BARS_PAD.l - BARS_PAD.r;
  const chartH = H - BARS_PAD.t - BARS_PAD.b;
  const rows = payload.bars || [];
  const smaSeries = (payload.indicators && payload.indicators.sma) || [];
  const domain = barsPriceDomain(rows, smaSeries);
  barsGrid(ctx, W, H, chartH);

  const width = barsCandleWidth(rows.length, chartW);
  rows.forEach((bar, i) => {
    const x = barsTimeIndex(rows, i, chartW, BARS_PAD.l);
    const up = bar.close >= bar.open;
    // 미확정 봉은 흐리게 — 분봉 API 정정이 오면 값이 바뀐다는 표시다.
    ctx.globalAlpha = bar.confirmed ? 1 : 0.4;
    ctx.strokeStyle = up ? BARS_UP : BARS_DOWN;
    ctx.fillStyle = up ? BARS_UP : BARS_DOWN;
    ctx.beginPath();
    ctx.moveTo(x, barsYAt(bar.high, domain, BARS_PAD.t, chartH));
    ctx.lineTo(x, barsYAt(bar.low, domain, BARS_PAD.t, chartH));
    ctx.stroke();
    const yOpen = barsYAt(bar.open, domain, BARS_PAD.t, chartH);
    const yClose = barsYAt(bar.close, domain, BARS_PAD.t, chartH);
    ctx.fillRect(x - width / 2, Math.min(yOpen, yClose), width, Math.max(1, Math.abs(yClose - yOpen)));
    ctx.globalAlpha = 1;
  });

  ctx.strokeStyle = BARS_SMA;
  ctx.beginPath();
  let started = false;
  smaSeries.forEach((value, i) => {
    if (!Number.isFinite(value)) { started = false; return; }
    const x = barsTimeIndex(rows, i, chartW, BARS_PAD.l);
    const y = barsYAt(value, domain, BARS_PAD.t, chartH);
    if (started) ctx.lineTo(x, y); else { ctx.moveTo(x, y); started = true; }
  });
  ctx.stroke();

  ctx.fillStyle = '#787b86';
  ctx.font = '10px Noto Sans KR,sans-serif';
  ctx.textAlign = 'right';
  ctx.fillText(Math.round(domain.max).toLocaleString(), BARS_PAD.l - 6, BARS_PAD.t + 8);
  ctx.fillText(Math.round(domain.min).toLocaleString(), BARS_PAD.l - 6, BARS_PAD.t + chartH);
}

function drawBarsMacdPanel(payload) {
  const canvas = document.getElementById('bars-macd');
  if (!canvas) return;
  const {ctx, W, H} = barsResize(canvas);
  ctx.clearRect(0, 0, W, H);
  const chartW = W - BARS_PAD.l - BARS_PAD.r;
  const chartH = H - BARS_PAD.t - BARS_PAD.b;
  const rows = payload.bars || [];
  const macdRows = (payload.indicators && payload.indicators.macd) || [];
  const domain = barsMacdDomain(macdRows);
  barsGrid(ctx, W, H, chartH);

  const zeroY = barsYAt(0, domain, BARS_PAD.t, chartH);
  ctx.strokeStyle = 'rgba(120,123,134,.45)';
  ctx.beginPath(); ctx.moveTo(BARS_PAD.l, zeroY); ctx.lineTo(W - BARS_PAD.r, zeroY); ctx.stroke();

  const width = barsCandleWidth(macdRows.length, chartW);
  macdRows.forEach((row, i) => {
    if (!row || !Number.isFinite(row.hist)) return;
    const x = barsTimeIndex(rows, i, chartW, BARS_PAD.l);
    const y = barsYAt(row.hist, domain, BARS_PAD.t, chartH);
    ctx.fillStyle = row.hist >= 0 ? BARS_UP : BARS_DOWN;
    ctx.fillRect(x - width / 2, Math.min(y, zeroY), width, Math.max(1, Math.abs(zeroY - y)));
  });

  for (const [key, color] of [['macd', BARS_SMA], ['signal', BARS_SIGNAL]]) {
    ctx.strokeStyle = color;
    ctx.beginPath();
    let started = false;
    macdRows.forEach((row, i) => {
      const value = row ? row[key] : null;
      if (!Number.isFinite(value)) { started = false; return; }
      const x = barsTimeIndex(rows, i, chartW, BARS_PAD.l);
      const y = barsYAt(value, domain, BARS_PAD.t, chartH);
      if (started) ctx.lineTo(x, y); else { ctx.moveTo(x, y); started = true; }
    });
    ctx.stroke();
  }
}

function drawBarsChart(payload) {
  drawBarsPricePanel(payload);
  drawBarsMacdPanel(payload);
  const sub = document.getElementById('bars-sub');
  if (!sub) return;
  const meta = payload.meta || {};
  if (!meta.bar_count) { sub.textContent = '봉 수집 대기'; return; }
  const unconfirmed = meta.bar_count - (meta.confirmed_count || 0);
  sub.textContent =
    `${payload.ticker || '-'} · ${meta.bar_count}봉 (미확정 ${unconfirmed})` +
    ` · 스파이크 ${meta.spike_dropped || 0} · 출처 ${meta.source || '-'}`;
}

async function refreshBarsChart() {
  try {
    const res = await fetch('/api/bars?track=B');
    if (!res.ok) return;
    drawBarsChart(await res.json());
  } catch (e) {
    // 차트 갱신 실패가 나머지 화면을 흔들면 안 된다
  }
}

document.addEventListener('DOMContentLoaded', () => {
  refreshBarsChart();
  setInterval(refreshBarsChart, 30000);
  window.addEventListener('resize', () => refreshBarsChart());
});
```

- [ ] **Step 4: 순수 헬퍼 테스트가 여전히 통과하는지 확인한다**

Run: `node tests/js/bars_chart_checks.js`
Expected: `all bars_chart checks passed` — 드로잉 코드를 더해도 추출 대상 함수는 그대로여야 한다

- [ ] **Step 5: A의 차트 테스트가 무수정으로 통과하는지 확인한다**

Run: `node tests/js/price_flow_checks.js`
Expected: 전부 통과

- [ ] **Step 6: 실제 분봉으로 눈으로 확인한다**

`data/backtest_bars/`의 실제 분봉을 `data/bars/`로 복사해 서버를 띄우고 화면을 본다.

```bash
mkdir -p data/bars
python -c "
import json, pathlib
src = json.loads(pathlib.Path('data/backtest_bars/20260727_006340.json').read_text())
for i, b in enumerate(src):
    b['confirmed'] = i < len(src) - 1     # 마지막 봉만 미확정으로 표시
    b['tick_count'] = 0
    b['spike_dropped'] = 0
    b['tick_derived'] = None
pathlib.Path('data/bars').mkdir(parents=True, exist_ok=True)
pathlib.Path('data/bars/20260727_006340.json').write_text(json.dumps(src), encoding='utf-8')
print(len(src), 'bars staged')
"
```

서버를 띄우고 `http://127.0.0.1:8080/?` 에서 `/api/bars?date=20260727&ticker=006340`을 확인한 뒤 화면을 본다.

확인할 것 넷이다.

1. 캔들이 그려지고 상승·하락 색이 구분된다
2. 마지막 봉이 흐리게 보인다 (미확정)
3. MACD 패널의 0 기준선이 화면 안에 있다
4. 봉이 31개뿐이라 SMA(20)은 20번째부터, MACD선은 26번째부터 그려진다

- [ ] **Step 7: 스테이징 파일을 지우고 커밋**

```bash
rm -f data/bars/20260727_006340.json
git add docs/html/assets/bars_chart.js docs/html/index.html docs/html/assets/app.css
git commit -m "feat(ui): draw the track B candle and MACD panels"
```

---

### Task 10: A 무간섭 측정

**Files:**
- Create: `tests/test_a_noninterference.py`

**Interfaces:**
- Consumes: `bars.install`·`bars.on_tick` (Task 4), `tick_capture.enqueue` (Task 3)
- Produces: 없음 (회귀 방지선)

프로덕션 계측을 넣으면 F4를 건드리게 되고 그 순간 "기존 스위트 무수정 통과" 기준이 깨진다. 그래서 **오프라인에서 결정적으로** 잰다.

1단계에서 A의 DB 쓰기 지연 측정이 빠지는 이유는 **1단계에 B의 DB 쓰기가 아예 없기 때문**이다. 이것도 테스트로 못박는다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_a_noninterference.py`:

```python
import time

import pytest

from src import bars, release
from src.modules import tick_capture


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(bars, "_BARS_DIR", tmp_path)
    tick_capture.clear_tick_listeners()
    bars.reset()
    yield
    bars.reset()
    tick_capture.clear_tick_listeners()


def _tick(i):
    ts = f"2026-08-27T09:35:{i % 60:02d}+09:00"
    raw = [""] * 46
    raw[0], raw[2] = "006340", str(14500 + (i % 50))
    return {
        "source_ts": ts, "received_at": ts, "price": 14500.0 + (i % 50),
        "qty": 10, "source": "ws", "valid": True, "ticker": "006340", "raw": raw,
    }


def _elapsed(ticks):
    started = time.perf_counter()
    for tick in ticks:
        tick_capture.enqueue(tick)
    return time.perf_counter() - started


def test_track_b_files_are_not_in_the_strategy_fingerprint():
    for name in (
        "src/bars.py",
        "src/indicators.py",
        "src/api/kis_minute_bars.py",
        "src/modules/tick_capture.py",
        "src/api/server.py",
    ):
        assert name not in release._STRATEGY_FILES


def test_the_listener_adds_no_measurable_cost_to_the_tick_path():
    ticks = [_tick(i) for i in range(20_000)]

    baseline = min(_elapsed(ticks) for _ in range(3))
    bars.install()
    with_listener = min(_elapsed(ticks) for _ in range(3))

    # 동기 경로에 더해지는 일은 deque.append 하나다. 3배는 매우 느슨한 상한이고,
    # 여기서 걸린다면 팬아웃 지점에서 집계·지표를 돌리고 있다는 뜻이다.
    assert with_listener < baseline * 3 + 0.05


def test_stage_one_never_opens_a_database_connection():
    from src import db

    assert db._conn is None          # 이 테스트는 db.init()을 부르지 않는다

    bars.install()
    for i in range(200):
        tick_capture.enqueue(_tick(i))
    bars.drain()

    # 봉 집계가 DB를 건드렸다면 aiosqlite 커넥션이 열렸을 것이다.
    assert db._conn is None


def test_the_bars_module_does_not_import_db():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(bars))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported.add(f"{node.module}.{alias.name}")

    assert "src.db" not in imported
    assert not any(name.endswith(".db") for name in imported)
```

- [ ] **Step 2: 실패를 확인한다**

Run: `python -m pytest tests/test_a_noninterference.py -v`
Expected: Task 1~9가 끝났다면 대부분 통과한다. `_STRATEGY_FILES`에 트랙 B 파일이 들어갔거나 팬아웃 지점에서 무거운 일을 하면 실패한다.

- [ ] **Step 3: 실패하면 원인을 고친다**

| 실패 | 원인 | 조치 |
|---|---|---|
| 지문 목록 테스트 | `_STRATEGY_FILES`에 B 파일을 넣었다 | 되돌린다. B는 `_TRACK_B_FILES`(2단계) |
| 지연 테스트 | 팬아웃 지점에서 집계·지표를 돌린다 | `on_tick`을 `deque.append` 하나로 되돌린다 |
| DB 테스트 | `bars.py`가 `db`를 import 한다 | 1단계는 파일에만 쓴다 |

- [ ] **Step 4: 전체 스위트를 돌린다**

Run: `python -m pytest -q && node tests/js/price_flow_checks.js && node tests/js/bars_chart_checks.js`
Expected: 파이썬 실패 0, JS 둘 다 통과

- [ ] **Step 5: 전략 지문이 1단계 시작 시점과 같은지 확인한다**

```bash
python -c "from src.release import strategy_fingerprint; print(strategy_fingerprint())"
```

Expected: `39cf806f8eac` — Task 3 Step 6에서 기록한 값과 **정확히 같아야 한다.** 다르면 `_STRATEGY_FILES`의 파일을 건드린 것이므로 `git diff`로 찾아 되돌린다.

- [ ] **Step 6: 커밋**

```bash
git add tests/test_a_noninterference.py
git commit -m "test: lock the track A non-interference guarantees for stage 1"
```

---

### Task 11: 1단계 마감 — 스펙 갱신

**Files:**
- Modify: `docs/superpowers/specs/2026-08-27-track-b-shadow-design.md`

**Interfaces:**
- Consumes: Task 0~10의 결과
- Produces: 없음

- [ ] **Step 1: 구현 상태 절을 추가한다**

스펙 §16(미결 사항) 앞에 `## 17. 1단계 구현 상태`를 넣고 커밋별 대응표를 쓴다. 모스펙의 §12 형식을 따른다 — 커밋 해시, 내용, 관련 절.

- [ ] **Step 2: 실장 미검증 항목을 적는다**

테스트로만 검증된 것과 장중 로그에서 확인해야 하는 것을 나눈다. 최소한 다음 넷이다.

- 09:11 이후 `TRACK_B_BARS_CORRECTED`가 1분에 한 번 찍히는가
- 09:00~09:11 구간에 분봉 API 호출 로그가 없는가
- A가 HOLDING이고 WS가 stale인 구간에 정정이 건너뛰어지는가
- `data/bars/YYYYMMDD_TICKER.json`이 실제로 매일 생기는가

- [ ] **Step 3: 2단계 착수 조건을 적는다**

v0 규칙의 숫자를 1단계 차트를 보고 정한다(§16). 며칠을 볼지는 미리 정하지 않는다.

- [ ] **Step 4: 커밋**

```bash
git add docs/superpowers/specs/2026-08-27-track-b-shadow-design.md
git commit -m "docs: record the stage 1 implementation status"
```

---

## Self-Review

**스펙 커버리지**

| 스펙 절 | 태스크 |
|---|---|
| §3.1 팬아웃 훅 | Task 3 |
| §3.2 지문 격리 | Task 3 Step 6, Task 10 |
| §4 모듈 경계 | Task 1·2·4·8 |
| §5 데이터 흐름 | Task 3·4·5 |
| §6.1 집계·틱 파생값 | Task 4 |
| §6.2 확정 봉 정정 | Task 5 |
| §6.3 유량 가드 | Task 5 |
| §6.4 B 전용 SpikeFilter | Task 4 (`test_track_b_uses_its_own_spike_filter_instance`) |
| §6.5 재시작 복구 | Task 6 |
| §7 지표 엔진 | Task 1 |
| §9.1~9.2 화면·렌더링 | Task 9 |
| §9.3 `/api/bars` | Task 7 |
| §9.4 확정/미확정 구분 | Task 9 Step 3·6 |
| §9.5 `app.js` 무수정 | Task 8 Step 5, Task 9 Step 5 |
| §9.6 마커 없음 | Task 9 (마커 코드 없음) |
| §9.7 실제 분봉으로 검증 | Task 9 Step 6 |
| §12.1 손절 앞 격리 | Task 3·4, Task 10 |
| §12.2 워커 격리 | Task 5 (`worker`의 CRIT 로깅) |
| §12.3 DB 쓰기 | Task 10 (1단계는 DB를 안 쓴다) |
| §13 수용 기준 | Task 10 |
| §15 선행 작업 | Task 0 |

**범위 밖 확인** — §8(신호 엔진 v0)·§10(예산)·§11(`shadow_trades`)·§12.4(트랙 상태)는 2단계다. 이 계획에 태스크가 없는 것이 맞다.

**검토에서 고친 것 둘**

1. §12.2의 "워커가 죽어도 A는 산다"에 전용 테스트가 없었다. 워커는 `asyncio.sleep(60)`으로 도는 무한 루프라 결정적으로 테스트하려면 주기 상수를 monkeypatch 해야 한다. Task 5에 아래 테스트를 추가하고, `worker()`의 `finally` 절 `drain()`을 try/except로 감쌌다 — 마지막 배출이 실패하면 태스크가 unretrieved exception으로 남는다.
2. Task 10의 "1단계는 DB를 안 쓴다" 테스트가 아무것도 검증하지 않는 껍데기였다. `db._conn`이 `None`으로 남는지 보는 것과 `bars` 모듈의 import를 AST로 훑는 것으로 바꿨다.

Task 5 구현 시 다음 테스트를 `tests/test_bars_correction.py`에 함께 넣는다.

```python
async def test_worker_logs_and_exits_without_propagating(monkeypatch):
    monkeypatch.setattr(bars, "_CORRECT_INTERVAL_SEC", 0.01)
    monkeypatch.setattr(bars, "_IDLE_STOP_SEC", 0.05)

    def exploding_drain():
        raise RuntimeError("drain exploded")

    monkeypatch.setattr(bars, "drain", exploding_drain)

    await bars.worker("20260827", "006340")   # 예외가 새어 나오면 실패
```

이 테스트는 `finally`의 `drain()`이 try/except로 감싸여 있어야 통과한다 (Task 5 Step 3의 `worker()` 참조).

**타입 일관성 확인** — `bars.series`가 돌려주는 봉 dict의 키(`confirmed`·`tick_count`·`spike_dropped`·`tick_derived`)를 Task 7의 `/api/bars` meta 계산과 Task 9의 `drawBarsChart`가 같은 이름으로 읽는다. `indicators.macd`가 돌려주는 `{macd, signal, hist}`를 `barsMacdDomain`과 MACD 패널이 같은 이름으로 읽는다. `_merge_official`은 Task 5와 Task 6이 공유한다.
