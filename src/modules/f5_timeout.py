"""F5. 타임아웃 청산 스케줄러 (11:00:00) — PRD §F5"""

import asyncio
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from src import db, notifier, state
from src.api import kis_rest
from src.utils.logger import log

KST = ZoneInfo("Asia/Seoul")
_RETRY = 3
_RETRY_INTERVAL = 2  # 초

_SELL_TR = {"REAL": "TTTC0011U", "PAPER": "VTTC0011U"}
_BAL_TR  = {"REAL": "TTTC8434R", "PAPER": "VTTC8434R"}
_CCLD_TR = {"REAL": "TTTC0081R", "PAPER": "VTTC0081R"}

_prefetch_qty: int = 0


async def precheck() -> None:
    """10:59:50 — 잔고 조회로 실제 보유 수량 확인."""
    global _prefetch_qty
    s = state.get()
    if s.position_status != "HOLDING" or not s.target_ticker:
        return
    mode = os.getenv("KIS_MODE", "PAPER")
    try:
        resp = await kis_rest.get(
            "/uapi/domestic-stock/v1/trading/inquire-balance",
            tr_id=_BAL_TR[mode],
            params={
                "CANO": kis_rest.account_no(),
                "ACNT_PRDT_CD": kis_rest.account_cd(),
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "",
                "INQR_DVSN": "01",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "01",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
        )
        for item in resp.get("output1", []):
            if item.get("pdno") == s.target_ticker:
                _prefetch_qty = int(item.get("hldg_qty") or 0)
                break
        else:
            _prefetch_qty = s.remaining_qty or 0
    except Exception as e:
        log("F5_PRECHECK_FAIL", level="WARN", ticker=s.target_ticker, error=str(e))
        _prefetch_qty = s.remaining_qty or 0
    log("F5_PRECHECK", level="INFO", ticker=s.target_ticker, prefetch_qty=_prefetch_qty)


async def execute() -> None:
    """11:00:00 — 미청산 잔여 전량 시장가 청산. Retry 최대 3회."""
    s = state.get()
    if s.position_status != "HOLDING":
        return  # 이미 F4에서 청산됨

    if not await state.set_closed("TIMEOUT"):
        return

    ticker = s.target_ticker
    qty = _prefetch_qty or s.remaining_qty or 0
    mode = os.getenv("KIS_MODE", "PAPER")

    for attempt in range(1, _RETRY + 1):
        try:
            resp = await _send_sell(ticker, qty, mode)
            sell_id = resp.get("output", {}).get("ODNO", "")
            fill = await _poll_fill(sell_id, timeout_sec=30)

            exit_price = fill["fill_price"] if fill else 0.0
            entry = s.entry_price or 0.0
            pnl_pct = round((exit_price / entry - 1) * 100, 2) if (entry and exit_price) else 0.0

            if s.trade_id:
                order_db_id = await db.record_order(
                    s.trade_id, sell_id, "SELL", qty, exit_price,
                    "TIMEOUT_SELL", ticker,
                )
                await db.update_order_fill(order_db_id, exit_price, qty, 0)
                await db.close_trade(s.trade_id, exit_price, "TIMEOUT", pnl_pct, s.highest_step)

            log("TIMEOUT_CLOSE", level="INFO", ticker=ticker,
                entry_price=entry, exit_price=exit_price, exit_qty=qty,
                pnl_pct=pnl_pct, fill_latency_ms=0)
            await notifier.send("TIMEOUT_CLOSE", level="INFO",
                                message=f"11시 청산: {ticker} {qty}주 @ {exit_price:,.0f}원")
            await state.persist(os.getenv("STATE_DIR", "data/state"),
                                datetime.now(KST).strftime("%Y%m%d"))
            return

        except Exception as e:
            log("TIMEOUT_RETRY", level="WARN", ticker=ticker, attempt=attempt, error=str(e))
            if attempt < _RETRY:
                await asyncio.sleep(_RETRY_INTERVAL)

    log("TIMEOUT_ORDER_FAILED", level="CRIT", ticker=ticker,
        attempt_count=_RETRY, last_error_code="", last_error_msg="Max retries exceeded")
    await notifier.send("TIMEOUT_ORDER_FAILED", level="CRIT",
                        message=f"11시 청산 실패! 수동 청산 필요. {ticker} {qty}주")


async def _send_sell(ticker: str, qty: int, mode: str) -> dict:
    return await kis_rest.post(
        "/uapi/domestic-stock/v1/trading/order-cash",
        tr_id=_SELL_TR[mode],
        body={
            "CANO": kis_rest.account_no(),
            "ACNT_PRDT_CD": kis_rest.account_cd(),
            "PDNO": ticker,
            "ORD_DVSN": "01",
            "ORD_QTY": str(qty),
            "ORD_UNPR": "0",
        },
    )


async def _poll_fill(order_id: str, timeout_sec: int = 30) -> dict | None:
    """매도 주문 체결을 timeout_sec 초 내에 폴링."""
    mode = os.getenv("KIS_MODE", "PAPER")
    today = datetime.now(KST).strftime("%Y%m%d")
    for _ in range(timeout_sec):
        try:
            resp = await kis_rest.get(
                "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
                tr_id=_CCLD_TR[mode],
                params={
                    "CANO": kis_rest.account_no(),
                    "ACNT_PRDT_CD": kis_rest.account_cd(),
                    "INQR_STRT_DT": today,
                    "INQR_END_DT": today,
                    "SLL_BUY_DVSN_CD": "01",
                    "INQR_DVSN": "00",
                    "PDNO": "",
                    "CCLD_DVSN": "00",
                    "ORD_GNO_BRNO": "",
                    "ODNO": order_id,
                    "INQR_DVSN_3": "00",
                    "INQR_DVSN_1": "",
                    "EXCG_ID_DVSN_CD": "KRX",
                    "CTX_AREA_FK100": "",
                    "CTX_AREA_NK100": "",
                },
            )
            for item in resp.get("output1", []):
                if item.get("odno") == order_id:
                    tot_qty = int(item.get("tot_ccld_qty") or 0)
                    tot_amt = float(item.get("tot_ccld_amt") or 0)
                    if tot_qty > 0:
                        return {"fill_price": round(tot_amt / tot_qty), "fill_qty": tot_qty}
        except Exception:
            pass
        await asyncio.sleep(1)
    return None
