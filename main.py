"""진입점 — 스케줄러 부트스트랩 및 장기 실행 태스크 관리"""

import asyncio
import contextlib
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

if os.getenv("DRY_RUN", "0") == "1":
    os.environ["LOG_DIR"] = os.getenv("DRY_RUN_LOG_DIR", "data/dry_run/logs")
    os.environ["STATE_DIR"] = os.getenv("DRY_RUN_STATE_DIR", "data/dry_run/state")
    os.environ["DB_DIR"] = os.getenv("DRY_RUN_DB_DIR", "data/dry_run/db")

import uvicorn  # noqa: E402

from src import db, notifier, state  # noqa: E402
from src.api import auth, kis_rest, server  # noqa: E402
from src.modules import f1_filter, f2_lockup, f3_entry, f4_tracking, f5_timeout  # noqa: E402
from src.scheduler import (  # noqa: E402
    F1_H,
    F1_M,
    F3_FILL_DEADLINE_H,
    F3_FILL_DEADLINE_M,
    F3_H,
    F3_M,
    F3_S,
    build,
)
from src.utils import logger, time_sync  # noqa: E402

KST = ZoneInfo("Asia/Seoul")

LOG_DIR = os.getenv("LOG_DIR", "data/logs")
STATE_DIR = os.getenv("STATE_DIR", "data/state")
NTP_SERVERS = [s.strip() for s in os.getenv("NTP_SERVER", "pool.ntp.org").split(",")]
DB_PATH = os.path.join(os.getenv("DB_DIR", "data/db"), "trading.db")
PID_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.pid")
F2_RETRY_F1_ON_FAIL = os.getenv(
    "F2_RETRY_F1_ON_FAIL",
    "0" if os.getenv("KIS_MODE", "PAPER") == "REAL" else "1",
) == "1"
F2_RETRY_F1_INTERVAL_SEC = int(
    os.getenv("F2_RETRY_F1_INTERVAL_SEC", str(f1_filter.F1_RETRY_INTERVAL_SEC))
)
F2_RETRY_F1_MIN_REMAINING_SEC = int(os.getenv("F2_RETRY_F1_MIN_REMAINING_SEC", "2"))
_BAL_TR = {"REAL": "TTTC8434R", "PAPER": "VTTC8434R"}

# 국내휴장일조회 — 실전 전용 TR (모의투자 미지원)
_HOLIDAY_CHECK_PATH = "/uapi/domestic-stock/v1/quotations/chk-holiday"
_HOLIDAY_CHECK_TR_ID = "CTCA0903R"

# F1 결과를 F2에 전달하기 위한 세션 변수
_f1_result: list[dict] = []
_f2_done = False
_f3_started = False
# 휴장 확인된 날짜(YYYYMMDD). 날짜를 함께 저장해 당일 확인된 휴장에만 적용 —
# HOLDING으로 일일 리셋이 막히거나 휴장 API가 실패해도 다음 거래일을 막지 않는다.
_market_closed_date: str | None = None


def _today() -> str:
    return datetime.now(KST).strftime("%Y%m%d")


def _scheduled_at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime.now(KST).replace(hour=hour, minute=minute, second=second, microsecond=0)


def _is_trading_weekday() -> bool:
    return datetime.now(KST).weekday() < 5  # 월(0)~금(4)


def _is_market_closed_today() -> bool:
    return _market_closed_date == _today()


def _past_f3_schedule() -> bool:
    return datetime.now(KST) >= _scheduled_at(F3_H, F3_M, F3_S)


def _before_f1_retry_deadline() -> bool:
    return datetime.now(KST) < _scheduled_at(f1_filter.F1_DEADLINE_H, f1_filter.F1_DEADLINE_M)


def _f2_retry_sleep_seconds() -> int:
    deadline = _scheduled_at(f1_filter.F1_DEADLINE_H, f1_filter.F1_DEADLINE_M)
    remaining = max(1, int((deadline - datetime.now(KST)).total_seconds()))
    return max(1, min(F2_RETRY_F1_INTERVAL_SEC, remaining))


