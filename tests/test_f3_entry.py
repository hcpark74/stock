from unittest.mock import AsyncMock

import pytest

import src.modules.f3_entry as f3
from src import state

_REAL_FETCH_BUYABLE_QTY = f3._fetch_buyable_qty


def _buyable(qty: int = 999_999, amt: float = 999_999_999.0) -> dict:
    return {
        "nrcvb_buy_qty": qty,
        "nrcvb_buy_amt": amt,
        "max_buy_qty": qty,
        "max_buy_amt": amt,
        "ord_psbl_cash": amt,
    }


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
    monkeypatch.setattr(f3, "_fetch_buyable_qty", AsyncMock(return_value=_buyable()))
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


def test_entry_fill_deadline_is_relative_to_now(monkeypatch):
    monkeypatch.setattr(f3, "F3_ENTRY_FIRST_FILL_SEC", 12.0)

    now = f3.datetime.now(f3.KST)
    deadline = f3._deadline_datetime(f3._entry_fill_deadline(attempt=1, force=False))

    assert deadline > now
    assert (deadline - now).total_seconds() <= 13


def test_entry_first_fill_deadline_uses_wider_initial_window(monkeypatch):
    monkeypatch.setattr(f3, "F3_ENTRY_FIRST_FILL_SEC", 12.0)

    now = f3.datetime.now(f3.KST)
    first_deadline = f3._deadline_datetime(f3._entry_fill_deadline(attempt=1, force=False))

    assert 10 <= (first_deadline - now).total_seconds() <= 13


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
async def test_fetch_available_cash_prefers_orderable_cash(monkeypatch):
    events = []
    monkeypatch.setattr(
        f3.kis_rest,
        "get",
        AsyncMock(return_value={
            "rt_cd": "0",
            "output2": [{
                "ord_psbl_cash": "1,234",
                "dnca_tot_amt": "9,999",
                "prvs_rcdl_excc_amt": "8,888",
            }],
        }),
    )
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))

    assert await f3._fetch_available_cash() == 1234.0
    assert events == [
        (
            "BALANCE_CASH_CHECK",
            {
                "level": "DEBUG",
                "cash": 1234.0,
                "cash_source": "ord_psbl_cash",
                "ord_psbl_cash": 1234.0,
                "ord_psbl_present": True,
                "dnca_tot_amt": 9999.0,
                "prvs_rcdl_excc_amt": 8888.0,
            },
        )
    ]


@pytest.mark.asyncio
async def test_fetch_available_cash_does_not_fall_back_when_orderable_cash_is_zero(monkeypatch):
    monkeypatch.setattr(
        f3.kis_rest,
        "get",
        AsyncMock(return_value={
            "rt_cd": "0",
            "output2": [{
                "ord_psbl_cash": "0",
                "dnca_tot_amt": "2,345",
                "prvs_rcdl_excc_amt": "8,888",
            }],
        }),
    )
    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)

    assert await f3._fetch_available_cash() == 0.0


@pytest.mark.asyncio
async def test_fetch_available_cash_falls_back_when_orderable_cash_missing(monkeypatch):
    monkeypatch.setattr(
        f3.kis_rest,
        "get",
        AsyncMock(return_value={
            "rt_cd": "0",
            "output2": [{
                "dnca_tot_amt": "2,345",
                "prvs_rcdl_excc_amt": "3,456",
            }],
        }),
    )
    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)

    assert await f3._fetch_available_cash() == 2345.0


