"""F4. 장중 추적 스탑 모듈 (09:00:00 ~ 10:59:59) — PRD §F4"""

import asyncio
import math
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from src import db, live, notifier, state
from src.api import kis_rest, kis_ws
from src.modules import vi_watch as vi_watch_mod
from src.modules.vi_watch import ViWatch
from src.utils.logger import log
from src.utils.spike_filter import SpikeFilter

KST = ZoneInfo("Asia/Seoul")
FORCE_TRAILING_HOUR = 10
FORCE_TRAILING_MINUTE = 50

STEP_SIZE      = 0.025   # 스텝 간격 +2.5% (params.json 로드 예정)
STEP_TRAIL     = 0.015   # 스텝 기준 하락폭 -1.5%
HARD_STOP_RATIO = 0.020  # Hard Stop -2.0% (trailing 미활성 구간 전용)
F4_REST_BACKUP_ENABLED = os.getenv("F4_REST_BACKUP_ENABLED", "1") == "1"
F4_REST_ONLY_WHEN_WS_STALE = os.getenv("F4_REST_ONLY_WHEN_WS_STALE", "1") == "1"
F4_WS_STALE_SEC = float(os.getenv("F4_WS_STALE_SEC", "2.0"))
F4_REST_POLL_INTERVAL_SEC = float(os.getenv("F4_REST_POLL_INTERVAL_SEC", "1.0"))
F4_FILL_POLL_INTERVAL_SEC = float(os.getenv("F4_FILL_POLL_INTERVAL_SEC", "0.5"))
F4_STATE_PERSIST_INTERVAL_SEC = float(os.getenv("F4_STATE_PERSIST_INTERVAL_SEC", "1.0"))
VI_WATCH_ENABLED = os.getenv("VI_WATCH_ENABLED", "1") == "1"
VI_FREEZE_SUSPECT_SEC = float(os.getenv("VI_FREEZE_SUSPECT_SEC", "10"))
VI_CHECK_COOLDOWN_SEC = float(os.getenv("VI_CHECK_COOLDOWN_SEC", "60"))

_SELL_TR = {"REAL": "TTTC0011U", "PAPER": "VTTC0011U"}
_CCLD_TR = {"REAL": "TTTC0081R", "PAPER": "VTTC0081R"}
_last_state_persist_at = 0.0
_close_in_progress = False
_close_in_progress_warned = False
_closing_task: asyncio.Task | None = None

_REARM_INTERVAL_SEC = 0.5
_REARM_HOLDING_INTERVAL_SEC = 5.0
_REARM_ERROR_INTERVAL_SEC = 5.0


async def run_forever() -> None:
    """F4 상주 진입점 — main.py에서 asyncio.create_task로 구동.

    run()은 한 사이클(HOLDING 대기 → 추적 → 청산)만 수행하고 반환하므로,
    거래일을 넘겨 살아 있는 프로세스에서는 이 루프가 다음 거래일의
    HOLDING을 다시 기다린다. HOLDING인 채로 사이클이 끝나는 경우는
    모니터 전원 비정상 종료뿐이므로, 재구독 폭주를 막는 백오프를 둔다.

    run()이 예외로 죽어도 루프는 유지한다 — main()은 이 태스크를 감시하지
    않으므로 여기서 전파되면 손절 감시가 영구히 사라진다. CancelledError는
    Exception이 아니므로 잡히지 않고 그대로 전파된다(종료 경로).
    """
    while True:
        try:
            await run()
        except Exception as e:
            log("F4_RUN_FOREVER_ERROR", level="CRIT", error=repr(e))
            try:
                await notifier.send(
                    "F4_RUN_FOREVER_ERROR",
                    level="CRIT",
                    message=(
                        f"F4 사이클 비정상 종료 — "
                        f"{_REARM_ERROR_INTERVAL_SEC:.0f}초 후 재시작: {e!r}"
                    ),
                )
            except Exception:
                pass  # 알림 실패가 상주 루프를 죽여선 안 된다
            await asyncio.sleep(_REARM_ERROR_INTERVAL_SEC)
            continue
        if state.get().position_status == "HOLDING":
            await asyncio.sleep(_REARM_HOLDING_INTERVAL_SEC)
        else:
            await asyncio.sleep(_REARM_INTERVAL_SEC)


