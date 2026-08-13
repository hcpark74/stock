"""REAL 주문 스모크 테스트의 준비도 게이트 연동."""

from unittest.mock import AsyncMock, Mock

from api_tests import order
from src.api import auth, kis_rest
from src.modules import f3_entry
from src.utils import logger


def test_api_smoke_logging_uses_configured_log_directory(monkeypatch, tmp_path):
    setup = Mock()
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.setattr(logger, "setup", setup)

    order.h.setup_logging()

    setup.assert_called_once_with(str(tmp_path))


def test_api_smoke_account_uses_documented_fallback(monkeypatch):
    monkeypatch.delenv("KIS_ACCT_NO", raising=False)
    monkeypatch.delenv("KIS_ACCT_CD", raising=False)
    monkeypatch.setenv("KIS_ACCOUNT_NO", "12345678-01")
    monkeypatch.setenv("KIS_ACCOUNT_TYPE", "03")

    assert order.h.acct_no() == "12345678-01"
    assert order.h.acct_cd() == "03"


def test_api_smoke_account_preserves_explicit_override(monkeypatch):
    monkeypatch.setenv("KIS_ACCOUNT_NO", "fallback")
    monkeypatch.setenv("KIS_ACCOUNT_TYPE", "01")
    monkeypatch.setenv("KIS_ACCT_NO", "override")
    monkeypatch.setenv("KIS_ACCT_CD", "02")

    assert order.h.acct_no() == "override"
    assert order.h.acct_cd() == "02"


async def test_real_smoke_buy_forwards_explicit_confirmation(monkeypatch):
    post = AsyncMock(return_value={"rt_cd": "0", "output": {"ODNO": "S1"}})
    monkeypatch.setattr(kis_rest, "post", post)

    await order._place_order(
        "REAL",
        "BUY",
        1,
        limit_price=75_100,
        send_guard=lambda: True,
        allow_real_smoke_buy=True,
    )

    assert post.await_args.kwargs["tr_id"] == "TTTC0012U"
    assert post.await_args.kwargs["allow_real_smoke_buy"] is True
    assert post.await_args.kwargs["body"]["ORD_DVSN"] == "00"
    assert post.await_args.kwargs["body"]["ORD_UNPR"] == "75100"
    assert callable(post.await_args.kwargs["send_guard"])


async def test_real_smoke_buy_requires_explicit_confirmation(monkeypatch):
    post = AsyncMock(return_value={"rt_cd": "1"})
    monkeypatch.setattr(kis_rest, "post", post)

    await order._place_order("REAL", "BUY", 1, limit_price=75_100)

    assert post.await_args.kwargs["allow_real_smoke_buy"] is False


async def test_real_smoke_buy_refuses_market_fallback(monkeypatch):
    post = AsyncMock()
    monkeypatch.setattr(kis_rest, "post", post)

    try:
        await order._place_order("REAL", "BUY", 1)
    except ValueError as exc:
        assert "limit price" in str(exc)
    else:
        raise AssertionError("market BUY fallback must not be reachable")

    post.assert_not_awaited()


async def test_real_smoke_limit_plan_uses_production_f3_caps(monkeypatch):
    # 코어 갭(3%)에서는 계획이 산출되며 그 절대 갭 상한은 프로덕션 _strict_gap_cap과
    # 동일해야 한다(유동성 미검증 → fail-closed 8% 상한).
    quote = f3_entry.EntryQuote(
        ask_price=10_300,
        ask_qty=10,
        antc_price=0,
        fetched_monotonic=f3_entry.time.monotonic(),
        rt_cd="0",
        msg_cd="MCA00000",
        msg1="OK",
    )
    monkeypatch.setattr(
        f3_entry,
        "_fetch_expected_price",
        AsyncMock(return_value=(10_300.0, 10_000.0)),
    )
    monkeypatch.setattr(f3_entry, "_fetch_final_entry_quote", AsyncMock(return_value=quote))

    plan = await order._prepare_limit_buy("005930")

    assert plan["gap_cap"] == f3_entry._strict_gap_cap(10_000) == 10_790
    assert plan["limit_price"] == 10_400
    assert plan["limit_price"] < 10_800
    assert round(plan["fresh_gap"], 4) == 0.03


async def test_real_smoke_rejects_high_gap_without_liquidity(monkeypatch):
    """9% 수동 REAL 스모크는 후보 대금이 없으므로 제출 전에 거부되어야 한다."""
    import pytest

    quote = f3_entry.EntryQuote(
        ask_price=10_900,
        ask_qty=10,
        antc_price=0,
        fetched_monotonic=f3_entry.time.monotonic(),
        rt_cd="0",
        msg_cd="MCA00000",
        msg1="OK",
    )
    monkeypatch.setattr(
        f3_entry,
        "_fetch_expected_price",
        AsyncMock(return_value=(10_850.0, 10_000.0)),
    )
    monkeypatch.setattr(f3_entry, "_fetch_final_entry_quote", AsyncMock(return_value=quote))

    with pytest.raises(RuntimeError) as exc:
        await order._prepare_limit_buy("005930")

    assert "HIGH_GAP_AMOUNT_LOW" in str(exc.value)


