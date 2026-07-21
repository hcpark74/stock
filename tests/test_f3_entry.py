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
    s.target_name = None
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
    monkeypatch.setattr(f3, "_fetch_vi_active", AsyncMock(return_value=None))
    # 마감 검사를 실제 시계와 분리 — 마감 동작 테스트는 개별적으로 False를 덮어쓴다
    monkeypatch.setattr(f3, "_before_deadline", lambda _deadline: True)
    yield
    f3._last_fill_poll_summary = {}


def test_gap_in_order_range_boundaries():
    """주문 전 갭 허용 구간의 경계 연산자를 고정한다: 하한 포함(<=), 상한 미포함(<).

    원 단위 정수 가격 쌍으로는 계산된 갭이 상수와 정확히 같아지지 않아
    (예: 10650/10000-1 == 0.06499999999999995) 가격 입력 테스트로는
    <와 <=의 회귀를 잡을 수 없다. 상수를 직접 넣어 경계를 검증한다.
    """
    assert f3._gap_in_order_range(f3.GAP_MIN_RECHECK) is True
    assert f3._gap_in_order_range(f3.GAP_MIN_RECHECK - 1e-9) is False
    assert f3._gap_in_order_range(f3.GAP_MAX_ORDER) is False
    assert f3._gap_in_order_range(f3.GAP_MAX_ORDER - 1e-9) is True


def test_fill_gap_reaches_max_boundary():
    """체결가 갭이 정확히 상한(7%)이면 청산 대상이다 (>=, 초과가 아닌 이상)."""
    assert f3._fill_gap_reaches_max(f3.GAP_MAX_FILL) is True
    assert f3._fill_gap_reaches_max(f3.GAP_MAX_FILL - 1e-9) is False


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
                "dnca_tot_amt": "3,456",
                "prvs_rcdl_excc_amt": "2,345",
            }],
        }),
    )
    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)

    assert await f3._fetch_available_cash() == 3456.0

@pytest.mark.asyncio
async def test_fetch_available_cash_uses_settlement_amount_when_larger(monkeypatch):
    # 매도대금 T+2 미결제 상태: 예수금(dnca)은 작지만 D+2 정산금(prvs)이 실제 주문가능금액에 가깝다
    events = []
    monkeypatch.setattr(
        f3.kis_rest,
        "get",
        AsyncMock(return_value={
            "rt_cd": "0",
            "output2": [{
                "dnca_tot_amt": "120,543",
                "prvs_rcdl_excc_amt": "8,865,465",
            }],
        }),
    )
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))

    assert await f3._fetch_available_cash() == 8_865_465.0
    assert events[0][1]["cash_source"] == "prvs_rcdl_excc_amt"

@pytest.mark.asyncio
async def test_fetch_available_cash_retries_rate_limit_then_succeeds(monkeypatch):
    """EGW00215 같은 일시 오류는 짧은 백오프 후 재시도해 정상 응답을 얻는다."""
    sleep = AsyncMock()
    get = AsyncMock(side_effect=[
        {"rt_cd": "1", "msg_cd": "EGW00215", "msg1": "초당 거래건수 초과"},
        {
            "rt_cd": "0",
            "output2": [{
                "ord_psbl_cash": "5,000",
                "dnca_tot_amt": "0",
                "prvs_rcdl_excc_amt": "0",
            }],
        },
    ])
    monkeypatch.setattr(f3.kis_rest, "get", get)
    monkeypatch.setattr(f3.asyncio, "sleep", sleep)
    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)

    assert await f3._fetch_available_cash() == 5000.0
    assert get.await_count == 2
    sleep.assert_awaited_once_with(f3.BALANCE_QUERY_RETRY_DELAY_SEC)

@pytest.mark.asyncio
async def test_fetch_available_cash_returns_none_when_retries_exhausted(monkeypatch):
    """잔고 조회가 끝내 실패하면 현금 0이 아니라 None(조회 실패)을 반환한다."""
    sleep = AsyncMock()
    get = AsyncMock(return_value={
        "rt_cd": "1", "msg_cd": "EGW00215", "msg1": "초당 거래건수 초과",
    })
    monkeypatch.setattr(f3.kis_rest, "get", get)
    monkeypatch.setattr(f3.asyncio, "sleep", sleep)
    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)

    assert await f3._fetch_available_cash() is None
    assert get.await_count == f3.BALANCE_QUERY_MAX_ATTEMPTS

@pytest.mark.asyncio
async def test_fetch_available_cash_retries_exception_then_succeeds(monkeypatch):
    """kis_rest.get 예외(JSON 파싱 오류 등)도 오류 응답과 동일하게 재시도한다."""
    sleep = AsyncMock()
    get = AsyncMock(side_effect=[
        RuntimeError("json parse error"),
        {
            "rt_cd": "0",
            "output2": [{
                "ord_psbl_cash": "5,000",
                "dnca_tot_amt": "0",
                "prvs_rcdl_excc_amt": "0",
            }],
        },
    ])
    monkeypatch.setattr(f3.kis_rest, "get", get)
    monkeypatch.setattr(f3.asyncio, "sleep", sleep)
    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)

    assert await f3._fetch_available_cash() == 5000.0
    assert get.await_count == 2

@pytest.mark.asyncio
async def test_fetch_available_cash_returns_none_when_exceptions_exhausted(monkeypatch):
    """예외가 계속되면 전파하지 않고 None을 반환해 단일 경로도 BALANCE_QUERY_FAILED로 수렴한다."""
    sleep = AsyncMock()
    get = AsyncMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(f3.kis_rest, "get", get)
    monkeypatch.setattr(f3.asyncio, "sleep", sleep)
    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)

    assert await f3._fetch_available_cash() is None
    assert get.await_count == f3.BALANCE_QUERY_MAX_ATTEMPTS

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
async def test_first_order_blocked_when_deadline_passed_before_balance(monkeypatch):
    """비강제 실행에서 마감(09:11)을 넘겼으면 잔고 조회 없이 최초 주문 전에 차단한다."""
    events = []
    send_buy = AsyncMock()
    notify = AsyncMock()
    fetch_cash = AsyncMock(return_value=1_000_000.0)
    _reset_state()

    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f3, "_existing_trade_for_today", AsyncMock(return_value=None))
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10310.0, 10000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", fetch_cash)
    monkeypatch.setattr(f3, "_before_deadline", lambda _deadline: False)
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3.notifier, "send", notify)
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3._run_single(force=False)

    send_buy.assert_not_awaited()
    fetch_cash.assert_not_awaited()
    assert state.get().day_skip is True
    blocked = [kwargs.get("reason") for event, kwargs in events if event == "F3_ENTRY_BLOCKED"]
    assert "ENTRY_DEADLINE_PASSED" in blocked
    f3.db.record_skip.assert_awaited_once()
    assert "ENTRY_DEADLINE_PASSED" in f3.db.record_skip.await_args.args[2]

@pytest.mark.asyncio
async def test_first_order_blocked_when_deadline_passed_with_picked(monkeypatch):
    """배치 경로에서 잔고 재시도로 지연된 경우에도 주문 직전 마감 검사가 최초 주문을 막는다."""
    send_buy = AsyncMock()
    notify = AsyncMock()
    _reset_state()
    picked = {
        "ticker": "006340",
        "expected_price": 10310.0,
        "prev_close": 10000.0,
        "cash": 1_000_000.0,
        "total_amount": 100_000,
        "total_qty": 9,
    }

    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(f3, "_existing_trade_for_today", AsyncMock(return_value=None))
    monkeypatch.setattr(f3, "_before_deadline", lambda _deadline: False)
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3.notifier, "send", notify)
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3._run_single(force=False, picked=picked)

    send_buy.assert_not_awaited()
    assert state.get().day_skip is True
    assert "ENTRY_DEADLINE_PASSED" in f3.db.record_skip.await_args.args[2]

