from unittest.mock import AsyncMock

import pytest

import src.modules.f3_entry as f3
from src import state


def _reset_state() -> None:
    s = state.get()
    s.trading_date = "20260701"
    s.target_ticker = "006340"
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
    s.day_skip = False


@pytest.fixture(autouse=True)
def reset_fill_poll_summary(monkeypatch):
    f3._last_fill_poll_summary = {}
    monkeypatch.setattr(f3, "F3_PRE_ORDER_QUIET_SEC", 0)
    yield
    f3._last_fill_poll_summary = {}


def test_parse_deadline_logs_invalid_value(monkeypatch):
    events = []
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))

    result = f3._parse_deadline("09:bad:08", (9, 0, 8))

    assert result == (9, 0, 8)
    assert events[0][0] == "F3_DEADLINE_PARSE_ERROR"
    assert events[0][1]["value"] == "09:bad:08"
    assert events[0][1]["default"] == "09:00:08"


@pytest.mark.asyncio
async def test_pre_order_quiet_wait_logs_and_sleeps(monkeypatch):
    events = []
    sleep = AsyncMock()

    monkeypatch.setattr(f3, "F3_PRE_ORDER_QUIET_SEC", 1.5)
    monkeypatch.setattr(f3.asyncio, "sleep", sleep)
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))

    await f3._pre_order_quiet_wait("006340", 1, 2, 12900.0, 774)

    sleep.assert_awaited_once_with(1.5)
    assert events == [
        (
            "ENTRY_PRE_ORDER_WAIT",
            {
                "level": "INFO",
                "ticker": "006340",
                "phase": "ENTRY",
                "sleep_sec": 1.5,
                "order_price": 12900.0,
                "order_qty": 774,
                "entry_attempt": 1,
                "max_attempts": 2,
            },
        )
    ]


@pytest.mark.asyncio
async def test_entry_fail_logs_fill_poll_summary(monkeypatch):
    events = []
    _reset_state()

    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10310.0, 10000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(
        f3,
        "_send_buy",
        AsyncMock(return_value={
            "rt_cd": "0",
            "msg_cd": "MCA00000",
            "msg1": "OK",
            "output": {"ODNO": "0000000937", "KRX_FWDG_ORD_ORGNO": "001"},
        }),
    )
    monkeypatch.setattr(
        f3,
        "_cancel_order",
        AsyncMock(return_value={"rt_cd": "0", "msg_cd": "MCA00000", "msg1": "CANCELED"}),
    )
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())
    monkeypatch.setattr(
        f3,
        "_poll_fill",
        AsyncMock(return_value=None),
    )
    f3._last_fill_poll_summary = {
        "poll_attempts": 6,
        "poll_last_rt_cd": "0",
        "poll_last_msg_cd": "MCA00000",
        "poll_last_output_count": 0,
        "poll_last_matched": False,
    }

    await f3.run(force=True)

    entry_fail = [kwargs for event, kwargs in events if event == "ENTRY_FAIL"][-1]
    assert entry_fail["reason"] == "UNFILLED"
    assert entry_fail["order_id"] == "0000000937"
    assert entry_fail["poll_attempts"] == 6
    assert entry_fail["poll_last_matched"] is False


@pytest.mark.asyncio
async def test_price_unavailable_blocks_entry_with_reason(monkeypatch):
    events = []
    send_buy = AsyncMock()
    _reset_state()

    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(0.0, 10000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3.run(force=True)

    blocked = [kwargs for event, kwargs in events if event == "F3_ENTRY_BLOCKED"][-1]
    assert blocked["reason"] == "PRICE_UNAVAILABLE"
    assert state.get().day_skip is True
    assert state.get().close_reason == "PRICE_UNAVAILABLE"
    send_buy.assert_not_awaited()
    f3.db.record_skip.assert_awaited_once()
    assert f3.db.record_skip.await_args.args[1] == "ENTRY_FAIL"


@pytest.mark.asyncio
async def test_insufficient_balance_blocks_entry_with_reason(monkeypatch):
    events = []
    send_buy = AsyncMock()
    _reset_state()

    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10310.0, 10000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1.0))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3.run(force=True)

    blocked = [kwargs for event, kwargs in events if event == "F3_ENTRY_BLOCKED"][-1]
    assert blocked["reason"] == "QTY_ZERO"
    assert state.get().day_skip is True
    assert state.get().close_reason == "INSUFFICIENT_BALANCE"
    send_buy.assert_not_awaited()
    f3.db.record_skip.assert_awaited_once()
    assert f3.db.record_skip.await_args.args[1] == "ENTRY_FAIL"


