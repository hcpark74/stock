# '개선' 메뉴 (파라미터 진단 화면) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 전략 파라미터별 진단 카드(현재값→근거→판정→조정 방향)를 보여주는 '개선' 메뉴를 추가하고, 통계 화면의 저판별력 '개선 힌트' 섹션을 제거한다.

**Architecture:** 백엔드는 `GET /api/improve` 엔드포인트 1개 — 순수 집계 함수 `_improve_from_rows(trades, orders, skips)`가 DB 행을 받아 카드별 집계를 반환하고, 판정 로직(뱃지·기준값·가이드 문구)은 프론트 app.js에 둔다. 스펙: `docs/superpowers/specs/2026-07-15-improve-menu-design.md`.

**Tech Stack:** FastAPI + aiosqlite (기존), 프론트는 빌드 없는 vanilla JS/CSS (docs/html).

## Global Constraints

- 테스트 실행: `python -m pytest <파일> -v` (Windows PowerShell). 기존 테스트는 `pytest.importorskip("fastapi")` 패턴을 따른다.
- 새 의존성 추가 금지. `src/scheduler.py`를 server.py에서 import하지 않는다(apscheduler를 테스트 import 경로에 끌어들이지 않기 위해) — F5 시각은 `_F5_EXEC_TIME = "11:00"` 상수로 두고 주석으로 scheduler와 동기화 명시.
- API 예외 시 기존 `/api/stats` 패턴: `log("API_IMPROVE_FAILED", level="WARN", error=repr(exc))` 후 빈 기본 구조 반환.
- UI 문구는 한국어, 아래 태스크의 문자열을 그대로 사용(임의 변경 금지).
- 뱃지 4단계: 양호(`#26a69a`) / 관찰(`#f7a600`) / 조정 검토(`#ef5350`) / 표본 부족(`#787b86`).
- 커밋 메시지는 저장소 관례(한국어 요약 — 상세)를 따르고 끝에 `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`를 붙인다.
- 판정 기준값(스펙 §카드별 판정 규칙): 편차 0.3%p, 손절 비중 50%, 근접 이탈 3건, 반납 2.0%p, 매수 슬리피지 최대 0.5%p·평균 0.25%p, GUARD 2건, 타임아웃 MFE 1.5%, 연속손실 3건. 표본 가드: 종합 10 / 손절 3 / 스텝 5 / 트레일 5 / 슬립 3 / 타임아웃 5 / 갭 10일.

---

### Task 1: 서버 순수 집계 함수 — trades 기반 지표

**Files:**
- Modify: `src/api/server.py` (import 1줄 + `/api/stats` 뒤, 대략 line 819 이후에 새 섹션 추가)
- Create: `tests/test_api_improve.py`

**Interfaces:**
- Consumes: `src.modules.f4_tracking`의 `STEP_SIZE`, `STEP_TRAIL`, `HARD_STOP_RATIO`(이미 import됨), `FORCE_TRAILING_HOUR`, `FORCE_TRAILING_MINUTE`(추가 import). `src.modules.f1_filter`의 `GAP_MIN`, `GAP_MAX`, `src.modules.f3_entry`의 `GAP_MAX_ORDER`, `GAP_MAX_FILL`(이미 import됨).
- Produces: `server._improve_from_rows(trades: list[dict], orders: list[dict], skips: dict[str, int]) -> dict` — 스펙 §API의 JSON 구조 전체를 반환(이번 태스크에서 `slippage`/`candidates`는 0 값 뼈대). `server._improve_params() -> dict`. Task 2가 slippage/candidates를 채우고, Task 3의 엔드포인트가 이 함수를 호출한다.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_api_improve.py` 생성:

```python
import pytest

pytest.importorskip("fastapi")

import src.api.server as server  # noqa: E402 — fastapi 미설치 시 모듈 스킵 이후 임포트


def _trade(**over):
    base = {
        "date": "20260701", "ticker": "005930", "name": "삼성전자",
        "entry_price": 10_000.0, "high_price": None, "highest_step": 0.0,
        "pnl_pct": 0.0, "close_reason": "TIMEOUT",
        "entry_at": "2026-07-01T09:12:00+09:00",
        "exit_at": "2026-07-01T11:00:00+09:00",
    }
    base.update(over)
    return base


def test_improve_empty_rows_returns_zero_structure():
    payload = server._improve_from_rows([], [], {})

    assert payload["overall"] == {
        "total": 0, "wins": 0, "win_rate": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
        "payoff_ratio": 0.0, "expectancy": 0.0,
        "cur_loss_streak": 0, "max_loss_streak": 0,
    }
    assert payload["mfe_rows"] == []
    assert payload["hold_time"] == {}
    assert payload["params"]["step_size_pct"] == 2.5
    assert payload["params"]["step_trail_pct"] == 1.5
    assert payload["params"]["hard_stop_pct"] == 2.0
    assert payload["params"]["gap_max_order_pct"] == 6.5
    assert payload["params"]["gap_max_fill_pct"] == 7.0
    assert payload["params"]["timeout_time"] == "11:00"
    assert payload["params"]["force_trailing_time"] == "10:50"


def test_improve_overall_payoff_and_expectancy():
    trades = [
        _trade(date="20260701", pnl_pct=2.0, close_reason="TRAILING"),
        _trade(date="20260702", pnl_pct=-1.0, close_reason="HARD_STOP"),
        _trade(date="20260703", pnl_pct=-1.0, close_reason="HARD_STOP"),
    ]

    o = server._improve_from_rows(trades, [], {})["overall"]

    assert o["total"] == 3
    assert o["wins"] == 1
    assert o["win_rate"] == 33.3
    assert o["avg_win"] == 2.0
    assert o["avg_loss"] == -1.0
    assert o["payoff_ratio"] == 2.0
    # 기대값 = 1/3*2.0 + 2/3*(-1.0) = 0.0
    assert o["expectancy"] == 0.0


def test_improve_loss_streaks_track_current_and_max():
    # 순서: 패 패 승 패 → 최대 2, 현재 1
    trades = [
        _trade(date="20260701", pnl_pct=-1.0),
        _trade(date="20260702", pnl_pct=-1.0),
        _trade(date="20260703", pnl_pct=1.0),
        _trade(date="20260704", pnl_pct=-1.0),
    ]

    o = server._improve_from_rows(trades, [], {})["overall"]

    assert o["max_loss_streak"] == 2
    assert o["cur_loss_streak"] == 1


def test_improve_mfe_rows_newest_first_with_giveback():
    trades = [
        _trade(date="20260701", high_price=10_210.0, pnl_pct=-2.13,
               close_reason="HARD_STOP"),
        _trade(date="20260702", high_price=10_400.0, pnl_pct=1.0,
               close_reason="TRAILING", highest_step=0.025),
    ]

    rows = server._improve_from_rows(trades, [], {})["mfe_rows"]

    assert [r["date"] for r in rows] == ["20260702", "20260701"]
    assert rows[1]["mfe_pct"] == 2.1
    assert rows[1]["giveback_pp"] == 4.23
    assert rows[0]["mfe_pct"] == 4.0
    assert rows[0]["giveback_pp"] == 3.0