def _f2_retry_remaining_seconds() -> int:
    deadline = _scheduled_at(f1_filter.F1_DEADLINE_H, f1_filter.F1_DEADLINE_M)
    return int((deadline - datetime.now(KST)).total_seconds())


async def _ensure_trading_day() -> None:
    global _f1_result, _f2_done, _f3_started, _market_closed_date
    today = _today()
    if await state.ensure_trading_day(today):
        _f1_result = []
        _f2_done = False
        _f3_started = False
        _market_closed_date = None
        logger.log("DAILY_STATE_RESET", level="INFO", date=today)


async def _check_market_holiday() -> None:
    """
    KIS 휴장일 조회로 당일 개장 여부 확인. 휴장이면 당일 잡 전체를 스킵한다.
    조회 실패·미확정 응답값은 fail-open(개장 가정) — 거래일을 놓치는 쪽이 더 큰 손실.
    휴장 플래그는 확인된 날짜에 바인딩되므로(_market_closed_date) 실패 경로에서
    전일 플래그가 남아도 당일 잡을 막지 않는다.
    CTCA0903R은 모의투자 미지원이므로 PAPER 모드는 평일 가드만 적용한다.
    """
    global _market_closed_date
    if os.getenv("KIS_MODE", "PAPER") != "REAL":
        return

    today = _today()
    try:
        resp = await kis_rest.get(
            _HOLIDAY_CHECK_PATH,
            tr_id=_HOLIDAY_CHECK_TR_ID,
            params={"BASS_DT": today, "CTX_AREA_NK": "", "CTX_AREA_FK": ""},
        )
    except Exception as exc:
        logger.log("HOLIDAY_CHECK_FAILED", level="WARN", error=repr(exc))
        return

    if str(resp.get("rt_cd", "0")) != "0":
        logger.log(
            "HOLIDAY_CHECK_FAILED",
            level="WARN",
            msg_cd=resp.get("msg_cd"),
            msg1=resp.get("msg1"),
        )
        return

    rows = resp.get("output", [])
    row = next(
        (r for r in rows if isinstance(r, dict) and r.get("bass_dt") == today), None
    )
    if row is None:
        logger.log("HOLIDAY_CHECK_FAILED", level="WARN", reason="TODAY_NOT_IN_RESPONSE")
        return

    opnd_yn = str(row.get("opnd_yn") or "").strip().upper()
    if opnd_yn == "Y":
        _market_closed_date = None
        return
    if opnd_yn != "N":
        # 미검증 스키마 대비 — 명시적 "N"만 휴장으로 인정하고 나머지는 fail-open
        logger.log(
            "HOLIDAY_CHECK_FAILED",
            level="WARN",
            reason="UNEXPECTED_OPND_YN",
            opnd_yn=row.get("opnd_yn"),
        )
        return

    if _market_closed_date == today:
        return  # 기동 시 체크 후 08:29 잡 재확인 등 — 중복 로그·알림 방지

    _market_closed_date = today
    s = state.get()
    s.day_skip = True
    s.close_reason = s.close_reason or "MARKET_CLOSED"
    logger.log("MARKET_CLOSED", level="INFO", date=today)
    await notifier.send(
        "MARKET_CLOSED",
        level="INFO",
        message=f"휴장일 감지({today}). 당일 거래 없음.",
    )


# ── 스케줄 작업 래퍼 ─────────────────────────────────────────────────

async def job_token_refresh() -> None:
    await _ensure_trading_day()
    await auth.refresh()
    await _check_market_holiday()


async def job_ntp_check() -> None:
    await _ensure_trading_day()
    time_sync.check_ntp(NTP_SERVERS)



async def _today_trade_exists() -> bool:
    try:
        return await db.get_trade_by_date(datetime.now(KST).strftime("%Y%m%d")) is not None
    except Exception as exc:
        logger.log("TRADE_EXISTENCE_CHECK_FAILED", level="WARN", error=repr(exc))
        return False


async def _skip_entry_pipeline_if_trade_exists(source: str) -> bool:
    """Once today's buy exists, never rerun F1/F2/F3 catch-up or scheduled entry."""
    global _f2_done, _f3_started
    if not await _today_trade_exists():
        return False
    _f2_done = True
    _f3_started = True
    state.get().day_skip = True
    state.get().close_reason = state.get().close_reason or "TRADE_ALREADY_EXISTS"
    logger.log("TRADE_ALREADY_EXISTS", level="WARN", source=source, action="SKIP_ENTRY_PIPELINE")
    return True

