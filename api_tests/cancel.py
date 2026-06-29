"""KIS 주문 취소 테스트 — 지정가 미체결 주문 생성 후 전량 취소.

취소 흐름:
  1. 삼성전자 지정가 100원 (절대 미체결) 매수 주문
  2. KRX_FWDG_ORD_ORGNO + ODNO 수신
  3. order-rvsecncl 취소 호출
  4. CCLD로 취소 확인 (cncl_yn=Y)

  python api_tests/cancel.py --confirm
"""
import asyncio
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import api_tests._helper as h

TICKER = "005930"   # 삼성전자
QTY    = 1
# 주문 가격은 현재가 조회 후 동적 산출 (현재가 × 0.85)
# 하한가(-30%)보다 높고 현재가보다 낮아 미체결 확정

_BUY_TR    = {"REAL": "TTTC0012U", "PAPER": "VTTC0012U"}
_CANCEL_TR = {"REAL": "TTTC0013U", "PAPER": "VTTC0013U"}
_CCLD_TR   = {"REAL": "TTTC0081R", "PAPER": "VTTC0081R"}


async def run(confirm: bool = False) -> bool:
    mode = h.mode()
    h.header(f"5. Cancel (주문취소)  TR={_CANCEL_TR[mode]}  mode={mode}")

    if not confirm:
        print("  --confirm 플래그 없음. 취소 테스트 건너뜁니다.")
        print("  실행: python api_tests/cancel.py --confirm")
        return True

    from src.api import auth, kis_rest

    if not await auth.load_or_refresh():
        h.fail("token")
        return False

    today = datetime.now().strftime("%Y%m%d")

    # 현재가 조회 → 미체결 확정 가격 산출 (현재가 × 0.85)
    price_resp = await kis_rest.get(
        "/uapi/domestic-stock/v1/quotations/inquire-price",
        tr_id="FHKST01010100",
        params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": TICKER},
    )
    current_price = int(price_resp.get("output", {}).get("stck_prpr") or 0)
    if not current_price:
        h.fail("현재가 조회", "stck_prpr=0")
        return False
    limit_price = str(int(current_price * 0.85 // 500 * 500))   # 500원 단위 절사 (10~50만원 호가단위)
    print(f"  현재가={current_price:,}원  → 주문가={limit_price}원 (×0.85)")

    # Step 1: 지정가 매수 주문 (미체결 확정)
    print(f"\n  [BUY 지정가] {TICKER} {QTY}주 @ {limit_price}원 (미체결용)")
    buy_resp = await kis_rest.post(
        "/uapi/domestic-stock/v1/trading/order-cash",
        tr_id=_BUY_TR[mode],
        body={
            "CANO":         h.acct_no(),
            "ACNT_PRDT_CD": h.acct_cd(),
            "PDNO":         TICKER,
            "ORD_DVSN":    "00",          # 지정가
            "ORD_QTY":     str(QTY),
            "ORD_UNPR":    limit_price,
        },
    )
    rt_cd      = buy_resp.get("rt_cd", "?")
    buy_out    = buy_resp.get("output", {})
    order_id   = buy_out.get("ODNO", "")
    org_no     = buy_out.get("KRX_FWDG_ORD_ORGNO", "")
    print(f"  rt_cd={rt_cd}  msg={buy_resp.get('msg1','').strip()}")
    print(f"  order_id={order_id}  org_no={org_no}")

    if not order_id:
        h.fail("BUY 주문 접수", "order_id 없음")
        return False
    h.ok("BUY 주문 접수 (지정가 100원)", order_id)

    await asyncio.sleep(1)

    # Step 2: 전량 취소
    print(f"\n  [CANCEL] order_id={order_id}")
    cancel_resp = await kis_rest.post(
        "/uapi/domestic-stock/v1/trading/order-rvsecncl",
        tr_id=_CANCEL_TR[mode],
        body={
            "CANO":              h.acct_no(),
            "ACNT_PRDT_CD":      h.acct_cd(),
            "KRX_FWDG_ORD_ORGNO": org_no,
            "ORGN_ODNO":         order_id,
            "ORD_DVSN":          "00",    # 원주문과 동일 (지정가)
            "RVSE_CNCL_DVSN_CD": "02",   # 02=취소
            "ORD_QTY":           "0",
            "ORD_UNPR":          "0",
            "QTY_ALL_ORD_YN":    "Y",    # 전량
        },
    )
    cancel_rt   = cancel_resp.get("rt_cd", "?")
    cancel_out  = cancel_resp.get("output", {})
    cancel_odno = cancel_out.get("odno", "")
    print(f"  rt_cd={cancel_rt}  msg={cancel_resp.get('msg1','').strip()}")
    print(f"  cancel_odno={cancel_odno}")

    if cancel_rt != "0":
        h.fail("취소 호출", cancel_resp.get("msg1", "").strip())
        return False
    h.ok("취소 호출", cancel_odno)

    # Step 3: CCLD로 취소 확인 (cncl_yn=Y) — 최대 10초 재시도
    print(f"\n  [CCLD 확인] order_id={order_id}")
    confirmed = False
    for attempt in range(10):
        await asyncio.sleep(1)
        try:
            ccld_resp = await kis_rest.get(
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
                    "CCLD_DVSN":        "00",
                    "ORD_GNO_BRNO":     "",
                    "ODNO":             order_id,
                    "INQR_DVSN_1":      "",
                    "INQR_DVSN_3":      "00",
                    "EXCG_ID_DVSN_CD":  "KRX",
                    "CTX_AREA_FK100":   "",
                    "CTX_AREA_NK100":   "",
                },
            )
            for item in ccld_resp.get("output1", []):
                if item.get("odno") == order_id:
                    cncl_yn    = item.get("cncl_yn", "")
                    rmn_qty    = int(item.get("rmn_qty") or 0)
                    ccld_qty   = int(item.get("tot_ccld_qty") or 0)
                    ord_qty    = int(item.get("ord_qty") or 0)
                    print(f"  [{attempt+1}s] cncl_yn={cncl_yn}  ord_qty={ord_qty}"
                          f"  ccld_qty={ccld_qty}  rmn_qty={rmn_qty}")
                    # PAPER: cncl_yn이 N으로 남는 경우에도
                    # 주문수량>0 + 체결량=0 + 잔량=0 → 취소 완료
                    if cncl_yn == "Y" or (ord_qty > 0 and ccld_qty == 0 and rmn_qty == 0):
                        confirmed = True
                    break
        except Exception as e:
            print(f"  [{attempt+1}s] poll error: {repr(e)}")
        if confirmed:
            break

    if confirmed:
        h.ok("취소 확인 (미체결+잔량0)", order_id)
        return True
    else:
        h.fail("취소 확인", "10s 내 CCLD 취소 상태 미확인")
        return False


if __name__ == "__main__":
    confirm = "--confirm" in sys.argv
    result = asyncio.run(run(confirm=confirm))
    raise SystemExit(0 if result else 1)
