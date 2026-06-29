"""PAPER 1-share buy/sell smoke test.

Flow:
1. Select a target through F1/F2.
2. Buy exactly 1 share at market.
3. Poll fill.
4. Sell exactly the filled quantity at market.

This script refuses to run outside KIS_MODE=PAPER.
"""

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import api_tests._helper as h

from src import db, state
from src.api import auth
from src.modules import f1_filter, f2_lockup, f3_entry
from src.utils import logger
from src.utils.logger import log

KST = ZoneInfo("Asia/Seoul")


def _deadline(seconds: int = 30) -> tuple[int, int, int]:
    dl = datetime.now(KST) + timedelta(seconds=seconds)
    return dl.hour, dl.minute, dl.second


async def run() -> bool:
    h.header("PAPER 1주 왕복 주문 스모크 테스트")

    if h.mode() != "PAPER":
        h.fail("mode guard", f"KIS_MODE={h.mode()}")
        return False

    logger.setup("data/logs")
    await db.init("data/db/trading.db")

    if not await auth.load_or_refresh():
        h.fail("token")
        await db.close()
        return False

    today = datetime.now(KST).strftime("%Y%m%d")
    await state.ensure_trading_day(today)

    candidates = await f1_filter.run()
    h.ok("f1", f"candidates={len(candidates)}")
    if not candidates:
        h.fail("target", "F1 후보 없음")
        await db.close()
        return False

    await f2_lockup.run(candidates)
    ticker = state.get().target_ticker
    if not ticker:
        h.fail("f2", "target_ticker 없음")
        await db.close()
        return False
    h.ok("f2", f"target={ticker}")

    qty = 1
    log("ORDER_SMOKE_BUY_START", level="INFO", ticker=ticker, order_qty=qty)
    buy_resp = await f3_entry._send_buy(ticker, qty, "PAPER")
    buy_out = buy_resp.get("output", {})
    buy_order_id = buy_out.get("ODNO", "")
    buy_org_no = buy_out.get("KRX_FWDG_ORD_ORGNO", "")
    print("  buy_resp:", {
        "rt_cd": buy_resp.get("rt_cd"),
        "msg_cd": buy_resp.get("msg_cd"),
        "msg1": buy_resp.get("msg1"),
        "order_id": buy_order_id,
        "org_no": buy_org_no,
    })

    if not buy_order_id or buy_resp.get("rt_cd") not in (None, "0"):
        h.fail("buy order", buy_resp.get("msg1", "주문 실패"))
        await db.close()
        return False

    buy_fill = await f3_entry._poll_fill(buy_order_id, _deadline())
    if not buy_fill:
        h.fail("buy fill", "30초 내 체결 없음, 취소 시도")
        if buy_org_no:
            await f3_entry._cancel_order(buy_order_id, buy_org_no, "PAPER")
        await db.close()
        return False

    h.ok("buy fill", f"{buy_fill['fill_qty']}주 @ {buy_fill['fill_price']:,}")
    log(
        "ORDER_SMOKE_BUY_FILLED", level="INFO", ticker=ticker,
        order_id=buy_order_id, fill_qty=buy_fill["fill_qty"],
        fill_price=buy_fill["fill_price"],
    )

    sell_qty = int(buy_fill["fill_qty"])
    await asyncio.sleep(1.2)
    log("ORDER_SMOKE_SELL_START", level="INFO", ticker=ticker, order_qty=sell_qty)
    sell_resp: dict = {}
    sell_order_id = ""
    for attempt in range(1, 6):
        if attempt > 1:
            await asyncio.sleep(1.2)
        sell_resp = await f3_entry._send_sell(ticker, sell_qty, "PAPER")
        sell_out = sell_resp.get("output", {})
        sell_order_id = sell_out.get("ODNO", "")
        print("  sell_resp:", {
            "attempt": attempt,
            "rt_cd": sell_resp.get("rt_cd"),
            "msg_cd": sell_resp.get("msg_cd"),
            "msg1": sell_resp.get("msg1"),
            "order_id": sell_order_id,
        })
        if sell_order_id and sell_resp.get("rt_cd") in (None, "0"):
            break

    if not sell_order_id or sell_resp.get("rt_cd") not in (None, "0"):
        h.fail("sell order", sell_resp.get("msg1", "주문 실패"))
        await db.close()
        return False

    sell_fill = await f3_entry._poll_fill(sell_order_id, _deadline())
    if not sell_fill:
        h.fail("sell fill", "30초 내 체결 없음")
        await db.close()
        return False

    h.ok("sell fill", f"{sell_fill['fill_qty']}주 @ {sell_fill['fill_price']:,}")
    log(
        "ORDER_SMOKE_SELL_FILLED", level="INFO", ticker=ticker,
        order_id=sell_order_id, fill_qty=sell_fill["fill_qty"],
        fill_price=sell_fill["fill_price"],
    )

    await db.close()
    h.ok("round trip", f"{ticker} 1주 매수/매도 완료")
    return True


if __name__ == "__main__":
    ok = asyncio.run(run())
    raise SystemExit(0 if ok else 1)
