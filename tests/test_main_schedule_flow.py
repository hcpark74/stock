from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

import main
from src import state as state_mod

_REAL_ENSURE_TRADING_DAY = main._ensure_trading_day


@pytest.fixture(autouse=True)
def reset_main_flow(monkeypatch):
    s = state_mod.get()
    s.trading_date = "20260702"
    s.day_skip = False
    s.close_reason = None
    s.target_ticker = None
    s.target_candidates = None
    s.position_status = "IDLE"
    main._f1_result = []
    main._f2_done = False
    main._f3_started = False
    main._market_closed_date = None
    monkeypatch.setattr(main, "_ensure_trading_day", AsyncMock())
    yield
    s.day_skip = False
    s.close_reason = None
    s.target_ticker = None
    s.target_candidates = None
    s.position_status = "IDLE"
    main._f1_result = []
    main._f2_done = False
    main._f3_started = False
    main._market_closed_date = None


async def test_restore_market_closed_from_db_sets_flag_and_day_skip(tmp_path):
    from src import db

    await db.init(str(tmp_path / "restore.db"))
    try:
        today = main._today()
        await db.record_skip(today, "MARKET_CLOSED", "msg_cd=40100000")

        await main._restore_market_closed_from_db()

        assert main._is_market_closed_today() is True
        assert state_mod.get().day_skip is True
        assert state_mod.get().close_reason == "MARKET_CLOSED"
    finally:
        await db.close()


async def test_restore_vi_active_day_skip_without_market_closed_flag(tmp_path):
    """VI_ACTIVE 스킵도 재시작 복원 대상 — 아니면 catchup이 F1~F3를 다시 돌려
    VI 해제가(추격 진입)로 들어간다. 휴장 플래그는 세우면 안 된다."""
    from src import db

    await db.init(str(tmp_path / "restore_vi.db"))
    try:
        await db.record_skip(main._today(), "VI_ACTIVE", "cntg_vi_hour=090032,vi_kind=2")

        await main._restore_market_closed_from_db()

        assert state_mod.get().day_skip is True
        assert state_mod.get().close_reason == "VI_ACTIVE"
        assert main._is_market_closed_today() is False
    finally:
        await db.close()


async def test_restore_market_closed_ignores_other_skip_reasons(tmp_path):
    from src import db

    await db.init(str(tmp_path / "restore2.db"))
    try:
        await db.record_skip(main._today(), "ENTRY_FAIL", "reason=NO_REMAINING_CANDIDATE")

        await main._restore_market_closed_from_db()

        assert main._is_market_closed_today() is False
        assert state_mod.get().day_skip is False
    finally:
        await db.close()


async def test_job_f1_runs_f3_without_force_before_f3_schedule(monkeypatch):
    async def fake_f2_run(candidates):
        assert candidates == [{"ticker": "005930"}]
        state_mod.get().target_ticker = "005930"

    f3_run = AsyncMock()
    monkeypatch.setattr(main.f1_filter, "run", AsyncMock(return_value=[{"ticker": "005930"}]))
    monkeypatch.setattr(main.f2_lockup, "run", fake_f2_run)
    monkeypatch.setattr(main.f3_entry, "run", f3_run)
    monkeypatch.setattr(main, "_past_f3_schedule", lambda: False)

    await main.job_f1()

    f3_run.assert_awaited_once_with(force=False)
    assert main._f2_done is True
    assert main._f3_started is True


async def test_job_f1_runs_f3_with_force_after_f3_schedule(monkeypatch):
    async def fake_f2_run(_candidates):
        state_mod.get().target_ticker = "005930"

    f3_run = AsyncMock()
    monkeypatch.setattr(main.f1_filter, "run", AsyncMock(return_value=[{"ticker": "005930"}]))
    monkeypatch.setattr(main.f2_lockup, "run", fake_f2_run)
    monkeypatch.setattr(main.f3_entry, "run", f3_run)
    monkeypatch.setattr(main, "_past_f3_schedule", lambda: True)

    await main.job_f1()

    f3_run.assert_awaited_once_with(force=True)


async def test_scheduled_f3_without_target_does_not_mark_started(monkeypatch):
    f3_run = AsyncMock()
    monkeypatch.setattr(main.f3_entry, "run", f3_run)

    await main.job_f3()

    f3_run.assert_not_awaited()
    assert main._f3_started is False


async def test_scheduled_f2_without_f1_result_does_not_mark_done(monkeypatch):
    f2_run = AsyncMock()
    monkeypatch.setattr(main.f2_lockup, "run", f2_run)

    await main.job_f2()

    f2_run.assert_not_awaited()
    assert main._f2_done is False


async def test_catchup_chains_f2_f3_before_scheduled_f2(monkeypatch):
    now = main.datetime.now(main.KST)

    def fake_scheduled_at(hour, minute, second=0):
        key = (hour, minute, second)
        if key == (main.F1_H, main.F1_M, 0):
            return now - timedelta(minutes=1)
        if key == (main.F3_H, main.F3_M, main.F3_S):
            return now + timedelta(minutes=8)
        if key == (main.F3_FILL_DEADLINE_H, main.F3_FILL_DEADLINE_M, 0):
            return now + timedelta(minutes=9)
        return now

    async def fake_f2_run(_candidates):
        state_mod.get().target_ticker = "005930"

    f3_run = AsyncMock()
    send = AsyncMock()
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.delenv("FORCE_CATCHUP", raising=False)
    monkeypatch.setattr(main, "_is_trading_weekday", lambda: True)
    monkeypatch.setattr(main, "_scheduled_at", fake_scheduled_at)
    monkeypatch.setattr(main.f1_filter, "run", AsyncMock(return_value=[{"ticker": "005930"}]))
    monkeypatch.setattr(main.f2_lockup, "run", fake_f2_run)
    monkeypatch.setattr(main.f3_entry, "run", f3_run)
    monkeypatch.setattr(main.notifier, "send", send)

    await main._run_catchup()

    main.f1_filter.run.assert_awaited_once()
    f3_run.assert_awaited_once_with(force=False)
    assert main._f2_done is True
    assert main._f3_started is True


