"""트랙 B 규칙 후보의 순수 함수 검증.

청산은 트랙 A와 같은 한 벌(하드스탑·스텝 트레일링·15:15)이어야 진입 시각만
비교된다. 봉은 내부 경로를 모르므로 고가·저가 순서를 양쪽으로 돌리고, 답이
갈리는 날은 AMBIGUOUS로 판정에서 뺀다 — STRATEGY_BACKTEST_20260820.md 관례.
"""

import pytest

from scripts.track_b_rules import (
    DEFAULT_PARAMS,
    HARD_STOP,
    RULES,
    STEP_SIZE,
    STEP_TRAIL,
    build_context,
    r1_high_reclaim,
    r2_vwap_reclaim,
    r3_indicator,
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