@pytest.mark.asyncio
async def test_send_buy_blocked_when_deadline_passes_during_prep(monkeypatch):
    """초기 검사 통과 후 quiet wait·수량 조회 중 마감을 넘기면
    주문 직전 최종 검사가 전송을 막고 ENTERING을 IDLE로 되돌린다."""
    events = []
    send_buy = AsyncMock()
    notify = AsyncMock()
    _reset_state()

    # 잔고 조회 전·1차 주문 전 검사는 통과, _send_buy 직전 최종 검사에서 마감 초과
    deadline_checks = iter([True, True])
    monkeypatch.setattr(f3, "_before_deadline", lambda deadline: next(deadline_checks, False))
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f3, "_existing_trade_for_today", AsyncMock(return_value=None))
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10310.0, 10000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3.notifier, "send", notify)
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3._run_single(force=False)

    send_buy.assert_not_awaited()
    assert state.get().position_status == "IDLE"
    assert state.get().day_skip is True
    blocked = [kwargs.get("reason") for event, kwargs in events if event == "F3_ENTRY_BLOCKED"]
    assert "ENTRY_DEADLINE_PASSED" in blocked
    f3.db.record_skip.assert_awaited_once()
    assert "ENTRY_DEADLINE_PASSED" in f3.db.record_skip.await_args.args[2]
    assert "stage=AT_ORDER" in f3.db.record_skip.await_args.args[2]


@pytest.mark.asyncio
async def test_send_buy_blocked_when_deadline_passes_during_rest_rate_wait(monkeypatch):
    """A REST dispatch guard rejection must restore IDLE and use the deadline path."""
    events = []
    send_buy = AsyncMock(return_value={
        "rt_cd": "1",
        "msg_cd": f3.kis_rest.SEND_GUARD_BLOCKED_MSG_CD,
        "msg1": "request blocked by send guard",
    })
    _reset_state()

    monkeypatch.setattr(f3, "_before_deadline", lambda _deadline: True)
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f3, "_existing_trade_for_today", AsyncMock(return_value=None))
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10310.0, 10000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3._run_single(force=False)

    send_buy.assert_awaited_once()
    assert callable(send_buy.await_args.kwargs["send_guard"])
    assert state.get().position_status == "IDLE"
    assert state.get().day_skip is True
    event_names = [event for event, _kwargs in events]
    assert "ENTRY_ORDER_SENT" not in event_names
    assert "ENTRY_DEADLINE_PASSED" in event_names
    assert "stage=AT_HTTP_SEND" in f3.db.record_skip.await_args.args[2]

@pytest.mark.asyncio
async def test_run_single_cash_none_blocks_with_balance_query_failed(monkeypatch):
    """단일 진입 경로에서도 잔고 조회 실패(None)는 QTY_ZERO가 아니라 BALANCE_QUERY_FAILED로 차단한다."""
    events = []
    send_buy = AsyncMock()
    _reset_state()

    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f3, "_existing_trade_for_today", AsyncMock(return_value=None))
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10310.0, 10000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=None))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3._run_single(force=True)

    assert state.get().day_skip is True
    assert state.get().close_reason == "BALANCE_QUERY_FAILED"
    blocked = [kwargs.get("reason") for event, kwargs in events if event == "F3_ENTRY_BLOCKED"]
    assert "QTY_ZERO" not in blocked
    send_buy.assert_not_awaited()
    assert f3.notifier.send.await_args.args[0] == "BALANCE_QUERY_FAILED"
    f3.db.record_skip.assert_awaited_once()
    assert f3.db.record_skip.await_args.args[1] == "ENTRY_FAIL"
    assert "BALANCE_QUERY_FAILED" in f3.db.record_skip.await_args.args[2]

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
async def test_market_closed_rejection_records_market_closed_skip(monkeypatch):
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
            "rt_cd": "1",
            "msg_cd": "40100000",
            "msg1": "모의투자 영업일이 아닙니다.",
            "output": {},
        }),
    )
    monkeypatch.setattr(f3.notifier, "send", notify)
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3.run(force=True)

    assert "MARKET_CLOSED" in [event for event, _ in events]
    assert state.get().position_status == "IDLE"
    assert state.get().day_skip is True
    assert state.get().close_reason == "MARKET_CLOSED"
    f3.db.record_skip.assert_awaited_once()
    assert f3.db.record_skip.await_args.args[1] == "MARKET_CLOSED"
    notify.assert_awaited_once()
    assert notify.await_args.args[0] == "MARKET_CLOSED"
    assert "휴장일" in notify.await_args.kwargs["message"]


@pytest.mark.asyncio
async def test_market_closed_rejection_stops_candidate_retry(monkeypatch):
    events = []
    _reset_state()
    state.get().target_ticker = "BAD001"
    state.get().target_candidates = [
        {"ticker": "BAD001", "name": "Bad", "expected_amount": 2_000_000.0},
        {"ticker": "GOOD02", "name": "Good", "expected_amount": 1_000_000.0},
    ]
    send_buy = AsyncMock(return_value={
        "rt_cd": "1",
        "msg_cd": "40100000",
        "msg1": "모의투자 영업일이 아닙니다.",
        "output": {},
    })

    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(
        f3,
        "_fetch_expected_price",
        AsyncMock(side_effect=[
            (10310.0, 10000.0),
            (10400.0, 10000.0),
            (10400.0, 10000.0),
        ]),
    )
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3.run(force=True)

    # 휴장 거부는 다른 후보로 재시도해도 같은 결과 — 즉시 당일 중단해야 한다
    send_buy.assert_awaited_once()
    assert "ENTRY_CANDIDATE_RETRY" not in [event for event, _ in events]
    assert state.get().day_skip is True
    assert state.get().close_reason == "MARKET_CLOSED"
    f3.db.record_skip.assert_awaited_once()
    assert f3.db.record_skip.await_args.args[1] == "MARKET_CLOSED"
    f3.notifier.send.assert_awaited_once()
    assert f3.notifier.send.await_args.args[0] == "MARKET_CLOSED"


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
async def test_existing_open_trade_blocks_new_entry_and_restores_holding(monkeypatch):
    events = []
    send_buy = AsyncMock()
    _reset_state()

    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(
        f3.db,
        "get_trade_by_date",
        AsyncMock(return_value={
            "id": 77,
            "date": "20260708",
            "ticker": "005930",
            "name": "삼성전자",
            "entry_price": 75000.0,
            "entry_qty": 10,
            "status": "OPEN",
        }),
    )
    monkeypatch.setattr(f3, "_send_buy", send_buy)

    await f3.run(force=True)

    send_buy.assert_not_awaited()
    assert state.get().position_status == "HOLDING"
    assert state.get().target_ticker == "005930"
    assert state.get().target_name == "삼성전자"
    assert state.get().trade_id == 77
    blocked = [kwargs for event, kwargs in events if event == "F3_ENTRY_BLOCKED"][-1]
    assert blocked["reason"] == "TRADE_ALREADY_EXISTS"
    assert blocked["existing_status"] == "OPEN"

@pytest.mark.asyncio
async def test_existing_open_trade_clears_stale_target_name_when_db_has_no_name(monkeypatch):
    _reset_state()
    state.get().target_name = "다른후보"

    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        f3.db,
        "get_trade_by_date",
        AsyncMock(return_value={
            "id": 77,
            "date": "20260708",
            "ticker": "005930",
            "entry_price": 75000.0,
            "entry_qty": 10,
            "status": "OPEN",
        }),
    )
    monkeypatch.setattr(f3, "_send_buy", AsyncMock())

    await f3.run(force=True)

    assert state.get().target_ticker == "005930"
    assert state.get().target_name is None


