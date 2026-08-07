"""REAL 주문 스모크 테스트의 준비도 게이트 연동."""

from unittest.mock import AsyncMock, Mock

from api_tests import order
from src.api import auth, kis_rest
from src.utils import logger


def test_api_smoke_logging_uses_configured_log_directory(monkeypatch, tmp_path):
    setup = Mock()
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.setattr(logger, "setup", setup)

    order.h.setup_logging()

    setup.assert_called_once_with(str(tmp_path))


async def test_real_smoke_buy_forwards_explicit_confirmation(monkeypatch):
    post = AsyncMock(return_value={"rt_cd": "0", "output": {"ODNO": "S1"}})
    monkeypatch.setattr(kis_rest, "post", post)

    await order._place_order(
        "REAL",
        "BUY",
        1,
        allow_real_smoke_buy=True,
    )

    assert post.await_args.kwargs["tr_id"] == "TTTC0012U"
    assert post.await_args.kwargs["allow_real_smoke_buy"] is True


async def test_real_smoke_buy_requires_explicit_confirmation(monkeypatch):
    post = AsyncMock(return_value={"rt_cd": "1"})
    monkeypatch.setattr(kis_rest, "post", post)

    await order._place_order("REAL", "BUY", 1)

    assert post.await_args.kwargs["allow_real_smoke_buy"] is False


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
