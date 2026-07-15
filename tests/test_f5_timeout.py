"""F5 타임아웃 청산 — 중복 매도 방지, 부분체결 평균가, 미체결 취소 확정 검증."""

from unittest.mock import AsyncMock, call

import pytest

from src import db, state
from src.modules import f5_timeout

_CLEAN_ORDER = {"ccld_qty": 0, "ccld_amt": 0.0, "rmn_qty": 0}


@pytest.fixture(autouse=True)
def holding_state(monkeypatch):
    s = state.get()
    s.position_status = "HOLDING"
    s.target_ticker = "005930"
    s.target_name = "삼성전자"
    s.entry_price = 10_000.0
    s.entry_qty = 10
    s.remaining_qty = 10
    s.trade_id = 7
    s.highest_step = 0.0
    monkeypatch.setattr(f5_timeout, "_RETRY_INTERVAL", 0)
    monkeypatch.setattr(f5_timeout, "_prefetch_qty", 10)
    monkeypatch.setattr(
        f5_timeout, "_prefetch_date",
        f5_timeout.datetime.now(f5_timeout.KST).strftime("%Y%m%d"))
    monkeypatch.setattr(f5_timeout.state, "persist", AsyncMock())
    # 주문 시점 기준가 조회 — 테스트에서 실제 KIS 호출 금지
    monkeypatch.setattr(
        f5_timeout, "_fetch_current_price",
        AsyncMock(return_value=10_000.0), raising=False)
    yield
    s.position_status = "IDLE"
    s.close_reason = None
    s.trade_id = 0


def _wire_db(monkeypatch):
    record_order = AsyncMock(return_value=42)
    update_fill = AsyncMock()
    close_trade = AsyncMock()
    monkeypatch.setattr(f5_timeout.db, "record_order", record_order)
    monkeypatch.setattr(f5_timeout.db, "update_order_fill", update_fill)
    monkeypatch.setattr(f5_timeout.db, "close_trade", close_trade)
    monkeypatch.setattr(f5_timeout.db, "update_order_status", AsyncMock())
    return record_order, update_fill, close_trade


async def test_unconfirmed_fill_retries_only_with_verified_state(monkeypatch):
    """직전 주문이 소멸(미체결 잔량 0)이고 잔고가 남아 있을 때만 재주문한다."""
    send_sell = AsyncMock(return_value={"output": {"ODNO": "S1"}})
    monkeypatch.setattr(f5_timeout, "_send_sell", send_sell)
    monkeypatch.setattr(f5_timeout, "_poll_fill", AsyncMock(return_value=None))
    monkeypatch.setattr(f5_timeout, "_fetch_order_status", AsyncMock(return_value=_CLEAN_ORDER))
    monkeypatch.setattr(f5_timeout, "_fetch_holding_qty", AsyncMock(return_value=10))
    record_order, update_fill, close_trade = _wire_db(monkeypatch)
    send = AsyncMock()
    monkeypatch.setattr(f5_timeout.notifier, "send", send)

    await f5_timeout.execute()

    assert send_sell.await_count == 3
    assert record_order.await_count == 3
    update_fill.assert_not_awaited()             # 0원 체결 기록 금지
    close_trade.assert_not_awaited()
    codes = [c.args[0] for c in send.await_args_list]
    assert "TIMEOUT_ORDER_FAILED" in codes


async def test_recovers_actual_fill_from_order_status(monkeypatch):
    """체결조회만 실패하고 실제로 팔린 경우 — 주문 상태 조회로 체결가를 복구하고
    재주문 없이 정상 종료한다."""
    send_sell = AsyncMock(return_value={"output": {"ODNO": "S1"}})
    monkeypatch.setattr(f5_timeout, "_send_sell", send_sell)
    monkeypatch.setattr(f5_timeout, "_poll_fill", AsyncMock(return_value=None))
    monkeypatch.setattr(f5_timeout, "_fetch_order_status", AsyncMock(
        return_value={"ccld_qty": 10, "ccld_amt": 101_000.0, "rmn_qty": 0}))
    monkeypatch.setattr(f5_timeout, "_fetch_holding_qty", AsyncMock(return_value=0))
    record_order, update_fill, close_trade = _wire_db(monkeypatch)
    send = AsyncMock()
    monkeypatch.setattr(f5_timeout.notifier, "send", send)

    await f5_timeout.execute()

    assert send_sell.await_count == 1            # 중복 매도 주문 없음
    close_trade.assert_awaited_once_with(7, 10_100, "TIMEOUT", 1.0, 0.0)
    codes = [c.args[0] for c in send.await_args_list]
    assert "TIMEOUT_ORDER_FAILED" not in codes