def test_improve_mfe_null_when_high_price_missing():
    rows = server._improve_from_rows([_trade(high_price=None)], [], {})["mfe_rows"]

    assert rows[0]["mfe_pct"] is None
    assert rows[0]["giveback_pp"] is None


def test_improve_step_counts_near_miss_and_step1():
    trades = [
        # 근접 이탈: 스텝1 미도달 + MFE 2.1% + 손실
        _trade(date="20260701", high_price=10_210.0, pnl_pct=-2.13,
               close_reason="HARD_STOP"),
        # 스텝1 도달
        _trade(date="20260702", high_price=10_400.0, pnl_pct=1.0,
               close_reason="TRAILING", highest_step=0.025),
        # MFE 1.5% + 손실 → 근접 이탈 (경계값 포함)
        _trade(date="20260703", high_price=10_150.0, pnl_pct=-0.5,
               close_reason="TIMEOUT"),
        # MFE 1.4% → 근접 이탈 아님
        _trade(date="20260704", high_price=10_140.0, pnl_pct=-0.5,
               close_reason="TIMEOUT"),
        # MFE 2.0%지만 수익 마감 → 근접 이탈 아님
        _trade(date="20260705", high_price=10_200.0, pnl_pct=0.3,
               close_reason="TIMEOUT"),
    ]

    s = server._improve_from_rows(trades, [], {})["step"]

    assert s["step1_n"] == 1
    assert s["step1_rate"] == 20.0
    assert s["near_miss_n"] == 2


def test_improve_hard_stop_slip_and_fast_stop():
    trades = [
        # -2.13% 체결, 8분 보유 → 편차 0.13%p, 빠른 손절
        _trade(date="20260701", pnl_pct=-2.13, close_reason="HARD_STOP",
               entry_at="2026-07-01T09:12:00+09:00",
               exit_at="2026-07-01T09:20:00+09:00"),
        # -2.07% 체결, 30분 보유
        _trade(date="20260702", pnl_pct=-2.07, close_reason="HARD_STOP",
               entry_at="2026-07-02T09:12:00+09:00",
               exit_at="2026-07-02T09:42:00+09:00"),
    ]

    h = server._improve_from_rows(trades, [], {})["hard_stop"]

    assert h["n"] == 2
    assert h["share_pct"] == 100.0
    assert h["avg_fill_pnl"] == -2.1
    assert h["avg_slip_pp"] == 0.1
    assert h["fast_stop_n"] == 1
    assert h["avg_min_to_stop"] == 19.0


def test_improve_trailing_and_timeout_sections():
    trades = [
        _trade(date="20260701", high_price=10_400.0, pnl_pct=1.0,
               close_reason="TRAILING", highest_step=0.025),
        _trade(date="20260702", high_price=10_150.0, pnl_pct=-0.5,
               close_reason="TIMEOUT"),
    ]

    payload = server._improve_from_rows(trades, [], {})

    assert payload["trailing"] == {"n": 1, "avg_giveback_pp": 3.0, "avg_pnl": 1.0}
    assert payload["timeout_exit"] == {"n": 1, "avg_pnl": -0.5, "avg_mfe": 1.5}


def test_improve_hold_time_grouped_by_reason():
    trades = [
        _trade(date="20260701", close_reason="HARD_STOP",
               entry_at="2026-07-01T09:12:00+09:00",
               exit_at="2026-07-01T09:20:00+09:00"),
        _trade(date="20260702", close_reason="TIMEOUT",
               entry_at="2026-07-02T09:12:00+09:00",
               exit_at="2026-07-02T11:00:00+09:00"),
    ]

    ht = server._improve_from_rows(trades, [], {})["hold_time"]

    assert ht["HARD_STOP"] == {"n": 1, "avg_min": 8.0}
    assert ht["TIMEOUT"] == {"n": 1, "avg_min": 108.0}
```

- [ ] **Step 2: 실패 확인**

Run: `python -m pytest tests/test_api_improve.py -v`
Expected: 전부 FAIL — `AttributeError: module 'src.api.server' has no attribute '_improve_from_rows'`

- [ ] **Step 3: 구현**

`src/api/server.py` line 62를 다음으로 교체:

```python
from src.modules.f4_tracking import (
    FORCE_TRAILING_HOUR,
    FORCE_TRAILING_MINUTE,
    HARD_STOP_RATIO,
    STEP_SIZE,
    STEP_TRAIL,
)
```

`api_stats`의 끝(대략 line 819, `# ─── /api/stream (SSE) ───` 주석 앞)에 추가:

```python
# ─── /api/improve ─────────────────────────────────────────────────────

_BUY_PHASES = {"FIRST_BUY", "PYRAMID_BUY"}
_F5_EXEC_TIME = "11:00"  # scheduler.F5_EXEC_H/M와 동기 유지 (직접 import 시 apscheduler가 테스트 경로에 끌려옴)
_NEAR_MISS_MFE_PCT = 1.5  # 스텝1 근접 이탈로 보는 최소 고점 수익률(%)
_FAST_STOP_MIN = 10  # 진입 후 이 분수 이내 손절이면 '빠른 손절'


def _improve_params() -> dict:
    return {
        "step_size_pct": round(STEP_SIZE * 100, 2),
        "step_trail_pct": round(STEP_TRAIL * 100, 2),
        "hard_stop_pct": round(HARD_STOP_RATIO * 100, 2),
        "gap_max_order_pct": round(GAP_MAX_ORDER * 100, 2),
        "gap_max_fill_pct": round(GAP_MAX_FILL * 100, 2),
        "f1_gap_min_pct": round(GAP_MIN * 100, 2),
        "f1_gap_core_max_pct": round(GAP_MAX * 100, 2),
        "timeout_time": _F5_EXEC_TIME,
        "force_trailing_time": f"{FORCE_TRAILING_HOUR:02d}:{FORCE_TRAILING_MINUTE:02d}",
    }


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _minutes_between(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    try:
        delta = datetime.fromisoformat(end) - datetime.fromisoformat(start)
    except (TypeError, ValueError):
        return None
    return delta.total_seconds() / 60.0


def _improve_from_rows(
    trades: list[dict], orders: list[dict], skips: dict[str, int]
) -> dict:
    """개선 화면 집계. trades는 date 오름차순의 CLOSED 행."""
    total = len(trades)
    pnls = [(t.get("pnl_pct") or 0.0) for t in trades]
    win_pnls = [p for p in pnls if p > 0]
    loss_pnls = [p for p in pnls if p <= 0]
    win_rate = len(win_pnls) / total if total else 0.0
    avg_win = _avg(win_pnls)
    avg_loss = _avg(loss_pnls)
    payoff = abs(avg_win / avg_loss) if avg_loss < 0 else 0.0
    expectancy = win_rate * avg_win + (1 - win_rate) * avg_loss

    max_streak = run = 0
    for p in pnls:
        run = run + 1 if p <= 0 else 0
        max_streak = max(max_streak, run)
    cur_streak = run  # 마지막 연속 구간이 곧 현재 진행 중 스트릭

    mfe_rows: list[dict] = []
    near_miss_n = 0
    step1_n = 0
    hold_by_reason: dict[str, list[float]] = {}
    trailing_gb: list[float] = []
    trailing_pnl: list[float] = []
    timeout_pnl: list[float] = []
    timeout_mfe: list[float] = []
    hs_pnl: list[float] = []
    hs_minutes: list[float] = []
    fast_stop_n = 0

    for t in trades:
        entry, high = t.get("entry_price"), t.get("high_price")
        pnl = t.get("pnl_pct")
        reason = t.get("close_reason") or ""
        mfe = round((high / entry - 1) * 100, 2) if entry and high else None
        giveback = (
            round(mfe - pnl, 2) if mfe is not None and pnl is not None else None
        )
        mfe_rows.append({
            "date": t.get("date"), "ticker": t.get("ticker"),
            "name": t.get("name"), "mfe_pct": mfe, "pnl_pct": pnl,
            "giveback_pp": giveback, "close_reason": reason,
        })

        if (t.get("highest_step") or 0.0) >= STEP_SIZE:
            step1_n += 1
        elif mfe is not None and mfe >= _NEAR_MISS_MFE_PCT and (pnl or 0.0) <= 0:
            near_miss_n += 1

        minutes = _minutes_between(t.get("entry_at"), t.get("exit_at"))
        if minutes is not None and reason:
            hold_by_reason.setdefault(reason, []).append(minutes)

        if reason == "TRAILING":
            if giveback is not None:
                trailing_gb.append(giveback)
            if pnl is not None:
                trailing_pnl.append(pnl)
        elif reason == "TIMEOUT":
            if pnl is not None:
                timeout_pnl.append(pnl)
            if mfe is not None:
                timeout_mfe.append(mfe)
        elif reason == "HARD_STOP":
            if pnl is not None:
                hs_pnl.append(pnl)
            if minutes is not None:
                hs_minutes.append(minutes)
                if minutes <= _FAST_STOP_MIN:
                    fast_stop_n += 1

    mfe_rows.reverse()  # 최신 거래 먼저

    avg_fill_pnl = _avg(hs_pnl)
    # 손절 체결 편차: 설정 레벨(-2.0%)보다 더 밀린 폭만 양수로
    avg_slip_pp = (
        max(0.0, -(avg_fill_pnl + HARD_STOP_RATIO * 100)) if hs_pnl else 0.0
    )

    empty_slip = {"n": 0, "avg_pp": 0.0, "max_pp": 0.0, "avg_latency_ms": 0}
    return {
        "params": _improve_params(),
        "overall": {
            "total": total,
            "wins": len(win_pnls),
            "win_rate": round(win_rate * 100, 1),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "payoff_ratio": round(payoff, 2),
            "expectancy": round(expectancy, 2),
            "cur_loss_streak": cur_streak,
            "max_loss_streak": max_streak,
        },
        "hard_stop": {
            "n": len(hs_pnl),
            "share_pct": round(len(hs_pnl) / total * 100, 1) if total else 0.0,
            "avg_fill_pnl": round(avg_fill_pnl, 2),
            "avg_slip_pp": round(avg_slip_pp, 2),
            "fast_stop_n": fast_stop_n,
            "avg_min_to_stop": round(_avg(hs_minutes), 1) if hs_minutes else 0.0,
        },
        "step": {
            "step1_n": step1_n,
            "step1_rate": round(step1_n / total * 100, 1) if total else 0.0,
            "near_miss_n": near_miss_n,
        },
        "trailing": {
            "n": len(trailing_pnl),
            "avg_giveback_pp": round(_avg(trailing_gb), 2),
            "avg_pnl": round(_avg(trailing_pnl), 2),
        },
        "slippage": {  # Task 2에서 채움
            "buy": dict(empty_slip),
            "sell": dict(empty_slip),
            "by_phase": {},
            "guard_n": 0,
        },
        "timeout_exit": {
            "n": len(timeout_pnl),
            "avg_pnl": round(_avg(timeout_pnl), 2),
            "avg_mfe": round(_avg(timeout_mfe), 2),
        },
        "candidates": {  # Task 2에서 채움
            "skips": {},
            "skip_days": 0,
            "trade_days": total,
        },
        "mfe_rows": mfe_rows,
        "hold_time": {
            reason: {"n": len(mins), "avg_min": round(_avg(mins), 1)}
            for reason, mins in hold_by_reason.items()
        },
    }
```

- [ ] **Step 4: 통과 확인**

Run: `python -m pytest tests/test_api_improve.py -v`
Expected: 전부 PASS

- [ ] **Step 5: 기존 테스트 회귀 확인**

Run: `python -m pytest tests/test_api_server.py -v`
Expected: 전부 PASS (import 변경이 기존 동작에 영향 없음)

- [ ] **Step 6: 커밋**

```bash
git add src/api/server.py tests/test_api_improve.py
git commit -m "개선 API 집계 함수 — trades 기반 MFE·스텝·손절·스트릭 지표"
```
(커밋 메시지 끝에 Co-Authored-By 트레일러 포함 — Global Constraints 참조)

---

### Task 2: 슬리피지·스킵 집계 추가

**Files:**
- Modify: `src/api/server.py` (`_improve_from_rows` 내 slippage/candidates 부분)
- Modify: `tests/test_api_improve.py` (테스트 추가)

**Interfaces:**
- Consumes: Task 1의 `_improve_from_rows`, `_avg`.
- Produces: `slippage.buy/sell/by_phase` = `{"n": int, "avg_pp": float, "max_pp": float, "avg_latency_ms": int}`, `slippage.guard_n: int`, `candidates.skips: dict[str, int]`, `candidates.skip_days: int`. orders 행 dict 키: `order_phase`, `order_price`, `fill_price`, `fill_latency_ms`.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_api_improve.py`에 추가:

```python
def _order(phase, order_price, fill_price, latency=500):
    return {
        "order_phase": phase, "order_price": order_price,
        "fill_price": fill_price, "fill_latency_ms": latency,
    }