async def job_f1() -> None:
    global _f1_result
    await _ensure_trading_day()
    if _is_market_closed_today():
        return
    if await _skip_entry_pipeline_if_trade_exists("JOB_F1"):
        return
    _f1_result = await f1_filter.run()
    await _run_f2_f3_after_f1(immediate=_past_f3_schedule())


async def job_f2() -> None:
    await _ensure_trading_day()
    if _is_market_closed_today():
        return
    if await _skip_entry_pipeline_if_trade_exists("JOB_F2"):
        return
    # F1 completion can trigger F2/F3 immediately; this scheduled job is a safety net.
    if _f2_done:
        return
    if not _f1_result:
        return
    await _run_f2_f3_after_f1(immediate=_past_f3_schedule())


async def job_f3() -> None:
    global _f3_started
    await _ensure_trading_day()
    if _is_market_closed_today():
        return
    if await _skip_entry_pipeline_if_trade_exists("JOB_F3"):
        return
    # Keep the scheduled F3 job as a fallback when F2 locked a target but chaining did not start F3.
    if _f3_started:
        return
    if not state.get().target_ticker or state.get().day_skip:
        return
    _f3_started = True
    await f3_entry.run()


async def job_f5_precheck() -> None:
    await _ensure_trading_day()
    if _is_market_closed_today():
        return
    await f5_timeout.precheck()


async def job_f5_exec() -> None:
    await _ensure_trading_day()
    if _is_market_closed_today():
        return
    await f5_timeout.execute()


async def _run_f2_f3_after_f1(*, immediate: bool = False) -> None:
    """
    Chain F2/F3 after F1 produced candidates.

    Normal F1 calls this right away; if F2 rejects every candidate and the
    env-controlled F2 retry is enabled, the chain can clear that F2-only
    day_skip and retry F1 before the F1 deadline. F3 only uses force mode if
    the scheduled F3 time has already passed. Catch-up calls it only inside the
    missed-run window and can force F3 when explicitly catching up after the
    scheduled F3 time.
    """
    global _f1_result, _f2_done, _f3_started
    if not _f1_result or state.get().day_skip:
        return

    f2_attempt = 0
    f2_retry_started = False
    while not _f2_done and _f1_result and not state.get().day_skip:
        f2_attempt += 1
        await f2_lockup.run(_f1_result)

        s = state.get()
        if s.target_ticker and not s.day_skip:
            _f2_done = True
            break

        if not _should_retry_f1_after_f2_fail():
            _f2_done = True
            break

        sleep_sec = _f2_retry_sleep_seconds()
        logger.log(
            "F2_FAIL_F1_RETRY",
            level="WARN",
            attempt=f2_attempt,
            retry_after_sec=sleep_sec,
            deadline=f"{f1_filter.F1_DEADLINE_H:02d}:{f1_filter.F1_DEADLINE_M:02d}:00",
            reason="F2_NO_TARGET",
        )
        await notifier.send(
            "F2_FAIL_F1_RETRY",
            level="WARN",
            message=(
                f"F2 후보가 전부 제외되어 F1을 {sleep_sec}초 후 재시도합니다. "
                f"deadline={f1_filter.F1_DEADLINE_H:02d}:{f1_filter.F1_DEADLINE_M:02d}:00"
            ),
        )
        f2_retry_started = True
        s.day_skip = False
        s.target_ticker = None
        s.target_name = None
        s.target_candidates = None
        await asyncio.sleep(sleep_sec)
        _f1_result = await f1_filter.run()

    if state.get().day_skip:
        _f2_done = True
        if f2_retry_started and not state.get().target_ticker:
            await notifier.send(
                "F2_RETRY_EXHAUSTED",
                level="WARN",
                message="F2 실패 후 F1 재시도까지 했지만 최종 후보를 확정하지 못했습니다.",
            )

    if _f2_done and not _f3_started and state.get().target_ticker and not state.get().day_skip:
        _f3_started = True
        await f3_entry.run(force=immediate)