@pytest.mark.asyncio
async def test_fetch_buyable_qty_uses_market_order_psbl_api(monkeypatch):
    events = []
    get = AsyncMock(return_value={
        "rt_cd": "0",
        "output": {
            "nrcvb_buy_qty": "12",
            "nrcvb_buy_amt": "123,000",
            "max_buy_qty": "15",
            "max_buy_amt": "150,000",
            "ord_psbl_cash": "200,000",
        },
    })
    monkeypatch.setattr(f3.kis_rest, "get", get)
    monkeypatch.setattr(f3.kis_rest, "account_no", lambda: "12345678")
    monkeypatch.setattr(f3.kis_rest, "account_cd", lambda: "01")
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))

    result = await _REAL_FETCH_BUYABLE_QTY("005930", "PAPER")

    assert result["query_failed"] is False
    assert result["nrcvb_buy_qty"] == 12
    assert result["nrcvb_buy_amt"] == 123000.0
    get.assert_awaited_once()
    assert get.await_args.args == ("/uapi/domestic-stock/v1/trading/inquire-psbl-order",)
    assert get.await_args.kwargs["tr_id"] == "VTTC8908R"
    assert get.await_args.kwargs["params"] == {
        "CANO": "12345678",
        "ACNT_PRDT_CD": "01",
        "PDNO": "005930",
        "ORD_UNPR": "",
        "ORD_DVSN": "01",
        "CMA_EVLU_AMT_ICLD_YN": "N",
        "OVRS_ICLD_YN": "N",
    }
    assert events[-1][0] == "BUYABLE_QTY_CHECK"


@pytest.mark.asyncio
async def test_fetch_buyable_qty_uses_real_tr_id(monkeypatch):
    get = AsyncMock(return_value={"rt_cd": "0", "output": {"nrcvb_buy_qty": "1"}})
    monkeypatch.setattr(f3.kis_rest, "get", get)
    monkeypatch.setattr(f3.kis_rest, "account_no", lambda: "12345678")
    monkeypatch.setattr(f3.kis_rest, "account_cd", lambda: "01")
    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)

    await _REAL_FETCH_BUYABLE_QTY("005930", "REAL")

    assert get.await_args.kwargs["tr_id"] == "TTTC8908R"


@pytest.mark.asyncio
async def test_fetch_buyable_qty_error_returns_zero(monkeypatch):
    events = []
    monkeypatch.setattr(
        f3.kis_rest,
        "get",
        AsyncMock(return_value={"rt_cd": "1", "msg_cd": "ERR", "msg1": "no cash"}),
    )
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))

    result = await _REAL_FETCH_BUYABLE_QTY("005930", "PAPER")

    assert result["query_failed"] is True
    assert result["nrcvb_buy_qty"] == 0
    assert result["msg_cd"] == "ERR"
    assert events[-1][0] == "BUYABLE_QTY_ERROR"
    assert events[-1][1]["msg_cd"] == "ERR"

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
    assert blocked["reason"] == "GAP_RECHECK_UNAVAILABLE"
    assert state.get().day_skip is True
    assert state.get().close_reason == "GAP_RECHECK_UNAVAILABLE"
    send_buy.assert_not_awaited()
    f3.db.record_skip.assert_awaited_once()
    assert f3.db.record_skip.await_args.args[1] == "ENTRY_FAIL"


