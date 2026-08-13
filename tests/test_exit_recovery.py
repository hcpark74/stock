"""R3 매도 주문 응답 유실 복구 테스트."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from src import db, state
from src.modules import exit_recovery, f4_tracking


@pytest.fixture
async def holding(monkeypatch):
    await db.init(":memory:")
    trade_id = await db.open_trade("20260806", "005930", 10_000.0, 10, "삼성전자")
    s = state.get()
    s.trading_date = "20260806"
    s.position_status = "HOLDING"
    s.target_ticker = "005930"
    s.target_name = "삼성전자"
    s.entry_price = 10_000.0
    s.entry_qty = 10
    s.remaining_qty = 10
    s.high_price = 10_200.0
    s.trade_id = trade_id
    s.pending_exit = None
    s.close_reason = None
    monkeypatch.setattr(state, "persist", AsyncMock())
    yield s
    s.position_status = "IDLE"
    s.pending_exit = None
    s.trade_id = 0
    await db.close()


async def test_sell_intent_is_durable_before_network_send(holding):
    async def send():
        row = await db.get_unresolved_exit_intent("20260806")
        assert row["submission_state"] == "SUBMITTING"
        assert state.get().position_status == "EXITING"
        assert state.get().pending_exit["submission_state"] == "SUBMITTING"
        return {"rt_cd": "0", "output": {"ODNO": "S100", "KRX_FWDG_ORD_ORGNO": "01"}}

    result = await exit_recovery.submit_sell(
        qty=10,
        reason="HARD_STOP",
        phase="CLOSE_SELL",
        trigger_price=9_800.0,
        mode="PAPER",
        send=send,
    )

    assert result.acknowledged is True
    row = await db.get_order_by_client_id(state.get().pending_exit["client_order_id"])
    assert row["kis_order_id"] == "S100"
    assert row["submission_state"] == "ACKNOWLEDGED"


async def test_response_loss_recovers_unique_matching_order(holding, monkeypatch):
    matched = {
        "order_id": "S200",
        "org_no": "01",
        "order_qty": 10,
        "fill_qty": 10,
        "remaining_qty": 0,
        "fill_amt": 98_000.0,
        "fill_price": 9_800.0,
    }
    find = AsyncMock(return_value=("MATCHED", matched))
    monkeypatch.setattr(exit_recovery, "find_matching_order", find)
    send = AsyncMock(return_value={
        "rt_cd": "1",
        "msg_cd": "ReadTimeout",
        "msg1": "response lost",
    })

    result = await exit_recovery.submit_sell(
        qty=10,
        reason="HARD_STOP",
        phase="CLOSE_SELL",
        trigger_price=9_800.0,
        mode="PAPER",
        send=send,
    )

    assert result.acknowledged is True
    assert result.uncertain is True
    assert result.order_id == "S200"
    assert send.await_count == 1
    row = await db.get_order_by_client_id(state.get().pending_exit["client_order_id"])
    assert row["kis_order_id"] == "S200"
    assert row["submission_state"] == "ACKNOWLEDGED"


async def test_unmatched_response_loss_stays_exiting_and_never_resends(holding, monkeypatch):
    monkeypatch.setattr(
        exit_recovery,
        "find_matching_order",
        AsyncMock(return_value=("NOT_FOUND", None)),
    )
    send = AsyncMock(return_value={
        "rt_cd": "1",
        "msg_cd": "ReadTimeout",
        "msg1": "response lost",
    })

    result = await exit_recovery.submit_sell(
        qty=10,
        reason="HARD_STOP",
        phase="CLOSE_SELL",
        trigger_price=9_800.0,
        mode="PAPER",
        send=send,
    )

    assert result.acknowledged is False
    assert result.uncertain is True
    assert send.await_count == 1
    assert state.get().position_status == "EXITING"
    assert state.get().pending_exit["submission_state"] == "UNKNOWN"


async def test_confirmed_broker_rejection_returns_to_holding(holding):
    result = await exit_recovery.submit_sell(
        qty=10,
        reason="HARD_STOP",
        phase="CLOSE_SELL",
        trigger_price=9_800.0,
        mode="PAPER",
        send=AsyncMock(return_value={
            "rt_cd": "1",
            "msg_cd": "40330000",
            "msg1": "rejected",
            "_response_meta": {
                "http_status": 200,
                "request_sent": True,
            },
        }),
    )

    assert result.acknowledged is False
    assert result.uncertain is False
    assert state.get().position_status == "HOLDING"
    assert state.get().pending_exit is None


async def test_restart_reconciliation_closes_confirmed_full_exit(holding, monkeypatch):
    finalize = AsyncMock()
    monkeypatch.setattr(f4_tracking, "finalize_trailing_shadow", finalize)
    submission = await exit_recovery.submit_sell(
        qty=10,
        reason="HARD_STOP",
        phase="CLOSE_SELL",
        trigger_price=9_800.0,
        mode="PAPER",
        send=AsyncMock(return_value={"rt_cd": "0", "output": {"ODNO": "S300"}}),
    )
    monkeypatch.setattr(
        exit_recovery,
        "fetch_order_status",
        AsyncMock(return_value={
            "order_id": "S300",
            "order_qty": 10,
            "fill_qty": 10,
            "remaining_qty": 0,
            "fill_amt": 98_000.0,
            "fill_price": 9_800.0,
        }),
    )
    monkeypatch.setattr(f4_tracking.notifier, "send", AsyncMock())

    recovered = await f4_tracking.recover_pending_exit()

    assert submission.acknowledged is True
    assert recovered is True
    assert state.get().position_status == "CLOSED"
    trade = await db.get_trade_by_date("20260806")
    assert trade["status"] == "CLOSED"
    assert trade["exit_qty"] == 10
    finalize.assert_awaited_once_with(
        trigger_price=9_800.0,
        actual_exit_price=9_800,
        exit_qty=10,
        actual_pnl_pct=-2.0,
        close_reason="HARD_STOP",
    )


async def test_restart_reconciliation_keeps_partial_exit_remainder_tracked(
    holding,
    monkeypatch,
):
    submission = await exit_recovery.submit_sell(
        qty=10,
        reason="HARD_STOP",
        phase="CLOSE_SELL",
        trigger_price=9_800.0,
        mode="PAPER",
        send=AsyncMock(return_value={"rt_cd": "0", "output": {"ODNO": "S301"}}),
    )
    monkeypatch.setattr(
        exit_recovery,
        "fetch_order_status",
        AsyncMock(return_value={
            "order_id": "S301",
            "order_qty": 10,
            "fill_qty": 6,
            "remaining_qty": 4,
            "fill_amt": 58_800.0,
            "fill_price": 9_800.0,
        }),
    )
    monkeypatch.setattr(f4_tracking.notifier, "send", AsyncMock())

    recovered = await f4_tracking.recover_pending_exit()

    assert submission.acknowledged is True
    assert recovered is False
    assert state.get().position_status == "EXITING"
    assert state.get().entry_qty == 10
    assert state.get().remaining_qty == 4
    trade = await db.get_trade_by_date("20260806")
    assert trade["status"] == "OPEN"


async def test_gateway_504_json_is_uncertain_and_never_rejected(holding, monkeypatch):
    monkeypatch.setattr(
        exit_recovery,
        "find_matching_order",
        AsyncMock(return_value=("NOT_FOUND", None)),
    )
    result = await exit_recovery.submit_sell(
        qty=10,
        reason="HARD_STOP",
        phase="CLOSE_SELL",
        trigger_price=9_800.0,
        mode="PAPER",
        send=AsyncMock(return_value={
            "rt_cd": "1",
            "msg_cd": "GW_TIMEOUT",
            "msg1": "gateway timeout",
            "_response_meta": {
                "http_status": 504,
                "request_sent": True,
            },
        }),
    )

    assert result.acknowledged is False
    assert result.uncertain is True
    assert state.get().position_status == "EXITING"
    assert state.get().pending_exit["submission_state"] == "UNKNOWN"


async def test_http_429_sell_is_unknown_reconciled_and_never_auto_resent(
    holding,
    monkeypatch,
):
    find = AsyncMock(return_value=("NOT_FOUND", None))
    monkeypatch.setattr(exit_recovery, "find_matching_order", find)
    send = AsyncMock(return_value={
        "rt_cd": "1",
        "msg_cd": "HTTP_429",
        "msg1": "HTTP 429 rate limit",
        "_response_meta": {
            "http_status": 429,
            "request_sent": True,
        },
    })

    result = await exit_recovery.submit_sell(
        qty=10,
        reason="HARD_STOP",
        phase="CLOSE_SELL",
        trigger_price=9_800.0,
        mode="PAPER",
        send=send,
    )

    assert result.acknowledged is False
    assert result.uncertain is True
    assert state.get().position_status == "EXITING"
    assert state.get().pending_exit["submission_state"] == "UNKNOWN"
    send.assert_awaited_once_with()
    find.assert_awaited_once()


async def test_reconciliation_excludes_known_prior_retry_order(holding, monkeypatch):
    now = datetime.now(exit_recovery.KST)
    prior_id = "PRIOR-CANCELLED"
    await db.record_order(
        holding.trade_id,
        prior_id,
        "SELL",
        10,
        0.0,
        "TIMEOUT_SELL",
        "005930",
    )
    monkeypatch.setattr(exit_recovery.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(exit_recovery.kis_rest, "get", AsyncMock(return_value={
        "rt_cd": "0",
        "output1": [{
            "odno": prior_id,
            "pdno": "005930",
            "ord_qty": "10",
            "ord_tmd": now.strftime("%H%M%S"),
            "tot_ccld_qty": "0",
            "rmn_qty": "0",
        }],
    }))
    intent = {
        "ticker": "005930",
        "requested_qty": 10,
        "submitted_at": now.isoformat(),
    }

    outcome, matched = await exit_recovery.find_matching_order(
        intent,
        "PAPER",
        excluded_order_ids={prior_id},
    )

    assert outcome == "NOT_FOUND"
    assert matched is None


async def test_reconciliation_reports_ambiguous_matches(holding, monkeypatch):
    now = datetime.now(exit_recovery.KST)
    rows = [
        {
            "odno": order_id,
            "pdno": "005930",
            "ord_qty": "10",
            "ord_tmd": now.strftime("%H%M%S"),
            "sll_buy_dvsn_cd": "01",
            "tot_ccld_qty": "0",
            "rmn_qty": "10",
        }
        for order_id in ("NEW-1", "NEW-2")
    ]
    monkeypatch.setattr(exit_recovery.kis_rest, "get", AsyncMock(return_value={
        "rt_cd": "0", "output1": rows,
    }))

    outcome, matched = await exit_recovery.find_matching_order(
        {
            "ticker": "005930",
            "requested_qty": 10,
            "submitted_at": now.isoformat(),
        },
        "PAPER",
    )

    assert outcome == "AMBIGUOUS"
    assert matched is None


async def test_reconciliation_query_failure_is_fail_closed(holding, monkeypatch):
    monkeypatch.setenv("EXIT_RECONCILE_ATTEMPTS", "1")
    monkeypatch.setattr(
        exit_recovery.kis_rest,
        "get",
        AsyncMock(side_effect=RuntimeError("query down")),
    )

    outcome, matched = await exit_recovery.find_matching_order(
        {
            "ticker": "005930",
            "requested_qty": 10,
            "submitted_at": datetime.now(exit_recovery.KST).isoformat(),
        },
        "PAPER",
    )

    assert outcome == "QUERY_FAILED"
    assert matched is None


def test_candidate_rejects_order_more_than_two_seconds_before_submit():
    submitted = datetime(2026, 8, 6, 15, 20, 10, tzinfo=exit_recovery.KST)
    row = {
        "odno": "OLD",
        "pdno": "005930",
        "ord_qty": "10",
        "ord_tmd": (submitted - timedelta(seconds=3)).strftime("%H%M%S"),
    }

    assert exit_recovery._candidate_from_row(row, {
        "ticker": "005930",
        "requested_qty": 10,
        "submitted_at": submitted.isoformat(),
    }) is None


def test_candidate_rejects_buy_even_when_ticker_qty_and_time_match():
    submitted = datetime(2026, 8, 6, 15, 20, 10, tzinfo=exit_recovery.KST)
    row = {
        "odno": "BUY-ORDER",
        "pdno": "005930",
        "ord_qty": "10",
        "ord_tmd": submitted.strftime("%H%M%S"),
        "sll_buy_dvsn_cd": "02",
    }

    assert exit_recovery._candidate_from_row(row, {
        "ticker": "005930",
        "requested_qty": 10,
        "submitted_at": submitted.isoformat(),
    }) is None


async def test_merge_db_intent_preserves_newer_state_values(holding, monkeypatch):
    pending = {
        "client_order_id": "same-client",
        "order_db_id": 0,
        "ticker": "005930",
        "requested_qty": 10,
        "phase": "CLOSE_SELL",
        "reason": "TRAILING",
        "submission_state": "UNKNOWN",
        "order_id": "STATE-ORDER",
    }
    monkeypatch.setattr(
        exit_recovery.db,
        "get_unresolved_exit_intent",
        AsyncMock(return_value={
            "id": 91,
            "client_order_id": "same-client",
            "ticker": "005930",
            "order_qty": 10,
            "order_phase": "CLOSE_SELL",
            "submission_state": "PREPARED",
            "kis_order_id": "",
        }),
    )

    merged = await exit_recovery.merge_db_intent({
        "position_status": "EXITING",
        "close_reason": "TRAILING",
        "pending_exit": pending,
    }, "20260806")

    assert merged["pending_exit"]["reason"] == "TRAILING"
    assert merged["pending_exit"]["submission_state"] == "UNKNOWN"
    assert merged["pending_exit"]["order_id"] == "STATE-ORDER"
    assert merged["pending_exit"]["order_db_id"] == 91


async def test_trade_id_zero_does_not_block_real_position_sell(holding):
    holding.trade_id = 0
    send = AsyncMock(return_value={
        "rt_cd": "0",
        "output": {"ODNO": "NO-DB-SELL"},
    })

    result = await exit_recovery.submit_sell(
        qty=10,
        reason="HARD_STOP",
        phase="CLOSE_SELL",
        trigger_price=9_800.0,
        mode="PAPER",
        send=send,
    )

    assert result.acknowledged is True
    assert result.order_db_id == 0
    send.assert_awaited_once_with()
    assert state.get().position_status == "EXITING"


async def test_trade_id_zero_response_loss_still_reconciles_unique_sell(
    holding,
    monkeypatch,
):
    holding.trade_id = 0
    matched = {
        "order_id": "NO-DB-MATCH",
        "org_no": "01",
        "order_qty": 10,
        "fill_qty": 0,
        "remaining_qty": 10,
        "fill_amt": 0.0,
        "fill_price": 0.0,
    }
    find = AsyncMock(return_value=("MATCHED", matched))
    monkeypatch.setattr(exit_recovery, "find_matching_order", find)

    result = await exit_recovery.submit_sell(
        qty=10,
        reason="HARD_STOP",
        phase="CLOSE_SELL",
        trigger_price=9_800.0,
        mode="PAPER",
        send=AsyncMock(return_value={
            "rt_cd": "1",
            "msg_cd": "ReadTimeout",
            "msg1": "response lost",
        }),
    )

    assert result.acknowledged is True
    assert result.uncertain is True
    assert result.order_db_id == 0
    assert result.order_id == "NO-DB-MATCH"
    find.assert_awaited_once()


async def test_trade_id_zero_restart_full_fill_closes_state(holding, monkeypatch):
    holding.trade_id = 0
    holding.position_status = "EXITING"
    holding.close_reason = "HARD_STOP"
    holding.pending_exit = {
        "client_order_id": "no-db-restart",
        "requested_qty": 10,
        "reason": "HARD_STOP",
        "submission_state": "UNKNOWN",
    }
    monkeypatch.setattr(
        exit_recovery,
        "reconcile_pending_intent",
        AsyncMock(return_value=("RECONCILED", {
            "order_id": "NO-DB-FILLED",
            "order_qty": 10,
            "fill_qty": 10,
            "remaining_qty": 0,
            "fill_amt": 98_000.0,
            "fill_price": 9_800.0,
        })),
    )
    monkeypatch.setattr(f4_tracking.notifier, "send", AsyncMock())

    recovered = await f4_tracking.recover_pending_exit()

    assert recovered is True
    assert state.get().position_status == "CLOSED"
    assert state.get().remaining_qty == 0


async def test_state_file_failure_still_blocks_network_send(holding, monkeypatch):
    send = AsyncMock()
    monkeypatch.setattr(
        state,
        "persist",
        AsyncMock(side_effect=OSError("state disk unavailable")),
    )

    result = await exit_recovery.submit_sell(
        qty=10,
        reason="HARD_STOP",
        phase="CLOSE_SELL",
        trigger_price=9_800.0,
        mode="PAPER",
        send=send,
    )

    assert result.acknowledged is False
    assert result.uncertain is False
    send.assert_not_awaited()
    assert state.get().position_status == "HOLDING"
