import src.scheduler as scheduler_mod
from src import schedule_times
from src.scheduler import MISFIRE_GRACE_TIME_SEC, build


async def _noop() -> None:
    pass


def test_scheduler_times_are_single_sourced_from_schedule_times():
    """스케줄 시각 상수는 schedule_times가 단일 출처 — 복제 시 UI 표시가 어긋난다."""
    for name in (
        "F1_H", "F1_M",
        "PAPER_FAST_PROBE_H", "PAPER_FAST_PROBE_M", "PAPER_FAST_PROBE_S",
        "BALANCE_PREFETCH_H", "BALANCE_PREFETCH_M", "BALANCE_PREFETCH_S",
        "F2_H", "F2_M", "F3_H", "F3_M", "F3_S",
        "F3_FILL_DEADLINE_H", "F3_FILL_DEADLINE_M",
        "F5_PRECHECK_H", "F5_PRECHECK_M", "F5_PRECHECK_S",
        "F5_EXEC_H", "F5_EXEC_M", "F5_EXEC_S",
    ):
        assert getattr(scheduler_mod, name) == getattr(schedule_times, name)


async def test_scheduler_jobs_allow_short_startup_misfires():
    scheduler = build(
        token_refresh=_noop,
        ntp_check=_noop,
        paper_fast_probe=_noop,
        balance_snapshot_prefetch=_noop,
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
            "paper_fast_probe",
            "balance_snapshot_prefetch",
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
        assert str(jobs["paper_fast_probe"].trigger) == (
            "cron[day_of_week='mon-fri', hour='8', minute='59', second='45']"
        )
        assert str(jobs["balance_snapshot_prefetch"].trigger) == (
            "cron[day_of_week='mon-fri', hour='8', minute='59', second='50']"
        )
    finally:
        scheduler.shutdown(wait=False)