def _should_retry_f1_after_f2_fail() -> bool:
    s = state.get()
    return (
        F2_RETRY_F1_ON_FAIL
        and os.getenv("DRY_RUN", "0") != "1"
        and s.day_skip
        and not s.target_ticker
        and _before_f1_retry_deadline()
        and _f2_retry_remaining_seconds() >= F2_RETRY_F1_MIN_REMAINING_SEC
    )


# ── F1 missed 보완 실행 ──────────────────────────────────────────────

async def _run_catchup() -> None:
    """
    F1 start 이후 F3 fill deadline 전에 기동하면 F1(~F3)이 missed 상태.
    F1 결과가 나오면 F2/F3 체인을 즉시 이어서 당일 파이프라인을 복구한다.
    F3 fill deadline 이후엔 진입 마감이 지났으므로 catchup 불가.
    """
    await _ensure_trading_day()
    dry_run = os.getenv("DRY_RUN", "0") == "1"
    force = dry_run or os.getenv("FORCE_CATCHUP", "0") == "1"

    if not force and not _is_trading_weekday():
        logger.log("CATCHUP_SKIP_WEEKEND", level="INFO",
                   message="주말(토·일) 기동 — 진입 catchup 생략")
        return

    if not force and _is_market_closed_today():
        logger.log("CATCHUP_SKIP_MARKET_CLOSED", level="INFO",
                   message="휴장일 기동 — 진입 catchup 생략")
        return

    if await _skip_entry_pipeline_if_trade_exists("CATCHUP"):
        return

    now = datetime.now(KST)
    f1_sched = _scheduled_at(F1_H, F1_M)
    f3_sched = _scheduled_at(F3_H, F3_M, F3_S)
    f3_fill_deadline = _scheduled_at(F3_FILL_DEADLINE_H, F3_FILL_DEADLINE_M)

    if not force and not (f1_sched <= now < f3_fill_deadline):
        return

    logger.log(
        "CATCHUP_START",
        level="WARN",
        message=(
            f"{'[FORCE] ' if force else ''}F1 missed 감지. "
            f"보완 실행 ({now.strftime('%H:%M:%S')} 기동)"
        ),
    )
    await notifier.send(
        "CATCHUP_START",
        level="WARN",
        message=(
            f"{'[FORCE] ' if force else ''}F1 missed 감지 "
            f"({now.strftime('%H:%M:%S')} 기동). 보완 실행 중..."
        ),
    )

    global _f1_result
    _f1_result = await f1_filter.run()

    now = datetime.now(KST)
    await _run_f2_f3_after_f1(immediate=force or now >= f3_sched)


# ── 재시작 복구 ──────────────────────────────────────────────────────