async def test_holiday_check_marks_market_closed_and_skips_jobs(monkeypatch):
    send = AsyncMock()
    today = main._today()
    monkeypatch.setenv("KIS_MODE", "REAL")
    monkeypatch.setattr(
        main.kis_rest,
        "get",
        AsyncMock(return_value={
            "rt_cd": "0",
            "output": [{"bass_dt": today, "opnd_yn": "N"}],
        }),
    )
    monkeypatch.setattr(main.notifier, "send", send)
    f1_run = AsyncMock()
    monkeypatch.setattr(main.f1_filter, "run", f1_run)
    f5_precheck = AsyncMock()
    monkeypatch.setattr(main.f5_timeout, "precheck", f5_precheck)

    await main._check_market_holiday()

    assert main._is_market_closed_today() is True
    assert state_mod.get().day_skip is True
    send.assert_awaited_once()
    assert send.await_args.args[0] == "MARKET_CLOSED"

    await main.job_f1()
    await main.job_f5_precheck()

    f1_run.assert_not_awaited()
    f5_precheck.assert_not_awaited()


async def test_holiday_check_fails_open_on_api_error(monkeypatch):
    monkeypatch.setenv("KIS_MODE", "REAL")
    monkeypatch.setattr(
        main.kis_rest,
        "get",
        AsyncMock(return_value={"rt_cd": "1", "msg_cd": "EGW00001", "msg1": "error"}),
    )
    monkeypatch.setattr(main.notifier, "send", AsyncMock())

    await main._check_market_holiday()

    assert main._is_market_closed_today() is False
    assert state_mod.get().day_skip is False


async def test_stale_holiday_flag_does_not_block_f5_when_api_fails_next_day(monkeypatch):
    """[P1 회귀] 전일 휴장 플래그가 HOLDING으로 리셋되지 못한 채 남고,
    당일 휴장 API까지 일시 실패해도 — 플래그가 날짜에 바인딩되므로
    당일 잡(F5 청산)을 막지 않아야 한다."""
    monkeypatch.setenv("KIS_MODE", "REAL")
    main._market_closed_date = "20260101"  # 과거 휴장일에 세워진 플래그
    monkeypatch.setattr(main.kis_rest, "get", AsyncMock(side_effect=RuntimeError("kis down")))
    monkeypatch.setattr(main.notifier, "send", AsyncMock())

    await main._check_market_holiday()

    assert main._is_market_closed_today() is False

    f5_exec = AsyncMock()
    monkeypatch.setattr(main.f5_timeout, "execute", f5_exec)
    await main.job_f5_exec()
    f5_exec.assert_awaited_once()


async def test_holiday_check_open_day_keeps_jobs_running(monkeypatch):
    today = main._today()
    monkeypatch.setenv("KIS_MODE", "REAL")
    monkeypatch.setattr(
        main.kis_rest,
        "get",
        AsyncMock(return_value={
            "rt_cd": "0",
            "output": [{"bass_dt": today, "opnd_yn": "Y"}],
        }),
    )
    monkeypatch.setattr(main.notifier, "send", AsyncMock())

    await main._check_market_holiday()

    assert main._is_market_closed_today() is False
    assert state_mod.get().day_skip is False


async def test_holiday_check_clears_flag_on_open_day_so_f5_runs(monkeypatch):
    """개장(Y) 확인 시 휴장 플래그를 해제해 F5 청산이 정상 실행되어야 한다."""
    today = main._today()
    monkeypatch.setenv("KIS_MODE", "REAL")
    monkeypatch.setattr(
        main.kis_rest,
        "get",
        AsyncMock(return_value={
            "rt_cd": "0",
            "output": [{"bass_dt": today, "opnd_yn": "Y"}],
        }),
    )
    main._market_closed_date = main._today()

    await main._check_market_holiday()

    assert main._is_market_closed_today() is False

    f5_exec = AsyncMock()
    monkeypatch.setattr(main.f5_timeout, "execute", f5_exec)
    await main.job_f5_exec()
    f5_exec.assert_awaited_once()


async def test_holiday_check_does_not_duplicate_notification(monkeypatch):
    send = AsyncMock()
    today = main._today()
    monkeypatch.setenv("KIS_MODE", "REAL")
    monkeypatch.setattr(
        main.kis_rest,
        "get",
        AsyncMock(return_value={
            "rt_cd": "0",
            "output": [{"bass_dt": today, "opnd_yn": "N"}],
        }),
    )
    monkeypatch.setattr(main.notifier, "send", send)

    await main._check_market_holiday()
    await main._check_market_holiday()

    assert main._is_market_closed_today() is True
    send.assert_awaited_once()


async def test_holiday_check_fails_open_on_unexpected_opnd_yn(monkeypatch):
    today = main._today()
    monkeypatch.setenv("KIS_MODE", "REAL")
    monkeypatch.setattr(
        main.kis_rest,
        "get",
        AsyncMock(return_value={
            "rt_cd": "0",
            "output": [{"bass_dt": today, "opnd_yn": None}],
        }),
    )
    monkeypatch.setattr(main.notifier, "send", AsyncMock())

    await main._check_market_holiday()

    assert main._is_market_closed_today() is False
    assert state_mod.get().day_skip is False


