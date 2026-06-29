"""F3. 진입 주문 모듈 (08:59:40 ~ 09:00:10) — PRD §F3"""

import asyncio
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src import db, notifier, state
from src.api import kis_rest
from src.utils.logger import log

KST = ZoneInfo("Asia/Seoul")

GAP_MIN_RECHECK = 0.020   # 재검증 하한 (F1 3%보다 낮음 — 완충)
GAP_MAX_RECHECK = 0.070
ALLOC_RATIO = 0.10         # 자본 대비 10% 투입
FIRST_RATIO = 0.70         # 1차 70%
SLIPPAGE_LIMIT = 0.005     # 슬리피지 허용 +0.5%
PYRAMID_MIN_UP = 0.005     # 피라미딩 조건 +0.5% 이상 유지

# KIS TR ID (PAPER/REAL 분기) — 신TR 기준
_BUY_TR    = {"REAL": "TTTC0012U", "PAPER": "VTTC0012U"}
_SELL_TR   = {"REAL": "TTTC0011U", "PAPER": "VTTC0011U"}
_CANCEL_TR = {"REAL": "TTTC0013U", "PAPER": "VTTC0013U"}
_CCLD_TR   = {"REAL": "TTTC0081R", "PAPER": "VTTC0081R"}
_BAL_TR    = {"REAL": "TTTC8434R", "PAPER": "VTTC8434R"}

_pending_buy_org_no: str = ""  # 매수 주문 후 저장, 취소 시 사용


