"""F3. 진입 주문 모듈 (09:10 이후) — PRD §F3"""

import asyncio
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src import db, notifier, state
from src.api import kis_rest
from src.utils.logger import log
from src.utils.number import to_float

KST = ZoneInfo("Asia/Seoul")

GAP_MIN_RECHECK = 0.020   # 재검증 하한 (F1 3%보다 낮음 — 완충)
GAP_MAX_ORDER = 0.065     # 주문 전 재검증 상한 — GAP_MAX_FILL과의 차이가 시장가 슬리피지 버퍼
GAP_MAX_FILL = 0.070      # 체결가 갭 상한 — 이상이면 SLIPPAGE_GUARD 즉시 청산
ALLOC_RATIO = float(os.getenv("F3_ALLOC_RATIO", "0.95"))  # 주문가능 현금 기본 95% 기준
FIRST_RATIO = 1.00         # 1차 100%
PYRAMID_MIN_UP = 0.005     # 피라미딩 조건 +0.5% 이상 유지
F3_ENTRY_MAX_ATTEMPTS = max(1, int(os.getenv("F3_ENTRY_MAX_ATTEMPTS", "2")))
F3_ENTRY_RETRY_DELAY_SEC = float(os.getenv("F3_ENTRY_RETRY_DELAY_SEC", "0.5"))
F3_ENTRY_CANCEL_RELEASE_WAIT_SEC = float(os.getenv("F3_ENTRY_CANCEL_RELEASE_WAIT_SEC", "1.5"))
# First order gets a wider polling window to absorb KIS/order fill latency after the open.
F3_ENTRY_FIRST_FILL_SEC = float(os.getenv("F3_ENTRY_FIRST_FILL_SEC", "12.0"))
F3_ENTRY_RETRY_FILL_SEC = float(os.getenv("F3_ENTRY_RETRY_FILL_SEC", "8.0"))
F3_ENTRY_RETRY_DEADLINE = os.getenv("F3_ENTRY_RETRY_DEADLINE", "09:11:00")
F3_PRE_ORDER_QUIET_SEC = float(os.getenv("F3_PRE_ORDER_QUIET_SEC", "1.5"))
F3_RECHECK_MAX_ATTEMPTS = max(1, int(os.getenv("F3_RECHECK_MAX_ATTEMPTS", "3")))
F3_RECHECK_RETRY_DELAY_SEC = float(os.getenv("F3_RECHECK_RETRY_DELAY_SEC", "1.0"))
F3_RECHECK_BATCH_TIMEOUT_SEC = float(os.getenv("F3_RECHECK_BATCH_TIMEOUT_SEC", "0"))
F3_FIRST_ORDER_AT = "IMMEDIATE"
F3_PYRAMID_AT = os.getenv("F3_PYRAMID_AT", "09:10:40")
F3_PYRAMID_FILL_SEC = float(os.getenv("F3_PYRAMID_FILL_SEC", "10.0"))

def _gap_in_order_range(gap: float) -> bool:
    """주문 전 재검증 갭 허용 구간: 하한 포함, 상한 미포함."""
    return GAP_MIN_RECHECK <= gap < GAP_MAX_ORDER


def _fill_gap_reaches_max(fill_gap: float) -> bool:
    """체결가 갭이 체결 상한 이상이면 SLIPPAGE_GUARD 청산 대상."""
    return fill_gap >= GAP_MAX_FILL


# KIS TR ID (PAPER/REAL 분기) — 신TR 기준
_BUY_TR    = {"REAL": "TTTC0012U", "PAPER": "VTTC0012U"}
_SELL_TR   = {"REAL": "TTTC0011U", "PAPER": "VTTC0011U"}
_CANCEL_TR = {"REAL": "TTTC0013U", "PAPER": "VTTC0013U"}
_CCLD_TR   = {"REAL": "TTTC0081R", "PAPER": "VTTC0081R"}
_BAL_TR    = {"REAL": "TTTC8434R", "PAPER": "VTTC8434R"}
_BUY_PSBL_TR = {"REAL": "TTTC8908R", "PAPER": "VTTC8908R"}

_last_fill_poll_summary: dict = {}
_pending_buy_org_no: str = ""  # 매수 주문 후 저장, 취소 시 사용
_CANDIDATE_RETRY_REASONS = {"ORDER_REJECTED", "BUYABLE_QTY_ZERO", "QTY_ZERO"}
# KIS "모의투자 영업일이 아닙니다" — CTCA0903R이 모의투자 미지원이라 주문 거부가 유일한 휴장 신호
_MARKET_CLOSED_MSG_CD = "40100000"


def _is_market_closed_rejection(resp: dict) -> bool:
    """휴장일 주문 거부 여부 — 다른 후보로 재시도해도 동일하므로 당일 전체 스킵 신호."""
    if str(resp.get("msg_cd") or "") == _MARKET_CLOSED_MSG_CD:
        return True
    return "영업일이 아닙" in str(resp.get("msg1") or "")


async def run(force: bool = False) -> None:
    s = state.get()
    candidates = _entry_candidate_tickers(s)
    if s.day_skip or len(candidates) <= 1 or os.getenv("DRY_RUN", "0") == "1":
        await _run_single(force=force)
        return

    original_ticker = s.target_ticker
    original_name = s.target_name
    original_candidates = list(s.target_candidates or [])
    rejected_tickers: set[str] = set()

    s = state.get()
    s.target_ticker = original_ticker
    s.target_name = original_name
    s.target_candidates = original_candidates
    ranked_candidates = await _rank_final_entry_candidates(s)
    if not ranked_candidates:
        return

    for candidate_index, picked in enumerate(ranked_candidates):
        if picked["ticker"] in rejected_tickers:
            continue
        if candidate_index > 0:
            refreshed = await _refresh_entry_candidate(picked)
            if refreshed is None:
                rejected_tickers.add(picked["ticker"])
                continue
            picked = refreshed

        s = state.get()
        s.target_ticker = picked["ticker"]
        s.target_name = picked["candidate"].get("name")
        s.target_candidates = [picked["candidate"]]
        result = await _run_single(force=force, picked=picked, allow_candidate_retry=True)
        if result not in _CANDIDATE_RETRY_REASONS:
            return
        rejected_tickers.add(picked["ticker"])
        log(
            "ENTRY_CANDIDATE_RETRY",
            level="WARN",
            ticker=picked["ticker"],
            rejected_count=len(rejected_tickers),
            remaining_candidates=[t for t in candidates if t not in rejected_tickers],
            reason=result,
        )
        # force(재시작 캐치업)는 시각 제약 없이 후보를 소진할 때까지 재시도한다
        if not force and not _before_deadline(_entry_retry_deadline()):
            s = state.get()
            s.day_skip = True
            s.close_reason = "ENTRY_FAIL"
            log(
                "ENTRY_CANDIDATE_RETRY_SKIPPED",
                level="WARN",
                ticker=picked["ticker"],
                rejected_count=len(rejected_tickers),
                reason="DEADLINE_REACHED",
            )
            await notifier.send(
                "ENTRY_FAIL",
                level="WARN",
                message=f"진입 재시도 마감시각 초과로 거래를 중단합니다. 마지막 거절={picked['ticker']}",
                ticker=picked["ticker"],
            )
            await db.record_skip(
                _today(),
                "ENTRY_FAIL",
                f"reason=CANDIDATE_RETRY_DEADLINE,rejected={','.join(sorted(rejected_tickers))}",
            )
            return

    s = state.get()
    s.day_skip = True
    s.close_reason = "ENTRY_FAIL"
    s.target_ticker = None
    s.target_name = None
    log(
        "ENTRY_CANDIDATE_RETRY_SKIPPED",
        level="WARN",
        rejected_count=len(rejected_tickers),
        reason="NO_REMAINING_CANDIDATE",
    )
    await notifier.send(
        "ENTRY_FAIL",
        level="WARN",
        message="진입 가능한 후보를 모두 소진해 거래를 중단합니다.",
        ticker=None,
    )
    await db.record_skip(
        _today(),
        "ENTRY_FAIL",
        f"reason=NO_REMAINING_CANDIDATE,rejected={','.join(sorted(rejected_tickers))}",
    )


