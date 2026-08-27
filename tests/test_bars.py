import asyncio
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


def test_a_spike_that_opens_a_minute_is_counted_too():
    # 봉이 아직 없다는 이유로 카운터를 건너뛰면 /api/bars의
    # meta.spike_dropped가 과소 보고된다.
    bars.on_tick(_tick(14570, minute="0935"))
    bars.drain()
    bars.on_tick(_tick(30000, minute="0936"))     # 0936의 첫 틱이 스파이크
    bars.on_tick(_tick(14600, minute="0936"))
    bars.drain()

    rows = bars.series("20260827", "006340")

    assert [r["time"] for r in rows] == ["093500", "093600"]
    assert rows[1]["spike_dropped"] == 1
    assert rows[1]["open"] == 14600.0             # 걸러진 가격은 OHLC에 없다
    assert rows[1]["tick_count"] == 1


def test_a_minute_made_only_of_spikes_opens_no_bar():
    bars.on_tick(_tick(14570, minute="0935"))
    bars.drain()
    bars.on_tick(_tick(30000, minute="0936"))
    bars.drain()

    assert [r["time"] for r in bars.series("20260827", "006340")] == ["093500"]


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


def test_a_ticker_switch_starts_a_separate_series_and_closes_the_old_one():
    bars.on_tick(_tick(14570, ticker="006340"))
    bars.on_tick(_tick(70000, ticker="005930"))
    bars.drain()

    # 새 계열은 이전 계열과 섞이지 않는다.
    assert len(bars.series("20260827", "005930")) == 1
    # 이전 계열은 메모리에서 마감되지만 디스크에는 온전히 남는다.
    assert ("20260827", "006340") not in bars._series
    closed = json.loads(
        bars.bars_path("20260827", "006340").read_text(encoding="utf-8")
    )
    assert [b["close"] for b in closed] == [14570.0]


def test_a_date_change_closes_the_previous_day_to_disk():
    bars.on_tick(_tick(14570, minute="0935"))
    bars.drain()

    tomorrow = _tick(14600, minute="0935")
    tomorrow["source_ts"] = tomorrow["received_at"] = "2026-08-28T09:35:00+09:00"
    bars.on_tick(tomorrow)
    bars.drain()

    assert ("20260827", "006340") not in bars._series
    assert len(bars.series("20260828", "006340")) == 1
    written = json.loads(
        bars.bars_path("20260827", "006340").read_text(encoding="utf-8")
    )
    assert [b["time"] for b in written] == ["093500"]
    assert written[0]["close"] == 14570.0


def test_a_re_traded_ticker_gets_a_fresh_spike_filter_the_next_day():
    # 어제 종가를 든 필터를 그대로 재사용하면, B의 전략 자체인 큰 시초 갭이
    # 첫 틱부터 스파이크로 걸러진다.
    bars.on_tick(_tick(14570, minute="0950"))
    bars.drain()
    yesterday_filter = bars._filters["006340"]

    gap_open = _tick(18000, minute="0900")      # +23.5% 갭 상승
    gap_open["source_ts"] = gap_open["received_at"] = "2026-08-28T09:00:00+09:00"
    bars.on_tick(gap_open)
    bars.drain()

    assert bars._filters["006340"] is not yesterday_filter
    rows = bars.series("20260828", "006340")
    assert len(rows) == 1
    assert rows[0]["open"] == 18000.0            # 걸러지지 않았다
    assert rows[0]["spike_dropped"] == 0


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


# ── 기동 배선 (FINDING 1) ────────────────────────────────────────────────


async def test_registered_ticks_become_bars_on_disk_without_any_other_wiring(monkeypatch):
    """install()+start()만으로 틱이 실제 봉이 되는지 끝에서 끝까지 확인한다.

    배선이 없던 동안 500틱을 넣어도 큐 500·봉 0이었다. drain()의 유일한
    호출자가 worker()이고 worker()는 _consume()이 있어야 생기는 순환이라,
    아무도 첫 drain을 돌리지 않았기 때문이다.
    """
    monkeypatch.setattr(bars, "_DRAIN_INTERVAL_SEC", 0.01)
    try:
        bars.install()
        bars.start()

        for price, minute in ((14570, "0935"), (14990, "0935"), (15100, "0936")):
            tick_capture.enqueue(_tick(price, minute=minute, qty=10))

        for _ in range(300):
            await asyncio.sleep(0.01)
            if len(bars.series("20260827", "006340")) == 2:
                break

        rows = bars.series("20260827", "006340")
        assert [r["time"] for r in rows] == ["093500", "093600"]
        assert rows[0]["open"] == 14570.0
        assert rows[0]["high"] == 14990.0
        assert rows[0]["volume"] == 20.0

        written = json.loads(
            bars.bars_path("20260827", "006340").read_text(encoding="utf-8")
        )
        assert [b["time"] for b in written] == ["093500", "093600"]
    finally:
        bars.reset()
        await asyncio.sleep(0)


async def test_start_is_idempotent():
    try:
        bars.start()
        first = bars._supervisor
        bars.start()

        assert bars._supervisor is first
        assert not first.done()
    finally:
        bars.reset()
        await asyncio.sleep(0)


def test_start_without_a_running_loop_returns_quietly():
    bars.start()

    assert bars._supervisor is None


async def test_stop_cancels_the_supervisor_and_clears_the_handle():
    bars.start()
    task = bars._supervisor

    bars.stop()
    await asyncio.sleep(0)

    assert task.cancelled()
    assert bars._supervisor is None
    bars.stop()          # 아무것도 돌지 않을 때 불러도 안전하다


async def test_reset_cancels_the_supervisor():
    bars.start()
    task = bars._supervisor

    bars.reset()
    await asyncio.sleep(0)

    assert task.cancelled()
    assert bars._supervisor is None


async def test_the_supervisor_survives_an_exploding_drain(monkeypatch):
    """수퍼바이저가 죽으면 봉 집계 전체가 소리 없이 멈춘다."""
    calls = {"n": 0}

    def exploding_drain():
        calls["n"] += 1
        raise RuntimeError("drain exploded")

    monkeypatch.setattr(bars, "_DRAIN_INTERVAL_SEC", 0.01)
    monkeypatch.setattr(bars, "drain", exploding_drain)
    try:
        bars.start()
        for _ in range(200):
            await asyncio.sleep(0.01)
            if calls["n"] >= 3:
                break

        assert calls["n"] >= 3                       # 계속 돈다
        assert not bars._supervisor.done()
    finally:
        bars.stop()
        await asyncio.sleep(0)