@pytest.mark.asyncio
async def test_order_rejected_sets_day_skip(monkeypatch):
    events = []
    _reset_state()

    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10310.0, 10000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(
        f3,
        "_send_buy",
        AsyncMock(return_value={
            "rt_cd": "7",
            "msg_cd": "ORDER_REJECTED",
            "msg1": "rejected",
            "output": {},
        }),
    )
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3.run(force=True)

    entry_fail = [kwargs for event, kwargs in events if event == "ENTRY_FAIL"][-1]
    assert entry_fail["reason"] == "ORDER_REJECTED"
    assert state.get().position_status == "IDLE"
    assert state.get().day_skip is True
    f3.db.record_skip.assert_awaited_once()
    assert f3.db.record_skip.await_args.args[1] == "ENTRY_FAIL"


@pytest.mark.asyncio
async def test_state_collision_blocks_entry_with_reason(monkeypatch):
    events = []
    send_buy = AsyncMock()
    _reset_state()
    state.get().position_status = "HOLDING"

    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10310.0, 10000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "_send_buy", send_buy)

    await f3.run(force=True)

    blocked = [kwargs for event, kwargs in events if event == "F3_ENTRY_BLOCKED"][-1]
    assert blocked["reason"] == "STATE_NOT_IDLE"
    assert blocked["position_status"] == "HOLDING"
    send_buy.assert_not_awaited()


@pytest.mark.asyncio
async def test_single_share_quantity_still_places_buy(monkeypatch):
    _reset_state()
    send_buy = AsyncMock(return_value={
        "rt_cd": "0",
        "msg_cd": "MCA00000",
        "msg1": "OK",
        "output": {"ODNO": "0000000937", "KRX_FWDG_ORD_ORGNO": "001"},
    })

    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(1000.0, 970.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=10_000.0))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3, "_poll_fill", AsyncMock(return_value={"fill_price": 1000, "fill_qty": 1}))
    monkeypatch.setattr(f3, "_fetch_current_price", AsyncMock(return_value=1000))
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())

    await f3.run(force=True)

    assert send_buy.await_args.args == ("006340", 1, "PAPER")
    assert state.get().position_status == "HOLDING"


@pytest.mark.asyncio
async def test_entry_rechecks_all_candidates_and_picks_one_before_order(monkeypatch):
    events = []
    _reset_state()
    state.get().target_ticker = "BAD001"
    state.get().target_candidates = [
        {"ticker": "GOOD02"},
    ]
    send_buy = AsyncMock(return_value={
        "rt_cd": "0",
        "msg_cd": "MCA00000",
        "msg1": "OK",
        "output": {"ODNO": "0000000937", "KRX_FWDG_ORD_ORGNO": "001"},
    })

    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(
        f3,
        "_fetch_expected_price",
        AsyncMock(side_effect=[
            (10100.0, 10000.0),
            (10310.0, 10000.0),
            (10310.0, 10000.0),
        ]),
    )
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3, "_poll_fill", AsyncMock(return_value={"fill_price": 10310, "fill_qty": 6}))
    monkeypatch.setattr(f3, "_fetch_current_price", AsyncMock(return_value=10300))
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())

    await f3.run(force=True)

    assert send_buy.await_args.args == ("GOOD02", 6, "PAPER")
    assert state.get().target_ticker == "GOOD02"
    assert state.get().position_status == "HOLDING"
    assert "F3_CANDIDATE_SNAPSHOT_MISSING" in [event for event, _ in events]


@pytest.mark.asyncio
async def test_entry_all_candidates_fail_recheck_skips_without_order(monkeypatch):
    _reset_state()
    state.get().target_ticker = "BAD001"
    state.get().target_candidates = [
        {"ticker": "BAD001"},
        {"ticker": "BAD002"},
        {"ticker": "BAD003"},
    ]
    send_buy = AsyncMock()

    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        f3,
        "_fetch_expected_price",
        AsyncMock(side_effect=[
            (10100.0, 10000.0),
            (10150.0, 10000.0),
            (10800.0, 10000.0),
        ]),
    )
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3.run(force=True)

    assert state.get().day_skip is True
    assert state.get().target_ticker is None
    assert state.get().position_status == "IDLE"
    send_buy.assert_not_awaited()
    f3.db.record_skip.assert_awaited_once()
    assert f3.db.record_skip.await_args.args[1] == "GAP_CHANGED"


