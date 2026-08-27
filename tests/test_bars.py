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
    # NOTE: consecutive deltas here are deliberately kept under the
    # production SpikeFilter's 3% single-tick threshold (src/utils/
    # spike_filter.py) — this test is about OHLC aggregation, not spike
    # filtering (see test_spike_ticks_are_dropped_from_the_bar_but_counted
    # for that). The brief's original sample prices (14570, 15180, 14410,
    # 15080) have consecutive deltas of 4.19%/5.07%/4.65%, which the real
    # SpikeFilter legitimately rejects, so they were replaced here.
    for price in (14570, 14990, 14550, 14830):
        bars.on_tick(_tick(price, qty=10))
    bars.drain()

    rows = bars.series("20260827", "006340")

    assert len(rows) == 1
    assert rows[0]["open"] == 14570.0
    assert rows[0]["high"] == 14990.0
    assert rows[0]["low"] == 14550.0
    assert rows[0]["close"] == 14830.0
    assert rows[0]["volume"] == 40.0
    assert rows[0]["tick_count"] == 4


def test_a_new_minute_opens_a_new_bar():
    # Second price kept within the SpikeFilter's 3% threshold of the
    # first (see note in test_ohlc_comes_from_the_ticks_in_that_minute).
    bars.on_tick(_tick(14570, minute="0935"))
    bars.on_tick(_tick(14990, minute="0936"))
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
    # Second price kept within the SpikeFilter's 3% threshold of the
    # first (see note in test_ohlc_comes_from_the_ticks_in_that_minute).
    bars.on_tick(_tick(14570, minute="0935"))
    bars.on_tick(_tick(14990, minute="0936"))
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
