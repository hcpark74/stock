"""KIS 매수가능조회 테스트 [v1_국내주식-007].

TR_ID: TTTC8908R (실전) / VTTC8908R (모의)

핵심 필드:
  nrcvb_buy_amt / nrcvb_buy_qty : 미수 미사용 기준 매수가능금액/수량
  max_buy_amt   / max_buy_qty   : 미수 사용 기준 최대매수금액/수량

  python api_tests/buy_psbl.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import api_tests._helper as h

TICKER = "005930"   # 삼성전자 (테스트용)

_TR_ID = {"REAL": "TTTC8908R", "PAPER": "VTTC8908R"}


async def run() -> bool:
    mode = h.mode()
    h.header(f"7. 매수가능조회  TR={_TR_ID[mode]}  mode={mode}")

    from src.api import auth, kis_rest

    if not await auth.load_or_refresh():
        h.fail("token")
        return False

    # 시장가(ORD_DVSN=01)로 조회해야 종목증거금율이 반영된 수량이 나옴
    # ORD_UNPR은 시장가 시 공란
    resp = await kis_rest.get(
        "/uapi/domestic-stock/v1/trading/inquire-psbl-order",
        tr_id=_TR_ID[mode],
        params={
            "CANO":               h.acct_no(),
            "ACNT_PRDT_CD":       h.acct_cd(),
            "PDNO":               TICKER,
            "ORD_UNPR":           "",    # 시장가 조회 시 공란
            "ORD_DVSN":           "01",  # 시장가 (증거금율 반영)
            "CMA_EVLU_AMT_ICLD_YN": "N",
            "OVRS_ICLD_YN":       "N",
        },
    )
    rt_cd = resp.get("rt_cd", "?")
    if rt_cd != "0":
        h.fail("inquire-psbl-order", resp.get("msg1", "").strip())
        return False

    out = resp.get("output", {})
    nrcvb_amt = int(out.get("nrcvb_buy_amt") or 0)
    nrcvb_qty = int(out.get("nrcvb_buy_qty") or 0)
    max_amt   = int(out.get("max_buy_amt") or 0)
    max_qty   = int(out.get("max_buy_qty") or 0)
    cash      = int(out.get("ord_psbl_cash") or 0)

    print(f"  [{TICKER}] 시장가 기준")
    print(f"    주문가능현금      : {cash:>15,}원")
    print(f"    미수없는 매수금액 : {nrcvb_amt:>15,}원  ({nrcvb_qty}주)")
    print(f"    최대 매수금액     : {max_amt:>15,}원  ({max_qty}주)")

    if nrcvb_amt > 0:
        h.ok("inquire-psbl-order", f"nrcvb_buy_qty={nrcvb_qty}주")
    else:
        # 예수금 0이어도 API 호출 자체는 성공
        h.ok("inquire-psbl-order", "nrcvb_buy_amt=0 (예수금 부족)")

    return True


if __name__ == "__main__":
    result = asyncio.run(run())
    raise SystemExit(0 if result else 1)