async def test_trading_day_rollover_while_holding_leaves_stale_flag_inert(monkeypatch):
    s = state_mod.get()
    s.trading_date = "20260701"
    s.position_status = "HOLDING"
    main._market_closed_date = "20260701"  # 전일 휴장에 세워진 플래그
    monkeypatch.setattr(main, "_today", lambda: "20260702")

    await _REAL_ENSURE_TRADING_DAY()

    # HOLDING 중엔 일일 리셋이 막혀 플래그가 남지만,
    # 날짜 바인딩 덕에 새 거래일에는 적용되지 않는다.
    assert s.trading_date == "20260701"
    assert main._market_closed_date == "20260701"
    assert main._is_market_closed_today() is False


async def test_holiday_check_skipped_in_paper_mode(monkeypatch):
    kis_get = AsyncMock()
    monkeypatch.setenv("KIS_MODE", "PAPER")
    monkeypatch.setattr(main.kis_rest, "get", kis_get)

    await main._check_market_holiday()

    kis_get.assert_not_awaited()
    assert main._is_market_closed_today() is False


async def test_catchup_skips_when_market_closed(monkeypatch):
    f1_run = AsyncMock(return_value=[{"ticker": "005930"}])
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.delenv("FORCE_CATCHUP", raising=False)
    monkeypatch.setattr(main, "_is_trading_weekday", lambda: True)
    monkeypatch.setattr(main.db, "get_trade_by_date", AsyncMock(return_value=None))
    monkeypatch.setattr(main.f1_filter, "run", f1_run)
    monkeypatch.setattr(main.notifier, "send", AsyncMock())
    main._market_closed_date = main._today()

    await main._run_catchup()

    f1_run.assert_not_awaited()
    assert main._f2_done is False
    assert main._f3_started is False


async def test_catchup_skips_on_weekend(monkeypatch):
    f1_run = AsyncMock(return_value=[{"ticker": "005930"}])
    f2_run = AsyncMock()
    f3_run = AsyncMock()
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.delenv("FORCE_CATCHUP", raising=False)
    monkeypatch.setattr(main, "_is_trading_weekday", lambda: False)
    monkeypatch.setattr(main.db, "get_trade_by_date", AsyncMock(return_value=None))
    monkeypatch.setattr(main.f1_filter, "run", f1_run)
    monkeypatch.setattr(main.f2_lockup, "run", f2_run)
    monkeypatch.setattr(main.f3_entry, "run", f3_run)
    monkeypatch.setattr(main.notifier, "send", AsyncMock())

    await main._run_catchup()

    f1_run.assert_not_awaited()
    f2_run.assert_not_awaited()
    f3_run.assert_not_awaited()
    assert main._f2_done is False
    assert main._f3_started is False


async def test_catchup_with_empty_f1_result_skips_f2_f3(monkeypatch):
    now = main.datetime.now(main.KST)

    def fake_scheduled_at(hour, minute, second=0):
        key = (hour, minute, second)
        if key == (main.F1_H, main.F1_M, 0):
            return now - timedelta(minutes=1)
        if key == (main.F3_H, main.F3_M, main.F3_S):
            return now + timedelta(minutes=8)
        if key == (main.F3_FILL_DEADLINE_H, main.F3_FILL_DEADLINE_M, 0):
            return now + timedelta(minutes=9)
        return now

    f2_run = AsyncMock()
    f3_run = AsyncMock()
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.delenv("FORCE_CATCHUP", raising=False)
    monkeypatch.setattr(main, "_is_trading_weekday", lambda: True)
    monkeypatch.setattr(main, "_scheduled_at", fake_scheduled_at)
    monkeypatch.setattr(main.f1_filter, "run", AsyncMock(return_value=[]))
    monkeypatch.setattr(main.f2_lockup, "run", f2_run)
    monkeypatch.setattr(main.f3_entry, "run", f3_run)
    monkeypatch.setattr(main.notifier, "send", AsyncMock())

    await main._run_catchup()

    f2_run.assert_not_awaited()
    f3_run.assert_not_awaited()
    assert main._f2_done is False
    assert main._f3_started is False


async def test_f2_failure_retries_f1_before_deadline(monkeypatch):
    async def fake_f2_run(_candidates):
        if fake_f2_run.calls == 0:
            fake_f2_run.calls += 1
            state_mod.get().day_skip = True
            return
        state_mod.get().target_ticker = "005930"

    fake_f2_run.calls = 0
    f3_run = AsyncMock()
    sleep = AsyncMock()
    send = AsyncMock()
    main._f1_result = [{"ticker": "VI_NEAR"}]

    monkeypatch.setattr(main, "F2_RETRY_F1_ON_FAIL", True)
    monkeypatch.setattr(main, "_before_f1_retry_deadline", lambda: True)
    monkeypatch.setattr(main, "_f2_retry_remaining_seconds", lambda: 30)
    monkeypatch.setattr(main, "_f2_retry_sleep_seconds", lambda: 1)
    monkeypatch.setattr(main.asyncio, "sleep", sleep)
    monkeypatch.setattr(main.f1_filter, "run", AsyncMock(return_value=[{"ticker": "005930"}]))
    monkeypatch.setattr(main.f2_lockup, "run", fake_f2_run)
    monkeypatch.setattr(main.f3_entry, "run", f3_run)
    monkeypatch.setattr(main.notifier, "send", send)

    await main._run_f2_f3_after_f1()

    sleep.assert_awaited_once_with(1)
    main.f1_filter.run.assert_awaited_once()
    send.assert_awaited_once()
    assert send.await_args.args[0] == "F2_FAIL_F1_RETRY"
    f3_run.assert_awaited_once_with(force=False)
    assert main._f2_done is True
    assert main._f3_started is True
    assert state_mod.get().day_skip is False


