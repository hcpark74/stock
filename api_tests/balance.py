"""KIS 잔고조회 테스트 — 보유종목 + 계좌 요약."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import api_tests._helper as h

_BAL_TR = {"REAL": "TTTC8434R", "PAPER": "VTTC8434R"}


async def run() -> bool:
    mode = h.mode()
    h.header(f"4. Balance (잔고조회)  TR={_BAL_TR[mode]}  mode={mode}")
    from src.api import auth, kis_rest

    if not await auth.load_or_refresh():
        h.fail("token")
        return False

    try:
        resp = await kis_rest.get(
            "/uapi/domestic-stock/v1/trading/inquire-balance",
            tr_id=_BAL_TR[mode],
            params={
                "CANO":                  h.acct_no(),
                "ACNT_PRDT_CD":          h.acct_cd(),
                "AFHR_FLPR_YN":          "N",
                "OFL_YN":                "",
                "INQR_DVSN":             "01",
                "UNPR_DVSN":             "01",
                "FUND_STTL_ICLD_YN":     "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN":             "01",
                "CTX_AREA_FK100":        "",
                "CTX_AREA_NK100":        "",
            },
        )
        rt_cd    = resp.get("rt_cd", "?")
        holdings = resp.get("output1", [])
        o2_list  = resp.get("output2", [])
        o2       = o2_list[0] if o2_list else {}

        print(f"\n  rt_cd : {rt_cd}  보유종목 : {len(holdings)}개")
        for item in holdings:
            avg = float(item.get("pchs_avg_pric") or 0)
            print(f"    {item.get('pdno')}  {item.get('prdt_name','')}"
                  f"  {item.get('hldg_qty')}주  평단={avg:,.0f}원"
                  f"  평가손익={int(item.get('evlu_pfls_amt') or 0):+,}원")
        if not holdings:
            print("    (보유 없음)")

        if o2:
            print()
            print(f"  예수금(D+0)  : {int(o2.get('dnca_tot_amt') or 0):>15,} 원")
            print(f"  D+2 예수금   : {int(o2.get('prvs_rcdl_excc_amt') or 0):>15,} 원")
            print(f"  유가평가금액 : {int(o2.get('scts_evlu_amt') or 0):>15,} 원")
            print(f"  총평가금액   : {int(o2.get('tot_evlu_amt') or 0):>15,} 원")
            print(f"  자산증감액   : {int(o2.get('asst_icdc_amt') or 0):>+15,} 원")
            print(f"  금일매수금액 : {int(o2.get('thdt_buy_amt') or 0):>15,} 원")
            print(f"  금일매도금액 : {int(o2.get('thdt_sll_amt') or 0):>15,} 원")

        if rt_cd == "0":
            h.ok("Balance 조회", f"보유 {len(holdings)}종목")
            return True
        else:
            h.fail("Balance 조회", resp.get("msg1", "").strip())
            return False

    except Exception as e:
        h.fail("Balance 조회", repr(e))
        return False


if __name__ == "__main__":
    result = asyncio.run(run())
    raise SystemExit(0 if result else 1)