async def run() -> None:
    """
    F4 단일 사이클 — WebSocket 구독 → 실패 시 REST 폴링 fallback.
    시작 시점 상태가 CLOSED면(당일 거래 종료) 즉시 반환한다.
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
    last_ws_tick_at = 0.0

    def is_ws_stale() -> bool:
        if not F4_REST_ONLY_WHEN_WS_STALE:
            return True
        if not live.ws_connected or last_ws_tick_at <= 0:
            return True
        return (time.monotonic() - last_ws_tick_at) >= F4_WS_STALE_SEC

    vi_watch = _make_vi_watch(ticker)

    async def on_tick(tick: dict) -> None:
        nonlocal last_ws_tick_at
        live.ws_connected = True
        last_ws_tick_at = time.monotonic()
        live.push_tick(tick["price"], ticker=ticker)
        await _process_tick(tick["price"], spike_filter)
        if vi_watch is not None:
            try:
                await _handle_vi_events(
                    await vi_watch.on_price(tick["price"], "ws"), ticker)
            except Exception as e:
                # 관측 전용 — VI 감시 오류가 스탑 추적을 깨선 안 된다
                log("VI_WATCH_ERROR", level="WARN", ticker=ticker, error=repr(e))

    ws_task = asyncio.create_task(kis_ws.subscribe(
        ticker, on_tick,
        stop_if=lambda: state.get().position_status != "HOLDING",
    ))
    tasks = [ws_task]
    if F4_REST_BACKUP_ENABLED:
        tasks.append(asyncio.create_task(
            _run_rest_price_backup(ticker, spike_filter, is_ws_stale, vi_watch=vi_watch)))
    try:
        # 모니터 태스크 하나가 예외로 죽어도 나머지 태스크는 유지한다.
        # 정상 종료(HOLDING 해제)만 감시 종료로 취급.
        pending = set(tasks)
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            crashed = [t for t in done if not t.cancelled() and t.exception() is not None]
            for task in crashed:
                log(
                    "F4_MONITOR_TASK_ERROR",
                    level="CRIT",
                    ticker=ticker,
                    error=repr(task.exception()),
                    remaining_monitors=len(pending),
                )
            if len(crashed) < len(done):
                break
            if crashed and state.get().position_status == "HOLDING":
                await notifier.send(
                    "F4_MONITOR_TASK_ERROR",
                    level="CRIT",
                    message=(
                        f"F4 모니터 태스크 비정상 종료. {ticker} "
                        f"잔여 모니터={len(pending)}개"
                    ),
                    ticker=ticker,
                )
    finally:
        closing = _closing_task
        for task in tasks:
            if task is closing:
                continue
            if not task.done():
                task.cancel()
        if closing is not None and not closing.done():
            await asyncio.shield(closing)
        await asyncio.gather(*tasks, return_exceptions=True)
        live.ws_connected = False


async def _run_rest_price_backup(
    ticker: str, spike_filter: SpikeFilter, is_ws_stale=None, vi_watch: ViWatch | None = None,
) -> None:
    """REST backup price monitor while HOLDING. Poll only when WS is stale by default."""
    log(
        "F4_REST_BACKUP_START",
        level="INFO",
        ticker=ticker,
        interval_sec=F4_REST_POLL_INTERVAL_SEC,
        only_when_ws_stale=F4_REST_ONLY_WHEN_WS_STALE,
        ws_stale_sec=F4_WS_STALE_SEC,
    )
    while state.get().position_status == "HOLDING":
        if is_ws_stale is not None and not is_ws_stale():
            await asyncio.sleep(F4_REST_POLL_INTERVAL_SEC)
            continue
        try:
            price = await _fetch_current_price(ticker)
            if price > 0:
                live.push_tick(price, ticker=ticker)
                await _process_tick(price, spike_filter)
                if vi_watch is not None:
                    await _handle_vi_events(
                        await vi_watch.on_price(price, "rest"), ticker)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # 백업 폴러가 죽으면 WS까지 함께 취소되므로 절대 전파하지 않는다
            log("F4_REST_BACKUP_ERROR", level="WARN", ticker=ticker, error=repr(e))
        await asyncio.sleep(F4_REST_POLL_INTERVAL_SEC)


# ── VI 감지 (관측 전용 — PRD 외 가시성 기능) ─────────────────────────

def _make_vi_watch(ticker: str) -> ViWatch | None:
    if not VI_WATCH_ENABLED:
        return None
    return ViWatch(
        ticker,
        lambda: _fetch_vi_status(ticker),
        freeze_sec=VI_FREEZE_SUSPECT_SEC,
        cooldown_sec=VI_CHECK_COOLDOWN_SEC,
    )


async def _fetch_vi_status(ticker: str) -> dict:
    """변동성완화장치(VI) 현황 조회 — vi_watch 공용 구현 위임."""
    return await vi_watch_mod.fetch_vi_status(ticker)


async def _handle_vi_events(events: list[dict], ticker: str) -> None:
    """ViWatch 이벤트를 로그·텔레그램·live(UI)로 전파한다."""
    for ev in events:
        etype = ev.get("type")
        fields = {k: v for k, v in ev.items() if k not in ("type", "ts")}
        if etype == "VI_DETECTED":
            log("VI_DETECTED", level="WARN", ticker=ticker, **fields)
            live.record_vi_detected(ev)
            await notifier.send(
                "VI_DETECTED",
                level="WARN",
                message=(
                    f"VI 발동 — 발동가 {ev.get('vi_prc')}원, "
                    f"기준가 {ev.get('vi_stnd_prc')}원({ev.get('vi_dprt')}%). "
                    f"2분 단일가 정지, 해제 시 재알림."
                ),
                ticker=ticker,
            )
        elif etype == "VI_RELEASED":
            log("VI_RELEASED", level="INFO", ticker=ticker, **fields)
            live.record_vi_released(ev)
            duration = ev.get("duration_sec") or 0
            await notifier.send(
                "VI_RELEASED",
                level="INFO",
                message=(
                    f"VI 해제 — 재개가 {ev.get('release_price')}원, "
                    f"정지 {duration:.0f}초. 스탑 감시 계속."
                ),
                ticker=ticker,
            )
        elif etype == "VI_CHECK_FAILED":
            log("VI_CHECK_FAILED", level="WARN", ticker=ticker, **fields)
        elif etype == "VI_CHECK_NEGATIVE":
            # 가격 동결인데 VI 아님 — 한산 or 실제 WS 장애 후보. 로그만 남긴다.
            log("VI_CHECK_NEGATIVE", level="INFO", ticker=ticker, **fields)


async def _process_tick(price: float, spike_filter: SpikeFilter) -> None:
    """단일 체결 틱 처리. 우선순위: Hard Stop > Step Trailing (상호 배타적)."""
    s = state.get()
    if s.position_status != "HOLDING":
        return
    if not spike_filter.is_valid(price, s.target_ticker):
        return

    entry = s.entry_price or 0.0
    before_high_price = s.high_price
    before_highest_step = s.highest_step
    before_trailing_active = s.trailing_active
    state.update_high_price(price)

    now = datetime.now(KST)
    late = (now.hour, now.minute) >= (FORCE_TRAILING_HOUR, FORCE_TRAILING_MINUTE)

    # 스텝 갱신 (highest_step은 단조 증가)
    pnl = price / entry - 1
    current_step = max(math.floor(pnl / STEP_SIZE) * STEP_SIZE, 0.0)
    if current_step > s.highest_step:
        s.highest_step = current_step
    if s.highest_step >= STEP_SIZE:
        s.trailing_active = True

    # 청산 10분 전 강제 활성화 (스텝 미달성이어도 trailing 발동)
    if late and not s.trailing_active:
        s.trailing_active = True

    high_changed = s.high_price != before_high_price
    step_changed = s.highest_step != before_highest_step
    trailing_activated = s.trailing_active and not before_trailing_active

    # [우선순위 1] Hard Stop (-2.0%): trailing 미활성 구간에서만 유효
    if not s.trailing_active and price <= entry * (1 - HARD_STOP_RATIO):
        await _trigger_close(price, "HARD_STOP")
        return

    # [우선순위 2] Step Trailing
    if s.trailing_active:
        stop = entry * (1 + s.highest_step - STEP_TRAIL)
        if price <= stop:
            await _trigger_close(price, "TRAILING")
            return

    if high_changed or step_changed or trailing_activated:
        await _persist_tracking_state(force=step_changed or trailing_activated)

async def _trigger_close(price: float, reason: str) -> None:
    """Run a close once; state becomes CLOSED only after sell/DB/persist succeeds."""
    global _close_in_progress, _close_in_progress_warned, _closing_task
    if _close_in_progress:
        if not _close_in_progress_warned:
            log("F4_CLOSE_ALREADY_IN_PROGRESS", level="WARN", ticker=state.get().target_ticker, reason=reason)
            _close_in_progress_warned = True
        return

    _close_in_progress = True
    _close_in_progress_warned = False
    close_task = asyncio.create_task(
        _execute_close(price, reason),
        name=f"f4_execute_close_{reason.lower()}",
    )
    _closing_task = close_task
    try:
        await asyncio.shield(close_task)
    except asyncio.CancelledError:
        log("F4_CLOSE_CANCEL_REQUESTED", level="CRIT", ticker=state.get().target_ticker, reason=reason)
        try:
            await notifier.send(
                "F4_CLOSE_CANCEL_REQUESTED",
                level="CRIT",
                message=f"F4 청산 태스크 취소 요청 감지: {state.get().target_ticker} {reason}. 청산 완료까지 대기합니다.",
                ticker=state.get().target_ticker,
            )
        finally:
            await asyncio.shield(close_task)
            raise
    finally:
        if close_task.done():
            _close_in_progress = False
            _close_in_progress_warned = False
            if _closing_task is close_task:
                _closing_task = None

async def _persist_tracking_state(force: bool = False) -> bool:
    """Persist in-trade trailing progress with a small throttle."""
    global _last_state_persist_at
    s = state.get()
    if s.position_status != "HOLDING" or not s.trade_id:
        return False

    now_mono = time.monotonic()
    if (
        not force
        and F4_STATE_PERSIST_INTERVAL_SEC > 0
        and now_mono - _last_state_persist_at < F4_STATE_PERSIST_INTERVAL_SEC
    ):
        return False

    state_saved = False
    db_saved = False
    try:
        await state.persist(
            os.getenv("STATE_DIR", "data/state"),
            datetime.now(KST).strftime("%Y%m%d"),
        )
        state_saved = True
    except Exception as exc:
        log(
            "F4_STATE_PERSIST_ERROR",
            level="WARN",
            ticker=s.target_ticker,
            error=repr(exc),
        )

    try:
        await db.update_trade_progress(s.trade_id, s.high_price, s.highest_step)
        db_saved = True
    except Exception as exc:
        log(
            "F4_DB_PROGRESS_ERROR",
            level="WARN",
            ticker=s.target_ticker,
            trade_id=s.trade_id,
            error=repr(exc),
        )

    if not (state_saved or db_saved):
        return False

    _last_state_persist_at = now_mono
    log(
        "F4_STATE_PERSISTED",
        level="DEBUG",
        ticker=s.target_ticker,
        high_price=s.high_price,
        highest_step=s.highest_step,
        trailing_active=s.trailing_active,
        state_saved=state_saved,
        db_saved=db_saved,
        force=force,
    )
    return True

async def _execute_close(price: float, reason: str) -> bool:
    try:
        return await _execute_close_impl(price, reason)
    except asyncio.CancelledError:
        s = state.get()
        log("F4_CLOSE_TASK_CANCELLED", level="CRIT", ticker=s.target_ticker, reason=reason)
        try:
            await asyncio.shield(notifier.send(
                "F4_CLOSE_TASK_CANCELLED",
                level="CRIT",
                message=f"F4 청산 태스크 직접 취소 감지: {s.target_ticker} {reason}. KIS 체결/잔고 확인 필요",
                ticker=s.target_ticker,
            ))
        except Exception:
            pass
        raise


async def _execute_close_impl(price: float, reason: str) -> bool:
    """잔여 전량 시장가 매도 후 로그/알림/DB 기록."""
    s = state.get()
    qty = s.remaining_qty or 0
    entry = s.entry_price or price
    mode = os.getenv("KIS_MODE", "PAPER")

    if os.getenv("DRY_RUN", "0") == "1":
        exit_price = price
        pnl_pct = round((exit_price / entry - 1) * 100, 2) if entry else 0.0
        event_name = "TRAILING_STOP" if reason == "TRAILING" else reason
        level = "INFO" if reason == "TRAILING" else "WARN"
        log_extra: dict = {}
        if reason == "TRAILING":
            stop_price = entry * (1 + s.highest_step - STEP_TRAIL)
            log_extra = {"highest_step": s.highest_step, "stop_price": round(stop_price, 0)}
        log(event_name, level=level, ticker=s.target_ticker,
            entry_price=entry, exit_price=exit_price, exit_qty=qty,
            pnl_pct=pnl_pct, dry_run=True, fill_latency_ms=0, **log_extra)
        await state.set_closed(reason)
        await state.persist(os.getenv("STATE_DIR", "data/state"),
                            datetime.now(KST).strftime("%Y%m%d"))
        return True

    sell_id = ""
    exit_price = price
    try:
        sell_resp = await _send_sell(s.target_ticker, qty, mode)
        output = sell_resp.get("output") if isinstance(sell_resp.get("output"), dict) else {}
        sell_id = output.get("ODNO", "")
        if sell_resp.get("rt_cd") not in ("0", 0, None) or not sell_id:
            raise RuntimeError(
                f"sell rejected rt_cd={sell_resp.get('rt_cd')} "
                f"msg_cd={sell_resp.get('msg_cd')} msg1={sell_resp.get('msg1')} "
                f"order_id={sell_id or '-'}"
            )
        fill = await _poll_fill(sell_id, timeout_sec=30)
        if fill:
            exit_price = fill["fill_price"]
    except Exception as e:
        log("F4_SELL_ERROR", level="CRIT", ticker=s.target_ticker, error=repr(e))
        await notifier.send(
            "F4_SELL_ERROR",
            level="CRIT",
            message=f"매도 주문 오류: {s.target_ticker} {repr(e)}. 수동 청산 필요",
            ticker=s.target_ticker,
        )
        return False

    pnl_pct = round((exit_price / entry - 1) * 100, 2) if entry else 0.0

    if s.trade_id:
        try:
            # order_price에는 매도 트리거 시점 가격을 기록한다 (체결가는 update_order_fill).
            order_db_id = await db.record_order(
                s.trade_id, sell_id, "SELL", qty, price, "CLOSE_SELL", s.target_ticker, s.target_name,
            )
            await db.update_order_fill(order_db_id, exit_price, qty, 0)
            await db.close_trade(s.trade_id, exit_price, reason, pnl_pct, s.highest_step)
        except Exception as e:
            log(
                "F4_CLOSE_RECORD_ERROR",
                level="CRIT",
                ticker=s.target_ticker,
                order_id=sell_id,
                error=repr(e),
            )
            await notifier.send(
                "F4_CLOSE_RECORD_ERROR",
                level="CRIT",
                message=f"F4 매도 성공 후 DB 기록 실패: {s.target_ticker} order_id={sell_id} {repr(e)}. 재매도 방지를 위해 CLOSED 처리합니다.",
                ticker=s.target_ticker,
            )
            await state.set_closed(reason)
            await state.persist(os.getenv("STATE_DIR", "data/state"), datetime.now(KST).strftime("%Y%m%d"))
            return True
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
        ticker=s.target_ticker,
    )
    await state.set_closed(reason)
    await state.persist(os.getenv("STATE_DIR", "data/state"),
                        datetime.now(KST).strftime("%Y%m%d"))
    return True


async def _run_dry_ticks(ticker: str, spike_filter: SpikeFilter) -> None:
    s = state.get()
    entry = float(s.entry_price or os.getenv("DRY_RUN_ENTRY_PRICE", "10300"))
    delay = float(os.getenv("DRY_RUN_STEP_DELAY", "0.2"))
    prices = [
        entry,
        round(entry * 1.026),
        round(entry * 1.032),
        # After the first step, trailing stop is entry * 1.010. Use a value
        # slightly below it so DRY_RUN deterministically closes.
        round(entry * 1.009),
    ]

    live.ws_connected = True
    log("DRY_RUN_F4_START", level="WARN", ticker=ticker, entry_price=entry, prices=prices)
    for price in prices:
        if state.get().position_status != "HOLDING":
            break
        live.push_tick(price, ticker=ticker)
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


async def _fetch_current_price(ticker: str) -> float:
    resp = await kis_rest.get(
        "/uapi/domestic-stock/v1/quotations/inquire-price",
        tr_id="FHKST01010100",
        params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker},
    )
    out = resp.get("output", {}) if isinstance(resp.get("output"), dict) else {}
    return float(out.get("stck_prpr") or out.get("antc_cnpr") or 0)


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
    mode = os.getenv("KIS_MODE", "PAPER")
    today = datetime.now(KST).strftime("%Y%m%d")
    attempts = max(1, round(timeout_sec / F4_FILL_POLL_INTERVAL_SEC))
    for _ in range(attempts):
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
        await asyncio.sleep(F4_FILL_POLL_INTERVAL_SEC)
    return None