async def test_unverified_close_when_balance_zero_but_no_fill_data(monkeypatch):
    """잔고 0인데 체결가를 끝내 확인 못 하면 임의 가격으로 닫지 않고 CRIT."""
    send_sell = AsyncMock(return_value={"output": {"ODNO": "S1"}})
    monkeypatch.setattr(f5_timeout, "_send_sell", send_sell)
    monkeypatch.setattr(f5_timeout, "_poll_fill", AsyncMock(return_value=None))
    monkeypatch.setattr(f5_timeout, "_fetch_order_status", AsyncMock(return_value=_CLEAN_ORDER))
    monkeypatch.setattr(f5_timeout, "_fetch_holding_qty", AsyncMock(return_value=0))
    record_order, update_fill, close_trade = _wire_db(monkeypatch)
    send = AsyncMock()
    monkeypatch.setattr(f5_timeout.notifier, "send", send)

    await f5_timeout.execute()

    assert send_sell.await_count == 1
    close_trade.assert_not_awaited()
    codes = [c.args[0] for c in send.await_args_list]
    assert "TIMEOUT_CLOSE_UNVERIFIED" in codes


async def test_pending_order_is_cancelled_before_reorder(monkeypatch):
    """직전 주문이 미체결로 살아 있으면 취소 확정 후에만 재주문하고,
    취소된 주문은 DB에 CANCELLED로 남긴다."""
    send_sell = AsyncMock(return_value={"output": {"ODNO": "S1", "KRX_FWDG_ORD_ORGNO": "91252"}})
    monkeypatch.setattr(f5_timeout, "_send_sell", send_sell)
    monkeypatch.setattr(f5_timeout, "_poll_fill", AsyncMock(side_effect=[
        None,                                                  # 1차 주문 체결 미확인
        {"fill_price": 10_100, "fill_qty": 10, "fill_amt": 101_000.0},  # 2차 전량 체결
    ]))
    monkeypatch.setattr(f5_timeout, "_fetch_order_status", AsyncMock(side_effect=[
        {"ccld_qty": 0, "ccld_amt": 0.0, "rmn_qty": 10},       # 미체결 잔량 존재
        {"ccld_qty": 0, "ccld_amt": 0.0, "rmn_qty": 0},        # 취소 확인
    ]))
    monkeypatch.setattr(f5_timeout, "_fetch_cancelable_qty", AsyncMock(return_value=None))
    cancel = AsyncMock(return_value=True)
    monkeypatch.setattr(f5_timeout, "_cancel_order", cancel)
    monkeypatch.setattr(f5_timeout, "_fetch_holding_qty", AsyncMock(return_value=10))
    record_order, update_fill, close_trade = _wire_db(monkeypatch)
    update_status = AsyncMock()
    monkeypatch.setattr(f5_timeout.db, "update_order_status", update_status)
    send = AsyncMock()
    monkeypatch.setattr(f5_timeout.notifier, "send", send)

    await f5_timeout.execute()

    cancel.assert_awaited_once_with("S1", "91252", "PAPER")
    assert send_sell.await_count == 2            # 취소 확정 후에만 재주문
    update_status.assert_awaited_once_with(42, "CANCELLED")   # 감사 이력 일치
    close_trade.assert_awaited_once_with(7, 10_100, "TIMEOUT", 1.0, 0.0)