async def test_f2_failure_after_deadline_does_not_retry_f1(monkeypatch):
    async def fake_f2_run(_candidates):
        state_mod.get().day_skip = True

    f3_run = AsyncMock()
    main._f1_result = [{"ticker": "VI_NEAR"}]

    monkeypatch.setattr(main, "F2_RETRY_F1_ON_FAIL", True)
    monkeypatch.setattr(main, "_before_f1_retry_deadline", lambda: False)
    monkeypatch.setattr(main.f1_filter, "run", AsyncMock())
    monkeypatch.setattr(main.f2_lockup, "run", fake_f2_run)
    monkeypatch.setattr(main.f3_entry, "run", f3_run)

    await main._run_f2_f3_after_f1()

    main.f1_filter.run.assert_not_awaited()
    f3_run.assert_not_awaited()
    assert main._f2_done is True
    assert state_mod.get().day_skip is True


async def test_f2_retry_marks_done_when_second_f1_finishes_with_day_skip(monkeypatch):
    async def fake_f2_run(_candidates):
        state_mod.get().day_skip = True

    async def fake_f1_run():
        state_mod.get().day_skip = True
        return []

    f3_run = AsyncMock()
    sleep = AsyncMock()
    send = AsyncMock()
    main._f1_result = [{"ticker": "VI_NEAR"}]

    monkeypatch.setattr(main, "F2_RETRY_F1_ON_FAIL", True)
    monkeypatch.setattr(main, "_before_f1_retry_deadline", lambda: True)
    monkeypatch.setattr(main, "_f2_retry_remaining_seconds", lambda: 30)
    monkeypatch.setattr(main, "_f2_retry_sleep_seconds", lambda: 1)
    monkeypatch.setattr(main.asyncio, "sleep", sleep)
    monkeypatch.setattr(main.f1_filter, "run", AsyncMock(side_effect=fake_f1_run))
    monkeypatch.setattr(main.f2_lockup, "run", fake_f2_run)
    monkeypatch.setattr(main.f3_entry, "run", f3_run)
    monkeypatch.setattr(main.notifier, "send", send)

    await main._run_f2_f3_after_f1()

    sleep.assert_awaited_once_with(1)
    main.f1_filter.run.assert_awaited_once()
    assert [call.args[0] for call in send.await_args_list] == [
        "F2_FAIL_F1_RETRY",
        "F2_RETRY_EXHAUSTED",
    ]
    f3_run.assert_not_awaited()
    assert main._f1_result == []
    assert main._f2_done is True
    assert main._f3_started is False
    assert state_mod.get().day_skip is True


async def test_f2_retry_exhausted_sent_when_second_f2_cannot_retry_again(monkeypatch):
    async def fake_f2_run(_candidates):
        state_mod.get().day_skip = True

    f3_run = AsyncMock()
    sleep = AsyncMock()
    send = AsyncMock()
    main._f1_result = [{"ticker": "VI_NEAR"}]
    retry_checks = iter([True, False])

    monkeypatch.setattr(main, "F2_RETRY_F1_ON_FAIL", True)
    monkeypatch.setattr(main, "_before_f1_retry_deadline", lambda: next(retry_checks))
    monkeypatch.setattr(main, "_f2_retry_remaining_seconds", lambda: 30)
    monkeypatch.setattr(main, "_f2_retry_sleep_seconds", lambda: 1)
    monkeypatch.setattr(main.asyncio, "sleep", sleep)
    monkeypatch.setattr(main.f1_filter, "run", AsyncMock(return_value=[{"ticker": "STILL_BAD"}]))
    monkeypatch.setattr(main.f2_lockup, "run", fake_f2_run)
    monkeypatch.setattr(main.f3_entry, "run", f3_run)
    monkeypatch.setattr(main.notifier, "send", send)

    await main._run_f2_f3_after_f1()

    sleep.assert_awaited_once_with(1)
    main.f1_filter.run.assert_awaited_once()
    assert [call.args[0] for call in send.await_args_list] == [
        "F2_FAIL_F1_RETRY",
        "F2_RETRY_EXHAUSTED",
    ]
    f3_run.assert_not_awaited()
    assert main._f2_done is True
    assert main._f3_started is False
    assert state_mod.get().day_skip is True


async def test_f2_failure_does_not_retry_f1_when_deadline_is_too_close(monkeypatch):
    async def fake_f2_run(_candidates):
        state_mod.get().day_skip = True

    main._f1_result = [{"ticker": "VI_NEAR"}]
    monkeypatch.setattr(main, "F2_RETRY_F1_ON_FAIL", True)
    monkeypatch.setattr(main, "F2_RETRY_F1_MIN_REMAINING_SEC", 2)
    monkeypatch.setattr(main, "_before_f1_retry_deadline", lambda: True)
    monkeypatch.setattr(main, "_f2_retry_remaining_seconds", lambda: 1)
    monkeypatch.setattr(main.f1_filter, "run", AsyncMock())
    monkeypatch.setattr(main.f2_lockup, "run", fake_f2_run)

    await main._run_f2_f3_after_f1()

    main.f1_filter.run.assert_not_awaited()
    assert main._f2_done is True
    assert state_mod.get().day_skip is True