@pytest.mark.asyncio
async def test_existing_closed_trade_blocks_new_entry_and_sets_day_skip(monkeypatch):
    events = []
    send_buy = AsyncMock()
    _reset_state()

    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(
        f3.db,
        "get_trade_by_date",
        AsyncMock(return_value={
            "id": 78,
            "date": "20260708",
            "ticker": "065770",
            "entry_price": 1869.0,
            "entry_qty": 327,
            "status": "CLOSED",
        }),
    )
    monkeypatch.setattr(f3, "_send_buy", send_buy)

    await f3.run(force=True)

    send_buy.assert_not_awaited()
    assert state.get().position_status == "IDLE"
    assert state.get().day_skip is True
    assert state.get().close_reason == "TRADE_ALREADY_EXISTS"
    blocked = [kwargs for event, kwargs in events if event == "F3_ENTRY_BLOCKED"][-1]
    assert blocked["reason"] == "TRADE_ALREADY_EXISTS"
    assert blocked["existing_status"] == "CLOSED"

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
async def test_entry_records_expected_price_as_order_price(monkeypatch):
    """orders.order_price에는 체결가가 아닌 주문 시점 예상가를 기록한다.

    두 값이 같으면 슬리피지 집계가 항상 0이 된다 — 체결가는 update_order_fill로만.
    """
    _reset_state()
    record_order = AsyncMock(return_value=1)
    update_fill = AsyncMock()

    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(1000.0, 970.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=10_000.0))
    monkeypatch.setattr(f3, "_send_buy", AsyncMock(return_value={
        "rt_cd": "0",
        "msg_cd": "MCA00000",
        "msg1": "OK",
        "output": {"ODNO": "0000000937", "KRX_FWDG_ORD_ORGNO": "001"},
    }))
    monkeypatch.setattr(f3, "_poll_fill", AsyncMock(return_value={"fill_price": 1010, "fill_qty": 9}))
    monkeypatch.setattr(f3, "_fetch_current_price", AsyncMock(return_value=1010))
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", record_order)
    monkeypatch.setattr(f3.db, "update_order_fill", update_fill)
    monkeypatch.setattr(f3.state, "persist", AsyncMock())

    await f3.run(force=True)

    assert record_order.await_args.args[4] == 1000.0  # 주문 시점 예상가
    assert update_fill.await_args.args[1] == 1010     # 체결가


@pytest.mark.asyncio
async def test_pyramid_records_current_price_as_order_price(monkeypatch):
    """피라미딩 주문도 order_price에는 주문 시점 현재가를 기록한다."""
    _reset_state()
    record_order = AsyncMock(return_value=1)
    update_fill = AsyncMock()

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
            {"fill_price": 1008, "fill_qty": 3},
        ]),
    )
    monkeypatch.setattr(f3, "_fetch_current_price", AsyncMock(return_value=1006))
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", record_order)
    monkeypatch.setattr(f3.db, "update_order_fill", update_fill)
    monkeypatch.setattr(f3.db, "mark_pyramided", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())

    await f3.run(force=True)

    assert record_order.await_args_list[0].args[4] == 1000.0  # 1차: 예상가
    assert record_order.await_args_list[1].args[4] == 1006    # 2차: 주문 시점 현재가
    assert update_fill.await_args_list[1].args[1] == 1008     # 2차 체결가


@pytest.mark.asyncio
async def test_entry_records_trade_with_target_name(monkeypatch):
    _reset_state()
    state.get().target_name = "대원전선"
    send_buy = AsyncMock(return_value={
        "rt_cd": "0",
        "msg_cd": "MCA00000",
        "msg1": "OK",
        "output": {"ODNO": "0000000937", "KRX_FWDG_ORD_ORGNO": "001"},
    })
    open_trade = AsyncMock(return_value=1)

    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(1000.0, 970.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=10_000.0))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(
        f3, "_poll_fill", AsyncMock(return_value={"fill_price": 1000, "fill_qty": 9}))
    monkeypatch.setattr(f3, "_fetch_current_price", AsyncMock(return_value=1000))
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", open_trade)
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())

    await f3.run(force=True)

    assert open_trade.await_args.kwargs.get("name") == "대원전선"


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
async def test_pick_final_entry_candidate_rechecks_quotes_concurrently(monkeypatch):
    _reset_state()
    state.get().target_ticker = "AAA001"
    state.get().target_candidates = [
        {"ticker": "AAA001", "name": "A", "expected_amount": 1_000_000.0},
        {"ticker": "BBB002", "name": "B", "expected_amount": 2_000_000.0},
    ]
    started: list[str] = []
    both_started = f3.asyncio.Event()

    async def fetch_expected_price(ticker: str, fallback_prev_close: float = 0.0):
        started.append(ticker)
        if len(started) >= 2:
            both_started.set()
        await both_started.wait()
        return (10310.0, 10000.0) if ticker == "AAA001" else (10400.0, 10000.0)

    monkeypatch.setattr(f3, "_fetch_expected_price", fetch_expected_price)
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)

    picked = await f3.asyncio.wait_for(f3._pick_final_entry_candidate(state.get()), timeout=1)

    assert started == ["AAA001", "BBB002"]
    assert picked["ticker"] == "BBB002"
    assert picked["total_qty"] == 91


@pytest.mark.asyncio
async def test_fetch_expected_price_uses_fallback_prev_close_without_retry(monkeypatch):
    get = AsyncMock(return_value={
        "rt_cd": "0",
        "output": {
            "antc_cnpr": "10310",
            "stck_prdy_clpr": "0",
        },
    })
    monkeypatch.setattr(f3, "F3_RECHECK_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(f3.kis_rest, "get", get)
    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)

    result = await f3._fetch_expected_price("005930", fallback_prev_close=10000.0)

    assert result == (10310.0, 10000.0)
    get.assert_awaited_once()

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
async def test_entry_retries_next_candidate_when_order_rejected(monkeypatch):
    events = []
    _reset_state()
    state.get().target_ticker = "BAD001"
    state.get().target_candidates = [
        {"ticker": "BAD001", "name": "Bad", "expected_amount": 2_000_000.0},
        {"ticker": "GOOD02", "name": "Good", "expected_amount": 1_000_000.0},
    ]
    send_buy = AsyncMock(side_effect=[
        {
            "rt_cd": "1",
            "msg_cd": "40070000",
            "msg1": "모의투자 주문처리가 안되었습니다(매매불가 종목)",
            "output": {},
        },
        {
            "rt_cd": "0",
            "msg_cd": "MCA00000",
            "msg1": "OK",
            "output": {"ODNO": "0000000938", "KRX_FWDG_ORD_ORGNO": "001"},
        },
    ])

    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(
        f3,
        "_fetch_expected_price",
        AsyncMock(side_effect=[
            (10310.0, 10000.0),
            (10400.0, 10000.0),
            (10400.0, 10000.0),
        ]),
    )
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3, "_poll_fill", AsyncMock(return_value={"fill_price": 10400, "fill_qty": 91}))
    monkeypatch.setattr(f3, "_fetch_current_price", AsyncMock(return_value=10400))
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())

    await f3.run(force=True)

    assert send_buy.await_args_list[0].args == ("BAD001", 92, "PAPER")
    assert send_buy.await_args_list[1].args == ("GOOD02", 91, "PAPER")
    assert state.get().target_ticker == "GOOD02"
    assert state.get().position_status == "HOLDING"
    final_pick = [kwargs for event, kwargs in events if event == "F3_FINAL_PICK"][-1]
    assert final_pick["name"] == "Bad"
    assert "ENTRY_CANDIDATE_RETRY" in [event for event, _ in events]
    f3.db.record_skip.assert_not_awaited()
    f3.notifier.send.assert_awaited_once()
    assert f3.notifier.send.await_args.args[0] == "ENTRY_EXECUTED"

