import pytest

from src import live, state


def _clear_state() -> None:
    live.clear_tick_history()
    s = state.get()
    s.trading_date = None
    s.target_ticker = None
    s.target_candidates = None
    s.entry_price = None
    s.entry_qty = None
    s.remaining_qty = None
    s.high_price = None
    s.position_status = "IDLE"
    s.close_reason = None
    s.order_id = None
    s.trailing_active = False
    s.highest_step = 0.0
    s.trade_id = 0
    s.daily_pnl_pct = 0.0
    s.day_skip = False


@pytest.fixture(autouse=True)
def clean_state():
    _clear_state()
    yield
    _clear_state()


async def test_new_trading_day_resets_daily_skip_state():
    await state.ensure_trading_day("20260627")
    s = state.get()
    s.day_skip = True
    s.target_ticker = "005930"
    s.close_reason = "NO_TARGET"
    s.position_status = "CLOSED"

    changed = await state.ensure_trading_day("20260628")

    assert changed is True
    assert s.trading_date == "20260628"
    assert s.day_skip is False
    assert s.target_ticker is None
    assert s.close_reason is None
    assert s.position_status == "IDLE"


async def test_new_trading_day_clears_tick_history():
    live.push_tick(75_000.0, ticker="005930")

    changed = await state.ensure_trading_day("20260703")

    assert changed is True
    assert live.tick_history() == []


async def test_same_trading_day_does_not_clear_day_skip():
    await state.ensure_trading_day("20260629")
    s = state.get()
    s.day_skip = True

    changed = await state.ensure_trading_day("20260629")

    assert changed is False
    assert s.day_skip is True


async def test_new_trading_day_does_not_clear_active_position():
    await state.ensure_trading_day("20260630")
    s = state.get()
    s.position_status = "HOLDING"
    s.target_ticker = "005930"
    s.remaining_qty = 10

    changed = await state.ensure_trading_day("20260701")

    assert changed is False
    assert s.trading_date == "20260630"
    assert s.position_status == "HOLDING"
    assert s.target_ticker == "005930"
    assert s.remaining_qty == 10


async def test_set_closed_clears_tick_history():
    s = state.get()
    s.position_status = "HOLDING"
    live.push_tick(75_000.0, ticker="005930")

    changed = await state.set_closed("TRAILING")

    assert changed is True
    assert live.tick_history() == []


async def test_target_candidates_persist_restore_round_trip(tmp_path):
    s = state.get()
    s.trading_date = "20260701"
    s.target_ticker = "005930"
    s.target_candidates = [
        {"ticker": "005930", "expected_amount": 10_000.0},
        {"ticker": "000660", "expected_amount": 9_000.0},
    ]

    await state.persist(str(tmp_path), "20260701")
    _clear_state()
    data = state.load(str(tmp_path))
    state.restore_from(data)

    restored = state.get()
    assert restored.target_ticker == "005930"
    assert restored.target_candidates == [
        {"ticker": "005930", "expected_amount": 10_000.0},
        {"ticker": "000660", "expected_amount": 9_000.0},
    ]


def test_restore_from_legacy_state_without_target_candidates():
    state.restore_from({
        "date": "20260701",
        "ticker": "005930",
        "position_status": "IDLE",
    })

    s = state.get()
    assert s.target_ticker == "005930"
    assert s.target_candidates is None