async def test_f2_failure_does_not_retry_f1_in_dry_run(monkeypatch):
    async def fake_f2_run(_candidates):
        state_mod.get().day_skip = True

    main._f1_result = [{"ticker": "VI_NEAR"}]
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setattr(main, "F2_RETRY_F1_ON_FAIL", True)
    monkeypatch.setattr(main, "_before_f1_retry_deadline", lambda: True)
    monkeypatch.setattr(main, "_f2_retry_remaining_seconds", lambda: 30)
    monkeypatch.setattr(main.f1_filter, "run", AsyncMock())
    monkeypatch.setattr(main.f2_lockup, "run", fake_f2_run)

    await main._run_f2_f3_after_f1()

    main.f1_filter.run.assert_not_awaited()
    assert main._f2_done is True
    assert state_mod.get().day_skip is True


async def test_scheduled_f2_and_f3_do_not_duplicate_completed_chain(monkeypatch):
    f2_run = AsyncMock()
    f3_run = AsyncMock()
    main._f1_result = [{"ticker": "005930"}]
    main._f2_done = True
    main._f3_started = True
    state_mod.get().target_ticker = "005930"

    monkeypatch.setattr(main.f2_lockup, "run", f2_run)
    monkeypatch.setattr(main.f3_entry, "run", f3_run)

    await main.job_f2()
    await main.job_f3()

    f2_run.assert_not_awaited()
    f3_run.assert_not_awaited()


async def test_scheduled_f3_with_locked_target_is_fallback_path(monkeypatch):
    f3_run = AsyncMock()
    state_mod.get().target_ticker = "005930"
    monkeypatch.setattr(main.f3_entry, "run", f3_run)

    await main.job_f3()

    f3_run.assert_awaited_once_with()
    assert main._f3_started is True


async def test_trading_day_rollover_resets_chain_flags(monkeypatch):
    s = state_mod.get()
    s.trading_date = "20260701"
    s.target_ticker = "005930"
    main._f1_result = [{"ticker": "005930"}]
    main._f2_done = True
    main._f3_started = True
    main._market_closed_date = main._today()

    monkeypatch.setattr(main, "_today", lambda: "20260702")

    await _REAL_ENSURE_TRADING_DAY()

    assert s.trading_date == "20260702"
    assert s.target_ticker is None
    assert main._f1_result == []
    assert main._f2_done is False
    assert main._f3_started is False
    assert main._is_market_closed_today() is False

async def test_recover_state_uses_db_open_trade_when_state_file_missing(monkeypatch):
    events = []
    send = AsyncMock()
    persist = AsyncMock()
    s = state_mod.get()
    s.position_status = "IDLE"

    today = main.datetime.now(main.KST).strftime("%Y%m%d")
    monkeypatch.setattr(main.state, "load", lambda _state_dir: None)
    monkeypatch.setattr(
        main.db,
        "get_trade_by_date",
        AsyncMock(return_value={
            "id": 77,
            "date": today,
            "ticker": "005930",
            "name": "삼성전자",
            "entry_price": 75000.0,
            "entry_qty": 10,
            "entry_at": "2026-07-02T09:01:00+09:00",
            "high_price": 78000.0,
            "highest_step": 0.05,
            "status": "OPEN",
        }),
    )
    monkeypatch.setattr(main.state, "persist", persist)
    monkeypatch.setattr(
        main.kis_rest,
        "get",
        AsyncMock(return_value={"output1": [{"pdno": "005930", "hldg_qty": "10"}]}),
    )
    monkeypatch.setattr(main.notifier, "send", send)
    monkeypatch.setattr(main.logger, "log", lambda event, **kwargs: events.append((event, kwargs)))

    await main._recover_state()

    assert s.position_status == "HOLDING"
    assert s.target_ticker == "005930"
    assert s.target_name == "삼성전자"
    assert s.entry_price == 75000.0
    assert s.remaining_qty == 10
    assert s.high_price == 78000.0
    assert s.highest_step == 0.05
    assert s.trailing_active is True
    assert s.trade_id == 77
    persist.assert_awaited_once()
    send.assert_awaited_once()
    assert send.await_args.args[0] == "PROCESS_RESTART_DETECTED"
    restart = [kwargs for event, kwargs in events if event == "PROCESS_RESTART_DETECTED"][-1]
    assert restart["recovery_source"] == "DB_OPEN_TRADE"


async def test_recover_state_prefers_holding_state_file_over_db(monkeypatch):
    send = AsyncMock()
    get_trade = AsyncMock()
    s = state_mod.get()
    today = main.datetime.now(main.KST).strftime("%Y%m%d")
    data = {
        "date": today,
        "ticker": "000660",
        "entry_price": 120000.0,
        "entry_qty": 3,
        "remaining_qty": 3,
        "high_price": 121000.0,
        "trailing_active": False,
        "highest_step": 0.0,
        "trade_id": 12,
        "position_status": "HOLDING",
    }

    monkeypatch.setattr(main.state, "load", lambda _state_dir: data)
    monkeypatch.setattr(main.db, "get_trade_by_date", get_trade)
    monkeypatch.setattr(
        main.kis_rest,
        "get",
        AsyncMock(return_value={"output1": [{"pdno": "000660", "hldg_qty": "3"}]}),
    )
    monkeypatch.setattr(main.notifier, "send", send)
    monkeypatch.setattr(main.logger, "log", lambda *args, **kwargs: None)

    await main._recover_state()

    assert s.position_status == "HOLDING"
    assert s.target_ticker == "000660"
    assert s.trade_id == 12
    get_trade.assert_not_awaited()
    send.assert_awaited_once()

