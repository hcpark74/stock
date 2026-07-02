"""F3. 진입 주문 모듈 (09:10 이후) — PRD §F3"""

import asyncio
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src import db, notifier, state
from src.api import kis_rest
from src.utils.logger import log
from src.utils.number import to_float

KST = ZoneInfo("Asia/Seoul")

GAP_MIN_RECHECK = 0.020   # 재검증 하한 (F1 3%보다 낮음 — 완충)
GAP_MAX_RECHECK = 0.070
ALLOC_RATIO = 0.10         # 자본 대비 10% 투입
FIRST_RATIO = 0.70         # 1차 70%
SLIPPAGE_LIMIT = 0.005     # 슬리피지 허용 +0.5%
PYRAMID_MIN_UP = 0.005     # 피라미딩 조건 +0.5% 이상 유지
F3_ENTRY_MAX_ATTEMPTS = max(1, int(os.getenv("F3_ENTRY_MAX_ATTEMPTS", "2")))
F3_ENTRY_RETRY_DELAY_SEC = float(os.getenv("F3_ENTRY_RETRY_DELAY_SEC", "0.5"))
# First order gets a wider polling window to absorb KIS/order fill latency after the open.
F3_ENTRY_FIRST_FILL_SEC = float(os.getenv("F3_ENTRY_FIRST_FILL_SEC", "12.0"))
F3_ENTRY_RETRY_FILL_SEC = float(os.getenv("F3_ENTRY_RETRY_FILL_SEC", "8.0"))
F3_ENTRY_RETRY_DEADLINE = os.getenv("F3_ENTRY_RETRY_DEADLINE", "09:11:00")
F3_PRE_ORDER_QUIET_SEC = float(os.getenv("F3_PRE_ORDER_QUIET_SEC", "1.5"))
F3_FIRST_ORDER_AT = os.getenv("F3_FIRST_ORDER_AT", "09:10:20")
F3_PYRAMID_AT = os.getenv("F3_PYRAMID_AT", "09:10:40")
F3_PYRAMID_FILL_SEC = float(os.getenv("F3_PYRAMID_FILL_SEC", "10.0"))

# KIS TR ID (PAPER/REAL 분기) — 신TR 기준
_BUY_TR    = {"REAL": "TTTC0012U", "PAPER": "VTTC0012U"}
_SELL_TR   = {"REAL": "TTTC0011U", "PAPER": "VTTC0011U"}
_CANCEL_TR = {"REAL": "TTTC0013U", "PAPER": "VTTC0013U"}
_CCLD_TR   = {"REAL": "TTTC0081R", "PAPER": "VTTC0081R"}
_BAL_TR    = {"REAL": "TTTC8434R", "PAPER": "VTTC8434R"}

_last_fill_poll_summary: dict = {}
_pending_buy_org_no: str = ""  # 매수 주문 후 저장, 취소 시 사용


async def run(force: bool = False) -> None:
    s = state.get()
    candidates = _entry_candidate_tickers(s)
    if s.day_skip or len(candidates) <= 1 or os.getenv("DRY_RUN", "0") == "1":
        await _run_single(force=force)
        return

    picked = await _pick_final_entry_candidate(state.get())
    if picked is None:
        return

    s = state.get()
    s.target_ticker = picked["ticker"]
    s.target_candidates = [picked["candidate"]]
    await _run_single(force=force, picked=picked)


