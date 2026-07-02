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


def test_tick_history_keeps_recent_120_rows():
    for i in range(130):
        live.push_tick(float(i), ticker="005930")

    rows = live.tick_history("005930")

    assert len(rows) == 120
    assert rows[0]["price"] == 10.0
    assert rows[-1]["price"] == 129.0