async def test_recover_state_terminal_state_file_skips_db_fallback(monkeypatch):
    events = []
    get_trade = AsyncMock(return_value={
        "id": 88,
        "ticker": "005930",
        "entry_price": 75000.0,
        "entry_qty": 10,
        "status": "OPEN",
    })
    today = main.datetime.now(main.KST).strftime("%Y%m%d")
    data = {
        "date": today,
        "ticker": "005930",
        "entry_price": 75000.0,
        "entry_qty": 10,
        "remaining_qty": 0,
        "position_status": "CLOSED",
        "close_reason": "TRAILING",
    }

    monkeypatch.setattr(main.state, "load", lambda _state_dir: data)
    monkeypatch.setattr(main.db, "get_trade_by_date", get_trade)
    monkeypatch.setattr(main.logger, "log", lambda event, **kwargs: events.append((event, kwargs)))

    await main._recover_state()

    # 당일 CLOSED 상태는 복원한다 — 재시작 후에도 UI가 청산 차트/마커를 유지하고,
    # CLOSED 상태가 당일 재진입도 막는다. DB fallback은 여전히 생략.
    assert state_mod.get().position_status == "CLOSED"
    assert state_mod.get().target_ticker == "005930"
    assert state_mod.get().close_reason == "TRAILING"
    get_trade.assert_not_awaited()
    skipped = [kwargs for event, kwargs in events if event == "PROCESS_RESTART_DETECTED"][-1]
    assert skipped["recovered_status"] == "STATE_FILE_TERMINAL_SKIP_DB_FALLBACK"


async def test_recover_state_db_open_trade_without_actual_holding_does_not_restore(monkeypatch):
    events = []
    send = AsyncMock()
    persist = AsyncMock()
    today = main.datetime.now(main.KST).strftime("%Y%m%d")

    monkeypatch.setattr(main.state, "load", lambda _state_dir: None)
    monkeypatch.setattr(
        main.db,
        "get_trade_by_date",
        AsyncMock(return_value={
            "id": 77,
            "date": today,
            "ticker": "005930",
            "entry_price": 75000.0,
            "entry_qty": 10,
            "entry_at": "2026-07-02T09:01:00+09:00",
            "high_price": 78000.0,
            "highest_step": 0.05,
            "pyramided": 0,
            "status": "OPEN",
        }),
    )
    monkeypatch.setattr(
        main.kis_rest,
        "get",
        AsyncMock(return_value={"output1": [{"pdno": "005930", "hldg_qty": "0"}]}),
    )
    monkeypatch.setattr(main.state, "persist", persist)
    monkeypatch.setattr(main.notifier, "send", send)
    monkeypatch.setattr(main.logger, "log", lambda event, **kwargs: events.append((event, kwargs)))

    await main._recover_state()

    assert state_mod.get().position_status == "IDLE"
    persist.assert_not_awaited()
    send.assert_awaited_once()
    blocked = [kwargs for event, kwargs in events if event == "PROCESS_RESTART_DETECTED"][-1]
    assert blocked["recovered_status"] == "DB_OPEN_TRADE_NO_ACTUAL_HOLDING"


async def test_recover_state_db_open_trade_uses_actual_holding_qty_for_pyramid(monkeypatch):
    persist = AsyncMock()
    today = main.datetime.now(main.KST).strftime("%Y%m%d")

    monkeypatch.setattr(main.state, "load", lambda _state_dir: None)
    monkeypatch.setattr(
        main.db,
        "get_trade_by_date",
        AsyncMock(return_value={
            "id": 77,
            "date": today,
            "ticker": "005930",
            "entry_price": 75000.0,
            "entry_qty": 70,
            "entry_at": "2026-07-02T09:01:00+09:00",
            "high_price": 78000.0,
            "highest_step": 0.05,
            "pyramided": 1,
            "status": "OPEN",
        }),
    )
    monkeypatch.setattr(
        main.kis_rest,
        "get",
        AsyncMock(return_value={"output1": [{"pdno": "005930", "hldg_qty": "100"}]}),
    )
    monkeypatch.setattr(main.state, "persist", persist)
    monkeypatch.setattr(main.notifier, "send", AsyncMock())
    monkeypatch.setattr(main.logger, "log", lambda *args, **kwargs: None)

    await main._recover_state()

    assert state_mod.get().position_status == "HOLDING"
    assert state_mod.get().entry_qty == 100
    assert state_mod.get().remaining_qty == 100
    persist.assert_awaited_once()

async def test_recover_state_idle_state_without_qty_allows_db_fallback(monkeypatch):
    persist = AsyncMock()
    today = main.datetime.now(main.KST).strftime("%Y%m%d")
    data = {
        "date": today,
        "ticker": None,
        "position_status": "IDLE",
    }

    monkeypatch.setattr(main.state, "load", lambda _state_dir: data)
    monkeypatch.setattr(
        main.db,
        "get_trade_by_date",
        AsyncMock(return_value={
            "id": 77,
            "date": today,
            "ticker": "005930",
            "entry_price": 75000.0,
            "entry_qty": 10,
            "entry_at": "2026-07-02T09:01:00+09:00",
            "high_price": 78000.0,
            "highest_step": 0.05,
            "pyramided": 0,
            "status": "OPEN",
        }),
    )
    monkeypatch.setattr(
        main.kis_rest,
        "get",
        AsyncMock(return_value={"output1": [{"pdno": "005930", "hldg_qty": "10"}]}),
    )
    monkeypatch.setattr(main.state, "persist", persist)
    monkeypatch.setattr(main.notifier, "send", AsyncMock())
    monkeypatch.setattr(main.logger, "log", lambda *args, **kwargs: None)

    await main._recover_state()

    assert state_mod.get().position_status == "HOLDING"
    assert state_mod.get().remaining_qty == 10
    persist.assert_awaited_once()