async def _run_single(force: bool = False, picked: dict | None = None, allow_candidate_retry: bool = False) -> str | None:
    """
    갭 재검증 후 설정된 시각에 배정 수량 100% 시장가 매수,
    체결 확인 / 슬리피지 가드, 2차 30% 피라미딩을 수행한다.
    force=True: FORCE_CATCHUP 모드. 시각 제약 없이 실행, fill 마감을 실행 시점 +30초로 설정.
    """
    s = state.get()
    if s.day_skip or not s.target_ticker:
        reason = "DAY_SKIP" if s.day_skip else "NO_TARGET"
        log("F3_SKIPPED", level="WARN",
            reason=reason)
        _log_entry_blocked(s.target_ticker, reason)
        return
    candidate_tickers = _entry_candidate_tickers(s)
    ticker = candidate_tickers[0]
    mode = os.getenv("KIS_MODE", "PAPER")

    if os.getenv("DRY_RUN", "0") == "1":
        await _run_dry_entry(ticker)
        return

    existing_trade = await _existing_trade_for_today()
    if existing_trade:
        await _block_existing_trade(ticker, existing_trade)
        return

    # ── 진입 직전 갭 재검증 ─────────────────────────────────────────
    if picked and picked.get("ticker") == ticker:
        expected_price = float(picked["expected_price"])
        prev_close = float(picked.get("prev_close") or 0)
    else:
        candidate = _candidate_for_ticker(s, ticker)
        fallback_prev_close = _candidate_prev_close(candidate)
        expected_price, prev_close = await _fetch_expected_price(
            ticker,
            fallback_prev_close=fallback_prev_close,
        )
        if prev_close <= 0:
            prev_close = fallback_prev_close
    if not expected_price or prev_close <= 0:
        s.day_skip = True
        s.close_reason = "GAP_RECHECK_UNAVAILABLE"
        _log_entry_blocked(
            ticker,
            "GAP_RECHECK_UNAVAILABLE",
            expected_price=expected_price,
            prev_close=prev_close,
        )
        log(
            "GAP_RECHECK_UNAVAILABLE",
            level="WARN",
            ticker=ticker,
            expected_price=expected_price,
            prev_close=prev_close,
            reason="MISSING_PREV_CLOSE" if prev_close <= 0 else "MISSING_EXPECTED_PRICE",
        )
        await notifier.send(
            "ENTRY_FAIL",
            level="WARN",
            message="진입 직전 갭 재검증 데이터가 없어 거래를 스킵합니다.",
            ticker=ticker,
        )
        await db.record_skip(
            _today(),
            "ENTRY_FAIL",
            f"reason=GAP_RECHECK_UNAVAILABLE,expected_price={expected_price},prev_close={prev_close}",
        )
        return

    gap = (expected_price / prev_close) - 1
    log(
        "F3_RECHECK",
        level="INFO",
        ticker=ticker,
        expected_price=expected_price,
        prev_close=prev_close,
        gap_pct=round(gap * 100, 2),
        gap_min_pct=round(GAP_MIN_RECHECK * 100, 2),
        gap_max_pct=round(GAP_MAX_ORDER * 100, 2),
    )
    if not _gap_in_order_range(gap):
        s.day_skip = True
        s.close_reason = "GAP_CHANGED"
        gap_reason = "BELOW_MIN" if gap < GAP_MIN_RECHECK else "ABOVE_MAX"
        log(
            "GAP_CHANGED", level="WARN", ticker=ticker,
            gap_at_lockup=None, gap_at_entry=round(gap * 100, 2),
            reason=gap_reason,
        )
        _log_entry_blocked(
            ticker,
            "GAP_CHANGED",
            gap_at_entry=round(gap * 100, 2),
            gap_min_pct=round(GAP_MIN_RECHECK * 100, 2),
            gap_max_pct=round(GAP_MAX_ORDER * 100, 2),
            gap_reason=gap_reason,
        )
        await notifier.send("GAP_CHANGED", level="WARN",
                            message=f"진입 직전 갭 변동({gap*100:.1f}%). 거래 스킵.",
                            ticker=ticker)
        await db.record_skip(_today(), "GAP_CHANGED", f"gap={gap*100:.2f}%")
        return
    # ── 잔고 조회 및 수량 산정 ────────────────────────────────────────
    if picked and picked.get("ticker") == ticker:
        cash = float(picked["cash"])
        total_amount = int(picked["total_amount"])
        total_qty = int(picked["total_qty"])
    else:
        cash = await _fetch_available_cash()
        total_amount = int(cash * ALLOC_RATIO)
        total_qty = int(total_amount / expected_price) if expected_price else 0
    if total_qty == 0:
        _log_entry_blocked(
            ticker,
            "QTY_ZERO",
            cash=cash,
            alloc_ratio=ALLOC_RATIO,
            order_price=expected_price,
            total_amount=total_amount,
            candidate_retry=allow_candidate_retry,
        )
        log("INSUFFICIENT_BALANCE", level="WARN", ticker=ticker,
            cash=cash, alloc_ratio=ALLOC_RATIO, order_price=expected_price,
            total_amount=total_amount, filter_count=0, reason="QTY_ZERO",
            candidate_retry=allow_candidate_retry)
        if allow_candidate_retry:
            return "QTY_ZERO"
        s.day_skip = True
        s.close_reason = "INSUFFICIENT_BALANCE"
        await db.record_skip(
            _today(),
            "ENTRY_FAIL",
            (
                "reason=QTY_ZERO,"
                f"cash={cash},alloc_ratio={ALLOC_RATIO},order_price={expected_price}"
            ),
        )
        return

    first_qty = max(1, int(total_qty * FIRST_RATIO))
    second_qty = total_qty - first_qty

    # ── 1차 배정 수량 100% 시장가 매수 ───────────────────────────────
    if not await state.set_entering():
        _log_entry_blocked(
            ticker,
            "STATE_NOT_IDLE",
            position_status=state.get().position_status,
        )
        return

    global _pending_buy_org_no
    fill = None
    order_id = "UNKNOWN"
    max_attempts = F3_ENTRY_MAX_ATTEMPTS if not force else 1
    last_run_attempt = 0
    last_entry_fail_reason = "UNFILLED"
    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            if not _before_deadline(_entry_retry_deadline()):
                log(
                    "ENTRY_RETRY_SKIPPED",
                    level="WARN",
                    ticker=ticker,
                    order_price=expected_price,
                    order_qty=first_qty,
                    entry_attempt=attempt,
                    max_attempts=max_attempts,
                    reason="DEADLINE_REACHED",
                )
                break
            await asyncio.sleep(F3_ENTRY_RETRY_DELAY_SEC)
            log(
                "ENTRY_RETRY_START",
                level="WARN",
                ticker=ticker,
                order_price=expected_price,
                order_qty=first_qty,
                entry_attempt=attempt,
                max_attempts=max_attempts,
            )

        await _pre_order_quiet_wait(ticker, attempt, max_attempts, expected_price, first_qty)
        last_run_attempt = attempt
        buyable = await _fetch_buyable_qty(ticker, mode)
        if buyable.get("query_failed"):
            _log_entry_blocked(
                ticker,
                "BUYABLE_QTY_QUERY_FAILED",
                order_price=expected_price,
                planned_qty=first_qty,
                entry_attempt=attempt,
                max_attempts=max_attempts,
                rt_cd=buyable.get("rt_cd"),
                msg_cd=buyable.get("msg_cd"),
                msg1=buyable.get("msg1"),
            )
            log(
                "BUYABLE_QTY_QUERY_FAILED",
                level="WARN",
                ticker=ticker,
                order_price=expected_price,
                planned_qty=first_qty,
                entry_attempt=attempt,
                max_attempts=max_attempts,
                rt_cd=buyable.get("rt_cd"),
                msg_cd=buyable.get("msg_cd"),
                msg1=buyable.get("msg1"),
            )
            last_entry_fail_reason = "BUYABLE_QTY_QUERY_FAILED"
            if attempt < max_attempts:
                continue
            break
        last_entry_fail_reason = "UNFILLED"

        buyable_qty = int(buyable.get("nrcvb_buy_qty", 0))
        order_qty = min(first_qty, buyable_qty)
        if 0 < order_qty < first_qty:
            log(
                "ENTRY_QTY_CLAMPED",
                level="WARN",
                ticker=ticker,
                planned_qty=first_qty,
                buyable_qty=buyable_qty,
                order_qty=order_qty,
                order_price=expected_price,
                entry_attempt=attempt,
                max_attempts=max_attempts,
                nrcvb_buy_amt=buyable.get("nrcvb_buy_amt"),
                max_buy_qty=buyable.get("max_buy_qty"),
                ord_psbl_cash=buyable.get("ord_psbl_cash"),
            )
        if order_qty <= 0:
            await state.reset_to_idle("ENTRY_FAIL")
            _log_entry_blocked(
                ticker,
                "BUYABLE_QTY_ZERO",
                order_price=expected_price,
                planned_qty=first_qty,
                buyable_qty=buyable_qty,
                entry_attempt=attempt,
                max_attempts=max_attempts,
                nrcvb_buy_amt=buyable.get("nrcvb_buy_amt"),
                ord_psbl_cash=buyable.get("ord_psbl_cash"),
                candidate_retry=allow_candidate_retry,
            )
            log(
                "INSUFFICIENT_BALANCE",
                level="WARN",
                ticker=ticker,
                cash=cash,
                alloc_ratio=ALLOC_RATIO,
                order_price=expected_price,
                planned_qty=first_qty,
                buyable_qty=buyable_qty,
                nrcvb_buy_amt=buyable.get("nrcvb_buy_amt"),
                ord_psbl_cash=buyable.get("ord_psbl_cash"),
                reason="BUYABLE_QTY_ZERO",
                candidate_retry=allow_candidate_retry,
            )
            if allow_candidate_retry:
                return "BUYABLE_QTY_ZERO"
            state.get().day_skip = True
            state.get().close_reason = "INSUFFICIENT_BALANCE"
            await notifier.send(
                "ENTRY_FAIL",
                level="WARN",
                message=f"종목별 매수가능수량이 0입니다. {ticker}",
                ticker=ticker,
            )
            await db.record_skip(
                _today(),
                "ENTRY_FAIL",
                f"reason=BUYABLE_QTY_ZERO,planned_qty={first_qty},buyable_qty={buyable_qty}",
            )
            return
        if order_qty < first_qty:
            first_qty = order_qty
            second_qty = 0

        order_resp = await _send_buy(ticker, first_qty, mode)
        order_id = order_resp.get("output", {}).get("ODNO", "UNKNOWN")
        _pending_buy_org_no = order_resp.get("output", {}).get("KRX_FWDG_ORD_ORGNO", "")
        log(
            "ENTRY_ORDER_SENT",
            level="INFO",
            ticker=ticker,
            order_id=order_id,
            org_no=_pending_buy_org_no,
            order_price=expected_price,
            order_qty=first_qty,
            order_type="MARKET",
            mode=mode,
            entry_attempt=attempt,
            max_attempts=max_attempts,
            rt_cd=order_resp.get("rt_cd"),
            msg_cd=order_resp.get("msg_cd"),
            msg1=order_resp.get("msg1"),
        )
        if order_id == "UNKNOWN" or str(order_resp.get("rt_cd", "0")) != "0":
            if _is_market_closed_rejection(order_resp):
                await state.reset_to_idle("MARKET_CLOSED")
                state.get().day_skip = True
                log(
                    "MARKET_CLOSED",
                    level="INFO",
                    ticker=ticker,
                    msg_cd=order_resp.get("msg_cd"),
                    msg1=order_resp.get("msg1"),
                )
                await notifier.send(
                    "MARKET_CLOSED",
                    level="INFO",
                    message=f"휴장일 감지(주문 거부 {order_resp.get('msg_cd')}). 당일 거래 없음.",
                    ticker=None,
                )
                await db.record_skip(
                    _today(),
                    "MARKET_CLOSED",
                    f"msg_cd={order_resp.get('msg_cd')},source=ORDER_REJECTION",
                )
                return "MARKET_CLOSED"
            await state.reset_to_idle("ENTRY_FAIL")
            if not allow_candidate_retry:
                state.get().day_skip = True
            log(
                "ENTRY_FAIL",
                level="WARN",
                ticker=ticker,
                order_id=order_id,
                order_price=expected_price,
                order_qty=first_qty,
                entry_attempt=attempt,
                max_attempts=max_attempts,
                reason="ORDER_REJECTED",
                rt_cd=order_resp.get("rt_cd"),
                msg_cd=order_resp.get("msg_cd"),
                msg1=order_resp.get("msg1"),
                candidate_retry=allow_candidate_retry,
            )
            if allow_candidate_retry:
                return "ORDER_REJECTED"
            await notifier.send(
                "ENTRY_FAIL",
                level="WARN",
                message=(
                    f"진입 주문 거절. {ticker} "
                    f"{order_resp.get('msg_cd') or ''} {order_resp.get('msg1') or ''}"
                ),
                ticker=ticker,
            )
            await db.record_skip(
                _today(),
                "ENTRY_FAIL",
                f"order_id={order_id},reason=ORDER_REJECTED",
            )
            return "ORDER_REJECTED"

        fill_deadline = _entry_fill_deadline(attempt, force)
        fill = await _poll_fill(order_id, deadline=fill_deadline, ticker=ticker)
        if fill:
            break

        cancel_resp = await _cancel_order(order_id, _pending_buy_org_no, mode)
        log(
            "ENTRY_CANCEL_SENT",
            level="WARN",
            ticker=ticker,
            order_id=order_id,
            org_no=_pending_buy_org_no,
            entry_attempt=attempt,
            max_attempts=max_attempts,
            rt_cd=cancel_resp.get("rt_cd"),
            msg_cd=cancel_resp.get("msg_cd"),
            msg1=cancel_resp.get("msg1"),
        )
        if attempt < max_attempts and F3_ENTRY_CANCEL_RELEASE_WAIT_SEC > 0:
            log(
                "ENTRY_CANCEL_RELEASE_WAIT",
                level="INFO",
                ticker=ticker,
                order_id=order_id,
                sleep_sec=F3_ENTRY_CANCEL_RELEASE_WAIT_SEC,
                entry_attempt=attempt,
                max_attempts=max_attempts,
            )
            await asyncio.sleep(F3_ENTRY_CANCEL_RELEASE_WAIT_SEC)
    if not fill:
        await state.reset_to_idle("ENTRY_FAIL")
        log("ENTRY_FAIL", level="WARN", ticker=ticker,
            order_id=order_id, order_price=expected_price,
            order_qty=first_qty, entry_attempt=last_run_attempt,
            max_attempts=max_attempts, reason=last_entry_fail_reason,
            **_last_fill_poll_summary)
        await notifier.send("ENTRY_FAIL", level="WARN",
                            message=f"진입 실패({last_entry_fail_reason}). {ticker}",
                            ticker=ticker)
        await db.record_skip(
            _today(),
            "ENTRY_FAIL",
            (
                f"order_id={order_id},reason={last_entry_fail_reason},attempts={last_run_attempt},"
                f"poll_attempts={_last_fill_poll_summary.get('poll_attempts', 0)}"
            ),
        )
        return

    fill_price: float = fill["fill_price"]
    fill_qty: int = fill["fill_qty"]

    # ── 슬리피지 가드: 체결가 기준 갭이 체결 상한을 벗어나면 청산 ────
    fill_gap = (fill_price / prev_close) - 1
    if _fill_gap_reaches_max(fill_gap):
        slippage_pct = (fill_price / expected_price - 1) * 100
        log("SLIPPAGE_GUARD", level="WARN", ticker=ticker,
            expected_price=expected_price, fill_price=fill_price,
            prev_close=prev_close,
            fill_gap_pct=round(fill_gap * 100, 3),
            gap_max_pct=round(GAP_MAX_FILL * 100, 2),
            slippage_pct=round(slippage_pct, 3))
        await notifier.send(
            "SLIPPAGE_GUARD", level="WARN",
            message=(
                f"체결가 갭 {fill_gap * 100:.2f}%가 상한 "
                f"{GAP_MAX_FILL * 100:.1f}% 이상. 즉시 청산."
            ),
            ticker=ticker)
        await _send_sell(ticker, fill_qty, mode)
        s.day_skip = True
        s.close_reason = "SLIPPAGE_GUARD"
        await db.record_skip(
            _today(), "SLIPPAGE_GUARD",
            f"expected={expected_price},fill={fill_price},"
            f"prev_close={prev_close},fill_gap_pct={round(fill_gap * 100, 3)}",
        )
        return

    # ── HOLDING 전환 + DB 기록 + 영속화 ──────────────────────────────
    await state.set_holding(fill_price, fill_qty, order_id)
    trade_id = await db.open_trade(
        _today(), ticker, fill_price, fill_qty, name=state.get().target_name,
    )
    state.get().trade_id = trade_id
    # order_price에는 주문 시점 예상가를 기록한다 — 체결가와 같으면 슬리피지 집계가 0이 된다.
    order_db_id = await db.record_order(
        trade_id, order_id, "BUY", fill_qty, expected_price, "FIRST_BUY", ticker, state.get().target_name,
    )
    await db.update_order_fill(order_db_id, fill_price, fill_qty, 0)
    await state.persist(os.getenv("STATE_DIR", "data/state"), _today())
    log("ENTRY_EXECUTED", level="INFO", ticker=ticker,
        order_id=order_id, order_price=expected_price, order_qty=first_qty,
        fill_price=fill_price, fill_qty=fill_qty, fill_latency_ms=0)
    await notifier.send("ENTRY_EXECUTED", level="INFO",
                        message=f"진입: {ticker} {fill_qty}주 @ {fill_price:,}원",
                        ticker=ticker)

    # 2nd buy is inactive while FIRST_RATIO is 1.00.
    if second_qty <= 0:
        log(
            "PYRAMID_SKIPPED",
            level="INFO",
            ticker=ticker,
            reason="NO_SECOND_QTY",
            first_ratio=FIRST_RATIO,
        )
        return

    # ── 2차 30% 피라미딩 ────────────────────────────────────────────
    if not force:
        await _sleep_until(*_pyramid_at())
    if state.get().position_status != "HOLDING":
        return

    current_price = await _fetch_current_price(ticker)
    if second_qty > 0 and current_price and current_price >= fill_price * (1 + PYRAMID_MIN_UP):
        await _pre_order_quiet_wait(ticker, 1, 1, current_price, second_qty, phase="PYRAMID")
        py_resp = await _send_buy(ticker, second_qty, mode)
        py_id     = py_resp.get("output", {}).get("ODNO", "")
        py_org_no = py_resp.get("output", {}).get("KRX_FWDG_ORD_ORGNO", "")
        py_fill = await _poll_fill(py_id, deadline=_pyramid_fill_deadline(), ticker=ticker)
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
                current_price, "PYRAMID_BUY", ticker, s.target_name,
            )
            await db.update_order_fill(
                py_order_db_id, py_fill["fill_price"], py_fill["fill_qty"], 0,
            )
            await db.mark_pyramided(trade_id)
            await state.persist(os.getenv("STATE_DIR", "data/state"), _today())
            log("PYRAMID_EXECUTED", level="INFO", ticker=ticker,
                fill_price=py_fill["fill_price"], fill_qty=py_fill["fill_qty"])
            await notifier.send(
                "PYRAMID_EXECUTED",
                level="INFO",
                message=f"추가 매수: {ticker} {py_fill['fill_qty']}주 @ {py_fill['fill_price']:,}원",
                ticker=ticker,
            )
    elif second_qty > 0:
        diff_pct = ((current_price or 0.0) / fill_price - 1) * 100
        log("PYRAMID_SKIPPED", level="INFO", ticker=ticker,
            entry_price=fill_price, current_price=current_price,
            diff_pct=round(diff_pct, 2))
        await notifier.send("PYRAMID_SKIPPED", level="INFO",
                            message=f"2차 피라미딩 생략. {ticker}")