async def test_cancel_skipped_when_no_cancelable_qty(monkeypatch):
    """취소가능수량이 0이면(그 사이 체결) 취소 요청 없이 상태 재조회로 체결을 복구한다."""
    send_sell = AsyncMock(return_value={"output": {"ODNO": "S1", "KRX_FWDG_ORD_ORGNO": "91252"}})
    monkeypatch.setattr(f5_timeout, "_send_sell", send_sell)
    monkeypatch.setattr(f5_timeout, "_poll_fill", AsyncMock(return_value=None))
    monkeypatch.setattr(f5_timeout, "_fetch_order_status", AsyncMock(side_effect=[
        {"ccld_qty": 0, "ccld_amt": 0.0, "rmn_qty": 10},       # 첫 확인: 미체결로 보임
        {"ccld_qty": 10, "ccld_amt": 101_000.0, "rmn_qty": 0},  # 재확인: 전량 체결됨
    ]))
    monkeypatch.setattr(f5_timeout, "_fetch_cancelable_qty", AsyncMock(return_value=0))
    cancel = AsyncMock(return_value=True)
    monkeypatch.setattr(f5_timeout, "_cancel_order", cancel)
    monkeypatch.setattr(f5_timeout, "_fetch_holding_qty", AsyncMock(return_value=0))
    record_order, update_fill, close_trade = _wire_db(monkeypatch)
    send = AsyncMock()
    monkeypatch.setattr(f5_timeout.notifier, "send", send)

    await f5_timeout.execute()

    cancel.assert_not_awaited()                  # 취소할 것이 없으면 요청도 없음
    assert send_sell.await_count == 1
    close_trade.assert_awaited_once_with(7, 10_100, "TIMEOUT", 1.0, 0.0)


async def test_no_reorder_when_cancel_unconfirmed(monkeypatch):
    """취소가 확인되지 않으면 재주문하지 않는다 — 이중 매도 금지."""
    send_sell = AsyncMock(return_value={"output": {"ODNO": "S1", "KRX_FWDG_ORD_ORGNO": "91252"}})
    monkeypatch.setattr(f5_timeout, "_send_sell", send_sell)
    monkeypatch.setattr(f5_timeout, "_poll_fill", AsyncMock(return_value=None))
    monkeypatch.setattr(f5_timeout, "_fetch_order_status", AsyncMock(
        return_value={"ccld_qty": 0, "ccld_amt": 0.0, "rmn_qty": 10}))
    monkeypatch.setattr(f5_timeout, "_fetch_cancelable_qty", AsyncMock(return_value=None))
    monkeypatch.setattr(f5_timeout, "_cancel_order", AsyncMock(return_value=True))
    monkeypatch.setattr(f5_timeout, "_fetch_holding_qty", AsyncMock(return_value=10))
    _wire_db(monkeypatch)
    send = AsyncMock()
    monkeypatch.setattr(f5_timeout.notifier, "send", send)

    await f5_timeout.execute()

    assert send_sell.await_count == 1
    codes = [c.args[0] for c in send.await_args_list]
    assert "TIMEOUT_ORDER_FAILED" in codes


async def test_cancel_order_sends_required_contract_fields(monkeypatch):
    """취소 요청 body가 KIS 공식 샘플 계약(EXCG_ID_DVSN_CD 포함)과 일치해야 한다."""
    post = AsyncMock(return_value={})
    monkeypatch.setattr(f5_timeout.kis_rest, "post", post)
    monkeypatch.setattr(f5_timeout.kis_rest, "account_no", lambda: "12345678")
    monkeypatch.setattr(f5_timeout.kis_rest, "account_cd", lambda: "01")

    ok = await f5_timeout._cancel_order("S1", "91252", "PAPER")

    assert ok is True
    kwargs = post.await_args.kwargs
    assert kwargs["tr_id"] == "VTTC0013U"
    body = kwargs["body"]
    assert body["EXCG_ID_DVSN_CD"] == "KRX"      # 공식 샘플 필수 필드
    assert body["ORGN_ODNO"] == "S1"
    assert body["KRX_FWDG_ORD_ORGNO"] == "91252"
    assert body["RVSE_CNCL_DVSN_CD"] == "02"
    assert body["QTY_ALL_ORD_YN"] == "Y"
    assert body["ORD_QTY"] == "0"


