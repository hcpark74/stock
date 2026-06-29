"""KIS 주식정정취소가능주문조회 테스트 [v1_국내주식-004].

TR_ID: TTTC0084R (실전 전용 — 모의투자 미지원)

취소주문 전 psbl_qty(정정취소가능수량) 확인용 API.
최대 50건, 이후 CTX_AREA 연속조회.

  python api_tests/cancel_psbl.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import api_tests._helper as h

_TR_ID = "TTTC0084R"   # REAL only — 모의투자 미지원


async def run() -> bool:
    mode = h.mode()
    h.header(f"6. 정정취소가능주문조회  TR={_TR_ID}  mode={mode}")

    if mode != "REAL":
        print("  모의투자 미지원 API — 건너뜁니다.")
        print("  실전(KIS_MODE=REAL) 환경에서만 동작합니다.")
        return True   # skip != fail

    from src.api import auth, kis_rest

    if not await auth.load_or_refresh():
        h.fail("token")
        return False

    resp = await kis_rest.get(
        "/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl",
        tr_id=_TR_ID,
        params={
            "CANO":           h.acct_no(),
            "ACNT_PRDT_CD":   h.acct_cd(),
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
            "INQR_DVSN_1":   "0",   # 0=주문별, 1=종목별
            "INQR_DVSN_2":   "0",   # 0=전체, 1=매도, 2=매수
        },
    )
    rt_cd = resp.get("rt_cd", "?")
    if rt_cd != "0":
        h.fail("inquire-psbl-rvsecncl", resp.get("msg1", "").strip())
        return False

    orders = resp.get("output", [])
    print(f"  정정/취소 가능 주문: {len(orders)}건")
    for item in orders[:5]:   # 최대 5건 출력
        odno     = item.get("odno", "")
        pdno     = item.get("pdno", "").strip()
        name     = item.get("prdt_name", "").strip()
        qty      = item.get("ord_qty", "0")
        unpr     = item.get("ord_unpr", "0")
        psbl_qty = item.get("psbl_qty", "0")
        dvsn     = "매수" if item.get("sll_buy_dvsn_cd") == "02" else "매도"
        print(f"    [{dvsn}] {name}({pdno})  주문={qty}주 @{unpr}원"
              f"  취소가능={psbl_qty}주  odno={odno}")
    if len(orders) > 5:
        print(f"    ... 외 {len(orders) - 5}건")

    h.ok("inquire-psbl-rvsecncl", f"{len(orders)}건")
    return True


if __name__ == "__main__":
    result = asyncio.run(run())
    raise SystemExit(0 if result else 1)
