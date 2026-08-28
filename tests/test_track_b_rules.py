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


def test_exit_constants_match_track_a():
    """A와 다른 값이 되면 '진입 시각만 다르다'는 전제가 깨진다."""
    from src.modules import f4_tracking

    assert STEP_SIZE == f4_tracking.STEP_SIZE
    assert STEP_TRAIL == f4_tracking.STEP_TRAIL
    assert HARD_STOP == f4_tracking.HARD_STOP_RATIO