async def test_fetch_cancelable_qty_uses_official_tr_on_real(monkeypatch):
    """실전에서는 최신 공식 TR(TTTC0084R)로 조회하고 psbl_qty를 파싱해야 한다."""
    kis_get = AsyncMock(return_value={
        "output": [
            {"odno": "OTHER", "psbl_qty": "3"},
            {"odno": "S1", "psbl_qty": "7"},
        ],
    })
    monkeypatch.setattr(f5_timeout.kis_rest, "get", kis_get)
    monkeypatch.setattr(f5_timeout.kis_rest, "account_no", lambda: "12345678")
    monkeypatch.setattr(f5_timeout.kis_rest, "account_cd", lambda: "01")

    qty = await f5_timeout._fetch_cancelable_qty("S1", "REAL")

    assert qty == 7
    kwargs = kis_get.await_args.kwargs
    assert kwargs["tr_id"] == "TTTC0084R"        # 최신 공식 샘플 TR
    assert kis_get.await_args.args[0] == "/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl"


async def test_fetch_cancelable_qty_skips_api_on_paper(monkeypatch):
    """모의투자는 미지원 API — 호출 없이 None을 반환해 바로 취소 요청으로 진행한다."""
    kis_get = AsyncMock()
    monkeypatch.setattr(f5_timeout.kis_rest, "get", kis_get)

    qty = await f5_timeout._fetch_cancelable_qty("S1", "PAPER")

    assert qty is None
    kis_get.assert_not_awaited()


async def test_update_order_status_marks_cancelled_in_real_db(tmp_path):
    """취소 확정 시 orders 행이 실제로 CANCELLED로 바뀌는지 DB 레벨에서 검사한다."""
    await db.init(str(tmp_path / "f5.db"))
    trade_id = await db.open_trade("20260714", "005930", 10_000.0, 10)
    order_id = await db.record_order(
        trade_id, "S1", "SELL", 10, 0.0, "TIMEOUT_SELL", "005930", "삼성전자")

    await db.update_order_status(order_id, "CANCELLED")

    conn = db.get()
    async with conn.execute("SELECT status FROM orders WHERE id=?", (order_id,)) as cur:
        row = await cur.fetchone()
    assert row["status"] == "CANCELLED"
    await db.close()


async def test_no_reorder_when_order_status_unknown(monkeypatch):
    """직전 주문 상태 불명 + 잔고 존재 — 미체결 주문이 살아 있을 수 있어 재주문 금지."""
    send_sell = AsyncMock(return_value={"output": {"ODNO": "S1"}})
    monkeypatch.setattr(f5_timeout, "_send_sell", send_sell)
    monkeypatch.setattr(f5_timeout, "_poll_fill", AsyncMock(return_value=None))
    monkeypatch.setattr(f5_timeout, "_fetch_order_status", AsyncMock(return_value=None))
    monkeypatch.setattr(f5_timeout, "_fetch_holding_qty", AsyncMock(return_value=10))
    _wire_db(monkeypatch)
    send = AsyncMock()
    monkeypatch.setattr(f5_timeout.notifier, "send", send)

    await f5_timeout.execute()

    assert send_sell.await_count == 1
    codes = [c.args[0] for c in send.await_args_list]
    assert "TIMEOUT_ORDER_FAILED" in codes