# ── 헬퍼 ─────────────────────────────────────────────────────────────

def _candidate_for_ticker(s: state.State, ticker: str) -> dict | None:
    for candidate in s.target_candidates or []:
        if isinstance(candidate, dict) and candidate.get("ticker") == ticker:
            return candidate
    return None


def _candidate_prev_close(candidate: dict | None) -> float:
    if not isinstance(candidate, dict):
        return 0.0
    prev_close = float(candidate.get("prev_close") or 0)
    if prev_close > 0:
        return prev_close
    # 갭으로 유도할 때는 반드시 스냅샷 가격을 사용한다.
    # 라이브 가격으로 유도하면 재계산 갭 ≡ 스냅샷 갭이 되어 GAP_CHANGED 가드가 무력화됨.
    snapshot_price = float(candidate.get("expected_price") or 0)
    gap_pct = candidate.get("gap_pct")
    if snapshot_price > 0 and gap_pct is not None:
        gap = float(gap_pct)
        if gap > -0.99:
            return snapshot_price / (1 + gap)
    return 0.0


async def _existing_trade_for_today() -> dict | None:
    try:
        return await db.get_trade_by_date(_today())
    except RuntimeError:
        return None


async def _block_existing_trade(ticker: str, existing_trade: dict) -> None:
    s = state.get()
    status = existing_trade.get("status")
    trade_id = int(existing_trade.get("id") or 0)
    existing_ticker = existing_trade.get("ticker")
    _log_entry_blocked(
        ticker,
        "TRADE_ALREADY_EXISTS",
        trade_id=trade_id,
        existing_ticker=existing_ticker,
        existing_status=status,
    )
    log(
        "TRADE_ALREADY_EXISTS",
        level="WARN",
        ticker=ticker,
        trade_id=trade_id,
        existing_ticker=existing_ticker,
        existing_status=status,
    )
    if status == "OPEN":
        entry_price = float(existing_trade.get("entry_price") or 0)
        entry_qty = int(existing_trade.get("entry_qty") or 0)
        s.target_ticker = existing_ticker or s.target_ticker
        if existing_ticker and existing_ticker != ticker:
            # 다른 종목의 기존 거래를 복구하면 이전 후보의 종목명이 남지 않게 한다.
            s.target_name = existing_trade.get("name")
        else:
            s.target_name = existing_trade.get("name") or s.target_name
        s.trade_id = trade_id
        if entry_price > 0 and entry_qty > 0 and s.position_status != "HOLDING":
            await state.set_holding(entry_price, entry_qty, s.order_id or "")
            state.get().trade_id = trade_id
        return
    s.day_skip = True
    s.close_reason = "TRADE_ALREADY_EXISTS"