@pytest.mark.asyncio
async def test_prev_close_zero_blocks_entry_with_warn_log(monkeypatch):
    events = []
    send_buy = AsyncMock()
    notify = AsyncMock()
    _reset_state()

    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10310.0, 0.0)))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3.notifier, "send", notify)
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3.run(force=True)

    send_buy.assert_not_awaited()
    blocked = [kwargs for event, kwargs in events if event == "F3_ENTRY_BLOCKED"][-1]
    assert blocked["reason"] == "GAP_RECHECK_UNAVAILABLE"
    unavailable = [kwargs for event, kwargs in events if event == "GAP_RECHECK_UNAVAILABLE"][-1]
    assert unavailable["reason"] == "MISSING_PREV_CLOSE"
    assert state.get().day_skip is True
    assert state.get().close_reason == "GAP_RECHECK_UNAVAILABLE"
    notify.assert_awaited_once()


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
    notify = AsyncMock()
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
            "msg_cd": "APBK1234",
            "msg1": "rejected",
            "output": {},
        }),
    )
    monkeypatch.setattr(f3.notifier, "send", notify)
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3.run(force=True)

    entry_fail = [kwargs for event, kwargs in events if event == "ENTRY_FAIL"][-1]
    assert entry_fail["reason"] == "ORDER_REJECTED"
    assert state.get().position_status == "IDLE"
    assert state.get().day_skip is True
    f3.db.record_skip.assert_awaited_once()
    assert f3.db.record_skip.await_args.args[1] == "ENTRY_FAIL"
    notify.assert_awaited_once()
    assert notify.await_args.args[0] == "ENTRY_FAIL"
    assert notify.await_args.kwargs["ticker"] == "006340"
    assert "rejected" in notify.await_args.kwargs["message"]


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
async def test_full_cash_quantity_places_first_buy(monkeypatch):
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
    monkeypatch.setattr(f3, "_poll_fill", AsyncMock(return_value={"fill_price": 1000, "fill_qty": 9}))
    monkeypatch.setattr(f3, "_fetch_current_price", AsyncMock(return_value=1000))
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())

    await f3.run(force=True)

    assert send_buy.await_args.args == ("006340", 9, "PAPER")
    assert state.get().position_status == "HOLDING"



@pytest.mark.asyncio
async def test_entry_qty_is_clamped_by_buyable_quantity(monkeypatch):
    events = []
    _reset_state()
    send_buy = AsyncMock(return_value={
        "rt_cd": "0",
        "msg_cd": "MCA00000",
        "msg1": "OK",
        "output": {"ODNO": "0000000937", "KRX_FWDG_ORD_ORGNO": "001"},
    })

    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(1000.0, 970.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=10_000.0))
    monkeypatch.setattr(f3, "_fetch_buyable_qty", AsyncMock(return_value=_buyable(qty=5, amt=5_000.0)))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3, "_poll_fill", AsyncMock(return_value={"fill_price": 1000, "fill_qty": 5}))
    monkeypatch.setattr(f3, "_fetch_current_price", AsyncMock(return_value=1000))
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())

    await f3.run(force=True)

    assert send_buy.await_args.args == ("006340", 5, "PAPER")
    clamped = [kwargs for event, kwargs in events if event == "ENTRY_QTY_CLAMPED"][-1]
    assert clamped["planned_qty"] == 9
    assert clamped["buyable_qty"] == 5
    assert clamped["order_qty"] == 5


@pytest.mark.asyncio
async def test_entry_blocks_when_buyable_quantity_is_zero(monkeypatch):
    events = []
    _reset_state()
    send_buy = AsyncMock()
    notify = AsyncMock()

    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(1000.0, 970.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=10_000.0))
    monkeypatch.setattr(f3, "_fetch_buyable_qty", AsyncMock(return_value=_buyable(qty=0, amt=0.0)))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3.notifier, "send", notify)
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3.run(force=True)

    send_buy.assert_not_awaited()
    assert state.get().position_status == "IDLE"
    assert state.get().day_skip is True
    assert state.get().close_reason == "INSUFFICIENT_BALANCE"
    blocked = [kwargs for event, kwargs in events if event == "F3_ENTRY_BLOCKED"][-1]
    assert blocked["reason"] == "BUYABLE_QTY_ZERO"
    insufficient = [kwargs for event, kwargs in events if event == "INSUFFICIENT_BALANCE"][-1]
    assert insufficient["reason"] == "BUYABLE_QTY_ZERO"
    f3.db.record_skip.assert_awaited_once()
    notify.assert_awaited_once()
    assert notify.await_args.args[0] == "ENTRY_FAIL"


