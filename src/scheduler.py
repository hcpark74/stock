"""APScheduler 설정 — F1~F5 및 유지보수 작업 등록"""

from collections.abc import Callable, Coroutine
from typing import Any

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

KST = pytz.timezone("Asia/Seoul")

Job = Callable[[], Coroutine[Any, Any, None]]

# 스케줄 시각 — catchup 로직과 단일 출처 공유
F1_H, F1_M = 8, 40
F2_H, F2_M = 8, 58
F3_H, F3_M, F3_S = 8, 59, 40
F3_FILL_DEADLINE_H, F3_FILL_DEADLINE_M = 9, 0


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
    scheduler = AsyncIOScheduler(timezone=KST)

    def cron(**kwargs: Any) -> CronTrigger:
        return CronTrigger(timezone=KST, **kwargs)

    # 08:30 — KIS 토큰 갱신 / NTP 검증
    scheduler.add_job(token_refresh, cron(hour=8, minute=30, second=0),  id="token_refresh")
    scheduler.add_job(ntp_check,     cron(hour=8, minute=30, second=10), id="ntp_check")

    # F1 — 갭/유동성 필터링
    scheduler.add_job(f1, cron(hour=F1_H, minute=F1_M, second=0), id="f1_filter")

    # F2 — 타겟 락업
    scheduler.add_job(f2, cron(hour=F2_H, minute=F2_M, second=0), id="f2_lockup")

    # F3 — 갭 재검증 → 진입 → 피라미딩 전 과정 포함
    scheduler.add_job(f3, cron(hour=F3_H, minute=F3_M, second=F3_S), id="f3_entry")

    # 09:59:50 — F5 Pre-Check
    scheduler.add_job(f5_precheck, cron(hour=9, minute=59, second=50), id="f5_precheck")

    # 10:00:00 — F5 Execute
    scheduler.add_job(f5_exec, cron(hour=10, minute=0, second=0), id="f5_exec")

    return scheduler
