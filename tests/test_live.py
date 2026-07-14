from datetime import datetime, timedelta

import pytest

from src import live

_T = datetime.fromisoformat


@pytest.fixture(autouse=True)
def clean_live_state():
    live.clear_tick_history()
    yield
    live.clear_tick_history()


def test_tick_history_filters_by_exact_ticker():
    live.push_tick(75_000.0)
    live.push_tick(75_500.0, ticker="005930")
    live.push_tick(120_000.0, ticker="000660")

    rows = live.tick_history("005930")

    assert len(rows) == 1
    assert rows[0]["ticker"] == "005930"
    assert rows[0]["price"] == 75_500.0


def test_tick_history_without_ticker_returns_all_rows():
    live.push_tick(75_000.0)
    live.push_tick(75_500.0, ticker="005930")

    rows = live.tick_history()

    assert [row["ticker"] for row in rows] == [None, "005930"]


def test_clear_tick_history_removes_all_rows():
    live.push_tick(75_500.0, ticker="005930")

    live.clear_tick_history()

    assert live.tick_history() == []


def test_tick_history_filters_since_timestamp():
    live._tick_history.append(
        {"ts": "2026-07-06T09:10:00+09:00", "ticker": "005930", "price": 75_000.0})
    live._tick_history.append(
        {"ts": "2026-07-06T09:10:01+09:00", "ticker": "005930", "price": 75_500.0})

    rows = live.tick_history("005930", since="2026-07-06T09:10:01+09:00")

    assert [row["price"] for row in rows] == [75_500.0]


def test_tick_history_keeps_recent_5000_rows():
    for i in range(5010):
        live.push_tick(float(i), ticker="005930")

    rows = live.tick_history("005930")

    assert len(rows) == 5000
    assert rows[0]["price"] == 10.0
    assert rows[-1]["price"] == 5009.0


def test_minute_price_history_uses_last_tick_per_minute():
    live._accumulate_minute(_T("2026-07-06T09:10:00+09:00"), "005930", 75_000.0)
    live._accumulate_minute(_T("2026-07-06T09:10:40+09:00"), "005930", 75_300.0)
    live._accumulate_minute(_T("2026-07-06T09:11:01+09:00"), "005930", 75_500.0)

    rows = live.minute_price_history("005930", since="2026-07-06T09:10:00+09:00")

    assert [row["ts"] for row in rows] == [
        "2026-07-06T09:10:00+09:00",
        "2026-07-06T09:11:00+09:00",
    ]
    assert [row["price"] for row in rows] == [75_300.0, 75_500.0]
    assert [row["tick_count"] for row in rows] == [2, 1]


def test_minute_price_history_since_includes_entry_minute():
    """entry_at이 09:10:30이어도 09:10 분봉은 포함되어야 한다 (분 단위 내림)."""
    live._accumulate_minute(_T("2026-07-06T09:09:50+09:00"), "005930", 74_000.0)
    live._accumulate_minute(_T("2026-07-06T09:10:40+09:00"), "005930", 75_300.0)

    rows = live.minute_price_history("005930", since="2026-07-06T09:10:30+09:00")

    assert [row["ts"] for row in rows] == ["2026-07-06T09:10:00+09:00"]


def test_minute_history_survives_tick_buffer_overflow():
    """tick 버퍼(5,000건)가 밀려나도 분봉은 유지된다 — 20분 복원 보장의 핵심."""
    base = _T("2026-07-06T09:00:00+09:00")
    # 12분 × 분당 500 tick = 6,000 tick → tick 버퍼는 앞 2분(1,000건)을 밀어낸다.
    for minute in range(12):
        for i in range(500):
            live._tick_history.append({
                "ts": (base + timedelta(minutes=minute, seconds=i * 0.1)).isoformat(),
                "ticker": "005930",
                "price": 10_000.0 + minute,
            })
            live._accumulate_minute(
                base + timedelta(minutes=minute), "005930", 10_000.0 + minute)

    assert len(live.tick_history("005930")) == 5000
    rows = live.minute_price_history("005930")
    assert len(rows) == 12
    assert rows[0]["ts"] == "2026-07-06T09:00:00+09:00"
    assert rows[0]["price"] == 10_000.0
    assert rows[-1]["price"] == 10_011.0


def test_push_tick_accumulates_minute_history():
    live.push_tick(75_500.0, ticker="005930")
    live.push_tick(75_600.0, ticker="005930")

    rows = live.minute_price_history("005930")

    assert len(rows) >= 1
    assert rows[-1]["price"] == 75_600.0
    assert sum(r["tick_count"] for r in rows) == 2
