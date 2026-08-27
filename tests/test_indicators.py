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
