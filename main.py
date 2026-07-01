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
from src.api import auth, server  # noqa: E402
from src.modules import f1_filter, f2_lockup, f3_entry, f4_tracking, f5_timeout  # noqa: E402
from src.scheduler import build, F1_H, F1_M, F2_H, F2_M, F3_H, F3_M, F3_S, F3_FILL_DEADLINE_H, F3_FILL_DEADLINE_M  # noqa: E402
from src.utils import logger, time_sync  # noqa: E402

KST = ZoneInfo("Asia/Seoul")

LOG_DIR = os.getenv("LOG_DIR", "data/logs")
STATE_DIR = os.getenv("STATE_DIR", "data/state")
NTP_SERVERS = [s.strip() for s in os.getenv("NTP_SERVER", "pool.ntp.org").split(",")]
DB_PATH = os.path.join(os.getenv("DB_DIR", "data/db"), "trading.db")

# F1 결과를 F2에 전달하기 위한 세션 변수
_f1_result: list[dict] = []


def _today() -> str:
    return datetime.now(KST).strftime("%Y%m%d")


async def _ensure_trading_day() -> None:
    global _f1_result
    today = _today()
    if await state.ensure_trading_day(today):
        _f1_result = []
        logger.log("DAILY_STATE_RESET", level="INFO", date=today)


# ── 스케줄 작업 래퍼 ─────────────────────────────────────────────────

async def job_token_refresh() -> None:
    await _ensure_trading_day()
    await auth.refresh()


async def job_ntp_check() -> None:
    await _ensure_trading_day()
    time_sync.check_ntp(NTP_SERVERS)


async def job_f1() -> None:
    global _f1_result
    await _ensure_trading_day()
    _f1_result = await f1_filter.run()


async def job_f2() -> None:
    await _ensure_trading_day()
    await f2_lockup.run(_f1_result)


async def job_f3() -> None:
    await _ensure_trading_day()
    await f3_entry.run()


async def job_f5_precheck() -> None:
    await _ensure_trading_day()
    await f5_timeout.precheck()


async def job_f5_exec() -> None:
    await _ensure_trading_day()
    await f5_timeout.execute()


# ── F1 missed 보완 실행 ──────────────────────────────────────────────

async def _run_catchup() -> None:
    """
    08:40~09:00 사이에 기동하면 F1(~F3)이 missed 상태.
    즉시 보완 실행해 당일 파이프라인을 복구한다.
    09:00 이후엔 F3 진입 마감이 지났으므로 catchup 불가.
    """
    await _ensure_trading_day()
    dry_run = os.getenv("DRY_RUN", "0") == "1"
    force = dry_run or os.getenv("FORCE_CATCHUP", "0") == "1"

    now = datetime.now(KST)
    f1_sched         = now.replace(hour=F1_H,                minute=F1_M,               second=0,    microsecond=0)
    f2_sched         = now.replace(hour=F2_H,                minute=F2_M,               second=0,    microsecond=0)
    f3_sched         = now.replace(hour=F3_H,                minute=F3_M,               second=F3_S, microsecond=0)
    f3_fill_deadline = now.replace(hour=F3_FILL_DEADLINE_H,  minute=F3_FILL_DEADLINE_M, second=0,    microsecond=0)

    if not force and not (f1_sched <= now < f3_fill_deadline):
        return

    logger.log("CATCHUP_START", level="WARN",
               message=f"{'[FORCE] ' if force else ''}F1 missed 감지. 보완 실행 ({now.strftime('%H:%M:%S')} 기동)")
    await notifier.send("CATCHUP_START", level="WARN",
                        message=f"{'[FORCE] ' if force else ''}F1 missed 감지 ({now.strftime('%H:%M:%S')} 기동). 보완 실행 중...")

    global _f1_result
    _f1_result = await f1_filter.run()

    now = datetime.now(KST)
    if force or now >= f2_sched:
        await f2_lockup.run(_f1_result)

    now = datetime.now(KST)
    if force or now >= f3_sched:
        await f3_entry.run(force=force)


# ── 재시작 복구 ──────────────────────────────────────────────────────

async def _recover_state() -> None:
    """
    프로세스 재시작 시 today_state.json으로 포지션 복구 (PRD §6-7).
    """
    data = state.load(STATE_DIR)
    today = datetime.now(KST).strftime("%Y%m%d")

    if data is None:
        # 상태 파일 없음 → KIS 잔고 직접 조회
        # TODO: KIS 잔고 조회 후 보유 종목 있으면 즉시 청산
        return

    if data.get("date") != today:
        await notifier.send("STALE_POSITION_DETECTED", level="CRIT",
                            message=f"전일 포지션 잔류 의심. date={data.get('date')}")
        return

    if data.get("position_status") == "HOLDING":
        # TODO: KIS 잔고 조회 → 실제 보유 수량 확인
        actual_qty = data.get("remaining_qty", 0)
        if actual_qty and actual_qty > 0:
            state.restore_from(data)
            logger.log("PROCESS_RESTART_DETECTED", level="WARN",
                       recovered_status="HOLDING_RESUMED", actual_qty=actual_qty)
            await notifier.send(
                "PROCESS_RESTART_DETECTED", level="WARN",
                message=f"재시작 감지. 포지션 복구: {data.get('ticker')} {actual_qty}주",
            )
        else:
            logger.log("PROCESS_RESTART_DETECTED", level="WARN",
                       recovered_status="ALREADY_CLOSED", actual_qty=0)


# ── PID 파일 관리 ────────────────────────────────────────────────────

def _write_pid() -> None:
    with open("main.pid", "w") as f:
        f.write(str(os.getpid()))


def _clear_pid() -> None:
    try:
        os.remove("main.pid")
    except FileNotFoundError:
        pass


# ── 메인 ─────────────────────────────────────────────────────────────

async def main() -> None:
    dry_run = os.getenv("DRY_RUN", "0") == "1"
    logger.setup(LOG_DIR)
    _write_pid()
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    await db.init(DB_PATH)
    await _ensure_trading_day()

    if dry_run:
        logger.log("DRY_RUN_START", level="WARN",
                   message="DRY_RUN=1: external auth, NTP, orders, and WebSocket are simulated")
    else:
        await auth.load_or_refresh()
        time_sync.check_ntp(NTP_SERVERS)
    await _recover_state()
    await _run_catchup()

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
