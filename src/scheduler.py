"""APScheduler 설정 — F1~F5 및 유지보수 작업 등록"""

from collections.abc import Callable, Coroutine
from typing import Any

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src.schedule_times import (  # noqa: F401 — main.py가 이 모듈 경유로 재사용
    F1_H,
    F1_M,
    F2_H,
    F2_M,
    F3_FILL_DEADLINE_H,
    F3_FILL_DEADLINE_M,
    F3_H,
    F3_M,
    F3_S,
    F5_EXEC_H,
    F5_EXEC_M,
    F5_EXEC_S,
    F5_PRECHECK_H,
    F5_PRECHECK_M,
    F5_PRECHECK_S,
)

KST = pytz.timezone("Asia/Seoul")

Job = Callable[[], Coroutine[Any, Any, None]]
MISFIRE_GRACE_TIME_SEC = 60

# KRX 개장 요일 — 시장 고정 특성이므로 env로 노출하지 않는다 (임시 공휴일은 별도)
TRADING_DAYS_OF_WEEK = "mon-fri"


def build(
    token_refresh: Job,
    ntp_check: Job,
    f1: Job,
    f2: Job,
    f3: Job,
    f5_precheck: Job,
    f5_exec: Job,
) -> AsyncIOScheduler:
    """
    스케줄러 빌드 후 반환. start()는 호출 측에서 수행.
    F4는 WebSocket 기반 장기 실행 태스크이므로 스케줄러가 아닌
    asyncio.create_task()로 별도 구동 (main.py 참조).
    """
    scheduler = AsyncIOScheduler(
        timezone=KST,
        job_defaults={
            "misfire_grace_time": MISFIRE_GRACE_TIME_SEC,
            "coalesce": True,
        },
    )

    def cron(**kwargs: Any) -> CronTrigger:
        return CronTrigger(timezone=KST, day_of_week=TRADING_DAYS_OF_WEEK, **kwargs)

    # 08:29:30 — KIS 토큰 선제 갱신, 08:30:10 — NTP 검증
    scheduler.add_job(token_refresh, cron(hour=8, minute=29, second=30), id="token_refresh")
    scheduler.add_job(ntp_check,     cron(hour=8, minute=30, second=10), id="ntp_check")

    # F1 — 갭/유동성 필터링
    scheduler.add_job(f1, cron(hour=F1_H, minute=F1_M, second=0), id="f1_filter")

    # F2 — 타겟 락업
    scheduler.add_job(f2, cron(hour=F2_H, minute=F2_M, second=0), id="f2_lockup")

    # F3 — 갭 재검증 → 진입 → 피라미딩 전 과정 포함
    scheduler.add_job(f3, cron(hour=F3_H, minute=F3_M, second=F3_S), id="f3_entry")

    # 10:59:50 — F5 Pre-Check
    scheduler.add_job(
        f5_precheck,
        cron(hour=F5_PRECHECK_H, minute=F5_PRECHECK_M, second=F5_PRECHECK_S),
        id="f5_precheck",
    )

    # 11:00:00 — F5 Execute
    scheduler.add_job(
        f5_exec,
        cron(hour=F5_EXEC_H, minute=F5_EXEC_M, second=F5_EXEC_S),
        id="f5_exec",
    )

    return scheduler