async def test_real_smoke_sell_does_not_request_buy_bypass(monkeypatch):
    post = AsyncMock(return_value={"rt_cd": "0", "output": {"ODNO": "S2"}})
    monkeypatch.setattr(kis_rest, "post", post)

    await order._place_order(
        "REAL",
        "SELL",
        1,
        allow_real_smoke_buy=True,
    )

    assert post.await_args.kwargs["tr_id"] == "TTTC0011U"
    assert post.await_args.kwargs["allow_real_smoke_buy"] is False


async def test_confirmed_order_smoke_initializes_audit_log_before_auth(monkeypatch):
    setup_logging = Mock()
    monkeypatch.setattr(order.h, "mode", lambda: "REAL")
    monkeypatch.setattr(order.h, "setup_logging", setup_logging)
    monkeypatch.setattr(auth, "load_or_refresh", AsyncMock(return_value=False))

    result = await order.run(confirm=True)

    assert result is False
    setup_logging.assert_called_once_with()


async def test_unconfirmed_order_smoke_has_no_logging_side_effect(monkeypatch):
    setup_logging = Mock()
    monkeypatch.setattr(order.h, "mode", lambda: "REAL")
    monkeypatch.setattr(order.h, "setup_logging", setup_logging)

    result = await order.run(confirm=False)

    assert result is True
    setup_logging.assert_not_called()


async def test_unfilled_limit_smoke_confirms_cancel_before_failing(monkeypatch):
    quote = f3_entry.EntryQuote(
        ask_price=10_300,
        ask_qty=10,
        antc_price=0,
        fetched_monotonic=f3_entry.time.monotonic(),
        rt_cd="0",
        msg_cd="MCA00000",
        msg1="OK",
    )
    cancel_confirmed = AsyncMock(return_value=("CANCELLED", None))
    monkeypatch.setattr(order.h, "mode", lambda: "REAL")
    monkeypatch.setattr(order.h, "setup_logging", Mock())
    monkeypatch.setattr(auth, "load_or_refresh", AsyncMock(return_value=True))
    monkeypatch.setattr(
        order,
        "_prepare_limit_buy",
        AsyncMock(
            return_value={
                "ticker": order.TICKER,
                "expected_price": 10_300.0,
                "prev_close": 10_000.0,
                "quote": quote,
                "fresh_gap": 0.03,
                "ask_cap": 10_400.0,
                "gap_cap": 10_990.0,
                "limit_price": 10_400.0,
            }
        ),
    )
    monkeypatch.setattr(
        order,
        "_place_order",
        AsyncMock(
            return_value={
                "rt_cd": "0",
                "msg_cd": "MCA00000",
                "output": {"ODNO": "LIMIT-1", "KRX_FWDG_ORD_ORGNO": "001"},
            }
        ),
    )
    monkeypatch.setattr(order, "_poll_fill", AsyncMock(return_value=None))
    monkeypatch.setattr(f3_entry, "_cancel_entry_order_confirmed", cancel_confirmed)

    result = await order.run(confirm=True)

    assert result is False
    cancel_confirmed.assert_awaited_once_with(
        "LIMIT-1",
        "001",
        "REAL",
        order.TICKER,
        1,
        1,
        expected_qty=1,
    )


async def test_smoke_send_guard_refusal_has_explicit_failure_reason(monkeypatch):
    quote = f3_entry.EntryQuote(
        ask_price=10_300,
        ask_qty=10,
        antc_price=0,
        fetched_monotonic=f3_entry.time.monotonic(),
        rt_cd="0",
        msg_cd="MCA00000",
        msg1="OK",
    )
    fail = Mock()
    monkeypatch.setattr(order.h, "mode", lambda: "REAL")
    monkeypatch.setattr(order.h, "setup_logging", Mock())
    monkeypatch.setattr(order.h, "fail", fail)
    monkeypatch.setattr(auth, "load_or_refresh", AsyncMock(return_value=True))
    monkeypatch.setattr(
        order,
        "_prepare_limit_buy",
        AsyncMock(
            return_value={
                "ticker": order.TICKER,
                "expected_price": 10_300.0,
                "prev_close": 10_000.0,
                "quote": quote,
                "fresh_gap": 0.03,
                "ask_cap": 10_400.0,
                "gap_cap": 10_990.0,
                "limit_price": 10_400.0,
            }
        ),
    )
    monkeypatch.setattr(
        order,
        "_place_order",
        AsyncMock(
            return_value={
                "rt_cd": "1",
                "msg_cd": kis_rest.SEND_GUARD_BLOCKED_MSG_CD,
                "msg1": "blocked before send",
                "output": {},
            }
        ),
    )

    result = await order.run(confirm=True)

    assert result is False
    fail.assert_called_once()
    assert fail.call_args.args[0] == "BUY 전송 차단"
    assert "신선도" in fail.call_args.args[1]