async def _recover_state() -> None:
    """
    프로세스 재시작 시 today_state.json을 우선 복구하고, 필요하면 DB OPEN trade를 보조로 복구한다.
    """
    data = state.load(STATE_DIR)
    today = datetime.now(KST).strftime("%Y%m%d")

    if data is not None and data.get("date") != today:
        stale_status = data.get("position_status")
        if stale_status in ("HOLDING", "ENTERING"):
            # 전일 미청산 포지션 의심 — 실계좌에 잔고가 남아 있을 수 있으므로
            # 운영자가 확인하기 전까지 당일 자동 진입(F1~F3)을 차단한다.
            # 해제: 계좌 확인 후 문제없으면 today_state.json 삭제 후 재시작.
            state.get().day_skip = True
            logger.log(
                "STALE_POSITION_DETECTED",
                level="CRIT",
                stale_date=data.get("date"),
                stale_status=stale_status,
                ticker=data.get("ticker"),
                entry_blocked=True,
            )
            await notifier.send(
                "STALE_POSITION_DETECTED",
                level="CRIT",
                message=(
                    f"전일 미청산 포지션 의심. date={data.get('date')} "
                    f"ticker={data.get('ticker')} status={stale_status}. "
                    "당일 자동 진입을 차단했습니다. 계좌 보유 수량과 미체결 주문을 "
                    "확인하고, 문제없으면 today_state.json 삭제 후 재시작하세요."
                ),
                ticker=data.get("ticker"),
            )
        else:
            await notifier.send(
                "STALE_POSITION_DETECTED",
                level="CRIT",
                message=f"전일 포지션 잔류 의심. date={data.get('date')}",
            )
        data = None

    if data is not None and data.get("position_status") == "HOLDING":
        actual_qty = await _verified_holding_qty(data.get("ticker"), "STATE_FILE")
        if actual_qty and actual_qty > 0:
            data = dict(data)
            data["entry_qty"] = actual_qty
            data["remaining_qty"] = actual_qty
            state.restore_from(data)
            logger.log(
                "PROCESS_RESTART_DETECTED",
                level="CRIT",
                recovered_status="HOLDING_RESUMED",
                recovery_source="STATE_FILE",
                actual_qty=actual_qty,
            )
            await notifier.send(
                "PROCESS_RESTART_DETECTED",
                level="CRIT",
                message=f"재시작 감지. 포지션 복구: {data.get('ticker')} {actual_qty}주",
                ticker=data.get("ticker"),
            )
            return
        if actual_qty is None:
            logger.log(
                "PROCESS_RESTART_DETECTED",
                level="CRIT",
                recovered_status="HOLDING_VERIFY_FAILED_SKIP_RESTORE",
                recovery_source="STATE_FILE",
                actual_qty=actual_qty,
            )
        else:
            logger.log(
                "PROCESS_RESTART_DETECTED",
                level="WARN",
                recovered_status="NO_ACTUAL_HOLDING",
                recovery_source="STATE_FILE",
                actual_qty=actual_qty,
            )
        return

    if data is not None and _is_terminal_state(data):
        if data.get("position_status") == "CLOSED":
            # 당일 청산 완료 상태 복원 — 재시작 후에도 UI가 청산 차트와 매수/매도
            # 마커를 유지하고, CLOSED 상태가 당일 재진입(set_entering)도 막는다.
            state.restore_from(data)
        logger.log(
            "PROCESS_RESTART_DETECTED",
            level="WARN",
            recovered_status="STATE_FILE_TERMINAL_SKIP_DB_FALLBACK",
            recovery_source="STATE_FILE",
            position_status=data.get("position_status"),
            remaining_qty=data.get("remaining_qty"),
        )
        return

    await _recover_open_trade_from_db(today)


def _is_terminal_state(data: dict) -> bool:
    status = data.get("position_status")
    remaining_qty = data.get("remaining_qty")
    return status == "CLOSED" or (
        remaining_qty is not None and int(float(remaining_qty)) <= 0
    )


async def _verified_holding_qty(ticker: str | None, recovery_source: str) -> int | None:
    if not ticker:
        logger.log(
            "PROCESS_RESTART_DETECTED",
            level="CRIT",
            recovered_status="HOLDING_VERIFY_SKIPPED",
            recovery_source=recovery_source,
            reason="MISSING_TICKER",
        )
        return None

    mode = os.getenv("KIS_MODE", "PAPER")
    try:
        resp = await kis_rest.get(
            "/uapi/domestic-stock/v1/trading/inquire-balance",
            tr_id=_BAL_TR[mode],
            params=kis_rest.balance_inquiry_params(),
        )
    except Exception as exc:
        logger.log(
            "PROCESS_RESTART_DETECTED",
            level="CRIT",
            recovered_status="HOLDING_VERIFY_FAILED",
            recovery_source=recovery_source,
            ticker=ticker,
            error=repr(exc),
        )
        await notifier.send(
            "PROCESS_RESTART_DETECTED",
            level="CRIT",
            message=f"재시작 복구 전 잔고 확인 실패: {ticker}. 수동 확인 필요.",
            ticker=ticker,
        )
        return None

    if str(resp.get("rt_cd", "0")) != "0":
        logger.log(
            "PROCESS_RESTART_DETECTED",
            level="CRIT",
            recovered_status="HOLDING_VERIFY_FAILED",
            recovery_source=recovery_source,
            ticker=ticker,
            rt_cd=resp.get("rt_cd"),
            msg_cd=resp.get("msg_cd"),
            msg1=resp.get("msg1"),
        )
        await notifier.send(
            "PROCESS_RESTART_DETECTED",
            level="CRIT",
            message=(
                f"재시작 복구 전 잔고 확인 실패: {ticker}. "
                f"{resp.get('msg_cd') or ''} {resp.get('msg1') or ''}. 수동 확인 필요."
            ),
            ticker=ticker,
        )
        return None

    for item in resp.get("output1", []):
        if isinstance(item, dict) and item.get("pdno") == ticker:
            return int(float(item.get("hldg_qty") or 0))
    return 0


