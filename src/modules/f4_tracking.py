"""F4. 장중 추적 스탑 모듈 (09:00:00 ~ 09:59:59) — PRD §F4"""

import asyncio
import math
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from src import db, live, notifier, state
from src.api import kis_rest, kis_ws
from src.utils.logger import log
from src.utils.spike_filter import SpikeFilter

KST = ZoneInfo("Asia/Seoul")

STEP_SIZE      = 0.025   # 스텝 간격 +2.5% (params.json 로드 예정)
STEP_TRAIL     = 0.015   # 스텝 기준 하락폭 -1.5%
HARD_STOP_RATIO = 0.020  # Hard Stop -2.0% (trailing 미활성 구간 전용)

_SELL_TR = {"REAL": "TTTC0011U", "PAPER": "VTTC0011U"}
_CCLD_TR = {"REAL": "TTTC0081R", "PAPER": "VTTC0081R"}


async def run() -> None:
    """
    F4 진입점 — WebSocket 구독 → 실패 시 REST 폴링 fallback.
    main.py에서 asyncio.create_task(f4_tracking.run()) 로 구동.
    """
    s = state.get()
    # HOLDING 상태가 될 때까지 대기
    while s.position_status not in ("HOLDING", "CLOSED"):
        await asyncio.sleep(0.5)
        s = state.get()

    if s.position_status == "CLOSED" or not s.target_ticker:
        return

    ticker = s.target_ticker
    spike_filter = SpikeFilter()

    if os.getenv("DRY_RUN", "0") == "1":
        await _run_dry_ticks(ticker, spike_filter)
        return

    live.ws_connected = False

    async def on_tick(tick: dict) -> None:
        live.ws_connected = True
        live.push_tick(tick["price"])
        await _process_tick(tick["price"], spike_filter)

    await kis_ws.subscribe(
        ticker, on_tick,
        stop_if=lambda: state.get().position_status != "HOLDING",
    )
    live.ws_connected = False


async def _process_tick(price: float, spike_filter: SpikeFilter) -> None:
    """단일 체결 틱 처리. 우선순위: Hard Stop > Step Trailing (상호 배타적)."""
    s = state.get()
    if s.position_status != "HOLDING":
        return
    if not spike_filter.is_valid(price, s.target_ticker):
        return

    entry = s.entry_price or 0.0
    state.update_high_price(price)

    now = datetime.now(KST)
    late = now.hour == 9 and now.minute >= 50

    # 스텝 갱신 (highest_step은 단조 증가)
    pnl = price / entry - 1
    current_step = max(math.floor(pnl / STEP_SIZE) * STEP_SIZE, 0.0)
    if current_step > s.highest_step:
        s.highest_step = current_step
    if s.highest_step >= STEP_SIZE:
        s.trailing_active = True

    # 09:50 강제 활성화 (스텝 미달성이어도 trailing 발동)
    if late and not s.trailing_active:
        s.trailing_active = True

    # [우선순위 1] Hard Stop (-2.0%): trailing 미활성 구간에서만 유효
    if not s.trailing_active and price <= entry * (1 - HARD_STOP_RATIO):
        if await state.set_closed("HARD_STOP"):
            await _execute_close(price, "HARD_STOP")
        return

    # [우선순위 2] Step Trailing
    if s.trailing_active:
        stop = entry * (1 + s.highest_step - STEP_TRAIL)
        if price <= stop:
            if await state.set_closed("TRAILING"):
                await _execute_close(price, "TRAILING")