@pytest.mark.asyncio
async def test_entry_retries_next_candidate_when_buyable_quantity_is_zero(monkeypatch):
    events = []
    _reset_state()
    state.get().target_ticker = "BAD001"
    state.get().target_candidates = [
        {"ticker": "BAD001", "name": "Bad", "expected_amount": 2_000_000.0},
        {"ticker": "GOOD02", "name": "Good", "expected_amount": 1_000_000.0},
    ]
    send_buy = AsyncMock(return_value={
        "rt_cd": "0",
        "msg_cd": "MCA00000",
        "msg1": "OK",
        "output": {"ODNO": "0000000938", "KRX_FWDG_ORD_ORGNO": "001"},
    })

    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(
        f3,
        "_fetch_expected_price",
        AsyncMock(side_effect=[
            (10310.0, 10000.0),
            (10400.0, 10000.0),
            (10400.0, 10000.0),
        ]),
    )
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(
        f3,
        "_fetch_buyable_qty",
        AsyncMock(side_effect=[
            {
                "query_failed": False,
                "nrcvb_buy_qty": 0,
                "nrcvb_buy_amt": 2_598_826.0,
                "max_buy_qty": 1,
                "max_buy_amt": 4_806_726.0,
                "ord_psbl_cash": 2_586_190.0,
            },
            _buyable(qty=91, amt=999_999_999.0),
        ]),
    )
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3, "_poll_fill", AsyncMock(return_value={"fill_price": 10400, "fill_qty": 91}))
    monkeypatch.setattr(f3, "_fetch_current_price", AsyncMock(return_value=10400))
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())

    await f3.run(force=True)

    send_buy.assert_awaited_once_with("GOOD02", 91, "PAPER")
    assert state.get().target_ticker == "GOOD02"
    assert state.get().position_status == "HOLDING"
    retry_events = [kwargs for event, kwargs in events if event == "ENTRY_CANDIDATE_RETRY"]
    assert retry_events[-1]["ticker"] == "BAD001"
    assert retry_events[-1]["reason"] == "BUYABLE_QTY_ZERO"
    f3.db.record_skip.assert_not_awaited()
    f3.notifier.send.assert_awaited_once()
    assert f3.notifier.send.await_args.args[0] == "ENTRY_EXECUTED"



@pytest.mark.asyncio
async def test_candidate_retry_reuses_ranked_candidates_and_refreshes_only_next(monkeypatch):
    _reset_state()
    state.get().target_ticker = "BAD001"
    state.get().target_candidates = [
        {"ticker": "BAD001", "name": "Bad", "expected_amount": 3_000_000.0},
        {"ticker": "GOOD02", "name": "Good", "expected_amount": 2_000_000.0},
        {"ticker": "THIRD3", "name": "Third", "expected_amount": 1_000_000.0},
    ]
    fetch_expected = AsyncMock(side_effect=[
        (10310.0, 10000.0),
        (10400.0, 10000.0),
        (10500.0, 10000.0),
        (10400.0, 10000.0),
    ])
    send_buy = AsyncMock(side_effect=[
        {"rt_cd": "1", "msg_cd": "40070000", "msg1": "주문 거절", "output": {}},
        {
            "rt_cd": "0",
            "msg_cd": "MCA00000",
            "msg1": "OK",
            "output": {"ODNO": "0000000938", "KRX_FWDG_ORD_ORGNO": "001"},
        },
    ])

    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "_pre_order_quiet_wait", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(f3, "_fetch_expected_price", fetch_expected)
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3, "_poll_fill", AsyncMock(return_value={"fill_price": 10400, "fill_qty": 91}))
    monkeypatch.setattr(f3, "_fetch_current_price", AsyncMock(return_value=10400))
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())

    await f3.run(force=True)

    assert fetch_expected.await_count == 4
    assert [call.args[0] for call in fetch_expected.await_args_list] == [
        "BAD001",
        "GOOD02",
        "THIRD3",
        "GOOD02",
    ]
    assert send_buy.await_args_list[0].args == ("BAD001", 92, "PAPER")
    assert send_buy.await_args_list[1].args == ("GOOD02", 91, "PAPER")
    assert state.get().target_ticker == "GOOD02"
    assert state.get().position_status == "HOLDING"


@pytest.mark.asyncio
async def test_candidate_retry_skips_next_candidate_when_freshness_gap_changes(monkeypatch):
    events = []
    _reset_state()
    state.get().target_ticker = "BAD001"
    state.get().target_candidates = [
        {"ticker": "BAD001", "name": "Bad", "expected_amount": 3_000_000.0},
        {"ticker": "STALE2", "name": "Stale", "expected_amount": 2_000_000.0},
        {"ticker": "GOOD03", "name": "Good", "expected_amount": 1_000_000.0},
    ]
    fetch_expected = AsyncMock(side_effect=[
        (10310.0, 10000.0),
        (10400.0, 10000.0),
        (10400.0, 10000.0),
        (10800.0, 10000.0),
        (10400.0, 10000.0),
    ])
    send_buy = AsyncMock(side_effect=[
        {"rt_cd": "1", "msg_cd": "40070000", "msg1": "주문 거절", "output": {}},
        {
            "rt_cd": "0",
            "msg_cd": "MCA00000",
            "msg1": "OK",
            "output": {"ODNO": "0000000939", "KRX_FWDG_ORD_ORGNO": "001"},
        },
    ])

    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "_pre_order_quiet_wait", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f3, "_fetch_expected_price", fetch_expected)
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3, "_poll_fill", AsyncMock(return_value={"fill_price": 10400, "fill_qty": 91}))
    monkeypatch.setattr(f3, "_fetch_current_price", AsyncMock(return_value=10400))
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())

    await f3.run(force=True)

    assert send_buy.await_args_list[0].args == ("BAD001", 92, "PAPER")
    assert send_buy.await_args_list[1].args == ("GOOD03", 91, "PAPER")
    assert state.get().target_ticker == "GOOD03"
    changed = [kwargs for event, kwargs in events if event == "GAP_CHANGED"][-1]
    assert changed["ticker"] == "STALE2"
    assert changed["freshness_check"] is True

@pytest.mark.asyncio
async def test_entry_recheck_uses_candidate_prev_close_when_quote_prev_close_missing(monkeypatch):
    _reset_state()
    state.get().target_ticker = "GOOD02"
    state.get().target_candidates = [
        {"ticker": "GOOD02", "prev_close": 10000.0, "gap_pct": 0.031, "expected_amount": 1_000_000.0},
    ]
    send_buy = AsyncMock(return_value={
        "rt_cd": "0",
        "msg_cd": "MCA00000",
        "msg1": "OK",
        "output": {"ODNO": "0000000937", "KRX_FWDG_ORD_ORGNO": "001"},
    })

    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10310.0, 0.0)))
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
    assert state.get().position_status == "HOLDING"
    f3.db.record_skip.assert_not_awaited()

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
async def test_entry_recheck_detects_gap_change_with_derived_prev_close(monkeypatch):
    """prev_close 미제공 시 스냅샷 가격으로 유도해야 갭 변동을 감지할 수 있다.

    라이브 가격으로 유도하면 재계산 갭 ≡ 스냅샷 갭이 되어
    GAP_CHANGED 가드가 무력화된다 (동어반복 버그).
    """
    _reset_state()
    state.get().target_ticker = "GOOD02"
    state.get().target_candidates = [
        # 스냅샷: 10310원 / 갭 3.1% → 유도 prev_close = 10000원
        {"ticker": "GOOD02", "gap_pct": 0.031, "expected_price": 10310.0,
         "expected_amount": 1_000_000.0},
    ]
    send_buy = AsyncMock()

    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)
    # 라이브 예상체결가 15% 급등, prev_close 미제공
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(11500.0, 0.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3.run(force=True)

    send_buy.assert_not_awaited()
    assert state.get().day_skip is True
    assert state.get().close_reason == "GAP_CHANGED"
    f3.db.record_skip.assert_awaited_once()
    assert f3.db.record_skip.await_args.args[1] == "GAP_CHANGED"

@pytest.mark.asyncio
async def test_candidate_retry_stops_at_deadline(monkeypatch):
    events = []
    _reset_state()
    state.get().target_ticker = "BAD001"
    state.get().target_candidates = [
        {"ticker": "BAD001", "name": "Bad", "expected_amount": 2_000_000.0},
        {"ticker": "GOOD02", "name": "Good", "expected_amount": 1_000_000.0},
    ]
    send_buy = AsyncMock(return_value={
        "rt_cd": "1",
        "msg_cd": "40070000",
        "msg1": "주문 거절",
        "output": {},
    })

    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "_pre_order_quiet_wait", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    # 첫 주문 전·전송 직전 검사는 통과, 이후(후보 재시도 시점)는 마감 초과
    deadline_checks = iter([True, True])
    monkeypatch.setattr(f3, "_before_deadline", lambda deadline: next(deadline_checks, False))
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10310.0, 10000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_fetch_buyable_qty", AsyncMock(return_value=_buyable(qty=92, amt=999_999_999.0)))
    notify = AsyncMock()
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3.notifier, "send", notify)
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3.run(force=False)

    # 마감시각 초과 → 첫 후보 거절 후 다음 후보를 시도하지 않는다
    send_buy.assert_awaited_once()
    assert state.get().day_skip is True
    skipped = [kwargs for event, kwargs in events if event == "ENTRY_CANDIDATE_RETRY_SKIPPED"]
    assert skipped and skipped[-1]["reason"] == "DEADLINE_REACHED"
    f3.db.record_skip.assert_awaited_once()
    assert "CANDIDATE_RETRY_DEADLINE" in f3.db.record_skip.await_args.args[2]
    notify.assert_awaited_once()
    assert notify.await_args.args[0] == "ENTRY_FAIL"

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
    # 잔고 조회 전·첫 주문 전·전송 직전 검사는 통과, 2차 시도 시점은 마감 초과
    deadline_checks = iter([True, True, True])
    monkeypatch.setattr(f3, "_before_deadline", lambda deadline: next(deadline_checks, False))
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