@pytest.mark.asyncio
async def test_buyable_query_failure_retries_without_day_skip(monkeypatch):
    events = []
    _reset_state()
    send_buy = AsyncMock(return_value={
        "rt_cd": "0",
        "msg_cd": "MCA00000",
        "msg1": "OK",
        "output": {"ODNO": "0000000937", "KRX_FWDG_ORD_ORGNO": "001"},
    })

    monkeypatch.setattr(f3, "F3_ENTRY_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(f3, "F3_ENTRY_RETRY_DELAY_SEC", 0)
    monkeypatch.setattr(f3, "_before_deadline", lambda deadline: True)
    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(1000.0, 970.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=10_000.0))
    monkeypatch.setattr(
        f3,
        "_fetch_buyable_qty",
        AsyncMock(side_effect=[
            {"query_failed": True, "rt_cd": "1", "msg_cd": "ConnectTimeout", "msg1": "timeout"},
            _buyable(qty=9, amt=9_000.0),
        ]),
    )
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3, "_poll_fill", AsyncMock(return_value={"fill_price": 1000, "fill_qty": 9}))
    monkeypatch.setattr(f3, "_fetch_current_price", AsyncMock(return_value=1000))
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())

    await f3.run(force=False)

    assert send_buy.await_count == 1
    assert state.get().day_skip is False
    assert state.get().position_status == "HOLDING"
    query_failed = [kwargs for event, kwargs in events if event == "BUYABLE_QTY_QUERY_FAILED"][-1]
    assert query_failed["entry_attempt"] == 1




@pytest.mark.asyncio
async def test_buyable_query_failure_reason_resets_after_successful_retry_unfilled(monkeypatch):
    events = []
    notify = AsyncMock()
    _reset_state()

    monkeypatch.setattr(f3, "F3_ENTRY_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(f3, "F3_ENTRY_RETRY_DELAY_SEC", 0)
    monkeypatch.setattr(f3, "F3_ENTRY_CANCEL_RELEASE_WAIT_SEC", 0)
    monkeypatch.setattr(f3, "_before_deadline", lambda deadline: True)
    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(1000.0, 970.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=10_000.0))
    monkeypatch.setattr(
        f3,
        "_fetch_buyable_qty",
        AsyncMock(side_effect=[
            {"query_failed": True, "rt_cd": "1", "msg_cd": "ConnectTimeout", "msg1": "timeout"},
            _buyable(qty=9, amt=9_000.0),
        ]),
    )
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
    monkeypatch.setattr(f3, "_poll_fill", AsyncMock(return_value=None))
    monkeypatch.setattr(f3, "_cancel_order", AsyncMock(return_value={"rt_cd": "0"}))
    monkeypatch.setattr(f3.notifier, "send", notify)
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3.run(force=False)

    entry_fail = [kwargs for event, kwargs in events if event == "ENTRY_FAIL"][-1]
    assert entry_fail["reason"] == "UNFILLED"
    assert "reason=UNFILLED" in f3.db.record_skip.await_args.args[2]
    assert "UNFILLED" in notify.await_args.kwargs["message"]


@pytest.mark.asyncio
async def test_full_first_entry_skips_pyramid_wait_when_no_second_qty(monkeypatch):
    events = []
    _reset_state()
    sleep_until = AsyncMock()
    fetch_current_price = AsyncMock(return_value=1000)

    monkeypatch.setattr(f3, "F3_ENTRY_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(f3, "_sleep_until", sleep_until)
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(1000.0, 970.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=10_000.0))
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
    monkeypatch.setattr(f3, "_poll_fill", AsyncMock(return_value={"fill_price": 1000, "fill_qty": 9}))
    monkeypatch.setattr(f3, "_fetch_current_price", fetch_current_price)
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())

    await f3.run(force=False)

    sleep_until.assert_not_awaited()
    fetch_current_price.assert_not_awaited()
    assert any(
        event == "PYRAMID_SKIPPED" and kwargs.get("reason") == "NO_SECOND_QTY"
        for event, kwargs in events
    )