async def test_no_reorder_when_balance_recheck_fails(monkeypatch):
    """잔고 확인이 불가능하면 맹목적 재주문을 하지 않는다."""
    send_sell = AsyncMock(return_value={"output": {"ODNO": "S1"}})
    monkeypatch.setattr(f5_timeout, "_send_sell", send_sell)
    monkeypatch.setattr(f5_timeout, "_poll_fill", AsyncMock(return_value=None))
    monkeypatch.setattr(f5_timeout, "_fetch_order_status", AsyncMock(return_value=_CLEAN_ORDER))
    monkeypatch.setattr(f5_timeout, "_fetch_holding_qty", AsyncMock(return_value=None))
    _wire_db(monkeypatch)
    send = AsyncMock()
    monkeypatch.setattr(f5_timeout.notifier, "send", send)

    await f5_timeout.execute()

    assert send_sell.await_count == 1
    codes = [c.args[0] for c in send.await_args_list]
    assert "TIMEOUT_ORDER_FAILED" in codes


async def test_stale_prefetch_from_previous_day_is_ignored(monkeypatch):
    """precheck가 누락된 날 전일 값(특히 0)이 남아 있어도 11시 청산을 건너뛰지 않는다."""
    monkeypatch.setattr(f5_timeout, "_prefetch_qty", 0)          # 전일에 기록된 0
    monkeypatch.setattr(f5_timeout, "_prefetch_date", "20260713")  # 어제 날짜
    send_sell = AsyncMock(return_value={"output": {"ODNO": "S1"}})
    monkeypatch.setattr(f5_timeout, "_send_sell", send_sell)
    monkeypatch.setattr(f5_timeout, "_poll_fill", AsyncMock(
        return_value={"fill_price": 10_100, "fill_qty": 10, "fill_amt": 101_000.0}))
    record_order, update_fill, close_trade = _wire_db(monkeypatch)
    monkeypatch.setattr(f5_timeout.notifier, "send", AsyncMock())

    await f5_timeout.execute()

    # 전일 0은 무시하고 상태 파일 수량(10주)으로 매도 진행
    assert send_sell.await_args.args[1] == 10
    close_trade.assert_awaited_once()


async def test_precheck_zero_holdings_blocks_any_sell(monkeypatch):
    """precheck가 잔고 0을 확인했으면 상태 파일 수량으로 대체하지 않고 주문 금지."""
    monkeypatch.setattr(f5_timeout, "_prefetch_qty", 0)
    send_sell = AsyncMock()
    monkeypatch.setattr(f5_timeout, "_send_sell", send_sell)
    record_order, update_fill, close_trade = _wire_db(monkeypatch)
    send = AsyncMock()
    monkeypatch.setattr(f5_timeout.notifier, "send", send)

    await f5_timeout.execute()

    send_sell.assert_not_awaited()               # 잔고 0 — 매도 주문 자체를 내지 않음
    close_trade.assert_not_awaited()
    codes = [c.args[0] for c in send.await_args_list]
    assert "TIMEOUT_NO_HOLDINGS" in codes


async def test_partial_fills_close_at_weighted_average_price(monkeypatch):
    """6주@10,100 + 4주@10,050 → 평균 10,080원으로 거래를 닫고,
    부분체결 주문은 PARTIAL_FILL로 기록한다."""
    send_sell = AsyncMock(return_value={"output": {"ODNO": "S1"}})
    monkeypatch.setattr(f5_timeout, "_send_sell", send_sell)
    monkeypatch.setattr(f5_timeout, "_poll_fill", AsyncMock(side_effect=[
        {"fill_price": 10_100, "fill_qty": 6, "fill_amt": 60_600.0},
        {"fill_price": 10_050, "fill_qty": 4, "fill_amt": 40_200.0},
    ]))
    monkeypatch.setattr(f5_timeout, "_fetch_order_status", AsyncMock(
        return_value={"ccld_qty": 6, "ccld_amt": 60_600.0, "rmn_qty": 0}))
    monkeypatch.setattr(f5_timeout, "_fetch_holding_qty", AsyncMock(return_value=4))
    record_order, update_fill, close_trade = _wire_db(monkeypatch)
    send = AsyncMock()
    monkeypatch.setattr(f5_timeout.notifier, "send", send)

    await f5_timeout.execute()

    assert send_sell.await_count == 2
    assert send_sell.await_args_list[1].args[1] == 4          # 잔량만 재주문
    assert update_fill.await_args_list == [
        call(42, 10_100, 6, 0, status="PARTIAL_FILL"),        # 부분체결 상태 기록
        call(42, 10_050, 4, 0, status="FILLED"),
    ]
    close_trade.assert_awaited_once_with(7, 10_080, "TIMEOUT", 0.8, 0.0)  # 평균 청산가
    codes = [c.args[0] for c in send.await_args_list]
    assert "TIMEOUT_ORDER_FAILED" not in codes