async def test_recover_state_stale_state_file_allows_today_db_fallback(monkeypatch):
    send = AsyncMock()
    persist = AsyncMock()
    today = main.datetime.now(main.KST).strftime("%Y%m%d")
    data = {
        "date": "20260701",
        "ticker": "000660",
        "remaining_qty": 5,
        "position_status": "HOLDING",
    }

    monkeypatch.setattr(main.state, "load", lambda _state_dir: data)
    monkeypatch.setattr(
        main.db,
        "get_trade_by_date",
        AsyncMock(return_value={
            "id": 77,
            "date": today,
            "ticker": "005930",
            "entry_price": 75000.0,
            "entry_qty": 10,
            "entry_at": "2026-07-02T09:01:00+09:00",
            "high_price": 78000.0,
            "highest_step": 0.05,
            "pyramided": 0,
            "status": "OPEN",
        }),
    )
    monkeypatch.setattr(
        main.kis_rest,
        "get",
        AsyncMock(return_value={"output1": [{"pdno": "005930", "hldg_qty": "10"}]}),
    )
    monkeypatch.setattr(main.state, "persist", persist)
    monkeypatch.setattr(main.notifier, "send", send)
    monkeypatch.setattr(main.logger, "log", lambda *args, **kwargs: None)

    await main._recover_state()

    assert state_mod.get().position_status == "HOLDING"
    assert state_mod.get().target_ticker == "005930"
    assert send.await_count == 2
    assert send.await_args_list[0].args[0] == "STALE_POSITION_DETECTED"
    assert send.await_args_list[1].args[0] == "PROCESS_RESTART_DETECTED"
    persist.assert_awaited_once()

async def test_recover_state_stale_holding_blocks_todays_entry(monkeypatch):
    """전일 상태가 HOLDING이면 계좌 확인 전까지 당일 자동 진입을 차단한다."""
    send = AsyncMock()
    data = {
        "date": "20260713",
        "ticker": "000660",
        "remaining_qty": 5,
        "position_status": "HOLDING",
    }
    monkeypatch.setattr(main.state, "load", lambda _state_dir: data)
    monkeypatch.setattr(main.db, "get_trade_by_date", AsyncMock(return_value=None))
    monkeypatch.setattr(main.notifier, "send", send)
    monkeypatch.setattr(main.logger, "log", lambda *args, **kwargs: None)

    await main._recover_state()

    assert state_mod.get().day_skip is True
    assert send.await_args_list[0].args[0] == "STALE_POSITION_DETECTED"
    assert "차단" in send.await_args_list[0].kwargs["message"]


async def test_recover_state_stale_closed_does_not_block_entry(monkeypatch):
    """전일 상태가 CLOSED(정상 청산)면 알림만 보내고 당일 거래는 진행한다."""
    send = AsyncMock()
    data = {
        "date": "20260713",
        "ticker": "000660",
        "remaining_qty": 5,
        "position_status": "CLOSED",
    }
    monkeypatch.setattr(main.state, "load", lambda _state_dir: data)
    monkeypatch.setattr(main.db, "get_trade_by_date", AsyncMock(return_value=None))
    monkeypatch.setattr(main.notifier, "send", send)
    monkeypatch.setattr(main.logger, "log", lambda *args, **kwargs: None)

    await main._recover_state()

    assert state_mod.get().day_skip is False
    assert send.await_args_list[0].args[0] == "STALE_POSITION_DETECTED"