@pytest.mark.asyncio
async def test_recheck_batch_timeout_uses_completed_candidates(monkeypatch):
    events = []
    _reset_state()
    state.get().target_ticker = "SLOW01"
    state.get().target_candidates = [
        {"ticker": "SLOW01", "name": "Slow", "expected_amount": 3_000_000.0},
        {"ticker": "FAST02", "name": "Fast", "expected_amount": 2_000_000.0},
    ]

    async def fetch_expected_price(ticker: str, fallback_prev_close: float = 0.0):
        if ticker == "SLOW01":
            await f3.asyncio.sleep(0.05)
        return (10400.0, 10000.0)

    monkeypatch.setattr(f3, "F3_RECHECK_BATCH_TIMEOUT_SEC", 0.01)
    monkeypatch.setattr(f3, "_fetch_expected_price", fetch_expected_price)
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))

    ranked = await f3._rank_final_entry_candidates(state.get())

    assert [item["ticker"] for item in ranked] == ["FAST02"]
    timeout = [kwargs for event, kwargs in events if event == "F3_RECHECK_BATCH_TIMEOUT"][-1]
    assert timeout["requested_count"] == 2
    assert timeout["completed_count"] == 1
    assert timeout["pending_tickers"] == ["SLOW01"]

@pytest.mark.asyncio
async def test_recheck_candidate_exception_blocks_only_that_candidate(monkeypatch):
    events = []
    _reset_state()
    state.get().target_ticker = "BAD001"
    state.get().target_candidates = [
        {"ticker": "BAD001", "name": "Bad", "expected_amount": 3_000_000.0},
        {"ticker": "GOOD02", "name": "Good", "expected_amount": 2_000_000.0},
    ]

    async def fetch_expected_price(ticker: str, fallback_prev_close: float = 0.0):
        if ticker == "BAD001":
            raise RuntimeError("quote down")
        return 10400.0, 10000.0

    monkeypatch.setattr(f3, "_fetch_expected_price", fetch_expected_price)
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))

    ranked = await f3._rank_final_entry_candidates(state.get())

    assert [item["ticker"] for item in ranked] == ["GOOD02"]
    event_names = [event for event, _ in events]
    assert "F3_RECHECK_QUOTE_ERROR" in event_names
    assert "F3_RECHECK_BATCH_TIMING" in event_names
    blocked = [kwargs for event, kwargs in events if event == "F3_ENTRY_BLOCKED"]
    assert any(item["ticker"] == "BAD001" and item["reason"] == "GAP_RECHECK_UNAVAILABLE" for item in blocked)


@pytest.mark.asyncio
async def test_recheck_cash_exception_blocks_with_balance_query_failed(monkeypatch):
    """잔고 조회 예외는 현금 0(INSUFFICIENT_BALANCE)이 아니라 BALANCE_QUERY_FAILED로 차단한다."""
    events = []
    _reset_state()
    state.get().target_ticker = "GOOD01"
    state.get().target_candidates = [
        {"ticker": "GOOD01", "name": "Good", "expected_amount": 2_000_000.0},
    ]

    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10400.0, 10000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(side_effect=RuntimeError("balance down")))
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    ranked = await f3._rank_final_entry_candidates(state.get())

    assert ranked is None
    assert state.get().day_skip is True
    assert state.get().close_reason == "BALANCE_QUERY_FAILED"
    assert "BALANCE_QUERY_ERROR" in [event for event, _ in events]
    blocked = [kwargs.get("reason") for event, kwargs in events if event == "F3_ENTRY_BLOCKED"]
    assert "QTY_ZERO" not in blocked
    f3.notifier.send.assert_awaited_once()
    assert f3.notifier.send.await_args.args[0] == "BALANCE_QUERY_FAILED"
    f3.db.record_skip.assert_awaited_once()
    assert f3.db.record_skip.await_args.args[1] == "ENTRY_FAIL"
    assert "BALANCE_QUERY_FAILED" in f3.db.record_skip.await_args.args[2]


@pytest.mark.asyncio
async def test_recheck_cash_none_blocks_with_balance_query_failed(monkeypatch):
    """잔고 조회가 재시도 끝에 None을 반환하면 후보 재검증 없이 BALANCE_QUERY_FAILED로 차단한다."""
    _reset_state()
    state.get().target_ticker = "GOOD01"
    state.get().target_candidates = [
        {"ticker": "GOOD01", "name": "Good", "expected_amount": 2_000_000.0},
    ]

    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10400.0, 10000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=None))
    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    ranked = await f3._rank_final_entry_candidates(state.get())

    assert ranked is None
    assert state.get().day_skip is True
    assert state.get().close_reason == "BALANCE_QUERY_FAILED"
    assert f3.notifier.send.await_args.args[0] == "BALANCE_QUERY_FAILED"
    assert "BALANCE_QUERY_FAILED" in f3.db.record_skip.await_args.args[2]


@pytest.mark.asyncio
async def test_slippage_guard_allows_fill_when_gap_stays_below_max(monkeypatch):
    """체결가가 expected_price보다 비싸도 갭 상한(7%) 안이면 보유를 유지한다."""
    _reset_state()
    send_sell = AsyncMock()

    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)
    # 재검증 갭 3.09% (1000/970). 체결 1015원 → expected 대비 +1.5%지만 갭 4.64% < 7%.
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(1000.0, 970.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=10_000.0))
    monkeypatch.setattr(f3, "_send_buy", AsyncMock(return_value={
        "rt_cd": "0",
        "msg_cd": "MCA00000",
        "msg1": "OK",
        "output": {"ODNO": "0000000937", "KRX_FWDG_ORD_ORGNO": "001"},
    }))
    monkeypatch.setattr(f3, "_poll_fill", AsyncMock(return_value={"fill_price": 1015, "fill_qty": 9}))
    monkeypatch.setattr(f3, "_fetch_current_price", AsyncMock(return_value=1015))
    monkeypatch.setattr(f3, "_send_sell", send_sell)
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())

    await f3.run(force=True)

    send_sell.assert_not_awaited()
    f3.db.record_skip.assert_not_awaited()
    assert state.get().position_status == "HOLDING"
    assert state.get().day_skip is False
    assert state.get().close_reason is None


@pytest.mark.asyncio
async def test_slippage_guard_exits_when_fill_gap_exceeds_max(monkeypatch):
    """체결가 기준 갭이 상한(7%)을 넘으면 즉시 청산하고 당일 스킵한다."""
    events = []
    _reset_state()
    send_sell = AsyncMock()

    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    # 재검증 갭 6.19% (1030/970) 통과. 체결 1045원 → 갭 7.73% > 7% 상한.
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(1030.0, 970.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=10_000.0))
    monkeypatch.setattr(f3, "_send_buy", AsyncMock(return_value={
        "rt_cd": "0",
        "msg_cd": "MCA00000",
        "msg1": "OK",
        "output": {"ODNO": "0000000937", "KRX_FWDG_ORD_ORGNO": "001"},
    }))
    monkeypatch.setattr(f3, "_poll_fill", AsyncMock(return_value={"fill_price": 1045, "fill_qty": 9}))
    monkeypatch.setattr(f3, "_fetch_current_price", AsyncMock(return_value=1045))
    monkeypatch.setattr(f3, "_send_sell", send_sell)
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())

    await f3.run(force=True)

    send_sell.assert_awaited_once_with("006340", 9, "PAPER")
    f3.db.open_trade.assert_not_awaited()
    assert state.get().day_skip is True
    assert state.get().close_reason == "SLIPPAGE_GUARD"
    f3.db.record_skip.assert_awaited_once()
    assert f3.db.record_skip.await_args.args[1] == "SLIPPAGE_GUARD"
    guard = [kwargs for event, kwargs in events if event == "SLIPPAGE_GUARD"][-1]
    assert guard["fill_gap_pct"] == pytest.approx(7.732, abs=0.001)
    assert guard["gap_max_pct"] == 7.0


