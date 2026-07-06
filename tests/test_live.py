import pytest

from src import live


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
    live._tick_history.append({"ts": "2026-07-06T09:10:00+09:00", "ticker": "005930", "price": 75_000.0})
    live._tick_history.append({"ts": "2026-07-06T09:10:01+09:00", "ticker": "005930", "price": 75_500.0})

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
    live._tick_history.append({"ts": "2026-07-06T09:10:00+09:00", "ticker": "005930", "price": 75_000.0})
    live._tick_history.append({"ts": "2026-07-06T09:10:40+09:00", "ticker": "005930", "price": 75_300.0})
    live._tick_history.append({"ts": "2026-07-06T09:11:01+09:00", "ticker": "005930", "price": 75_500.0})

    rows = live.minute_price_history("005930", since="2026-07-06T09:10:00+09:00")

    assert [row["ts"] for row in rows] == [
        "2026-07-06T09:10:00+09:00",
        "2026-07-06T09:11:00+09:00",
    ]
    assert [row["price"] for row in rows] == [75_300.0, 75_500.0]
    assert [row["tick_count"] for row in rows] == [2, 1]
