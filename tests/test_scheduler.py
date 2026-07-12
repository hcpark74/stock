from src.scheduler import MISFIRE_GRACE_TIME_SEC, build


async def _noop() -> None:
    pass


async def test_scheduler_jobs_allow_short_startup_misfires():
    scheduler = build(
        token_refresh=_noop,
        ntp_check=_noop,
        f1=_noop,
        f2=_noop,
        f3=_noop,
        f5_precheck=_noop,
        f5_exec=_noop,
    )
    scheduler.start(paused=True)
    try:
        jobs = {job.id: job for job in scheduler.get_jobs()}

        assert set(jobs) == {
            "token_refresh",
            "ntp_check",
            "f1_filter",
            "f2_lockup",
            "f3_entry",
            "f5_precheck",
            "f5_exec",
        }
        assert all(job.misfire_grace_time == MISFIRE_GRACE_TIME_SEC for job in jobs.values())
        assert all(job.coalesce is True for job in jobs.values())
        # 주말(토·일) 실행 방지 — 모든 잡은 월~금에만 트리거되어야 한다
        assert all("day_of_week='mon-fri'" in str(job.trigger) for job in jobs.values())
        assert str(jobs["token_refresh"].trigger) == (
            "cron[day_of_week='mon-fri', hour='8', minute='29', second='30']"
        )
        assert str(jobs["ntp_check"].trigger) == (
            "cron[day_of_week='mon-fri', hour='8', minute='30', second='10']"
        )
    finally:
        scheduler.shutdown(wait=False)