async def _run_single(force: bool = False, picked: dict | None = None) -> None:
    """
    갭 재검증 후 설정된 시각에 1차 70% 시장가 매수,
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

    # ── 진입 직전 갭 재검증 ─────────────────────────────────────────
    if picked and picked.get("ticker") == ticker:
        expected_price = float(picked["expected_price"])
        prev_close = float(picked.get("prev_close") or 0)
    else:
        expected_price, prev_close = await _fetch_expected_price(ticker)
    if prev_close and expected_price:
        gap = (expected_price / prev_close) - 1
        log(
            "F3_RECHECK",
            level="INFO",
            ticker=ticker,
            expected_price=expected_price,
            prev_close=prev_close,
            gap_pct=round(gap * 100, 2),
            gap_min_pct=round(GAP_MIN_RECHECK * 100, 2),
            gap_max_pct=round(GAP_MAX_RECHECK * 100, 2),
        )
        if not (GAP_MIN_RECHECK <= gap < GAP_MAX_RECHECK):
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
                gap_max_pct=round(GAP_MAX_RECHECK * 100, 2),
                gap_reason=gap_reason,
            )
            await notifier.send("GAP_CHANGED", level="WARN",
                                message=f"진입 직전 갭 변동({gap*100:.1f}%). 거래 스킵.")
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
    if not expected_price or expected_price == 0:
        s.day_skip = True
        s.close_reason = "PRICE_UNAVAILABLE"
        _log_entry_blocked(
            ticker,
            "PRICE_UNAVAILABLE",
            order_price=expected_price,
            cash=cash,
        )
        log(
            "ENTRY_FAIL",
            level="WARN",
            ticker=ticker,
            order_id=None,
            order_price=expected_price,
            order_qty=0,
            cash=cash,
            reason="PRICE_UNAVAILABLE",
        )
        await db.record_skip(
            _today(),
            "ENTRY_FAIL",
            f"reason=PRICE_UNAVAILABLE,cash={cash}",
        )
        return
    if total_qty == 0:
        s.day_skip = True
        s.close_reason = "INSUFFICIENT_BALANCE"
        _log_entry_blocked(
            ticker,
            "QTY_ZERO",
            cash=cash,
            alloc_ratio=ALLOC_RATIO,
            order_price=expected_price,
            total_amount=total_amount,
        )
        log("INSUFFICIENT_BALANCE", level="WARN", ticker=ticker,
            cash=cash, alloc_ratio=ALLOC_RATIO, order_price=expected_price,
            total_amount=total_amount, filter_count=0, reason="QTY_ZERO")
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

    # ── 1차 70% 시장가 매수 ────────────────────────────────────────
    if not force:
        await _sleep_until(*_first_order_at())
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
            await state.reset_to_idle("ENTRY_FAIL")
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
            )
            await db.record_skip(
                _today(),
                "ENTRY_FAIL",
                f"order_id={order_id},reason=ORDER_REJECTED",
            )
            return

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

    if not fill:
        await state.reset_to_idle("ENTRY_FAIL")
        log("ENTRY_FAIL", level="WARN", ticker=ticker,
            order_id=order_id, order_price=expected_price,
            order_qty=first_qty, entry_attempt=last_run_attempt,
            max_attempts=max_attempts, reason="UNFILLED",
            **_last_fill_poll_summary)
        await notifier.send("ENTRY_FAIL", level="WARN",
                            message=f"진입 미체결. {ticker}")
        await db.record_skip(
            _today(),
            "ENTRY_FAIL",
            (
                f"order_id={order_id},reason=UNFILLED,attempts={last_run_attempt},"
                f"poll_attempts={_last_fill_poll_summary.get('poll_attempts', 0)}"
            ),
        )
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
                py_fill["fill_price"], "PYRAMID_BUY", ticker,
            )
            await db.update_order_fill(
                py_order_db_id, py_fill["fill_price"], py_fill["fill_qty"], 0,
            )
            await state.persist(os.getenv("STATE_DIR", "data/state"), _today())
            log("PYRAMID_EXECUTED", level="INFO", ticker=ticker,
                fill_price=py_fill["fill_price"], fill_qty=py_fill["fill_qty"])
    elif second_qty > 0:
        diff_pct = ((current_price or 0.0) / fill_price - 1) * 100
        log("PYRAMID_SKIPPED", level="INFO", ticker=ticker,
            entry_price=fill_price, current_price=current_price,
            diff_pct=round(diff_pct, 2))
        await notifier.send("PYRAMID_SKIPPED", level="INFO",
                            message=f"2차 피라미딩 생략. {ticker}")


# ── 헬퍼 ─────────────────────────────────────────────────────────────

def _entry_candidate_tickers(s: state.State) -> list[str]:
    tickers: list[str] = []
    for candidate in s.target_candidates or []:
        ticker = candidate.get("ticker") if isinstance(candidate, dict) else str(candidate)
        if ticker and ticker not in tickers:
            tickers.append(ticker)
    if s.target_ticker and s.target_ticker not in tickers:
        tickers.insert(0, s.target_ticker)
    return tickers


async def _pick_final_entry_candidate(s: state.State) -> dict | None:
    candidates = s.target_candidates or []
    candidate_by_ticker = {
        c.get("ticker"): c
        for c in candidates
        if isinstance(c, dict) and c.get("ticker")
    }
    tickers = _entry_candidate_tickers(s)
    cash = await _fetch_available_cash()
    total_amount = int(cash * ALLOC_RATIO)
    quote_results = await asyncio.gather(*(_fetch_expected_price(ticker) for ticker in tickers))
    valid: list[dict] = []
    blocked_reasons: list[str] = []

    for rank, (ticker, quote) in enumerate(zip(tickers, quote_results), start=1):
        expected_price, prev_close = quote
        candidate = candidate_by_ticker.get(ticker)
        if candidate is None:
            log(
                "F3_CANDIDATE_SNAPSHOT_MISSING",
                level="WARN",
                ticker=ticker,
                candidate_rank=rank,
                candidates=tickers,
            )
            candidate = {"ticker": ticker}
        if prev_close and expected_price:
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
                gap_max_pct=round(GAP_MAX_RECHECK * 100, 2),
            )
            if not (GAP_MIN_RECHECK <= gap < GAP_MAX_RECHECK):
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
                    gap_max_pct=round(GAP_MAX_RECHECK * 100, 2),
                    gap_reason=reason,
                )
                continue

        if not expected_price:
            blocked_reasons.append("PRICE_UNAVAILABLE")
            _log_entry_blocked(
                ticker,
                "PRICE_UNAVAILABLE",
                candidate_rank=rank,
                order_price=expected_price,
                cash=cash,
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
        await notifier.send(
            reason,
            level="WARN",
            message="F3 후보 전체가 주문 전 재검증에서 제외되었습니다.",
        )
        await db.record_skip(
            _today(),
            "ENTRY_FAIL" if reason != "GAP_CHANGED" else "GAP_CHANGED",
            f"reason={reason},candidates={','.join(tickers)}",
        )
        return None

    picked = max(
        valid,
        key=lambda item: (
            item["candidate"].get("expected_amount", 0.0),
            item["candidate"].get("buy_sell_ratio", 0.0),
            -item["candidate_rank"],
        ),
    )
    log(
        "F3_FINAL_PICK",
        level="INFO",
        ticker=picked["ticker"],
        candidate_rank=picked["candidate_rank"],
        checked_count=len(tickers),
        valid_count=len(valid),
        candidates=tickers,
        expected_price=picked["expected_price"],
        total_qty=picked["total_qty"],
    )
    return picked


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


def _first_order_at() -> tuple[int, int, int]:
    return _parse_deadline(F3_FIRST_ORDER_AT, (9, 10, 20))


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
    cash = to_float(summary.get("ord_psbl_cash"))
    if cash <= 0:
        cash = to_float(summary.get("dnca_tot_amt"))
    if cash <= 0:
        cash = to_float(summary.get("prvs_rcdl_excc_amt"))

    log(
        "BALANCE_CASH_CHECK",
        level="DEBUG",
        cash=cash,
        ord_psbl_cash=to_float(summary.get("ord_psbl_cash")),
        dnca_tot_amt=to_float(summary.get("dnca_tot_amt")),
        prvs_rcdl_excc_amt=to_float(summary.get("prvs_rcdl_excc_amt")),
    )
    return cash


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
