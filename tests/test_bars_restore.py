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
    # restore_day의 테스트는 _consume을 거치지 않지만, 이 파일의 픽스처가
    # 그 사실에 계속 의존하게 두지 않기 위해 다른 봉 테스트 파일과 동일하게
    # ensure_worker를 no-op으로 스텁한다.
    monkeypatch.setattr(bars, "ensure_worker", lambda *a, **k: None)
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