@pytest.mark.asyncio
async def test_entry_recheck_blocks_gap_between_order_max_and_fill_max(monkeypatch):
    """주문 전 갭이 주문 상한(6.5%) 이상이면 체결 상한(7%) 미만이라도 주문하지 않는다.

    상한 근처 갭은 시장가 슬리피지로 체결 상한을 넘겨 SLIPPAGE_GUARD
    즉시 청산(확정 손실)으로 이어지기 쉬우므로, 6.5~7.0% 구간은
    주문 전에 걸러 슬리피지 버퍼를 확보한다.
    """
    _reset_state()
    send_buy = AsyncMock()

    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)
    # 재검증 갭 6.7% (10670/10000) — 주문 상한 6.5%와 체결 상한 7% 사이
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10670.0, 10000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    notify = AsyncMock()
    monkeypatch.setattr(f3.notifier, "send", notify)
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3.run(force=True)

    send_buy.assert_not_awaited()
    assert state.get().day_skip is True
    assert state.get().close_reason == "GAP_CHANGED"
    f3.db.record_skip.assert_awaited_once()
    assert f3.db.record_skip.await_args.args[1] == "GAP_CHANGED"


@pytest.mark.asyncio
async def test_slippage_guard_allows_fill_in_order_fill_buffer_zone(monkeypatch):
    """체결가 갭이 주문 상한(6.5%)~체결 상한(7%) 사이면 보유를 유지한다.

    체결 후 가드는 체결 상한(GAP_MAX_FILL) 기준이어야 하며,
    주문 상한(GAP_MAX_ORDER)을 잘못 적용하면 버퍼 구간 체결이
    불필요한 즉시 청산으로 이어진다.
    """
    _reset_state()
    send_sell = AsyncMock()

    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)
    # 재검증 갭 6.19% (1030/970) 통과. 체결 1037원 → 갭 6.91% (6.5%~7% 버퍼 구간)
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(1030.0, 970.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=10_000.0))
    monkeypatch.setattr(f3, "_send_buy", AsyncMock(return_value={
        "rt_cd": "0",
        "msg_cd": "MCA00000",
        "msg1": "OK",
        "output": {"ODNO": "0000000937", "KRX_FWDG_ORD_ORGNO": "001"},
    }))
    monkeypatch.setattr(f3, "_poll_fill", AsyncMock(return_value={"fill_price": 1037, "fill_qty": 9}))
    monkeypatch.setattr(f3, "_fetch_current_price", AsyncMock(return_value=1037))
    monkeypatch.setattr(f3, "_send_sell", send_sell)
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())

    await f3.run(force=True)

    send_sell.assert_not_awaited()
    f3.db.record_skip.assert_not_awaited()
    assert state.get().position_status == "HOLDING"
    assert state.get().day_skip is False
    assert state.get().close_reason is None

# ── 진입 전 VI 체크 ───────────────────────────────────────────────────

_VI_ACTIVE_INFO = {
    "vi_kind_code": "2", "cntg_vi_hour": "090032", "bsop_date": "20260720",
    "vi_prc": "1140", "vi_stnd_prc": "0", "vi_dprt": "0.00", "vi_count": "1",
}


@pytest.mark.asyncio
async def test_entry_blocked_when_vi_active_single_candidate(monkeypatch):
    """2026-07-20 멤레이비티: VI 정지 중 시장가 진입 → 전량 미체결·당일 종료.

    진입 직전 VI 발동 중이면 주문을 내지 않고 차단해야 한다.
    단일 후보 경로에서는 당일 스킵으로 기록한다."""
    events = []
    _reset_state()
    send_buy = AsyncMock()
    notify = AsyncMock()

    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10310.0, 10000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3.notifier, "send", notify)
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())
    monkeypatch.setattr(
        f3, "_fetch_vi_active", AsyncMock(return_value=dict(_VI_ACTIVE_INFO)))

    await f3.run(force=True)

    send_buy.assert_not_awaited()
    assert state.get().day_skip is True
    assert state.get().close_reason == "VI_ACTIVE"
    f3.db.record_skip.assert_awaited_once()
    assert f3.db.record_skip.await_args.args[1] == "VI_ACTIVE"
    notify.assert_awaited_once()
    assert notify.await_args.args[0] == "VI_ENTRY_BLOCKED"
    blocked = [kwargs for event, kwargs in events if event == "F3_ENTRY_BLOCKED"]
    assert blocked and blocked[-1]["reason"] == "VI_ACTIVE"


@pytest.mark.asyncio
async def test_entry_retries_next_candidate_when_vi_active(monkeypatch):
    """1순위가 VI 정지 중이면 취소·재주문으로 시간을 허비하지 않고
    즉시 다음 후보로 넘어가야 한다."""
    events = []
    _reset_state()
    state.get().target_ticker = "BAD001"
    state.get().target_candidates = [
        {"ticker": "BAD001", "name": "Bad", "expected_amount": 2_000_000.0},
        {"ticker": "GOOD02", "name": "Good", "expected_amount": 1_000_000.0},
    ]
    send_buy = AsyncMock(return_value={
        "rt_cd": "0",
        "msg_cd": "MCA00000",
        "msg1": "OK",
        "output": {"ODNO": "0000000938", "KRX_FWDG_ORD_ORGNO": "001"},
    })

    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(
        f3,
        "_fetch_expected_price",
        AsyncMock(side_effect=[
            (10310.0, 10000.0),
            (10400.0, 10000.0),
            (10400.0, 10000.0),
        ]),
    )
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_fetch_buyable_qty", AsyncMock(return_value=_buyable(qty=91)))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(
        f3, "_poll_fill", AsyncMock(return_value={"fill_price": 10400, "fill_qty": 91}))
    monkeypatch.setattr(f3, "_fetch_current_price", AsyncMock(return_value=10400))
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())
    monkeypatch.setattr(
        f3, "_fetch_vi_active",
        AsyncMock(side_effect=[dict(_VI_ACTIVE_INFO), None]))

    await f3.run(force=True)

    send_buy.assert_awaited_once_with("GOOD02", 91, "PAPER")
    assert state.get().target_ticker == "GOOD02"
    assert state.get().position_status == "HOLDING"
    retry_events = [kwargs for event, kwargs in events if event == "ENTRY_CANDIDATE_RETRY"]
    assert retry_events[-1]["ticker"] == "BAD001"
    assert retry_events[-1]["reason"] == "VI_ACTIVE"
    f3.db.record_skip.assert_not_awaited()
    assert f3.notifier.send.await_args.args[0] == "ENTRY_EXECUTED"


@pytest.mark.asyncio
async def test_entry_proceeds_when_vi_check_fails(monkeypatch):
    """VI 조회 실패는 진입을 막지 않는다(fail-open) — 관측 실패로 기회를 버리지 않는다."""
    events = []
    _reset_state()
    send_buy = AsyncMock(return_value={
        "rt_cd": "0",
        "msg_cd": "MCA00000",
        "msg1": "OK",
        "output": {"ODNO": "0000000938", "KRX_FWDG_ORD_ORGNO": "001"},
    })

    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10310.0, 10000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_fetch_buyable_qty", AsyncMock(return_value=_buyable(qty=92)))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(
        f3, "_poll_fill", AsyncMock(return_value={"fill_price": 10310, "fill_qty": 92}))
    monkeypatch.setattr(f3, "_fetch_current_price", AsyncMock(return_value=10310))
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())
    monkeypatch.setattr(
        f3, "_fetch_vi_active",
        AsyncMock(side_effect=RuntimeError("VI status query failed")))

    await f3.run(force=True)

    send_buy.assert_awaited_once()
    assert state.get().position_status == "HOLDING"
    errors = [kwargs for event, kwargs in events if event == "F3_VI_CHECK_ERROR"]
    assert errors and "VI status query failed" in errors[-1]["error"]
    f3.db.record_skip.assert_not_awaited()


