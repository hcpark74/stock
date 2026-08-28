"""트랙 B 규칙 비교 하네스 검증.

선택 규칙이 미래를 보면 백테스트가 실시간에 없는 정보를 쓴다. 진입가가 신호 봉
종가면 봉이 닫히는 순간을 미리 안 것이 된다. 두 가지가 이 파일의 핵심이다.
"""

import pytest

from scripts.track_b_backtest import (
    ENTRY_DEADLINE,
    SIGNAL_START,
    bootstrap_ci,
    correlation,
    find_signal,
    gate_report,
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
        # 랭크 1 — 09:38 에 고가 돌파
        "AAA": _bars([("093500", 100), ("093600", 100), ("093700", 100),
                      ("093800", 130)]),
        # 랭크 2 — 09:37 에 먼저 돌파한다
        "BBB": _bars([("093500", 100), ("093600", 100), ("093700", 130),
                      ("093800", 100)]),
    }
    signal = find_signal(bars_by_ticker, ["AAA", "BBB"], "R1", DEFAULT_PARAMS)
    assert signal["ticker"] == "BBB"
    assert signal["signal_time"] == "093700"


def test_same_bar_tie_goes_to_higher_rank():
    bars_by_ticker = {
        "AAA": _bars([("093500", 100), ("093600", 100), ("093700", 130)]),
        "BBB": _bars([("093500", 100), ("093600", 100), ("093700", 130)]),
    }
    signal = find_signal(bars_by_ticker, ["AAA", "BBB"], "R1", DEFAULT_PARAMS)
    assert signal["ticker"] == "AAA"
    assert signal["rank"] == 1


def test_signal_ignores_bars_before_0935_and_after_1400():
    early = _bars([("091000", 100), ("091100", 130)])
    late = _bars([("135900", 100), ("140000", 100), ("140100", 130)])
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
