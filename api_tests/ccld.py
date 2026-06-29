"""KIS 주문체결조회 테스트 — 당일 전체 + 미체결."""
import asyncio
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import api_tests._helper as h

_CCLD_TR = {"REAL": "TTTC0081R", "PAPER": "VTTC0081R"}


async def _query(mode: str, ccld_dvsn: str, label: str) -> list:
    from src.api import kis_rest

    today = datetime.now().strftime("%Y%m%d")
    resp = await kis_rest.get(
        "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
        tr_id=_CCLD_TR[mode],
        params={
            "CANO":             h.acct_no(),
            "ACNT_PRDT_CD":     h.acct_cd(),
            "INQR_STRT_DT":     today,
            "INQR_END_DT":      today,
            "SLL_BUY_DVSN_CD":  "00",
            "INQR_DVSN":        "00",
            "PDNO":             "",
            "CCLD_DVSN":        ccld_dvsn,
            "ORD_GNO_BRNO":     "",
            "ODNO":             "",
            "INQR_DVSN_1":      "",
            "INQR_DVSN_3":      "00",
            "EXCG_ID_DVSN_CD":  "KRX",
            "CTX_AREA_FK100":   "",
            "CTX_AREA_NK100":   "",
        },
    )
    orders = resp.get("output1", [])
    out2   = resp.get("output2", {})
    rt_cd  = resp.get("rt_cd", "?")

    print(f"\n  [{label}]  rt_cd={rt_cd}  {len(orders)}건")
    for o in orders:
        side = "매수" if o.get("sll_buy_dvsn_cd") == "02" else "매도"
        print(f"    {o.get('odno')}  {side}  {o.get('pdno')} {o.get('prdt_name','')}"
              f"  주문{o.get('ord_qty')}주 체결{o.get('tot_ccld_qty')}주"
              f"  잔여{o.get('rmn_qty')}주  {o.get('ord_tmd','')}")
    if not orders:
        print("    (없음)")
    if out2:
        total_amt = int(out2.get("prsm_tlex_smtl") or 0)
        print(f"  총체결금액: {total_amt:,}원")

    return orders


async def run() -> bool:
    mode = h.mode()
    h.header(f"3. CCLD (주문체결조회)  TR={_CCLD_TR[mode]}  mode={mode}")
    from src.api import auth

    if not await auth.load_or_refresh():
        h.fail("token")
        return False

    try:
        orders = await _query(mode, "00", "전체 (체결+미체결)")
        await _query(mode, "02", "미체결만")
        h.ok("CCLD 조회", f"오늘 주문 {len(orders)}건")
        return True
    except Exception as e:
        h.fail("CCLD 조회", repr(e))
        return False


if __name__ == "__main__":
    result = asyncio.run(run())
    raise SystemExit(0 if result else 1)