@pytest.mark.asyncio
async def test_all_candidates_vi_active_records_vi_active(monkeypatch):
    """모든 후보가 VI로만 소진되면 최종 사유도 VI_ACTIVE로 기록한다.

    시작 복원(main._restore_market_closed_from_db)은 VI_ACTIVE만 복원하므로
    ENTRY_FAIL로 남기면 재시작 catch-up이 VI 해제가에 추격 진입할 수 있다."""
    events = []
    _reset_state()
    state.get().target_ticker = "BAD001"
    state.get().target_candidates = [
        {"ticker": "BAD001", "name": "Bad", "expected_amount": 2_000_000.0},
        {"ticker": "BAD002", "name": "Bad2", "expected_amount": 1_000_000.0},
    ]
    send_buy = AsyncMock()
    notify = AsyncMock()

    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(
        f3,
        "_fetch_expected_price",
        AsyncMock(side_effect=[
            (10310.0, 10000.0),
            (10400.0, 10000.0),
            (10400.0, 10000.0),
        ]),
    )
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3.notifier, "send", notify)
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())
    monkeypatch.setattr(
        f3, "_fetch_vi_active",
        AsyncMock(side_effect=[dict(_VI_ACTIVE_INFO), dict(_VI_ACTIVE_INFO)]),
    )

    await f3.run(force=True)

    send_buy.assert_not_awaited()
    assert state.get().day_skip is True
    assert state.get().close_reason == "VI_ACTIVE"
    skipped = [kwargs for event, kwargs in events if event == "ENTRY_CANDIDATE_RETRY_SKIPPED"]
    assert skipped and skipped[-1]["reason"] == "NO_REMAINING_CANDIDATE"
    f3.db.record_skip.assert_awaited_once()
    assert f3.db.record_skip.await_args.args[1] == "VI_ACTIVE"
    assert "NO_REMAINING_CANDIDATE" in f3.db.record_skip.await_args.args[2]
    notify.assert_awaited_once()
    assert notify.await_args.args[0] == "ENTRY_FAIL"


@pytest.mark.asyncio
async def test_mixed_rejection_reasons_keep_entry_fail(monkeypatch):
    """소진 사유에 VI가 아닌 거절이 섞이면 최종 사유는 ENTRY_FAIL을 유지한다."""
    events = []
    _reset_state()
    state.get().target_ticker = "BAD001"
    state.get().target_candidates = [
        {"ticker": "BAD001", "name": "Bad", "expected_amount": 2_000_000.0},
        {"ticker": "BAD002", "name": "Bad2", "expected_amount": 1_000_000.0},
    ]
    send_buy = AsyncMock(return_value={
        "rt_cd": "1",
        "msg_cd": "APBK0919",
        "msg1": "주문 거부",
        "output": {},
    })
    notify = AsyncMock()

    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(
        f3,
        "_fetch_expected_price",
        AsyncMock(side_effect=[
            (10310.0, 10000.0),
            (10400.0, 10000.0),
            (10400.0, 10000.0),
        ]),
    )
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3.notifier, "send", notify)
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    async def vi_status(ticker):
        return dict(_VI_ACTIVE_INFO) if ticker == "BAD001" else None

    monkeypatch.setattr(f3, "_fetch_vi_active", AsyncMock(side_effect=vi_status))

    await f3.run(force=True)

    assert state.get().day_skip is True
    assert state.get().close_reason == "ENTRY_FAIL"
    f3.db.record_skip.assert_awaited_once()
    assert f3.db.record_skip.await_args.args[1] == "ENTRY_FAIL"
    assert "NO_REMAINING_CANDIDATE" in f3.db.record_skip.await_args.args[2]


@pytest.mark.asyncio
async def test_entry_retry_rechecks_vi_before_second_order(monkeypatch):
    """1차 미체결 폴링(최대 12초)·취소·대기 중 VI가 발동하면
    2차 시장가 주문을 보내지 않고 당일 VI_ACTIVE로 차단해야 한다."""
    events = []
    _reset_state()
    cancel_order = AsyncMock(return_value={"rt_cd": "0"})
    send_buy = AsyncMock(return_value={
        "rt_cd": "0",
        "msg_cd": "MCA00000",
        "msg1": "OK",
        "output": {"ODNO": "0000000937", "KRX_FWDG_ORD_ORGNO": "001"},
    })
    notify = AsyncMock()

    monkeypatch.setattr(f3, "F3_ENTRY_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(f3, "F3_ENTRY_RETRY_DELAY_SEC", 0)
    monkeypatch.setattr(f3, "F3_ENTRY_CANCEL_RELEASE_WAIT_SEC", 0)
    monkeypatch.setattr(f3, "_before_deadline", lambda deadline: True)
    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10310.0, 10000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3, "_poll_fill", AsyncMock(return_value=None))
    monkeypatch.setattr(f3, "_cancel_order", cancel_order)
    monkeypatch.setattr(f3.notifier, "send", notify)
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    async def vi_after_cancel(ticker):
        # 1차 주문 취소 이후(= 재시도 직전)에만 VI 발동 상태를 돌려준다.
        return dict(_VI_ACTIVE_INFO) if cancel_order.await_count else None

    monkeypatch.setattr(f3, "_fetch_vi_active", AsyncMock(side_effect=vi_after_cancel))

    await f3.run()

    send_buy.assert_awaited_once()
    assert state.get().position_status == "IDLE"
    assert state.get().day_skip is True
    assert state.get().close_reason == "VI_ACTIVE"
    f3.db.record_skip.assert_awaited_once()
    assert f3.db.record_skip.await_args.args[1] == "VI_ACTIVE"
    blocked = [kwargs for event, kwargs in events if event == "F3_ENTRY_BLOCKED"]
    assert blocked and blocked[-1]["reason"] == "VI_ACTIVE"


@pytest.mark.asyncio
async def test_entry_retry_vi_recheck_moves_to_next_candidate(monkeypatch):
    """다후보 경로: 1순위 재시도 직전 VI가 감지되면 취소 상태를 정리하고
    다음 후보로 넘어가 정상 진입해야 한다."""
    events = []
    _reset_state()
    state.get().target_ticker = "BAD001"
    state.get().target_candidates = [
        {"ticker": "BAD001", "name": "Bad", "expected_amount": 2_000_000.0},
        {"ticker": "GOOD02", "name": "Good", "expected_amount": 1_000_000.0},
    ]
    cancel_order = AsyncMock(return_value={"rt_cd": "0"})
    send_buy = AsyncMock(return_value={
        "rt_cd": "0",
        "msg_cd": "MCA00000",
        "msg1": "OK",
        "output": {"ODNO": "0000000938", "KRX_FWDG_ORD_ORGNO": "001"},
    })

    monkeypatch.setattr(f3, "F3_ENTRY_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(f3, "F3_ENTRY_RETRY_DELAY_SEC", 0)
    monkeypatch.setattr(f3, "F3_ENTRY_CANCEL_RELEASE_WAIT_SEC", 0)
    monkeypatch.setattr(f3, "_before_deadline", lambda deadline: True)
    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(
        f3,
        "_fetch_expected_price",
        AsyncMock(side_effect=[
            (10310.0, 10000.0),
            (10400.0, 10000.0),
            (10400.0, 10000.0),
        ]),
    )
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(
        f3,
        "_poll_fill",
        AsyncMock(side_effect=[None, {"fill_price": 10400, "fill_qty": 91}]),
    )
    monkeypatch.setattr(f3, "_cancel_order", cancel_order)
    monkeypatch.setattr(f3, "_fetch_current_price", AsyncMock(return_value=10400))
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())

    async def vi_status(ticker):
        # BAD001은 1차 취소 이후에만 VI 발동, GOOD02는 항상 정상.
        if ticker == "BAD001" and cancel_order.await_count:
            return dict(_VI_ACTIVE_INFO)
        return None

    monkeypatch.setattr(f3, "_fetch_vi_active", AsyncMock(side_effect=vi_status))

    await f3.run()

    assert send_buy.await_count == 2
    assert send_buy.await_args_list[0].args[0] == "BAD001"
    assert send_buy.await_args_list[-1].args == ("GOOD02", 91, "PAPER")
    assert state.get().target_ticker == "GOOD02"
    assert state.get().position_status == "HOLDING"
    retry_events = [kwargs for event, kwargs in events if event == "ENTRY_CANDIDATE_RETRY"]
    assert retry_events and retry_events[-1]["ticker"] == "BAD001"
    assert retry_events[-1]["reason"] == "VI_ACTIVE"
    f3.db.record_skip.assert_not_awaited()