def _entry_candidate_tickers(s: state.State) -> list[str]:
    tickers: list[str] = []
    for candidate in s.target_candidates or []:
        ticker = candidate.get("ticker") if isinstance(candidate, dict) else str(candidate)
        if ticker and ticker not in tickers:
            tickers.append(ticker)
    if s.target_ticker and s.target_ticker not in tickers:
        tickers.insert(0, s.target_ticker)
    return tickers


async def _rank_final_entry_candidates(
    s: state.State,
    exclude_tickers: set[str] | None = None,
) -> list[dict] | None:
    candidates = s.target_candidates or []
    candidate_by_ticker = {
        c.get("ticker"): c
        for c in candidates
        if isinstance(c, dict) and c.get("ticker")
    }
    exclude_tickers = exclude_tickers or set()
    tickers = [ticker for ticker in _entry_candidate_tickers(s) if ticker not in exclude_tickers]
    valid: list[dict] = []
    blocked_reasons: list[str] = []

    async def recheck_one(rank: int, ticker: str) -> dict:
        candidate = candidate_by_ticker.get(ticker)
        fallback_prev_close = _candidate_prev_close(candidate)
        try:
            expected_price, prev_close = await _fetch_expected_price(
                ticker,
                fallback_prev_close=fallback_prev_close,
            )
        except Exception as exc:
            log(
                "F3_RECHECK_QUOTE_ERROR",
                level="WARN",
                ticker=ticker,
                candidate_rank=rank,
                error=repr(exc),
            )
            expected_price, prev_close = 0.0, fallback_prev_close
        if prev_close <= 0:
            prev_close = fallback_prev_close
        if candidate is None:
            log(
                "F3_CANDIDATE_SNAPSHOT_MISSING",
                level="WARN",
                ticker=ticker,
                candidate_rank=rank,
                candidates=tickers,
            )
            candidate = {"ticker": ticker}
        return {
            "rank": rank,
            "ticker": ticker,
            "candidate": candidate,
            "expected_price": expected_price,
            "prev_close": prev_close,
        }

    async def fetch_available_cash_safe() -> float:
        try:
            return await _fetch_available_cash()
        except Exception as exc:
            log(
                "BALANCE_QUERY_ERROR",
                level="WARN",
                reason="EXCEPTION",
                error=repr(exc),
            )
            return 0.0

    batch_started = time.perf_counter()
    cash_task = asyncio.create_task(fetch_available_cash_safe())
    recheck_tasks = [
        asyncio.create_task(recheck_one(rank, ticker))
        for rank, ticker in enumerate(tickers, start=1)
    ]
    task_tickers = dict(zip(recheck_tasks, tickers, strict=False))
    if F3_RECHECK_BATCH_TIMEOUT_SEC > 0:
        done, pending = await asyncio.wait(
            recheck_tasks,
            timeout=F3_RECHECK_BATCH_TIMEOUT_SEC,
        )
        if pending:
            log(
                "F3_RECHECK_BATCH_TIMEOUT",
                level="WARN",
                requested_count=len(recheck_tasks),
                completed_count=len(done),
                pending_count=len(pending),
                pending_tickers=[task_tickers[task] for task in pending],
                timeout_sec=F3_RECHECK_BATCH_TIMEOUT_SEC,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        recheck_rows = [task.result() for task in recheck_tasks if task in done]
    else:
        recheck_rows = await asyncio.gather(*recheck_tasks)
    cash = await cash_task
    batch_elapsed_ms = round((time.perf_counter() - batch_started) * 1000, 1)
    log(
        "F3_RECHECK_BATCH_TIMING",
        level="DEBUG",
        requested_count=len(recheck_tasks),
        completed_count=len(recheck_rows),
        elapsed_ms=batch_elapsed_ms,
        timeout_sec=F3_RECHECK_BATCH_TIMEOUT_SEC,
    )
    total_amount = int(cash * ALLOC_RATIO)

    for row in recheck_rows:
        rank = int(row["rank"])
        ticker = str(row["ticker"])
        candidate = row["candidate"]
        expected_price = float(row["expected_price"] or 0)
        prev_close = float(row["prev_close"] or 0)
        if not expected_price or prev_close <= 0:
            blocked_reasons.append("GAP_RECHECK_UNAVAILABLE")
            _log_entry_blocked(
                ticker,
                "GAP_RECHECK_UNAVAILABLE",
                candidate_rank=rank,
                expected_price=expected_price,
                prev_close=prev_close,
                cash=cash,
            )
            log(
                "GAP_RECHECK_UNAVAILABLE",
                level="WARN",
                ticker=ticker,
                candidate_rank=rank,
                expected_price=expected_price,
                prev_close=prev_close,
                reason="MISSING_PREV_CLOSE" if prev_close <= 0 else "MISSING_EXPECTED_PRICE",
            )
            continue

        gap = (expected_price / prev_close) - 1
        log(
            "F3_RECHECK",
            level="INFO",
            ticker=ticker,
            candidate_rank=rank,
            expected_price=expected_price,
            prev_close=prev_close,
            gap_pct=round(gap * 100, 2),
            gap_min_pct=round(GAP_MIN_RECHECK * 100, 2),
            gap_max_pct=round(GAP_MAX_ORDER * 100, 2),
        )
        if not _gap_in_order_range(gap):
            reason = "BELOW_MIN" if gap < GAP_MIN_RECHECK else "ABOVE_MAX"
            blocked_reasons.append("GAP_CHANGED")
            log(
                "GAP_CHANGED",
                level="WARN",
                ticker=ticker,
                candidate_rank=rank,
                gap_at_lockup=None,
                gap_at_entry=round(gap * 100, 2),
                reason=reason,
            )
            _log_entry_blocked(
                ticker,
                "GAP_CHANGED",
                candidate_rank=rank,
                gap_at_entry=round(gap * 100, 2),
                gap_min_pct=round(GAP_MIN_RECHECK * 100, 2),
                gap_max_pct=round(GAP_MAX_ORDER * 100, 2),
                gap_reason=reason,
            )
            continue
        total_qty = int(total_amount / expected_price)
        if total_qty == 0:
            blocked_reasons.append("INSUFFICIENT_BALANCE")
            _log_entry_blocked(
                ticker,
                "QTY_ZERO",
                candidate_rank=rank,
                cash=cash,
                alloc_ratio=ALLOC_RATIO,
                order_price=expected_price,
                total_amount=total_amount,
            )
            continue

        valid.append(
            {
                "ticker": ticker,
                "candidate": candidate,
                "candidate_rank": rank,
                "expected_price": expected_price,
                "prev_close": prev_close,
                "cash": cash,
                "total_amount": total_amount,
                "total_qty": total_qty,
            }
        )

    if not valid:
        reason = blocked_reasons[-1] if blocked_reasons else "NO_ENTRY_CANDIDATE"
        s.day_skip = True
        s.close_reason = reason
        s.target_ticker = None
        s.target_name = None
        alert_event = "GAP_CHANGED" if reason == "GAP_CHANGED" else "ENTRY_FAIL"
        alert_ticker = tickers[0] if tickers else None
        await notifier.send(
            alert_event,
            level="WARN",
            message=f"F3 후보 전체가 주문 전 재검증에서 제외되었습니다. 사유={reason}",
            ticker=alert_ticker,
        )
        await db.record_skip(
            _today(),
            "ENTRY_FAIL" if reason != "GAP_CHANGED" else "GAP_CHANGED",
            f"reason={reason},candidates={','.join(tickers)}",
        )
        return None

    ranked = sorted(
        valid,
        key=lambda item: (
            item["candidate"].get("expected_amount", 0.0),
            item["candidate"].get("buy_sell_ratio", 0.0),
            -item["candidate_rank"],
        ),
        reverse=True,
    )
    picked = ranked[0]
    log(
        "F3_FINAL_PICK",
        level="INFO",
        ticker=picked["ticker"],
        name=picked["candidate"].get("name"),
        candidate_rank=picked["candidate_rank"],
        checked_count=len(tickers),
        valid_count=len(valid),
        candidates=tickers,
        expected_price=picked["expected_price"],
        total_qty=picked["total_qty"],
    )
    return ranked


async def _pick_final_entry_candidate(
    s: state.State,
    exclude_tickers: set[str] | None = None,
) -> dict | None:
    ranked = await _rank_final_entry_candidates(s, exclude_tickers=exclude_tickers)
    return ranked[0] if ranked else None


async def _refresh_entry_candidate(picked: dict) -> dict | None:
    ticker = str(picked["ticker"])
    candidate = picked.get("candidate")
    if not isinstance(candidate, dict):
        candidate = None
    fallback_prev_close = _candidate_prev_close(candidate)
    expected_price, prev_close = await _fetch_expected_price(
        ticker,
        fallback_prev_close=fallback_prev_close,
    )
    if prev_close <= 0:
        prev_close = fallback_prev_close
    if not expected_price or prev_close <= 0:
        _log_entry_blocked(
            ticker,
            "GAP_RECHECK_UNAVAILABLE",
            candidate_rank=picked.get("candidate_rank"),
            expected_price=expected_price,
            prev_close=prev_close,
            freshness_check=True,
        )
        log(
            "GAP_RECHECK_UNAVAILABLE",
            level="WARN",
            ticker=ticker,
            candidate_rank=picked.get("candidate_rank"),
            expected_price=expected_price,
            prev_close=prev_close,
            freshness_check=True,
            reason="MISSING_PREV_CLOSE" if prev_close <= 0 else "MISSING_EXPECTED_PRICE",
        )
        return None

    gap = (expected_price / prev_close) - 1
    log(
        "F3_RECHECK",
        level="INFO",
        ticker=ticker,
        candidate_rank=picked.get("candidate_rank"),
        expected_price=expected_price,
        prev_close=prev_close,
        gap_pct=round(gap * 100, 2),
        gap_min_pct=round(GAP_MIN_RECHECK * 100, 2),
        gap_max_pct=round(GAP_MAX_ORDER * 100, 2),
        freshness_check=True,
    )
    if not _gap_in_order_range(gap):
        reason = "BELOW_MIN" if gap < GAP_MIN_RECHECK else "ABOVE_MAX"
        log(
            "GAP_CHANGED",
            level="WARN",
            ticker=ticker,
            candidate_rank=picked.get("candidate_rank"),
            gap_at_lockup=None,
            gap_at_entry=round(gap * 100, 2),
            reason=reason,
            freshness_check=True,
        )
        _log_entry_blocked(
            ticker,
            "GAP_CHANGED",
            candidate_rank=picked.get("candidate_rank"),
            gap_at_entry=round(gap * 100, 2),
            gap_min_pct=round(GAP_MIN_RECHECK * 100, 2),
            gap_max_pct=round(GAP_MAX_ORDER * 100, 2),
            gap_reason=reason,
            freshness_check=True,
        )
        return None

    total_amount = int(picked["total_amount"])
    total_qty = int(total_amount / expected_price)
    if total_qty == 0:
        _log_entry_blocked(
            ticker,
            "QTY_ZERO",
            candidate_rank=picked.get("candidate_rank"),
            cash=picked.get("cash"),
            alloc_ratio=ALLOC_RATIO,
            order_price=expected_price,
            total_amount=total_amount,
            freshness_check=True,
        )
        return None

    refreshed = dict(picked)
    refreshed.update({
        "expected_price": expected_price,
        "prev_close": prev_close,
        "total_qty": total_qty,
    })
    return refreshed

def _today() -> str:
    return datetime.now(KST).strftime("%Y%m%d")


def _log_entry_blocked(ticker: str | None, reason: str, **extra: object) -> None:
    log(
        "F3_ENTRY_BLOCKED",
        level="WARN",
        ticker=ticker,
        reason=reason,
        **extra,
    )


async def _sleep_until(h: int, m: int, s: int) -> None:
    if os.getenv("PYTEST_CURRENT_TEST"):
        return
    now = datetime.now(KST)
    target = now.replace(hour=h, minute=m, second=s, microsecond=0)
    delta = (target - now).total_seconds()
    if delta > 0:
        await asyncio.sleep(delta)


async def _pre_order_quiet_wait(
    ticker: str,
    attempt: int,
    max_attempts: int,
    order_price: float | None,
    order_qty: int,
    *,
    phase: str = "ENTRY",
) -> None:
    if F3_PRE_ORDER_QUIET_SEC <= 0:
        return
    log(
        "ENTRY_PRE_ORDER_WAIT",
        level="INFO",
        ticker=ticker,
        phase=phase,
        sleep_sec=F3_PRE_ORDER_QUIET_SEC,
        order_price=order_price,
        order_qty=order_qty,
        entry_attempt=attempt,
        max_attempts=max_attempts,
    )
    await asyncio.sleep(F3_PRE_ORDER_QUIET_SEC)


def _parse_deadline(value: str, default: tuple[int, int, int]) -> tuple[int, int, int]:
    try:
        h, m, s = [int(part) for part in value.split(":")]
        return h, m, s
    except (ValueError, AttributeError) as exc:
        log(
            "F3_DEADLINE_PARSE_ERROR",
            level="WARN",
            value=str(value),
            default=f"{default[0]:02d}:{default[1]:02d}:{default[2]:02d}",
            error=repr(exc),
        )
        return default


def _deadline_datetime(deadline: tuple[int, int, int]) -> datetime:
    h, m, s = deadline
    return datetime.now(KST).replace(hour=h, minute=m, second=s, microsecond=0)


def _entry_retry_deadline() -> tuple[int, int, int]:
    return _parse_deadline(F3_ENTRY_RETRY_DEADLINE, (9, 11, 0))



def _pyramid_at() -> tuple[int, int, int]:
    return _parse_deadline(F3_PYRAMID_AT, (9, 10, 40))


def _pyramid_fill_deadline() -> tuple[int, int, int]:
    return _deadline_after_seconds(F3_PYRAMID_FILL_SEC)


def _before_deadline(deadline: tuple[int, int, int]) -> bool:
    return datetime.now(KST) < _deadline_datetime(deadline)


def _deadline_after_seconds(seconds: float) -> tuple[int, int, int]:
    target = datetime.now(KST) + timedelta(seconds=seconds)
    return target.hour, target.minute, target.second


def _entry_fill_deadline(attempt: int, force: bool) -> tuple[int, int, int]:
    if force:
        return _deadline_after_seconds(30)
    if attempt == 1:
        return _deadline_after_seconds(F3_ENTRY_FIRST_FILL_SEC)

    retry_deadline = _deadline_datetime(_entry_retry_deadline())
    target = min(datetime.now(KST) + timedelta(seconds=F3_ENTRY_RETRY_FILL_SEC), retry_deadline)
    return target.hour, target.minute, target.second


async def _run_dry_entry(ticker: str) -> None:
    expected_price = float(os.getenv("DRY_RUN_EXPECTED_PRICE", "10300"))
    fill_price = float(os.getenv("DRY_RUN_ENTRY_PRICE", str(expected_price)))
    fill_qty = int(os.getenv("DRY_RUN_ENTRY_QTY", "10"))
    order_id = f"DRY-{datetime.now(KST).strftime('%H%M%S')}"

    if not await state.set_entering():
        log("DRY_RUN_F3_SKIPPED", level="WARN", ticker=ticker, reason="STATE_NOT_IDLE")
        await db.record_skip(_today(), "DRY_RUN_F3_SKIPPED", "reason=STATE_NOT_IDLE")
        return

    await asyncio.sleep(float(os.getenv("DRY_RUN_STEP_DELAY", "0.2")))
    await state.set_holding(fill_price, fill_qty, order_id)
    await state.persist(os.getenv("STATE_DIR", "data/state"), _today())

    log(
        "DRY_RUN_ENTRY_EXECUTED",
        level="WARN",
        ticker=ticker,
        order_id=order_id,
        order_price=expected_price,
        order_qty=fill_qty,
        fill_price=fill_price,
        fill_qty=fill_qty,
        fill_latency_ms=0,
    )


async def _fetch_expected_price(
    ticker: str,
    fallback_prev_close: float = 0.0,
) -> tuple[float, float]:
    """Return expected price and previous close. Before open, prefer antc_cnpr."""
    last_expected = 0.0
    last_prev_close = 0.0
    for attempt in range(1, F3_RECHECK_MAX_ATTEMPTS + 1):
        resp = await kis_rest.get(
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            tr_id="FHKST01010100",
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker},
        )
        out = resp.get("output", {}) if isinstance(resp.get("output"), dict) else {}
        expected = float(out.get("antc_cnpr") or out.get("stck_prpr") or 0)
        prev_close = float(out.get("stck_prdy_clpr") or 0)
        effective_prev_close = prev_close if prev_close > 0 else fallback_prev_close
        last_expected, last_prev_close = expected, effective_prev_close
        if expected and effective_prev_close > 0:
            if attempt > 1:
                log(
                    "F3_RECHECK_QUOTE_RECOVERED",
                    level="INFO",
                    ticker=ticker,
                    attempt=attempt,
                    max_attempts=F3_RECHECK_MAX_ATTEMPTS,
                    expected_price=expected,
                    prev_close=effective_prev_close,
                )
            return expected, effective_prev_close
        if attempt < F3_RECHECK_MAX_ATTEMPTS:
            log(
                "F3_RECHECK_QUOTE_RETRY",
                level="WARN",
                ticker=ticker,
                attempt=attempt,
                max_attempts=F3_RECHECK_MAX_ATTEMPTS,
                retry_after_sec=F3_RECHECK_RETRY_DELAY_SEC,
                expected_price=expected,
                prev_close=effective_prev_close,
                rt_cd=resp.get("rt_cd"),
                msg_cd=resp.get("msg_cd"),
                msg1=resp.get("msg1"),
            )
            await asyncio.sleep(F3_RECHECK_RETRY_DELAY_SEC)
    return last_expected, last_prev_close