async def test_timeout_sell_records_order_time_price(monkeypatch):
    """TIMEOUT_SELL의 order_price에는 주문 발송 전 현재가를 기록한다 (0 고정 금지).

    order_price=0이면 슬리피지 집계에서 타임아웃 매도가 전부 제외되고,
    발송 후에 조회하면 체결 이후 가격이 기준이 되어 슬리피지가 왜곡된다.
    """
    calls = []
    send_sell = AsyncMock(
        side_effect=lambda *a, **k: calls.append("sell") or {"output": {"ODNO": "S1"}})
    monkeypatch.setattr(f5_timeout, "_send_sell", send_sell)
    monkeypatch.setattr(f5_timeout, "_poll_fill", AsyncMock(
        return_value={"fill_price": 10_100, "fill_qty": 10, "fill_amt": 101_000.0}))
    monkeypatch.setattr(
        f5_timeout, "_fetch_current_price",
        AsyncMock(side_effect=lambda *a, **k: calls.append("price") or 10_050.0),
        raising=False)
    record_order, update_fill, close_trade = _wire_db(monkeypatch)
    monkeypatch.setattr(f5_timeout.notifier, "send", AsyncMock())

    await f5_timeout.execute()

    assert record_order.await_args.args[4] == 10_050.0
    assert calls == ["price", "sell"]  # 기준가 조회는 주문 발송보다 먼저
    close_trade.assert_awaited_once()


async def test_timeout_sell_price_fetch_failure_still_records_and_sells(monkeypatch):
    """현재가 조회 실패는 청산을 막지 않는다 — order_price 0.0으로 기록하고 진행."""
    send_sell = AsyncMock(return_value={"output": {"ODNO": "S1"}})
    monkeypatch.setattr(f5_timeout, "_send_sell", send_sell)
    monkeypatch.setattr(f5_timeout, "_poll_fill", AsyncMock(
        return_value={"fill_price": 10_100, "fill_qty": 10, "fill_amt": 101_000.0}))
    monkeypatch.setattr(
        f5_timeout, "_fetch_current_price",
        AsyncMock(side_effect=RuntimeError("quote down")), raising=False)
    record_order, update_fill, close_trade = _wire_db(monkeypatch)
    monkeypatch.setattr(f5_timeout.notifier, "send", AsyncMock())

    await f5_timeout.execute()

    assert record_order.await_args.args[4] == 0.0
    close_trade.assert_awaited_once()


async def test_confirmed_fill_closes_trade_once(monkeypatch):
    """전량 체결이 확인되면 1회 시도로 체결가 기준 기록 후 종료."""
    send_sell = AsyncMock(return_value={"output": {"ODNO": "S1"}})
    monkeypatch.setattr(f5_timeout, "_send_sell", send_sell)
    monkeypatch.setattr(f5_timeout, "_poll_fill", AsyncMock(
        return_value={"fill_price": 10_100, "fill_qty": 10, "fill_amt": 101_000.0}))
    record_order, update_fill, close_trade = _wire_db(monkeypatch)
    monkeypatch.setattr(f5_timeout.notifier, "send", AsyncMock())

    await f5_timeout.execute()

    assert send_sell.await_count == 1
    update_fill.assert_awaited_once_with(42, 10_100, 10, 0, status="FILLED")
    close_trade.assert_awaited_once_with(7, 10_100, "TIMEOUT", 1.0, 0.0)