async def test_smoke_sell_uses_actual_buy_fill_qty(monkeypatch):
    quote = f3_entry.EntryQuote(
        ask_price=10_300,
        ask_qty=10,
        antc_price=0,
        fetched_monotonic=f3_entry.time.monotonic(),
        rt_cd="0",
        msg_cd="MCA00000",
        msg1="OK",
    )
    place = AsyncMock(
        side_effect=[
            {
                "rt_cd": "0",
                "msg_cd": "MCA00000",
                "output": {"ODNO": "BUY-1", "KRX_FWDG_ORD_ORGNO": "001"},
            },
            {"rt_cd": "0", "msg_cd": "MCA00000", "output": {"ODNO": "SELL-1"}},
        ]
    )
    monkeypatch.setattr(order, "QTY", 3)
    monkeypatch.setattr(order.h, "mode", lambda: "REAL")
    monkeypatch.setattr(order.h, "setup_logging", Mock())
    monkeypatch.setattr(order.h, "ok", Mock())
    monkeypatch.setattr(auth, "load_or_refresh", AsyncMock(return_value=True))
    monkeypatch.setattr(
        order,
        "_prepare_limit_buy",
        AsyncMock(
            return_value={
                "ticker": order.TICKER,
                "expected_price": 10_300.0,
                "prev_close": 10_000.0,
                "quote": quote,
                "fresh_gap": 0.03,
                "ask_cap": 10_400.0,
                "gap_cap": 10_990.0,
                "limit_price": 10_400.0,
            }
        ),
    )
    monkeypatch.setattr(order, "_place_order", place)
    monkeypatch.setattr(
        order,
        "_poll_fill",
        AsyncMock(side_effect=[{"qty": 2, "price": 10_300}, {"qty": 2, "price": 10_310}]),
    )
    monkeypatch.setattr(order.asyncio, "sleep", AsyncMock())

    result = await order.run(confirm=True)

    assert result is True
    assert place.await_args_list[0].args[2] == 3
    assert place.await_args_list[1].args[2] == 2


# ── PAPER 스모크 매수 게이트: 고갭 유동성 정책 연동 ────────────────────────
from api_tests import order_smoke  # noqa: E402
from src import state  # noqa: E402


def _smoke_quote(ask_price: float) -> f3_entry.EntryQuote:
    return f3_entry.EntryQuote(
        ask_price=ask_price,
        ask_qty=10,
        antc_price=0,
        fetched_monotonic=f3_entry.time.monotonic(),
        rt_cd="0",
        msg_cd="MCA00000",
        msg1="OK",
    )


def _lock_candidate(ticker: str, amount) -> None:
    s = state.get()
    s.target_ticker = ticker
    s.target_candidates = [{"ticker": ticker, "expected_amount": amount, "prev_close": 10_000}]


def test_order_smoke_gate_rejects_high_gap_low_amount():
    """잠긴 후보가 고갭·저대금이면 매수 게이트가 지정가를 확정하지 않는다."""
    _lock_candidate("005930", f3_entry.HIGH_GAP_MIN_EXPECTED_AMOUNT - 1)
    limit, reason = order_smoke._smoke_buy_limit_or_reason(
        "005930", 10_300.0, 10_000.0, _smoke_quote(10_850)
    )
    assert limit is None
    assert reason == "HIGH_GAP_AMOUNT_LOW"


def test_order_smoke_gate_allows_high_gap_qualifying_amount():
    """적격 유동성이면 고갭에서도 10% 상한 기반 지정가를 확정한다."""
    _lock_candidate("005930", f3_entry.HIGH_GAP_MIN_EXPECTED_AMOUNT)
    limit, reason = order_smoke._smoke_buy_limit_or_reason(
        "005930", 10_300.0, 10_000.0, _smoke_quote(10_850)
    )
    assert reason == "OK"
    assert limit == min(
        f3_entry._floor_to_tick(10_850 * (1 + f3_entry.F3_ASK_SLIPPAGE_RATIO)),
        f3_entry._strict_gap_cap(10_000, expected_amount=f3_entry.HIGH_GAP_MIN_EXPECTED_AMOUNT),
    )


def test_order_smoke_gate_allows_core_gap_without_amount():
    """코어 갭(3%)은 대금 미검증이어도 통과하며 8% fail-closed 상한을 쓴다."""
    _lock_candidate("005930", None)
    limit, reason = order_smoke._smoke_buy_limit_or_reason(
        "005930", 10_300.0, 10_000.0, _smoke_quote(10_300)
    )
    assert reason == "OK"
    assert limit == 10_400