async def _recover_open_trade_from_db(today: str) -> None:
    """Recover HOLDING state from today's OPEN trade when KIS confirms the holding."""
    try:
        trade = await db.get_trade_by_date(today)
    except RuntimeError:
        return
    if not trade or trade.get("status") != "OPEN":
        return

    entry_price = float(trade.get("entry_price") or 0)
    entry_qty = int(trade.get("entry_qty") or 0)
    if entry_price <= 0 or entry_qty <= 0:
        logger.log(
            "PROCESS_RESTART_DETECTED",
            level="WARN",
            recovered_status="DB_OPEN_TRADE_INCOMPLETE",
            recovery_source="DB_OPEN_TRADE",
            trade_id=trade.get("id"),
            ticker=trade.get("ticker"),
        )
        return

    actual_qty = await _verified_holding_qty(trade.get("ticker"), "DB_OPEN_TRADE")
    if not actual_qty or actual_qty <= 0:
        logger.log(
            "PROCESS_RESTART_DETECTED",
            level="CRIT",
            recovered_status="DB_OPEN_TRADE_NO_ACTUAL_HOLDING",
            recovery_source="DB_OPEN_TRADE",
            trade_id=trade.get("id"),
            ticker=trade.get("ticker"),
            db_entry_qty=entry_qty,
            actual_qty=actual_qty,
            pyramided=trade.get("pyramided"),
        )
        await notifier.send(
            "PROCESS_RESTART_DETECTED",
            level="CRIT",
            message=(
                f"DB OPEN trade가 있지만 실제 보유 수량이 없습니다: "
                f"{trade.get('ticker')}. 자동 복구 중단."
            ),
            ticker=trade.get("ticker"),
        )
        return

    highest_step = float(trade.get("highest_step") or 0.0)
    restore_data = {
        "date": today,
        "ticker": trade.get("ticker"),
        "name": None,
        "target_candidates": [],
        "entry_price": entry_price,
        "entry_at": trade.get("entry_at"),
        "entry_qty": actual_qty,
        "remaining_qty": actual_qty,
        "high_price": trade.get("high_price") or entry_price,
        "trailing_active": highest_step >= f4_tracking.STEP_SIZE,
        "highest_step": highest_step,
        "trade_id": int(trade.get("id") or 0),
        "position_status": "HOLDING",
        "close_reason": None,
    }
    state.restore_from(restore_data)
    await state.persist(STATE_DIR, today)
    logger.log(
        "PROCESS_RESTART_DETECTED",
        level="CRIT",
        recovered_status="HOLDING_RESUMED",
        recovery_source="DB_OPEN_TRADE",
        trade_id=restore_data["trade_id"],
        actual_qty=actual_qty,
        db_entry_qty=entry_qty,
        pyramided=trade.get("pyramided"),
        high_price=restore_data["high_price"],
        highest_step=highest_step,
    )
    await notifier.send(
        "PROCESS_RESTART_DETECTED",
        level="CRIT",
        message=f"DB 기준 포지션 복구: {trade.get('ticker')} {actual_qty}주",
        ticker=trade.get("ticker"),
    )

# ── PID 파일 관리 ────────────────────────────────────────────────────

_pid_lock_file = None


def _read_pid(path: str = PID_PATH) -> int | None:
    try:
        raw = open(path, encoding="utf-8").read().strip()
        return int(raw) if raw else None
    except (OSError, ValueError):
        return None


def _try_lock_pid_file(handle) -> bool:
    try:
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False


def _unlock_pid_file(handle) -> None:
    try:
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