def test_improve_slippage_adverse_is_positive_for_both_sides():
    orders = [
        # 매수: 비싸게 체결 = 불리 → +0.3
        _order("FIRST_BUY", 10_000.0, 10_030.0, latency=400),
        # 매도: 싸게 체결 = 불리 → +0.2
        _order("CLOSE_SELL", 10_000.0, 9_980.0, latency=600),
        # 매도: 비싸게 체결 = 유리 → -0.1
        _order("TIMEOUT_SELL", 10_000.0, 10_010.0, latency=800),
    ]

    sl = server._improve_from_rows([], orders, {})["slippage"]

    assert sl["buy"] == {"n": 1, "avg_pp": 0.3, "max_pp": 0.3, "avg_latency_ms": 400}
    assert sl["sell"]["n"] == 2
    assert sl["sell"]["avg_pp"] == 0.05
    assert sl["sell"]["max_pp"] == 0.2
    assert sl["sell"]["avg_latency_ms"] == 700
    assert sl["by_phase"]["FIRST_BUY"]["n"] == 1
    assert sl["by_phase"]["CLOSE_SELL"]["avg_pp"] == 0.2


def test_improve_slippage_skips_rows_without_prices():
    orders = [
        _order("FIRST_BUY", None, 10_030.0),
        _order("FIRST_BUY", 0, 10_030.0),
        _order("FIRST_BUY", 10_000.0, None),
    ]

    sl = server._improve_from_rows([], orders, {})["slippage"]

    assert sl["buy"]["n"] == 0
    assert sl["by_phase"] == {}


def test_improve_guard_count_and_skips():
    trades = [_trade(date="20260701", close_reason="SLIPPAGE_GUARD", pnl_pct=-1.2)]
    skips = {"NO_TARGET": 3, "GAP_CHANGED": 1}

    payload = server._improve_from_rows(trades, [], skips)

    assert payload["slippage"]["guard_n"] == 1
    assert payload["candidates"] == {
        "skips": {"NO_TARGET": 3, "GAP_CHANGED": 1},
        "skip_days": 4,
        "trade_days": 1,
    }
```

- [ ] **Step 2: 실패 확인**

Run: `python -m pytest tests/test_api_improve.py -v`
Expected: 새 테스트 3개 FAIL (`sl["buy"]["n"] == 0` 등 0-뼈대 값과 불일치), 기존 테스트 PASS

- [ ] **Step 3: 구현**

`_improve_from_rows`에서 `avg_slip_pp = ...` 계산 직후, `empty_slip = ...` 줄 앞에 추가:

```python
    slip_acc: dict[str, dict[str, list]] = {}
    for o in orders:
        phase = o.get("order_phase") or ""
        order_price = o.get("order_price") or 0
        fill_price = o.get("fill_price")
        if not order_price or fill_price is None:
            continue
        pp = (fill_price - order_price) / order_price * 100
        if phase not in _BUY_PHASES:
            pp = -pp  # 매도는 싸게 체결될수록 불리 → 부호 반전해 '불리=양수' 통일
        acc = slip_acc.setdefault(phase, {"pps": [], "lat": []})
        acc["pps"].append(pp)
        if o.get("fill_latency_ms") is not None:
            acc["lat"].append(o["fill_latency_ms"])

    def _slip_summary(pps: list[float], lat: list[float]) -> dict:
        return {
            "n": len(pps),
            "avg_pp": round(_avg(pps), 3),
            "max_pp": round(max(pps), 3) if pps else 0.0,
            "avg_latency_ms": round(_avg(lat)) if lat else 0,
        }

    by_phase = {ph: _slip_summary(a["pps"], a["lat"]) for ph, a in slip_acc.items()}
    buy_pps = [p for ph, a in slip_acc.items() if ph in _BUY_PHASES for p in a["pps"]]
    buy_lat = [v for ph, a in slip_acc.items() if ph in _BUY_PHASES for v in a["lat"]]
    sell_pps = [p for ph, a in slip_acc.items() if ph not in _BUY_PHASES for p in a["pps"]]
    sell_lat = [v for ph, a in slip_acc.items() if ph not in _BUY_PHASES for v in a["lat"]]
    guard_n = sum(1 for t in trades if t.get("close_reason") == "SLIPPAGE_GUARD")
```

그리고 반환 dict에서 `empty_slip` 선언과 slippage/candidates 항목을 다음으로 교체:

```python
        "slippage": {
            "buy": _slip_summary(buy_pps, buy_lat),
            "sell": _slip_summary(sell_pps, sell_lat),
            "by_phase": by_phase,
            "guard_n": guard_n,
        },
```

```python
        "candidates": {
            "skips": skips,
            "skip_days": sum(skips.values()),
            "trade_days": total,
        },
```

(`empty_slip` 변수와 `# Task 2에서 채움` 주석 2개는 삭제)

- [ ] **Step 4: 통과 확인**

Run: `python -m pytest tests/test_api_improve.py -v`
Expected: 전부 PASS

- [ ] **Step 5: 커밋**

```bash
git add src/api/server.py tests/test_api_improve.py
git commit -m "개선 API 집계 — 주문 슬리피지(불리=양수)·스킵·GUARD 카운트"
```

---

### Task 3: `/api/improve` 엔드포인트 + DB 통합 테스트

**Files:**
- Modify: `src/api/server.py` (`_improve_from_rows` 뒤에 엔드포인트 추가)
- Modify: `tests/test_api_improve.py` (통합 테스트 추가)

**Interfaces:**
- Consumes: `_improve_from_rows` (Task 1·2), `db.get()`, `log()`.
- Produces: `GET /api/improve` → 스펙 §API JSON. 프론트(Task 4·5)가 이 계약에 의존.

- [ ] **Step 1: 실패하는 통합 테스트 작성**

`tests/test_api_improve.py` 상단 import를 다음으로 교체:

```python
import json

import pytest

pytest.importorskip("fastapi")

import src.api.server as server  # noqa: E402 — fastapi 미설치 시 모듈 스킵 이후 임포트
from src import db  # noqa: E402
```

파일 끝에 추가:

```python
@pytest.mark.asyncio
async def test_api_improve_empty_db_returns_default_structure(tmp_path):
    await db.init(str(tmp_path / "improve.db"))

    resp = await server.api_improve()
    payload = json.loads(resp.body.decode("utf-8"))

    assert payload["overall"]["total"] == 0
    assert payload["mfe_rows"] == []
    assert payload["slippage"]["buy"]["n"] == 0
    assert payload["candidates"]["skips"] == {}
    assert payload["params"]["hard_stop_pct"] == 2.0
    await db.close()


@pytest.mark.asyncio
async def test_api_improve_aggregates_seeded_trades_orders_skips(tmp_path):
    await db.init(str(tmp_path / "improve.db"))
    conn = db.get()

    # 거래 1: 손절 (MFE 2.1%, 8분 보유 → 근접 이탈 + 빠른 손절)
    t1 = await db.open_trade("20260701", "005930", 10_000.0, 10, name="삼성전자")
    await db.update_trade_progress(t1, 10_210.0, 0.0)
    await db.close_trade(t1, 9_787.0, "HARD_STOP", -2.13, 0.0)
    # 거래 2: 트레일링 (스텝1 도달, MFE 4.0%)
    t2 = await db.open_trade("20260702", "000660", 10_000.0, 10, name="SK하이닉스")
    await db.update_trade_progress(t2, 10_400.0, 0.025)
    await db.close_trade(t2, 10_100.0, "TRAILING", 1.0, 0.025)
    # 진입·청산 시각을 결정적으로 고정 (close_trade는 now를 기록)
    await conn.execute(
        "UPDATE trades SET entry_at=?, exit_at=? WHERE id=?",
        ("2026-07-01T09:12:00+09:00", "2026-07-01T09:20:00+09:00", t1))
    await conn.execute(
        "UPDATE trades SET entry_at=?, exit_at=? WHERE id=?",
        ("2026-07-02T09:12:00+09:00", "2026-07-02T10:30:00+09:00", t2))
    await conn.commit()

    # 주문: 매수 불리 +0.3%p 체결
    o1 = await db.record_order(t1, "KIS001", "BUY", 10, 10_000.0,
                               "FIRST_BUY", "005930", name="삼성전자")
    await db.update_order_fill(o1, 10_030.0, 10, 400)
    # 미체결(PENDING) 주문은 집계에서 제외되어야 함
    await db.record_order(t1, "KIS002", "SELL", 10, 10_000.0,
                          "CLOSE_SELL", "005930")

    await db.record_skip("20260703", "NO_TARGET", "후보 없음")

    resp = await server.api_improve()
    payload = json.loads(resp.body.decode("utf-8"))

    assert payload["overall"]["total"] == 2
    assert payload["step"]["step1_n"] == 1
    assert payload["step"]["near_miss_n"] == 1
    assert payload["hard_stop"]["n"] == 1
    assert payload["hard_stop"]["fast_stop_n"] == 1
    assert payload["mfe_rows"][0]["date"] == "20260702"  # 최신 먼저
    assert payload["slippage"]["buy"]["n"] == 1
    assert payload["slippage"]["buy"]["avg_pp"] == 0.3
    assert payload["candidates"]["skips"] == {"NO_TARGET": 1}
    assert payload["candidates"]["trade_days"] == 2
    await db.close()
```

- [ ] **Step 2: 실패 확인**

Run: `python -m pytest tests/test_api_improve.py -v`
Expected: 새 테스트 2개 FAIL — `AttributeError: ... no attribute 'api_improve'`

- [ ] **Step 3: 엔드포인트 구현**

`src/api/server.py`의 `_improve_from_rows` 정의 바로 뒤에 추가:

```python
@app.get("/api/improve")
async def api_improve() -> JSONResponse:
    try:
        conn = db.get()
        async with conn.execute(
            """SELECT date, ticker, name, entry_price, high_price, highest_step,
                      pnl_pct, close_reason, entry_at, exit_at
               FROM trades WHERE status='CLOSED' ORDER BY date"""
        ) as cur:
            trades = [dict(r) for r in await cur.fetchall()]
        async with conn.execute(
            """SELECT order_phase, order_price, fill_price, fill_latency_ms
               FROM orders WHERE status IN ('FILLED', 'PARTIAL_FILL')"""
        ) as cur:
            orders = [dict(r) for r in await cur.fetchall()]
        async with conn.execute(
            "SELECT reason, COUNT(*) as n FROM daily_skips GROUP BY reason"
        ) as cur:
            skips = {r["reason"]: r["n"] for r in await cur.fetchall()}
        return JSONResponse(_improve_from_rows(trades, orders, skips))
    except Exception as exc:
        log("API_IMPROVE_FAILED", level="WARN", error=repr(exc))
        return JSONResponse(_improve_from_rows([], [], {}))
```

- [ ] **Step 4: 통과 확인**

Run: `python -m pytest tests/test_api_improve.py tests/test_api_server.py -v`
Expected: 전부 PASS

- [ ] **Step 5: 커밋**

```bash
git add src/api/server.py tests/test_api_improve.py
git commit -m "/api/improve 엔드포인트 — 파라미터 진단 집계 제공"
```

---

### Task 4: 프론트 — 개선 메뉴·화면·진단 카드

**Files:**
- Modify: `docs/html/index.html` (사이드바 메뉴 1줄 + `sc-improve` 화면 블록)
- Modify: `docs/html/assets/app.js` (`go()` 연결 + 카드 판정/렌더 함수)
- Modify: `docs/html/assets/app.css` (카드 스타일)

**Interfaces:**
- Consumes: `GET /api/improve` (Task 3의 JSON 계약), 기존 헬퍼 `$`, `esc`, `fmt`, `fmtPct`, `sampleNote`, `reasonName`.
- Produces: `loadImprove()` (go()에서 호출), `renderImprove(d)`, 판정 함수 7종 `judgeOverall/judgeHardStop/judgeStepSize/judgeStepTrail/judgeSlipBuffer/judgeTimeout/judgeGapRange` — 각각 `[level, evidence, guide]` 배열 반환(level ∈ 'ok'|'watch'|'adjust'|'nodata'). Task 5가 `renderImprove` 안에서 상세 표 렌더를 호출.

- [ ] **Step 1: 사이드바 메뉴 추가**

`docs/html/index.html` line 43(통계 버튼)과 44(설정 버튼) 사이에 삽입:

```html
    <button class="li menu-item" title="개선" onclick="go('improve',this)"><span class="li-dot"></span><span>개선</span></button>
```

- [ ] **Step 2: 화면 마크업 추가**

`index.html`의 `<!-- ══ STATS ══ -->` 블록 닫는 `</div>`(line 316) 바로 뒤에 삽입:

```html
    <!-- ══ IMPROVE ══ -->
    <div id="sc-improve" class="sc sc-stats">
      <div class="stats-inner">
        <div class="scard full">
          <div class="card-ttl">파라미터 진단</div>
          <div class="stats-note" id="imp-sample-note">표본 대기</div>
          <div class="imp-grid" id="imp-cards"><div class="empty">로딩 중...</div></div>
        </div>
        <div class="scard full">
          <div class="card-ttl">거래별 고점 반납</div>
          <div class="tbl-wrap">
            <table>
              <thead><tr><th>날짜</th><th>종목</th><th>고점(MFE)</th><th>최종 손익</th><th>반납</th><th>청산 사유</th></tr></thead>
              <tbody id="imp-mfe-tbody"><tr><td colspan="6" class="empty">로딩 중...</td></tr></tbody>
            </table>
          </div>
        </div>
        <div class="scard full">
          <div class="card-ttl">슬리피지 상세</div>
          <div class="tbl-wrap">
            <table>
              <thead><tr><th>구간</th><th>건수</th><th>평균 슬리피지</th><th>최대</th><th>평균 체결지연</th></tr></thead>
              <tbody id="imp-slip-tbody"><tr><td colspan="5" class="empty">로딩 중...</td></tr></tbody>
            </table>
          </div>
        </div>
        <div class="scard full">
          <div class="card-ttl">스킵 · 보유시간</div>
          <div class="factor-grid" id="imp-skip-hold"><div class="empty">로딩 중...</div></div>
        </div>
      </div>
    </div>
```