@pytest.mark.asyncio
async def test_pyramid_fill_sends_executed_alert(monkeypatch):
    _reset_state()
    notify = AsyncMock()

    monkeypatch.setattr(f3, "FIRST_RATIO", 0.7)
    monkeypatch.setattr(f3, "F3_ENTRY_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(1000.0, 970.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=10_000.0))
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
    monkeypatch.setattr(
        f3,
        "_poll_fill",
        AsyncMock(side_effect=[
            {"fill_price": 1000, "fill_qty": 7},
            {"fill_price": 1006, "fill_qty": 3},
        ]),
    )
    monkeypatch.setattr(f3, "_fetch_current_price", AsyncMock(return_value=1006))
    monkeypatch.setattr(f3.notifier, "send", notify)
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.db, "mark_pyramided", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())

    await f3.run(force=True)

    notify.assert_any_await(
        "PYRAMID_EXECUTED",
        level="INFO",
        message="추가 매수: 006340 3주 @ 1,006원",
        ticker="006340",
    )


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
    monkeypatch.setattr(f3, "_poll_fill", AsyncMock(return_value={"fill_price": 10310, "fill_qty": 92}))
    monkeypatch.setattr(f3, "_fetch_current_price", AsyncMock(return_value=10300))
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())

    await f3.run(force=True)

    assert send_buy.await_args.args == ("GOOD02", 92, "PAPER")
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
    notify = AsyncMock()
    monkeypatch.setattr(f3.notifier, "send", notify)
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3.run(force=True)

    assert state.get().day_skip is True
    assert state.get().target_ticker is None
    assert state.get().position_status == "IDLE"
    send_buy.assert_not_awaited()
    f3.db.record_skip.assert_awaited_once()
    assert f3.db.record_skip.await_args.args[1] == "GAP_CHANGED"
    notify.assert_awaited_once()
    assert notify.await_args.args[0] == "GAP_CHANGED"
    assert notify.await_args.kwargs["ticker"] == "BAD001"


@pytest.mark.asyncio
async def test_entry_all_candidates_gap_recheck_unavailable_alerts_entry_fail(monkeypatch):
    _reset_state()
    state.get().target_ticker = "BAD001"
    state.get().target_candidates = [
        {"ticker": "BAD001"},
        {"ticker": "BAD002"},
    ]
    send_buy = AsyncMock()
    notify = AsyncMock()

    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        f3,
        "_fetch_expected_price",
        AsyncMock(side_effect=[
            (0.0, 10000.0),
            (0.0, 10000.0),
        ]),
    )
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3.notifier, "send", notify)
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3.run(force=True)

    assert state.get().day_skip is True
    assert state.get().target_ticker is None
    assert state.get().close_reason == "GAP_RECHECK_UNAVAILABLE"
    send_buy.assert_not_awaited()
    f3.db.record_skip.assert_awaited_once()
    assert f3.db.record_skip.await_args.args[1] == "ENTRY_FAIL"
    notify.assert_awaited_once()
    assert notify.await_args.args[0] == "ENTRY_FAIL"
    assert notify.await_args.kwargs["ticker"] == "BAD001"
    assert "GAP_RECHECK_UNAVAILABLE" in notify.await_args.kwargs["message"]


@pytest.mark.asyncio
async def test_entry_does_not_wait_for_first_order_time(monkeypatch):
    _reset_state()
    sleep_until = AsyncMock()
    send_buy = AsyncMock(return_value={
        "rt_cd": "0",
        "output": {"ODNO": "0000000937", "KRX_FWDG_ORD_ORGNO": "001"},
    })

    monkeypatch.setattr(f3, "F3_ENTRY_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(f3, "_sleep_until", sleep_until)
    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10310.0, 10000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3, "_poll_fill", AsyncMock(return_value=None))
    monkeypatch.setattr(f3, "_cancel_order", AsyncMock(return_value={"rt_cd": "0"}))
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3.run(force=False)

    send_buy.assert_awaited_once()
    sleep_until.assert_not_awaited()


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