async def test_recover_state_holding_state_rt_cd_error_sends_alert_and_skips_restore(monkeypatch):
    events = []
    send = AsyncMock()
    today = main.datetime.now(main.KST).strftime("%Y%m%d")
    data = {
        "date": today,
        "ticker": "000660",
        "entry_price": 120000.0,
        "entry_qty": 3,
        "remaining_qty": 3,
        "high_price": 121000.0,
        "trailing_active": False,
        "highest_step": 0.0,
        "trade_id": 12,
        "position_status": "HOLDING",
    }

    monkeypatch.setattr(main.state, "load", lambda _state_dir: data)
    monkeypatch.setattr(
        main.kis_rest,
        "get",
        AsyncMock(return_value={"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "token expired"}),
    )
    monkeypatch.setattr(main.notifier, "send", send)
    monkeypatch.setattr(main.logger, "log", lambda event, **kwargs: events.append((event, kwargs)))

    await main._recover_state()

    assert state_mod.get().position_status == "IDLE"
    send.assert_awaited_once()
    assert send.await_args.args[0] == "PROCESS_RESTART_DETECTED"
    statuses = [kwargs.get("recovered_status")
                for event, kwargs in events if event == "PROCESS_RESTART_DETECTED"]
    assert "HOLDING_VERIFY_FAILED" in statuses
    assert "HOLDING_VERIFY_FAILED_SKIP_RESTORE" in statuses


async def test_recover_state_db_open_trade_rt_cd_error_sends_alert_and_skips_restore(monkeypatch):
    events = []
    send = AsyncMock()
    persist = AsyncMock()
    today = main.datetime.now(main.KST).strftime("%Y%m%d")

    monkeypatch.setattr(main.state, "load", lambda _state_dir: None)
    monkeypatch.setattr(
        main.db,
        "get_trade_by_date",
        AsyncMock(return_value={
            "id": 77,
            "date": today,
            "ticker": "005930",
            "entry_price": 75000.0,
            "entry_qty": 10,
            "entry_at": "2026-07-02T09:01:00+09:00",
            "high_price": 78000.0,
            "highest_step": 0.05,
            "pyramided": 0,
            "status": "OPEN",
        }),
    )
    monkeypatch.setattr(
        main.kis_rest,
        "get",
        AsyncMock(return_value={"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "token expired"}),
    )
    monkeypatch.setattr(main.state, "persist", persist)
    monkeypatch.setattr(main.notifier, "send", send)
    monkeypatch.setattr(main.logger, "log", lambda event, **kwargs: events.append((event, kwargs)))

    await main._recover_state()

    assert state_mod.get().position_status == "IDLE"
    persist.assert_not_awaited()
    send.assert_awaited()
    assert send.await_args_list[0].args[0] == "PROCESS_RESTART_DETECTED"
    statuses = [kwargs.get("recovered_status")
                for event, kwargs in events if event == "PROCESS_RESTART_DETECTED"]
    assert "HOLDING_VERIFY_FAILED" in statuses
    assert "DB_OPEN_TRADE_NO_ACTUAL_HOLDING" in statuses


async def test_catchup_skips_f1_f2_f3_when_today_trade_exists(monkeypatch):
    now = main.datetime.now(main.KST)

    def fake_scheduled_at(hour, minute, second=0):
        key = (hour, minute, second)
        if key == (main.F1_H, main.F1_M, 0):
            return now - timedelta(minutes=1)
        if key == (main.F3_H, main.F3_M, main.F3_S):
            return now + timedelta(minutes=8)
        if key == (main.F3_FILL_DEADLINE_H, main.F3_FILL_DEADLINE_M, 0):
            return now + timedelta(minutes=9)
        return now

    f1_run = AsyncMock(return_value=[{"ticker": "005930"}])
    f2_run = AsyncMock()
    f3_run = AsyncMock()
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.delenv("FORCE_CATCHUP", raising=False)
    monkeypatch.setattr(main, "_is_trading_weekday", lambda: True)
    monkeypatch.setattr(main, "_scheduled_at", fake_scheduled_at)
    monkeypatch.setattr(
        main.db, "get_trade_by_date", AsyncMock(return_value={"id": 7, "ticker": "365660"}))
    monkeypatch.setattr(main.f1_filter, "run", f1_run)
    monkeypatch.setattr(main.f2_lockup, "run", f2_run)
    monkeypatch.setattr(main.f3_entry, "run", f3_run)
    monkeypatch.setattr(main.notifier, "send", AsyncMock())

    await main._run_catchup()

    f1_run.assert_not_awaited()
    f2_run.assert_not_awaited()
    f3_run.assert_not_awaited()
    assert main._f2_done is True
    assert main._f3_started is True
    assert state_mod.get().day_skip is True
    assert state_mod.get().close_reason == "TRADE_ALREADY_EXISTS"


async def test_job_f1_skips_when_today_trade_exists(monkeypatch):
    f1_run = AsyncMock(return_value=[{"ticker": "005930"}])
    monkeypatch.setattr(
        main.db, "get_trade_by_date", AsyncMock(return_value={"id": 7, "ticker": "365660"}))
    monkeypatch.setattr(main.f1_filter, "run", f1_run)
    monkeypatch.setattr(main.f3_entry, "run", AsyncMock())

    await main.job_f1()

    f1_run.assert_not_awaited()
    assert main._f2_done is True
    assert main._f3_started is True


async def test_main_spawns_resident_f4_loop(monkeypatch):
    """[2026-07-16 인시던트 재발 방지] main()은 일회성 run()이 아니라
    상주 루프 run_forever()로 F4 태스크를 띄워야 한다."""
    import asyncio
    import contextlib

    started = asyncio.Event()

    async def fake_run_forever():
        started.set()
        await asyncio.Event().wait()

    class FakeUviServer:
        def __init__(self, _config):
            self.should_exit = False

        async def serve(self):
            while not self.should_exit:
                await asyncio.sleep(0.01)

    run_once = AsyncMock()
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setattr(main.logger, "setup", lambda *a, **k: None)
    monkeypatch.setattr(main.logger, "log", lambda *a, **k: None)
    monkeypatch.setattr(main, "_write_pid", lambda: True)
    monkeypatch.setattr(main, "_clear_pid", lambda: None)
    monkeypatch.setattr(main.db, "init", AsyncMock())
    monkeypatch.setattr(main.db, "close", AsyncMock())
    monkeypatch.setattr(main, "_recover_state", AsyncMock())
    monkeypatch.setattr(main, "_run_catchup", AsyncMock())
    monkeypatch.setattr(main.f4_tracking, "run", run_once)
    monkeypatch.setattr(main.f4_tracking, "run_forever", fake_run_forever, raising=False)
    monkeypatch.setattr(main.uvicorn, "Config", lambda *a, **k: None)
    monkeypatch.setattr(main.uvicorn, "Server", FakeUviServer)

    task = asyncio.create_task(main.main())
    try:
        await asyncio.wait_for(started.wait(), 1)
        run_once.assert_not_called()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
            await asyncio.wait_for(task, 5)