async def _execute_close(price: float, reason: str) -> None:
    """잔여 전량 시장가 매도 후 로그/알림/DB 기록."""
    s = state.get()
    qty = s.remaining_qty or 0
    entry = s.entry_price or price
    mode = os.getenv("KIS_MODE", "PAPER")

    sell_id = ""
    exit_price = price
    if os.getenv("DRY_RUN", "0") == "1":
        sell_id = f"DRY-SELL-{datetime.now(KST).strftime('%H%M%S')}"
    else:
        try:
            sell_resp = await _send_sell(s.target_ticker, qty, mode)
            sell_id = sell_resp.get("output", {}).get("ODNO", "")
            fill = await _poll_fill(sell_id, timeout_sec=30)
            if fill:
                exit_price = fill["fill_price"]
        except Exception as e:
            log("F4_SELL_ERROR", level="CRIT", ticker=s.target_ticker, error=repr(e))

    pnl_pct = round((exit_price / entry - 1) * 100, 2) if entry else 0.0

    if s.trade_id:
        order_db_id = await db.record_order(
            s.trade_id, sell_id, "SELL", qty, exit_price, "CLOSE_SELL", s.target_ticker,
        )
        await db.update_order_fill(order_db_id, exit_price, qty, 0)
        await db.close_trade(s.trade_id, exit_price, reason, pnl_pct, s.highest_step)

    event_name = "TRAILING_STOP" if reason == "TRAILING" else reason
    level = "INFO" if reason == "TRAILING" else "WARN"

    log_extra: dict = {}
    if reason == "TRAILING":
        stop_price = entry * (1 + s.highest_step - STEP_TRAIL)
        log_extra = {"highest_step": s.highest_step, "stop_price": round(stop_price, 0)}

    log(event_name, level=level, ticker=s.target_ticker,
        entry_price=entry, exit_price=exit_price, exit_qty=qty,
        pnl_pct=pnl_pct, fill_latency_ms=0, **log_extra)
    await notifier.send(
        event_name, level=level,
        message=f"{reason} 청산: {s.target_ticker} @ {exit_price:,}원 (P&L {pnl_pct:+.2f}%)",
    )
    await state.persist(os.getenv("STATE_DIR", "data/state"),
                        datetime.now(KST).strftime("%Y%m%d"))


async def _run_dry_ticks(ticker: str, spike_filter: SpikeFilter) -> None:
    s = state.get()
    entry = float(s.entry_price or os.getenv("DRY_RUN_ENTRY_PRICE", "10300"))
    delay = float(os.getenv("DRY_RUN_STEP_DELAY", "0.2"))
    prices = [
        entry,
        round(entry * 1.026),
        round(entry * 1.032),
        round(entry * 1.010),
    ]

    live.ws_connected = True
    log("DRY_RUN_F4_START", level="WARN", ticker=ticker, entry_price=entry, prices=prices)
    for price in prices:
        if state.get().position_status != "HOLDING":
            break
        live.push_tick(price)
        log("DRY_RUN_TICK", level="INFO", ticker=ticker, price=price)
        await _process_tick(price, spike_filter)
        await asyncio.sleep(delay)
    live.ws_connected = False
    log(
        "DRY_RUN_F4_DONE",
        level="WARN",
        ticker=ticker,
        position_status=state.get().position_status,
        close_reason=state.get().close_reason,
    )


async def _send_sell(ticker: str, qty: int, mode: str) -> dict:
    return await kis_rest.post(
        "/uapi/domestic-stock/v1/trading/order-cash",
        tr_id=_SELL_TR[mode],
        body={
            "CANO": os.getenv("KIS_ACCT_NO", ""),
            "ACNT_PRDT_CD": os.getenv("KIS_ACCT_CD", "01"),
            "PDNO": ticker,
            "ORD_DVSN": "01",
            "ORD_QTY": str(qty),
            "ORD_UNPR": "0",
        },
    )


async def _poll_fill(order_id: str, timeout_sec: int = 30) -> dict | None:
    mode = os.getenv("KIS_MODE", "PAPER")
    today = datetime.now(KST).strftime("%Y%m%d")
    for _ in range(timeout_sec):
        try:
            resp = await kis_rest.get(
                "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
                tr_id=_CCLD_TR[mode],
                params={
                    "CANO": os.getenv("KIS_ACCT_NO", ""),
                    "ACNT_PRDT_CD": os.getenv("KIS_ACCT_CD", "01"),
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