async def _fetch_current_price(ticker: str) -> float:
    """현재 체결가 반환."""
    resp = await kis_rest.get(
        "/uapi/domestic-stock/v1/quotations/inquire-price",
        tr_id="FHKST01010100",
        params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker},
    )
    return float(resp.get("output", {}).get("stck_prpr") or 0)


async def _fetch_available_cash() -> float:
    """주문가능 현금 반환 (주식잔고조회 TTTC8434R).

    ord_psbl_cash 우선, 부재 시 dnca_tot_amt와 prvs_rcdl_excc_amt(D+2 정산금) 중 큰 값.
    """
    mode = os.getenv("KIS_MODE", "PAPER")
    resp = await kis_rest.get(
        "/uapi/domestic-stock/v1/trading/inquire-balance",
        tr_id=_BAL_TR[mode],
        params=kis_rest.balance_inquiry_params(),
    )
    if str(resp.get("rt_cd", "0")) != "0":
        log(
            "BALANCE_QUERY_ERROR",
            level="WARN",
            rt_cd=resp.get("rt_cd"),
            msg_cd=resp.get("msg_cd"),
            msg1=resp.get("msg1"),
        )
        return 0.0

    output2 = resp.get("output2")
    if not isinstance(output2, list) or not output2 or not isinstance(output2[0], dict):
        log(
            "BALANCE_QUERY_ERROR",
            level="WARN",
            reason="MISSING_OUTPUT2",
            rt_cd=resp.get("rt_cd"),
            msg_cd=resp.get("msg_cd"),
            msg1=resp.get("msg1"),
        )
        return 0.0

    summary = output2[0]
    ord_psbl_present = "ord_psbl_cash" in summary and str(summary.get("ord_psbl_cash", "")).strip() != ""
    ord_psbl_cash = to_float(summary.get("ord_psbl_cash"))
    dnca_tot_amt = to_float(summary.get("dnca_tot_amt"))
    prvs_rcdl_excc_amt = to_float(summary.get("prvs_rcdl_excc_amt"))
    cash_source = "ord_psbl_cash"
    cash = ord_psbl_cash
    if not ord_psbl_present:
        # 매도대금 T+2 미결제 상태에서는 dnca_tot_amt가 실제 주문가능금액을 과소평가하므로
        # D+2 정산금(prvs_rcdl_excc_amt)과 비교해 큰 값을 사용한다.
        # 과대평가되더라도 주문 직전 종목별 매수가능조회(nrcvb_buy_qty)가 상한을 재적용한다.
        if prvs_rcdl_excc_amt > dnca_tot_amt:
            cash = prvs_rcdl_excc_amt
            cash_source = "prvs_rcdl_excc_amt"
        else:
            cash = dnca_tot_amt
            cash_source = "dnca_tot_amt"

    log(
        "BALANCE_CASH_CHECK",
        level="DEBUG",
        cash=cash,
        cash_source=cash_source,
        ord_psbl_cash=ord_psbl_cash,
        ord_psbl_present=ord_psbl_present,
        dnca_tot_amt=dnca_tot_amt,
        prvs_rcdl_excc_amt=prvs_rcdl_excc_amt,
    )
    return cash