- [ ] **Step 3: 카드 CSS 추가**

`docs/html/assets/app.css`의 `.factor-row` 규칙(line 252) 뒤에 추가:

```css
.imp-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:8px;margin-top:8px;}
.imp-card{border:1px solid var(--bd);background:rgba(255,255,255,.014);padding:10px 12px;display:flex;flex-direction:column;gap:6px;}
.imp-head{display:flex;align-items:center;gap:8px;}
.imp-name{font-size:12px;font-weight:700;letter-spacing:.02em;}
.imp-cur{font-family:var(--mn);font-size:11px;color:var(--mu);}
.imp-badge{margin-left:auto;font-size:10px;padding:2px 7px;border:1px solid;white-space:nowrap;}
.imp-ok{color:#26a69a;border-color:#26a69a55;}
.imp-watch{color:#f7a600;border-color:#f7a60055;}
.imp-adjust{color:#ef5350;border-color:#ef535055;}
.imp-nodata{color:#787b86;border-color:#787b8655;}
.imp-ev{font-family:var(--mn);font-size:11px;color:var(--mu);line-height:1.5;}
.imp-guide{font-size:12px;line-height:1.5;}
```

- [ ] **Step 4: go() 연결 + 로드/판정/렌더 함수**

`docs/html/assets/app.js`의 `go()` 안 `if (id==='stats')   loadStats();`(line 48) 뒤에 추가:

```js
  if (id==='improve') loadImprove();
```

`loadStats()` 함수(line 1402~1408) 뒤에 추가:

```js
// ── 개선(파라미터 진단) ──────────────────────────────────────────────────
async function loadImprove() {
  try {
    const r = await fetch('/api/improve');
    if(!r.ok) return;
    renderImprove(await r.json());
  } catch(e){}
}

const IMP_BADGE = {
  ok:     {txt:'양호',      cls:'imp-ok'},
  watch:  {txt:'관찰',      cls:'imp-watch'},
  adjust: {txt:'조정 검토', cls:'imp-adjust'},
  nodata: {txt:'표본 부족', cls:'imp-nodata'},
};

function impCard(title, cur, [level, evidence, guide]) {
  const b = IMP_BADGE[level];
  return `<div class="imp-card">
    <div class="imp-head"><span class="imp-name">${esc(title)}</span>
      <span class="imp-cur">현재 ${esc(cur)}</span>
      <span class="imp-badge ${b.cls}">${b.txt}</span></div>
    <div class="imp-ev">${esc(evidence)}</div>
    <div class="imp-guide">${esc(guide)}</div>
  </div>`;
}

function judgeOverall(d) {
  const o = d.overall;
  const ev = `기대값 ${fmtPct(o.expectancy)} · 손익비 ${(o.payoff_ratio||0).toFixed(2)} · 승률 ${o.win_rate}% · 연속손실 ${o.cur_loss_streak}건(최대 ${o.max_loss_streak})`;
  if (o.total < 10) return ['nodata', ev, `판정까지 ${10 - o.total}건 더 필요합니다.`];
  if (o.total >= 20 && o.expectancy < 0) return ['adjust', ev, '기대값이 음수입니다(20건 이상 누적). 파라미터 이전에 전략 자체를 재검토하세요.'];
  if (o.cur_loss_streak >= 3) return ['adjust', ev, `연속 손실 ${o.cur_loss_streak}건입니다(기준 3건). 일시 중단을 검토하세요.`];
  if (o.payoff_ratio < 1) return ['watch', ev, '손익비가 1 미만입니다(기준 1.0). 이긴 거래의 크기가 진 거래보다 작습니다.'];
  return ['ok', ev, '기대값·손익비·스트릭 모두 경고 기준 이내입니다.'];
}

function judgeHardStop(d) {
  const h = d.hard_stop;
  const ev = `손절 ${h.n}건(${h.share_pct}%) · 체결 편차 ${h.avg_slip_pp}%p · 10분 내 손절 ${h.fast_stop_n}건 · 평균 ${h.avg_min_to_stop}분`;
  if (h.n < 3) return ['nodata', ev, `판정까지 손절 표본 ${3 - h.n}건 더 필요합니다.`];
  if (h.avg_slip_pp > 0.3) return ['adjust', ev, `손절 체결이 설정(-${d.params.hard_stop_pct}%)보다 평균 ${h.avg_slip_pp}%p 밀립니다(기준 0.3%p). 지정가 손절 전환 또는 폭 조정을 검토하세요.`];
  if (d.overall.total >= 10 && h.share_pct > 50) return ['adjust', ev, `손절 비중이 ${h.share_pct}%로 절반을 넘습니다(기준 50%). 손절 폭보다 진입 품질을 우선 점검하세요.`];
  if (h.fast_stop_n / h.n >= 0.5) return ['watch', ev, `손절의 ${Math.round(h.fast_stop_n / h.n * 100)}%가 진입 10분 내 발생 — 시초 변동성 구간입니다. 진입 지연을 검토하세요.`];
  return ['ok', ev, '체결 편차·손절 비중 모두 기준 이내입니다.'];
}

function judgeStepSize(d) {
  const s = d.step;
  const ev = `스텝1 도달 ${s.step1_n}건(${s.step1_rate}%) · 근접 이탈 ${s.near_miss_n}건`;
  if (d.overall.total < 5) return ['nodata', ev, `판정까지 ${5 - d.overall.total}건 더 필요합니다.`];
  if (s.near_miss_n >= 3 && s.near_miss_n > s.step1_n) return ['adjust', ev, `고점 +1.5~${d.params.step_size_pct}%에서 손실로 끝난 거래(${s.near_miss_n}건)가 스텝1 도달(${s.step1_n}건)보다 많습니다. 간격 2.0% 축소를 검토하세요.`];
  if (s.near_miss_n >= 2) return ['watch', ev, `근접 이탈이 ${s.near_miss_n}건 누적됐습니다(조정 기준 3건). 추이를 관찰하세요.`];
  if (s.step1_rate >= 40) return ['ok', ev, `스텝1 도달률 ${s.step1_rate}%로 양호합니다(기준 40%).`];
  return ['ok', ev, '근접 이탈이 없어 현재 간격에 무리가 없습니다.'];
}

function judgeStepTrail(d) {
  const t = d.trailing;
  const ev = `트레일링 청산 ${t.n}건 · 평균 반납 ${t.avg_giveback_pp}%p · 평균 손익 ${fmtPct(t.avg_pnl)}`;
  if (t.n < 5) return ['nodata', ev, `판정까지 트레일링 청산 ${5 - t.n}건 더 필요합니다.`];
  if (t.avg_giveback_pp > 2.0) return ['adjust', ev, `고점 대비 평균 ${t.avg_giveback_pp}%p 반납하고 청산됩니다(기준 2.0%p). 폭 축소를 검토하세요.`];
  if (t.avg_giveback_pp > 1.5) return ['watch', ev, `반납폭이 설정(${d.params.step_trail_pct}%)을 넘고 있습니다(관찰 기준 1.5%p).`];
  return ['ok', ev, '고점 반납이 설정 범위 이내입니다.'];
}

function judgeSlipBuffer(d) {
  const sl = d.slippage;
  const buf = (d.params.gap_max_fill_pct - d.params.gap_max_order_pct).toFixed(1);
  const ev = `매수 슬리피지 평균 ${sl.buy.avg_pp}%p·최대 ${sl.buy.max_pp}%p (${sl.buy.n}건) · GUARD ${sl.guard_n}건`;
  if (sl.buy.n < 3) return ['nodata', ev, `판정까지 매수 체결 ${3 - sl.buy.n}건 더 필요합니다.`];
  if (sl.guard_n >= 2 || sl.buy.max_pp > 0.5) return ['adjust', ev, `슬리피지가 버퍼(${buf}%p)를 위협합니다(GUARD ${sl.guard_n}건, 최대 ${sl.buy.max_pp}%p). GAP_MAX_ORDER 하향 또는 버퍼 확대를 검토하세요.`];
  if (sl.buy.avg_pp > 0.25) return ['watch', ev, '평균 매수 슬리피지가 0.25%p를 넘었습니다. 버퍼 소진 추이를 관찰하세요.'];
  return ['ok', ev, `슬리피지가 버퍼(${buf}%p) 대비 여유 있습니다.`];
}

function judgeTimeout(d) {
  const to = d.timeout_exit;
  const ev = `시간 청산 ${to.n}건 · 평균 손익 ${fmtPct(to.avg_pnl)} · 평균 고점 +${to.avg_mfe}%`;
  if (to.n < 5) return ['nodata', ev, `판정까지 시간 청산 ${5 - to.n}건 더 필요합니다.`];
  if (to.avg_pnl < 0) return ['adjust', ev, `시간 청산 평균이 음수입니다 — 보유시간 내 회복에 실패하고 있습니다. 청산 시각(${d.params.timeout_time}) 단축을 검토하세요.`];
  if (to.avg_mfe >= 1.5) return ['watch', ev, `시간 청산 전 고점이 평균 +${to.avg_mfe}%였습니다(기준 1.5%). 강제 트레일링(${d.params.force_trailing_time}) 앞당김을 검토하세요.`];
  return ['ok', ev, '시간 청산 성과에 경고 신호가 없습니다.'];
}

function judgeGapRange(d) {
  const c = d.candidates;
  const days = c.skip_days + c.trade_days;
  const skipList = Object.entries(c.skips || {}).map(([k, v]) => `${k} ${v}`).join(' · ') || '스킵 없음';
  const ev = `거래일 ${c.trade_days} · 스킵일 ${c.skip_days} (${skipList})`;
  const note = ' 진입 시점 갭이 저장되면 정밀 판정이 가능합니다.';
  if (days < 10) return ['nodata', ev, `판정까지 실행일 ${10 - days}일 더 필요합니다.` + note];
  if (c.skip_days > c.trade_days) return ['watch', ev, `스킵일이 거래일보다 많습니다. 후보 부족이면 갭 범위(${d.params.f1_gap_min_pct}~${d.params.f1_gap_core_max_pct}%) 확대를 검토하되 신중하게.` + note];
  return ['ok', ev, '후보 공급에 문제가 없습니다.' + note];
}

function renderImprove(d) {
  $('imp-sample-note').textContent = sampleNote(d.overall.total || 0);
  const p = d.params;
  const cards = [
    ['전략 종합',     `${d.overall.total}건`,                          judgeOverall(d)],
    ['HARD_STOP',    `-${p.hard_stop_pct}%`,                           judgeHardStop(d)],
    ['STEP_SIZE',    `+${p.step_size_pct}%`,                           judgeStepSize(d)],
    ['STEP_TRAIL',   `-${p.step_trail_pct}%`,                          judgeStepTrail(d)],
    ['슬리피지 버퍼', `${p.gap_max_order_pct}→${p.gap_max_fill_pct}%`, judgeSlipBuffer(d)],
    ['F5 타임아웃',  p.timeout_time,                                   judgeTimeout(d)],
    ['F1 갭 범위',   `${p.f1_gap_min_pct}~${p.f1_gap_core_max_pct}%`,  judgeGapRange(d)],
  ];
  $('imp-cards').innerHTML = cards.map(([t, cur, j]) => impCard(t, cur, j)).join('');
}
```