@pytest.mark.asyncio
async def test_entry_retry_vi_check_runs_right_before_send(monkeypatch):
    """재시도 VI 재확인은 quiet wait·매수가능수량 조회까지 끝난 뒤,
    _send_buy 직전에 수행해야 한다 — 그 사이 발동한 VI도 잡아야 한다."""
    events = []
    _reset_state()
    fetch_buyable = AsyncMock(return_value=_buyable())
    send_buy = AsyncMock(return_value={
        "rt_cd": "0",
        "msg_cd": "MCA00000",
        "msg1": "OK",
        "output": {"ODNO": "0000000937", "KRX_FWDG_ORD_ORGNO": "001"},
    })

    monkeypatch.setattr(f3, "F3_ENTRY_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(f3, "F3_ENTRY_RETRY_DELAY_SEC", 0)
    monkeypatch.setattr(f3, "F3_ENTRY_CANCEL_RELEASE_WAIT_SEC", 0)
    monkeypatch.setattr(f3, "_before_deadline", lambda deadline: True)
    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10310.0, 10000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_fetch_buyable_qty", fetch_buyable)
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3, "_poll_fill", AsyncMock(return_value=None))
    monkeypatch.setattr(f3, "_cancel_order", AsyncMock(return_value={"rt_cd": "0"}))
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    async def vi_status(ticker):
        # 2차 시도의 매수가능수량 조회가 끝난 시점부터 VI 발동 — 조회~주문
        # 사이 창에서 발동한 VI를 모델링한다.
        return dict(_VI_ACTIVE_INFO) if fetch_buyable.await_count >= 2 else None

    monkeypatch.setattr(f3, "_fetch_vi_active", AsyncMock(side_effect=vi_status))

    await f3.run()

    send_buy.assert_awaited_once()
    assert state.get().position_status == "IDLE"
    assert state.get().day_skip is True
    assert state.get().close_reason == "VI_ACTIVE"
    f3.db.record_skip.assert_awaited_once()
    assert f3.db.record_skip.await_args.args[1] == "VI_ACTIVE"


@pytest.mark.asyncio
async def test_cancel_rejected_because_filled_recovers_fill(monkeypatch):
    """취소 거부의 흔한 원인은 기체결 — 취소 실패 시 체결을 재확인해
    재주문 없이 HOLDING으로 전환해야 한다(중복 주문 방지)."""
    _reset_state()
    send_buy = AsyncMock(return_value={
        "rt_cd": "0",
        "msg_cd": "MCA00000",
        "msg1": "OK",
        "output": {"ODNO": "0000000937", "KRX_FWDG_ORD_ORGNO": "001"},
    })

    monkeypatch.setattr(f3, "F3_ENTRY_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(f3, "F3_ENTRY_RETRY_DELAY_SEC", 0)
    monkeypatch.setattr(f3, "F3_ENTRY_CANCEL_RELEASE_WAIT_SEC", 0)
    monkeypatch.setattr(f3, "_before_deadline", lambda deadline: True)
    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(f3, "_fetch_expected_price", AsyncMock(return_value=(10310.0, 10000.0)))
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(
        f3,
        "_poll_fill",
        AsyncMock(side_effect=[None, {"fill_price": 10310, "fill_qty": 92}]),
    )
    monkeypatch.setattr(
        f3, "_cancel_order",
        AsyncMock(return_value={"rt_cd": "1", "msg_cd": "APBK0919", "msg1": "취소 불가"}),
    )
    monkeypatch.setattr(f3.notifier, "send", AsyncMock())
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())
    monkeypatch.setattr(f3.db, "open_trade", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "record_order", AsyncMock(return_value=1))
    monkeypatch.setattr(f3.db, "update_order_fill", AsyncMock())
    monkeypatch.setattr(f3.state, "persist", AsyncMock())

    await f3.run()

    send_buy.assert_awaited_once()
    assert state.get().position_status == "HOLDING"
    assert state.get().entry_price == 10310
    assert state.get().entry_qty == 92
    f3.db.record_skip.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_unconfirmed_blocks_candidate_switch_and_idle(monkeypatch):
    """취소 성공을 확인하지 못하면 주문이 살아 있을 수 있다 —
    재주문·후보 전환·IDLE 전환 없이 중단하고 운영자에게 알린다."""
    events = []
    _reset_state()
    state.get().target_ticker = "BAD001"
    state.get().target_candidates = [
        {"ticker": "BAD001", "name": "Bad", "expected_amount": 2_000_000.0},
        {"ticker": "GOOD02", "name": "Good", "expected_amount": 1_000_000.0},
    ]
    send_buy = AsyncMock(return_value={
        "rt_cd": "0",
        "msg_cd": "MCA00000",
        "msg1": "OK",
        "output": {"ODNO": "0000000937", "KRX_FWDG_ORD_ORGNO": "001"},
    })
    notify = AsyncMock()

    monkeypatch.setattr(f3, "F3_ENTRY_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(f3, "F3_ENTRY_RETRY_DELAY_SEC", 0)
    monkeypatch.setattr(f3, "F3_ENTRY_CANCEL_RELEASE_WAIT_SEC", 0)
    monkeypatch.setattr(f3, "_before_deadline", lambda deadline: True)
    monkeypatch.setattr(f3, "_sleep_until", AsyncMock())
    monkeypatch.setattr(f3, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(
        f3,
        "_fetch_expected_price",
        AsyncMock(side_effect=[
            (10310.0, 10000.0),
            (10400.0, 10000.0),
            (10400.0, 10000.0),
        ]),
    )
    monkeypatch.setattr(f3, "_fetch_available_cash", AsyncMock(return_value=1_000_000.0))
    monkeypatch.setattr(f3, "_send_buy", send_buy)
    monkeypatch.setattr(f3, "_poll_fill", AsyncMock(return_value=None))
    monkeypatch.setattr(
        f3, "_cancel_order",
        AsyncMock(return_value={"rt_cd": "1", "msg_cd": "APBK1234", "msg1": "시스템 오류"}),
    )
    monkeypatch.setattr(f3.notifier, "send", notify)
    monkeypatch.setattr(f3.db, "record_skip", AsyncMock())

    await f3.run()

    # 살아 있을 수 있는 주문 옆에서 다른 후보로 갈아타면 안 된다.
    send_buy.assert_awaited_once()
    assert send_buy.await_args.args[0] == "BAD001"
    # IDLE로 전환하지 않는다 — 미체결 주문 생존 가능성이 남아 있다.
    assert state.get().position_status == "ENTERING"
    assert state.get().day_skip is True
    f3.db.record_skip.assert_awaited_once()
    assert f3.db.record_skip.await_args.args[1] == "ENTRY_FAIL"
    assert "CANCEL_UNCONFIRMED" in f3.db.record_skip.await_args.args[2]
    unconfirmed_alerts = [
        call for call in notify.await_args_list
        if call.args[0] == "ENTRY_CANCEL_UNCONFIRMED"
    ]
    assert unconfirmed_alerts and unconfirmed_alerts[-1].kwargs["level"] == "ERROR"