async def _fetch_buyable_qty(ticker: str, mode: str) -> dict:
    """종목별 시장가 매수가능수량 조회 [v1_국내주식-007]."""
    resp = await kis_rest.get(
        "/uapi/domestic-stock/v1/trading/inquire-psbl-order",
        tr_id=_BUY_PSBL_TR[mode],
        params={
            "CANO": kis_rest.account_no(),
            "ACNT_PRDT_CD": kis_rest.account_cd(),
            "PDNO": ticker,
            "ORD_UNPR": "",
            "ORD_DVSN": "01",
            "CMA_EVLU_AMT_ICLD_YN": "N",
            "OVRS_ICLD_YN": "N",
        },
    )
    if str(resp.get("rt_cd", "0")) != "0":
        log(
            "BUYABLE_QTY_ERROR",
            level="WARN",
            ticker=ticker,
            rt_cd=resp.get("rt_cd"),
            msg_cd=resp.get("msg_cd"),
            msg1=resp.get("msg1"),
        )
        return {
            "query_failed": True,
            "nrcvb_buy_qty": 0,
            "nrcvb_buy_amt": 0.0,
            "max_buy_qty": 0,
            "max_buy_amt": 0.0,
            "ord_psbl_cash": 0.0,
            "rt_cd": resp.get("rt_cd"),
            "msg_cd": resp.get("msg_cd"),
            "msg1": resp.get("msg1"),
        }

    out = resp.get("output", {}) if isinstance(resp.get("output"), dict) else {}
    result = {
        "query_failed": False,
        "nrcvb_buy_qty": int(to_float(out.get("nrcvb_buy_qty"))),
        "nrcvb_buy_amt": to_float(out.get("nrcvb_buy_amt")),
        "max_buy_qty": int(to_float(out.get("max_buy_qty"))),
        "max_buy_amt": to_float(out.get("max_buy_amt")),
        "ord_psbl_cash": to_float(out.get("ord_psbl_cash")),
    }
    log("BUYABLE_QTY_CHECK", level="DEBUG", ticker=ticker, **result)
    return result