- [ ] **Step 5: 수동 검증**

Run: `python -m uvicorn src.api.server:app --port 8899`
브라우저에서 `http://localhost:8899` 접속 → 사이드바 '개선' 클릭.
Expected: 카드 7장이 표시되고(DB 미초기화 상태면 전부 '표본 부족'), 콘솔 에러 없음. 확인 후 서버 종료(Ctrl+C).

- [ ] **Step 6: 커밋**

```bash
git add docs/html/index.html docs/html/assets/app.js docs/html/assets/app.css
git commit -m "개선 메뉴 — 파라미터 진단 카드 7종(판정 뱃지·조정 가이드)"
```

---

### Task 5: 프론트 — 하단 상세 표 3종

**Files:**
- Modify: `docs/html/assets/app.js` (`renderImprove` 끝에 호출 3줄 + 렌더 함수 3개)

**Interfaces:**
- Consumes: Task 4의 `renderImprove(d)`, Task 3의 payload 중 `mfe_rows`, `slippage.by_phase`, `candidates.skips`, `hold_time`. 기존 헬퍼 `esc`, `fmt`, `fmtPct`, `reasonName`.
- Produces: `renderMfeTable(rows)`, `renderSlipTable(sl)`, `renderSkipHold(d)` — Task 4에서 만든 `imp-mfe-tbody`/`imp-slip-tbody`/`imp-skip-hold` 요소를 채운다.

- [ ] **Step 1: renderImprove에 호출 추가**

`renderImprove(d)` 함수에서 `$('imp-cards').innerHTML = cards.map(([t, cur, j]) => impCard(t, cur, j)).join('');` 줄과 함수 닫는 `}` 사이에 다음 3줄을 삽입:

```js
  renderMfeTable(d.mfe_rows);
  renderSlipTable(d.slippage);
  renderSkipHold(d);
```

- [ ] **Step 2: 렌더 함수 3개 추가**

`renderImprove` 함수 뒤에 추가:

```js
function renderMfeTable(rows) {
  const tb = $('imp-mfe-tbody');
  if (!rows || !rows.length) {
    tb.innerHTML = '<tr><td colspan="6" class="empty">폐쇄 거래 없음</td></tr>';
    return;
  }
  tb.innerHTML = rows.map(r => {
    const mfe = r.mfe_pct == null ? '—' : fmtPct(r.mfe_pct);
    const gb = r.giveback_pp == null ? '—' : r.giveback_pp.toFixed(2) + '%p';
    const pc = (r.pnl_pct || 0) >= 0 ? 'pup' : 'pdn';
    return `<tr><td>${esc(r.date)}</td><td>${esc(r.name || r.ticker)}</td><td>${mfe}</td><td class="${pc}">${fmtPct(r.pnl_pct)}</td><td>${gb}</td><td>${esc(reasonName(r.close_reason))}</td></tr>`;
  }).join('');
}

function renderSlipTable(sl) {
  const tb = $('imp-slip-tbody');
  const phaseLabel = {FIRST_BUY:'1차 매수', PYRAMID_BUY:'피라미딩 매수', CLOSE_SELL:'청산 매도', TIMEOUT_SELL:'시간 청산 매도', SLIPPAGE_SELL:'슬리피지 청산'};
  const rows = Object.entries(sl.by_phase || {});
  if (!rows.length) {
    tb.innerHTML = '<tr><td colspan="5" class="empty">체결 데이터 없음</td></tr>';
    return;
  }
  tb.innerHTML = rows.map(([ph, v]) =>
    `<tr><td>${esc(phaseLabel[ph] || ph)}</td><td>${fmt(v.n)}건</td><td>${v.avg_pp}%p</td><td>${v.max_pp}%p</td><td>${fmt(v.avg_latency_ms)}ms</td></tr>`
  ).join('');
}

function renderSkipHold(d) {
  const el = $('imp-skip-hold');
  const skipLabel = {NO_TARGET:'후보 없음', GAP_CHANGED:'갭 이탈', ENTRY_FAIL:'진입 실패', SLIPPAGE_GUARD:'슬리피지', MANUAL:'수동'};
  const skipRows = Object.entries(d.candidates.skips || {})
    .map(([k, v]) => `<div class="factor-row"><span>${esc(skipLabel[k] || k)}</span><span>${fmt(v)}건</span></div>`)
    .join('') || '<div class="empty">스킵 없음</div>';
  const holdRows = Object.entries(d.hold_time || {})
    .map(([k, v]) => `<div class="factor-row"><span>${esc(reasonName(k))}</span><span>${v.avg_min}분 · ${fmt(v.n)}건</span></div>`)
    .join('') || '<div class="empty">데이터 없음</div>';
  el.innerHTML =
    `<div class="factor-cell"><div class="factor-name">스킵 사유</div>${skipRows}</div>` +
    `<div class="factor-cell"><div class="factor-name">청산사유별 평균 보유시간</div>${holdRows}</div>`;
}
```

- [ ] **Step 3: 수동 검증**

Task 4 Step 5와 동일하게 서버를 띄우고 '개선' 화면 진입.
Expected: 표 3개가 "폐쇄 거래 없음 / 체결 데이터 없음 / 스킵 없음·데이터 없음"으로 렌더되고 콘솔 에러 없음. (운영 DB가 있는 환경이면 실제 9건이 표시되는지 확인.)

- [ ] **Step 4: 커밋**

```bash
git add docs/html/assets/app.js
git commit -m "개선 메뉴 — 고점 반납·슬리피지·스킵/보유시간 상세 표"
```

---

### Task 6: 통계 화면 힌트 제거 + 캐시버스터 갱신

**Files:**
- Modify: `docs/html/index.html` (힌트 scard 제거, 스크립트 버전 갱신)
- Modify: `docs/html/assets/app.js` (`renderStatsHints` 및 호출 제거)
- Modify: `docs/html/assets/app.css` (힌트 전용 스타일 제거)

**Interfaces:**
- Consumes: 없음 (제거 작업).
- Produces: 통계 화면은 승률/청산사유/월별/전략축 카드만 유지. `sampleNote()`와 `reasonName()`은 다른 곳(통계 표본 안내줄, 개선 화면)에서 계속 쓰이므로 **유지**.

- [ ] **Step 1: index.html에서 힌트 카드 제거**

line 297~302의 다음 블록을 삭제:

```html
        <div class="scard full">
          <div class="card-ttl">개선 힌트</div>
          <div class="hint-list" id="stats-hints">
            <div class="empty">통계 로딩 중...</div>
          </div>
        </div>
```

- [ ] **Step 2: 캐시버스터 갱신**

index.html 마지막 줄의 스크립트 태그를 갱신:

```html
<script src="assets/app.js?v=20260715-improve"></script>
```

`<head>`의 `app.css` link 태그에도 `?v=` 쿼리가 있으면 같은 값(`20260715-improve`)으로 갱신. 없으면 그대로 둔다.

- [ ] **Step 3: app.js에서 힌트 로직 제거**

- line 1096의 `renderStatsHints(s);` 호출 삭제 (renderStats 안, `drawBar(s.by_reason);`와 `renderFactorGrid(s);` 사이).
- `function renderStatsHints(s) { ... }` 전체(line 1170~1183) 삭제.
- `sampleNote`, `reasonName`, `renderFactorGrid`는 삭제하지 않는다.

- [ ] **Step 4: app.css에서 힌트 전용 스타일 제거**

`.hint-list`, `.hint-item`, `.hint-k`, `.hint-v` 셀렉터의 규칙(line 245~248 부근)을 삭제. 삭제 전 `docs/html` 내에서 각 클래스명을 검색해 다른 사용처가 없음을 확인한다 (개선 화면은 `imp-*` 클래스만 사용).

- [ ] **Step 5: 회귀 확인 (수동 + 자동)**

Run: `python -m pytest tests/ -v`
Expected: 전부 PASS

서버를 띄우고(`python -m uvicorn src.api.server:app --port 8899`):
- 통계 화면: 힌트 카드가 사라지고 나머지 카드(승률·청산사유·월별·전략축) 정상, 콘솔에 `renderStatsHints is not defined` 에러 없음.
- 개선 화면: 카드·표 정상.

- [ ] **Step 6: 커밋**

```bash
git add docs/html/index.html docs/html/assets/app.js docs/html/assets/app.css
git commit -m "통계 화면 개선 힌트 제거 — 개선 메뉴 진단 카드로 대체"
```

---

## 완료 기준

- `python -m pytest tests/ -v` 전부 PASS.
- 사이드바 '개선' 메뉴 → 카드 7장(전략 종합, HARD_STOP, STEP_SIZE, STEP_TRAIL, 슬리피지 버퍼, F5 타임아웃, F1 갭 범위)이 실제 파라미터 현재값과 함께 표시.
- 표본 부족 시 각 카드에 "판정까지 N건 더 필요" 표시.
- 통계 화면에서 '개선 힌트' 섹션이 사라지고 기존 기능은 유지.
