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
    # ensure_worker는 실행 중인 루프가 있으면 실제 60초 asyncio 태스크를
    # 만든다. 이 파일의 비동기 테스트마다 그 태스크가 살아남고, 동기인
    # reset()이 await 없이 cancel()만 하면 "Task was destroyed but it is
    # pending" 잡음과 flake로 이어진다. 워커 자체를 직접 테스트하는
    # test_worker_logs_and_exits_without_propagating만 예외로 둔다.
    monkeypatch.setattr(bars, "ensure_worker", lambda *a, **k: None)
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


async def test_worker_logs_and_exits_without_propagating(monkeypatch):
    monkeypatch.setattr(bars, "_CORRECT_INTERVAL_SEC", 0.01)
    monkeypatch.setattr(bars, "_IDLE_STOP_SEC", 0.05)

    def exploding_drain():
        raise RuntimeError("drain exploded")

    monkeypatch.setattr(bars, "drain", exploding_drain)

    await bars.worker("20260827", "006340")   # 예외가 새어 나오면 실패