async def _send_buy(ticker: str, qty: int, mode: str) -> dict:
    """시장가 매수 주문 (ORD_DVSN=01)."""
    return await kis_rest.post(
        "/uapi/domestic-stock/v1/trading/order-cash",
        tr_id=_BUY_TR[mode],
        body={
            "CANO": kis_rest.account_no(),
            "ACNT_PRDT_CD": kis_rest.account_cd(),
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
            "CANO": kis_rest.account_no(),
            "ACNT_PRDT_CD": kis_rest.account_cd(),
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
            "CANO": kis_rest.account_no(),
            "ACNT_PRDT_CD": kis_rest.account_cd(),
            "KRX_FWDG_ORD_ORGNO": org_no,
            "ORGN_ODNO": order_id,
            "ORD_DVSN": "01",
            "RVSE_CNCL_DVSN_CD": "02",
            "ORD_QTY": "0",
            "ORD_UNPR": "0",
            "QTY_ALL_ORD_YN": "Y",
        },
    )


async def _poll_fill(
    order_id: str,
    deadline: tuple[int, int, int],
    ticker: str | None = None,
) -> dict | None:
    """주문 체결을 1초 간격으로 폴링. deadline(시, 분, 초) 도달 시 None."""
    global _last_fill_poll_summary
    h, m, s = deadline
    mode = os.getenv("KIS_MODE", "PAPER")
    today = datetime.now(KST).strftime("%Y%m%d")
    attempts = 0
    _last_fill_poll_summary = {
        "poll_attempts": 0,
        "poll_deadline": f"{h:02d}:{m:02d}:{s:02d}",
        "poll_last_rt_cd": None,
        "poll_last_msg_cd": None,
        "poll_last_msg1": None,
        "poll_last_output_count": 0,
        "poll_last_matched": False,
        "poll_last_ccld_qty": 0,
        "poll_last_ccld_amt": 0.0,
        "poll_last_error": None,
    }
    while True:
        now = datetime.now(KST)
        if now >= now.replace(hour=h, minute=m, second=s, microsecond=0):
            log("ENTRY_FILL_POLL_TIMEOUT", level="WARN", ticker=ticker,
                order_id=order_id, **_last_fill_poll_summary)
            return None
        try:
            attempts += 1
            resp = await kis_rest.get(
                "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
                tr_id=_CCLD_TR[mode],
                params={
                    "CANO": kis_rest.account_no(),
                    "ACNT_PRDT_CD": kis_rest.account_cd(),
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
            rows = resp.get("output1", []) or []
            _last_fill_poll_summary.update({
                "poll_attempts": attempts,
                "poll_last_rt_cd": resp.get("rt_cd"),
                "poll_last_msg_cd": resp.get("msg_cd"),
                "poll_last_msg1": resp.get("msg1"),
                "poll_last_output_count": len(rows),
                "poll_last_matched": False,
                "poll_last_error": None,
            })
            for item in rows:
                if item.get("odno") == order_id:
                    tot_qty = int(item.get("tot_ccld_qty") or 0)
                    tot_amt = float(item.get("tot_ccld_amt") or 0)
                    _last_fill_poll_summary.update({
                        "poll_last_matched": True,
                        "poll_last_ccld_qty": tot_qty,
                        "poll_last_ccld_amt": tot_amt,
                    })
                    if tot_qty > 0:
                        return {
                            "fill_price": round(tot_amt / tot_qty),
                            "fill_qty": tot_qty,
                        }
        except Exception as exc:
            _last_fill_poll_summary.update({
                "poll_attempts": attempts,
                "poll_last_error": str(exc)[:160],
            })
        await asyncio.sleep(1)


