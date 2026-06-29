"""KIS 주문 테스트 — 시장가 매수 → 체결 확인 → 매도 → 체결 확인.

실제 주문이 발생합니다. --confirm 플래그 또는 confirm=True 인자 필수.
  python api_tests/order.py --confirm
"""
import asyncio
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import api_tests._helper as h

TICKER       = "005930"   # 삼성전자
QTY          = 1
FILL_TIMEOUT = 60         # 초

_BUY_TR  = {"REAL": "TTTC0012U", "PAPER": "VTTC0012U"}
_SELL_TR = {"REAL": "TTTC0011U", "PAPER": "VTTC0011U"}
_CCLD_TR = {"REAL": "TTTC0081R", "PAPER": "VTTC0081R"}


async def _place_order(mode: str, side: str, qty: int) -> dict:
    from src.api import kis_rest

    tr_id = _BUY_TR[mode] if side == "BUY" else _SELL_TR[mode]
    return await kis_rest.post(
        "/uapi/domestic-stock/v1/trading/order-cash",
        tr_id=tr_id,
        body={
            "CANO":         h.acct_no(),
            "ACNT_PRDT_CD": h.acct_cd(),
            "PDNO":         TICKER,
            "ORD_DVSN":    "01",    # 시장가
            "ORD_QTY":     str(qty),
            "ORD_UNPR":    "0",
        },
    )


async def _poll_fill(order_id: str, mode: str, timeout: int = FILL_TIMEOUT) -> dict | None:
    from src.api import kis_rest

    today = datetime.now().strftime("%Y%m%d")
    for elapsed in range(timeout):
        try:
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
            for item in resp.get("output1", []):
                if item.get("odno") == order_id:
                    qty_filled = int(item.get("tot_ccld_qty") or 0)
                    amt = float(item.get("tot_ccld_amt") or 0)
                    if qty_filled > 0:
                        return {"qty": qty_filled, "price": round(amt / qty_filled)}
        except Exception as e:
            print(f"    poll[{elapsed}s] {repr(e)}")
        await asyncio.sleep(1)
    return None


async def run(confirm: bool = False) -> bool:
    mode = h.mode()
    h.header(f"2. Order (매수+매도)  {TICKER} {QTY}주  mode={mode}")

    if not confirm:
        print("  --confirm 플래그 없음. 주문 테스트 건너뜁니다.")
        print("  실행: python api_tests/order.py --confirm")
        return True   # skip ≠ fail

    from src.api import auth

    if not await auth.load_or_refresh():
        h.fail("token")
        return False

    # BUY
    print(f"\n  [BUY] {TICKER} {QTY}주 시장가")
    buy_resp = await _place_order(mode, "BUY", QTY)
    buy_id   = buy_resp.get("output", {}).get("ODNO", "")
    print(f"  msg_cd={buy_resp.get('msg_cd')}  {buy_resp.get('msg1','').strip()}")
    print(f"  order_id : {buy_id}")

    if not buy_id:
        h.fail("BUY 주문", "order ID 없음")
        return False
    h.ok("BUY 주문 접수", buy_id)

    print(f"  체결 대기 (최대 {FILL_TIMEOUT}s)...")
    buy_fill = await _poll_fill(buy_id, mode)
    if buy_fill:
        h.ok("BUY 체결", f"{buy_fill['qty']}주 @ {buy_fill['price']:,}원")
    else:
        h.fail("BUY 체결", f"{FILL_TIMEOUT}s 내 미체결")
        return False

    await asyncio.sleep(1)

    # SELL
    print(f"\n  [SELL] {TICKER} {QTY}주 시장가")
    sell_resp = await _place_order(mode, "SELL", QTY)
    sell_id   = sell_resp.get("output", {}).get("ODNO", "")
    print(f"  msg_cd={sell_resp.get('msg_cd')}  {sell_resp.get('msg1','').strip()}")
    print(f"  order_id : {sell_id}")

    if not sell_id:
        h.fail("SELL 주문", "order ID 없음")
        return False
    h.ok("SELL 주문 접수", sell_id)

    print(f"  체결 대기 (최대 {FILL_TIMEOUT}s)...")
    sell_fill = await _poll_fill(sell_id, mode)
    if sell_fill:
        h.ok("SELL 체결", f"{sell_fill['qty']}주 @ {sell_fill['price']:,}원")
        pnl     = sell_fill["price"] - buy_fill["price"]
        pnl_pct = pnl / buy_fill["price"] * 100
        print(f"\n  BUY  : {buy_fill['price']:,} KRW")
        print(f"  SELL : {sell_fill['price']:,} KRW")
        print(f"  P&L  : {pnl:+,} KRW ({pnl_pct:+.2f}%)")
    else:
        h.fail("SELL 체결", f"{FILL_TIMEOUT}s 내 미체결")
        return False

    return True


if __name__ == "__main__":
    confirm = "--confirm" in sys.argv
    result = asyncio.run(run(confirm=confirm))
    raise SystemExit(0 if result else 1)