async def run(force: bool = False) -> None:
    """
    [08:59:40] 갭 재검증
    [08:59:50] 1차 70% 시장가 매수
    [09:00:00] 체결 확인 / 슬리피지 가드
    [09:00:10] 2차 30% 피라미딩 (조건부)
    force=True: FORCE_CATCHUP 모드. 시각 제약 없이 실행, fill 마감을 실행 시점 +30초로 설정.
    """
    s = state.get()
    if s.day_skip or not s.target_ticker:
        log("F3_SKIPPED", level="WARN",
            reason="DAY_SKIP" if s.day_skip else "NO_TARGET")
        return
    ticker = s.target_ticker
    mode = os.getenv("KIS_MODE", "PAPER")

    # ── [08:59:40] 갭 재검증 ─────────────────────────────────────────
    expected_price, prev_close = await _fetch_expected_price(ticker)
    if prev_close and expected_price:
        gap = (expected_price / prev_close) - 1
        if not (GAP_MIN_RECHECK <= gap < GAP_MAX_RECHECK):
            s.day_skip = True
            s.close_reason = "GAP_CHANGED"
            log(
                "GAP_CHANGED", level="WARN", ticker=ticker,
                gap_at_lockup=None, gap_at_entry=round(gap * 100, 2),
                reason="BELOW_MIN" if gap < GAP_MIN_RECHECK else "ABOVE_MAX",
            )
            await notifier.send("GAP_CHANGED", level="WARN",
                                message=f"진입 직전 갭 변동({gap*100:.1f}%). 거래 스킵.")
            await db.record_skip(_today(), "GAP_CHANGED", f"gap={gap*100:.2f}%")
            return

    # ── 잔고 조회 및 수량 산정 ────────────────────────────────────────
    cash = await _fetch_available_cash()
    total_amount = int(cash * ALLOC_RATIO)
    if not expected_price or expected_price == 0:
        return
    total_qty = int(total_amount / expected_price)
    if total_qty == 0:
        log("INSUFFICIENT_BALANCE", level="WARN", ticker=ticker,
            filter_count=0, reason="QTY_ZERO")
        s.day_skip = True
        return

    first_qty = int(total_qty * FIRST_RATIO)
    second_qty = total_qty - first_qty

    # ── [08:59:50] 1차 70% 시장가 매수 ──────────────────────────────
    await _sleep_until(8, 59, 50)
    if not await state.set_entering():
        return

    global _pending_buy_org_no
    order_resp = await _send_buy(ticker, first_qty, mode)
    order_id = order_resp.get("output", {}).get("ODNO", "UNKNOWN")
    _pending_buy_org_no = order_resp.get("output", {}).get("KRX_FWDG_ORD_ORGNO", "")

    # ── 1차 체결 확인 (08:59:50 ~ 09:00:00, 1초 간격) ───────────────
    if force:
        dl = datetime.now(KST) + timedelta(seconds=30)
        fill_deadline = (dl.hour, dl.minute, dl.second)
    else:
        fill_deadline = (9, 0, 0)
    fill = await _poll_fill(order_id, deadline=fill_deadline)
    if not fill:
        await _cancel_order(order_id, _pending_buy_org_no, mode)
        await state.reset_to_idle("ENTRY_FAIL")
        log("ENTRY_FAIL", level="WARN", ticker=ticker,
            order_id=order_id, order_price=expected_price,
            order_qty=first_qty, reason="UNFILLED")
        await notifier.send("ENTRY_FAIL", level="WARN",
                            message=f"진입 미체결. {ticker}")
        await db.record_skip(_today(), "ENTRY_FAIL", f"order_id={order_id}")
        return

    fill_price: float = fill["fill_price"]
    fill_qty: int = fill["fill_qty"]

    # ── 슬리피지 가드 ────────────────────────────────────────────────
    if fill_price > expected_price * (1 + SLIPPAGE_LIMIT):
        slippage_pct = (fill_price / expected_price - 1) * 100
        log("SLIPPAGE_GUARD", level="WARN", ticker=ticker,
            expected_price=expected_price, fill_price=fill_price,
            slippage_pct=round(slippage_pct, 3))
        await notifier.send("SLIPPAGE_GUARD", level="WARN",
                            message=f"슬리피지 {slippage_pct:.2f}% 초과. 즉시 청산.")
        await _send_sell(ticker, fill_qty, mode)
        s.day_skip = True
        s.close_reason = "SLIPPAGE_GUARD"
        await db.record_skip(_today(), "SLIPPAGE_GUARD",
                             f"expected={expected_price},fill={fill_price}")
        return

    # ── HOLDING 전환 + DB 기록 + 영속화 ──────────────────────────────
    await state.set_holding(fill_price, fill_qty, order_id)
    trade_id = await db.open_trade(_today(), ticker, fill_price, fill_qty)
    state.get().trade_id = trade_id
    order_db_id = await db.record_order(
        trade_id, order_id, "BUY", fill_qty, fill_price, "FIRST_BUY", ticker,
    )
    await db.update_order_fill(order_db_id, fill_price, fill_qty, 0)
    await state.persist(os.getenv("STATE_DIR", "data/state"), _today())
    log("ENTRY_EXECUTED", level="INFO", ticker=ticker,
        order_id=order_id, order_price=expected_price, order_qty=first_qty,
        fill_price=fill_price, fill_qty=fill_qty, fill_latency_ms=0)
    await notifier.send("ENTRY_EXECUTED", level="INFO",
                        message=f"진입: {ticker} {fill_qty}주 @ {fill_price:,}원")

    # ── [09:00:10] 2차 30% 피라미딩 ──────────────────────────────────
    await _sleep_until(9, 0, 10)
    if state.get().position_status != "HOLDING":
        return

    current_price = await _fetch_current_price(ticker)
    if current_price and current_price >= fill_price * (1 + PYRAMID_MIN_UP):
        py_resp = await _send_buy(ticker, second_qty, mode)
        py_id     = py_resp.get("output", {}).get("ODNO", "")
        py_org_no = py_resp.get("output", {}).get("KRX_FWDG_ORD_ORGNO", "")
        py_fill = await _poll_fill(py_id, deadline=(9, 0, 20))
        if not py_fill:
            if py_id and py_org_no:
                await _cancel_order(py_id, py_org_no, mode)
            log("PYRAMID_TIMEOUT", level="WARN", ticker=ticker, py_id=py_id)
        if py_fill:
            s = state.get()
            s.entry_qty = (s.entry_qty or 0) + py_fill["fill_qty"]
            s.remaining_qty = (s.remaining_qty or 0) + py_fill["fill_qty"]
            py_order_db_id = await db.record_order(
                trade_id, py_id, "BUY", py_fill["fill_qty"],
                py_fill["fill_price"], "PYRAMID_BUY", ticker,
            )
            await db.update_order_fill(
                py_order_db_id, py_fill["fill_price"], py_fill["fill_qty"], 0,
            )
            await state.persist(os.getenv("STATE_DIR", "data/state"), _today())
            log("PYRAMID_EXECUTED", level="INFO", ticker=ticker,
                fill_price=py_fill["fill_price"], fill_qty=py_fill["fill_qty"])
    else:
        diff_pct = ((current_price or 0.0) / fill_price - 1) * 100
        log("PYRAMID_SKIPPED", level="INFO", ticker=ticker,
            entry_price=fill_price, current_price=current_price,
            diff_pct=round(diff_pct, 2))
        await notifier.send("PYRAMID_SKIPPED", level="INFO",
                            message=f"2차 피라미딩 생략. {ticker}")


# ── 헬퍼 ─────────────────────────────────────────────────────────────

