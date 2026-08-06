"""REAL 취소 스모크 테스트의 매수 게이트 연동."""

from unittest.mock import AsyncMock

from api_tests import cancel
from src.api import auth, kis_rest


async def test_real_cancel_smoke_forwards_confirmation_only_to_test_buy(monkeypatch):
    get = AsyncMock(side_effect=[
        {"rt_cd": "0", "output": {"stck_prpr": "100000"}},
        {
            "rt_cd": "0",
            "output1": [{
                "odno": "BUY-CANCEL-TEST",
                "cncl_yn": "Y",
                "ord_qty": "1",
                "tot_ccld_qty": "0",
                "rmn_qty": "0",
            }],
        },
    ])
    post = AsyncMock(side_effect=[
        {
            "rt_cd": "0",
            "output": {
                "ODNO": "BUY-CANCEL-TEST",
                "KRX_FWDG_ORD_ORGNO": "01",
            },
        },
        {"rt_cd": "0", "output": {"odno": "CANCEL-TEST"}},
    ])
    monkeypatch.setattr(cancel.h, "mode", lambda: "REAL")
    monkeypatch.setattr(auth, "load_or_refresh", AsyncMock(return_value=True))
    monkeypatch.setattr(kis_rest, "get", get)
    monkeypatch.setattr(kis_rest, "post", post)
    monkeypatch.setattr(cancel.asyncio, "sleep", AsyncMock())

    result = await cancel.run(confirm=True)

    assert result is True
    assert post.await_args_list[0].kwargs["tr_id"] == "TTTC0012U"
    assert post.await_args_list[0].kwargs["allow_real_smoke_buy"] is True
    assert post.await_args_list[1].kwargs["tr_id"] == "TTTC0013U"
    assert "allow_real_smoke_buy" not in post.await_args_list[1].kwargs