def _write_pid() -> bool:
    global _pid_lock_file
    if _pid_lock_file is not None:
        return True

    path = PID_PATH
    current_pid = os.getpid()
    try:
        handle = open(path, "a+", encoding="utf-8")
    except OSError as e:
        logger.log("PID_WRITE_ERROR", level="WARN", path=path, error=repr(e))
        return False

    if not _try_lock_pid_file(handle):
        existing_pid = _read_pid(path)
        logger.log(
            "PROCESS_ALREADY_RUNNING",
            level="CRIT",
            path=path,
            existing_pid=existing_pid,
            current_pid=current_pid,
        )
        handle.close()
        return False

    try:
        handle.seek(0)
        existing_raw = handle.read().strip()
        existing_pid = int(existing_raw) if existing_raw else None
    except ValueError:
        existing_pid = None

    if existing_pid and existing_pid != current_pid:
        logger.log(
            "STALE_PID_REPLACED",
            level="WARN",
            path=path,
            stale_pid=existing_pid,
            current_pid=current_pid,
        )

    try:
        handle.seek(0)
        handle.truncate()
        handle.write(str(current_pid))
        handle.flush()
        os.fsync(handle.fileno())
        _pid_lock_file = handle
        return True
    except OSError as e:
        _unlock_pid_file(handle)
        handle.close()
        logger.log("PID_WRITE_ERROR", level="WARN", path=path, error=repr(e))
        return False


def _clear_pid() -> None:
    global _pid_lock_file
    handle = _pid_lock_file
    if handle is None:
        return
    _pid_lock_file = None
    _unlock_pid_file(handle)
    handle.close()


# ── 메인 ─────────────────────────────────────────────────────────────

async def main() -> None:
    dry_run = os.getenv("DRY_RUN", "0") == "1"
    logger.setup(LOG_DIR)
    if not _write_pid():
        raise SystemExit(2)
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    await db.init(DB_PATH)
    await _ensure_trading_day()

    if dry_run:
        logger.log("DRY_RUN_START", level="WARN",
                   message="DRY_RUN=1: external auth, NTP, orders, and WebSocket are simulated")
    else:
        await auth.load_or_refresh()
        time_sync.check_ntp(NTP_SERVERS)
        # 휴장일에 기동해도 catchup·스케줄 잡이 돌지 않도록 시작 시점에 1회 확인
        await _check_market_holiday()
    await _recover_state()

    # F4: WebSocket 기반 장기 실행 (HOLDING 전까지 내부에서 대기)
    f4_task = asyncio.create_task(f4_tracking.run(), name="f4_tracking")

    # Telegram 알림 워커
    notifier_task = None
    if not dry_run:
        notifier_task = asyncio.create_task(notifier.worker(), name="notifier")

    # Web UI exposes account assets; bind to localhost unless explicitly opened.
    ui_port = int(os.getenv("UI_PORT", "8080"))
    ui_host = os.getenv("UI_HOST", "127.0.0.1")
    config = uvicorn.Config(server.app, host=ui_host, port=ui_port,
                            log_level="warning", loop="none")
    uvi = uvicorn.Server(config)
    uvi.install_signal_handlers = lambda: None  # uvicorn의 시그널 핸들러 비활성화
    ui_task = asyncio.create_task(uvi.serve(), name="ui_server")

    # Catchup은 F3 진입(실주문)까지 인라인 수행할 수 있으므로 F4 손절 추적·알림·UI가
    # 살아있는 상태에서 실행한다. 스케줄러보다는 먼저 완료해 예약 잡과의 중복 실행을 막는다.
    await _run_catchup()

    scheduler = None
    if not dry_run:
        scheduler = build(
            token_refresh=job_token_refresh,
            ntp_check=job_ntp_check,
            f1=job_f1,
            f2=job_f2,
            f3=job_f3,
            f5_precheck=job_f5_precheck,
            f5_exec=job_f5_exec,
        )
        scheduler.start()

    try:
        await asyncio.Event().wait()
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)
        uvi.should_exit = True          # uvicorn graceful 종료 신호
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(ui_task, timeout=2.0)
        tasks = [task for task in (f4_task, notifier_task) if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(*tasks)
        await db.close()
        _clear_pid()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