def _today() -> str:
    return datetime.now(KST).strftime("%Y%m%d")


async def _sleep_until(h: int, m: int, s: int) -> None:
    now = datetime.now(KST)
    target = now.replace(hour=h, minute=m, second=s, microsecond=0)
    delta = (target - now).total_seconds()
    if delta > 0:
        await asyncio.sleep(delta)


async def _fetch_expected_price(ticker: str) -> tuple[float, float]:
    """예상 체결가 및 전일 종가 반환. 장전: antc_cnpr 우선."""
    resp = await kis_rest.get(
        "/uapi/domestic-stock/v1/quotations/inquire-price",
        tr_id="FHKST01010100",
        params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker},
    )
    out = resp.get("output", {})
    expected = float(out.get("antc_cnpr") or out.get("stck_prpr") or 0)
    prev_close = float(out.get("stck_prdy_clpr") or 0)
    return expected, prev_close


async def _fetch_current_price(ticker: str) -> float:
    """현재 체결가 반환."""
    resp = await kis_rest.get(
        "/uapi/domestic-stock/v1/quotations/inquire-price",
        tr_id="FHKST01010100",
        params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker},
    )
    return float(resp.get("output", {}).get("stck_prpr") or 0)


async def _fetch_available_cash() -> float:
    """D+0 예수금 총금액 반환 (주식잔고조회 TTTC8434R)."""
    mode = os.getenv("KIS_MODE", "PAPER")
    resp = await kis_rest.get(
        "/uapi/domestic-stock/v1/trading/inquire-balance",
        tr_id=_BAL_TR[mode],
        params={
            "CANO": os.getenv("KIS_ACCT_NO", ""),
            "ACNT_PRDT_CD": os.getenv("KIS_ACCT_CD", "01"),
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
    summary = (resp.get("output2") or [{}])[0]
    return float(summary.get("dnca_tot_amt") or 0)


async def _send_buy(ticker: str, qty: int, mode: str) -> dict:
    """시장가 매수 주문 (ORD_DVSN=01)."""
    return await kis_rest.post(
        "/uapi/domestic-stock/v1/trading/order-cash",
        tr_id=_BUY_TR[mode],
        body={
            "CANO": os.getenv("KIS_ACCT_NO", ""),
            "ACNT_PRDT_CD": os.getenv("KIS_ACCT_CD", "01"),
            "PDNO": ticker,
            "ORD_DVSN": "01",
            "ORD_QTY": str(qty),
            "ORD_UNPR": "0",
        },
    )


async def _send_sell(ticker: str, qty: int, mode: str) -> dict:
    """시장가 매도 주문 (ORD_DVSN=01)."""
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


async def _cancel_order(order_id: str, org_no: str, mode: str) -> dict:
    """주문 전량 취소 (RVSE_CNCL_DVSN_CD=02)."""
    return await kis_rest.post(
        "/uapi/domestic-stock/v1/trading/order-rvsecncl",
        tr_id=_CANCEL_TR[mode],
        body={
            "CANO": os.getenv("KIS_ACCT_NO", ""),
            "ACNT_PRDT_CD": os.getenv("KIS_ACCT_CD", "01"),
            "KRX_FWDG_ORD_ORGNO": org_no,
            "ORGN_ODNO": order_id,
            "ORD_DVSN": "01",
            "RVSE_CNCL_DVSN_CD": "02",
            "ORD_QTY": "0",
            "ORD_UNPR": "0",
            "QTY_ALL_ORD_YN": "Y",
        },
    )


async def _poll_fill(order_id: str, deadline: tuple[int, int, int]) -> dict | None:
    """주문 체결을 1초 간격으로 폴링. deadline(시, 분, 초) 도달 시 None."""
    h, m, s = deadline
    mode = os.getenv("KIS_MODE", "PAPER")
    today = datetime.now(KST).strftime("%Y%m%d")
    while True:
        now = datetime.now(KST)
        if now >= now.replace(hour=h, minute=m, second=s, microsecond=0):
            return None
        try:
            resp = await kis_rest.get(
                "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
                tr_id=_CCLD_TR[mode],
                params={
                    "CANO": os.getenv("KIS_ACCT_NO", ""),
                    "ACNT_PRDT_CD": os.getenv("KIS_ACCT_CD", "01"),
                    "INQR_STRT_DT": today,
                    "INQR_END_DT": today,
                    "SLL_BUY_DVSN_CD": "00",
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
                        return {
                            "fill_price": round(tot_amt / tot_qty),
                            "fill_qty": tot_qty,
                        }
        except Exception:
            pass
        await asyncio.sleep(1)