async def test_exception_then_confirmed_fill_recovers(monkeypatch):
    """1차 주문 예외 후 2차에서 잔고 재검증을 거쳐 체결 확인되면 정상 종료."""
    send_sell = AsyncMock(side_effect=[RuntimeError("boom"), {"output": {"ODNO": "S2"}}])
    monkeypatch.setattr(f5_timeout, "_send_sell", send_sell)
    monkeypatch.setattr(f5_timeout, "_poll_fill", AsyncMock(
        return_value={"fill_price": 9_900, "fill_qty": 10, "fill_amt": 99_000.0}))
    fetch_qty = AsyncMock(return_value=10)
    monkeypatch.setattr(f5_timeout, "_fetch_holding_qty", fetch_qty)
    record_order, update_fill, close_trade = _wire_db(monkeypatch)
    send = AsyncMock()
    monkeypatch.setattr(f5_timeout.notifier, "send", send)

    await f5_timeout.execute()

    assert send_sell.await_count == 2
    fetch_qty.assert_awaited()
    close_trade.assert_awaited_once()
    codes = [c.args[0] for c in send.await_args_list]
    assert "TIMEOUT_ORDER_FAILED" not in codes


async def test_db_record_failure_does_not_trigger_reorder(monkeypatch):
    """DB 기록 실패는 주문 실패가 아니다 — 새 매도 주문을 유발하지 않는다."""
    send_sell = AsyncMock(return_value={"output": {"ODNO": "S1"}})
    monkeypatch.setattr(f5_timeout, "_send_sell", send_sell)
    monkeypatch.setattr(f5_timeout, "_poll_fill", AsyncMock(
        return_value={"fill_price": 10_100, "fill_qty": 10, "fill_amt": 101_000.0}))
    monkeypatch.setattr(
        f5_timeout.db, "record_order", AsyncMock(side_effect=RuntimeError("db down")))
    update_fill = AsyncMock()
    close_trade = AsyncMock()
    monkeypatch.setattr(f5_timeout.db, "update_order_fill", update_fill)
    monkeypatch.setattr(f5_timeout.db, "close_trade", close_trade)
    send = AsyncMock()
    monkeypatch.setattr(f5_timeout.notifier, "send", send)

    await f5_timeout.execute()

    assert send_sell.await_count == 1
    update_fill.assert_not_awaited()             # order_db_id 없음 — 체결 기록 생략
    close_trade.assert_awaited_once()
    codes = [c.args[0] for c in send.await_args_list]
    assert "TIMEOUT_ORDER_FAILED" not in codes


async def test_poll_fill_waits_for_full_quantity(monkeypatch):
    """부분체결 상태에서는 조기 반환하지 않고 전량 체결까지 폴링을 계속한다."""
    responses = [
        {"output1": [{"odno": "S1", "tot_ccld_qty": "6", "tot_ccld_amt": "60600"}]},
        {"output1": [{"odno": "S1", "tot_ccld_qty": "10", "tot_ccld_amt": "101000"}]},
    ]
    kis_get = AsyncMock(side_effect=responses)
    monkeypatch.setattr(f5_timeout.kis_rest, "get", kis_get)
    monkeypatch.setattr(f5_timeout.asyncio, "sleep", AsyncMock())

    fill = await f5_timeout._poll_fill("S1", timeout_sec=5, expect_qty=10)

    assert fill == {"fill_price": 10_100, "fill_qty": 10, "fill_amt": 101_000.0}
    assert kis_get.await_count == 2


async def test_poll_fill_returns_partial_after_timeout(monkeypatch):
    """타임아웃까지 전량이 안 되면 마지막 부분체결을 반환한다 (execute가 잔량 처리)."""
    monkeypatch.setattr(f5_timeout.kis_rest, "get", AsyncMock(return_value={
        "output1": [{"odno": "S1", "tot_ccld_qty": "6", "tot_ccld_amt": "60600"}]}))
    monkeypatch.setattr(f5_timeout.asyncio, "sleep", AsyncMock())

    fill = await f5_timeout._poll_fill("S1", timeout_sec=2, expect_qty=10)

    assert fill == {"fill_price": 10_100, "fill_qty": 6, "fill_amt": 60_600.0}