@pytest.mark.asyncio
async def test_entry_retries_after_unfilled_order(monkeypatch):
    events = []
    _reset_state()

    monkeypatch.setattr(f3, "F3_ENTRY_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(f3, "F3_ENTRY_RETRY_DELAY_SEC", 0)
    monkeypatch.setattr(f3, "_before_deadline", lambda deadline: True)
    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10310.0, 10000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(
        f3,
        "_send_buy",
        AsyncMock(side_effect=[
            {
                "rt_cd": "0",
                "msg_cd": "MCA00000",
                "msg1": "OK",
                "output": {"ODNO": "0000000937", "KRX_FWDG_ORD_ORGNO": "001"},
            },
            {
                "rt_cd": "0",
                "msg_cd": "MCA00000",
                "msg1": "OK",
                "output": {"ODNO": "0000000938", "KRX_FWDG_ORD_ORGNO": "001"},
            },
        ]),
    )
    monkeypatch.setattr(f3, "_cancel_order", AsyncMock(return_value={"rt_cd": "0"}))
    monkeypatch.setattr(
        f3,
        "_poll_fill",
        AsyncMock(side_effect=[None, {"fill_price": 10310, "fill_qty": 67}]),
    )
    monkeypatch.setattr(f3, "_fetch_current_price", AsyncMock(return_value=10300))
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())

    await f3.run()

    event_names = [event for event, _ in events]
    assert event_names.count("ENTRY_ORDER_SENT") == 2
    assert "ENTRY_RETRY_START" in event_names
    assert "ENTRY_EXECUTED" in event_names
    assert state.get().position_status == "HOLDING"


@pytest.mark.asyncio
async def test_entry_cancels_last_unfilled_attempt(monkeypatch):
    _reset_state()
    cancel_order = AsyncMock(return_value={"rt_cd": "0"})

    monkeypatch.setattr(f3, "F3_ENTRY_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(f3, "F3_ENTRY_RETRY_DELAY_SEC", 0)
    monkeypatch.setattr(f3, "_before_deadline", lambda deadline: True)
    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10310.0, 10000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(
        f3,
        "_send_buy",
        AsyncMock(side_effect=[
            {
                "rt_cd": "0",
                "output": {"ODNO": "0000000937", "KRX_FWDG_ORD_ORGNO": "001"},
            },
            {
                "rt_cd": "0",
                "output": {"ODNO": "0000000938", "KRX_FWDG_ORD_ORGNO": "001"},
            },
        ]),
    )
    monkeypatch.setattr(f3, "_poll_fill", AsyncMock(side_effect=[None, None]))
    monkeypatch.setattr(f3, "_cancel_order", cancel_order)
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3.run()

    assert cancel_order.await_count == 2
    assert cancel_order.await_args_list[-1].args[:3] == ("0000000938", "001", "PAPER")
    assert state.get().position_status == "IDLE"


@pytest.mark.asyncio
async def test_entry_fail_uses_last_run_attempt_when_retry_skipped(monkeypatch):
    events = []
    _reset_state()

    monkeypatch.setattr(f3, "F3_ENTRY_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(f3, "_before_deadline", lambda deadline: False)
    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10310.0, 10000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(
        f3,
        "_send_buy",
        AsyncMock(return_value={
            "rt_cd": "0",
            "output": {"ODNO": "0000000937", "KRX_FWDG_ORD_ORGNO": "001"},
        }),
    )
    monkeypatch.setattr(f3, "_poll_fill", AsyncMock(return_value=None))
    monkeypatch.setattr(f3, "_cancel_order", AsyncMock(return_value={"rt_cd": "0"}))
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3.run()

    event_names = [event for event, _ in events]
    assert "ENTRY_RETRY_SKIPPED" in event_names
    entry_fail = [kwargs for event, kwargs in events if event == "ENTRY_FAIL"][-1]
    assert entry_fail["entry_attempt"] == 1
    assert entry_fail["max_attempts"] == 2
    assert "attempts=1" in f3.db.record_skip.await_args.args[2]


@pytest.mark.asyncio
async def test_poll_fill_updates_summary_from_kis_response(monkeypatch):
    events = []
    future = f3.datetime.now(f3.KST) + f3.timedelta(seconds=30)
    deadline = (future.hour, future.minute, future.second)

    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(
        f3.kis_rest,
        "get",
        AsyncMock(return_value={
            "rt_cd": "0",
            "msg_cd": "MCA00000",
            "msg1": "OK",
            "output1": [
                {
                    "odno": "0000000937",
                    "tot_ccld_qty": "67",
                    "tot_ccld_amt": "690770",
                }
            ],
        }),
    )

    fill = await f3._poll_fill("0000000937", deadline=deadline, ticker="006340")

    assert fill == {"fill_price": 10310, "fill_qty": 67}
    assert f3._last_fill_poll_summary["poll_attempts"] == 1
    assert f3._last_fill_poll_summary["poll_last_matched"] is True
    assert f3._last_fill_poll_summary["poll_last_ccld_qty"] == 67
    assert f3._last_fill_poll_summary["poll_last_output_count"] == 1
    assert not events


@pytest.mark.asyncio
async def test_dry_run_entry_state_collision_records_skip(monkeypatch):
    _reset_state()
    state.get().position_status = "HOLDING"
    record_skip = AsyncMock()

    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(f3.db, "record_skip", record_skip)

    await f3._run_dry_entry("006340")

    record_skip.assert_awaited_once()
    args = record_skip.await_args.args
    assert args[1] == "DRY_RUN_F3_SKIPPED"
    assert "STATE_NOT_IDLE" in args[2]
