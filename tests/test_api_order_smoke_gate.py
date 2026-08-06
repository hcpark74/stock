"""REAL 주문 스모크 테스트의 준비도 게이트 연동."""

from unittest.mock import AsyncMock

from api_tests import order
from src.api import kis_rest


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
