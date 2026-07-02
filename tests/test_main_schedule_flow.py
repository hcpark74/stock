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
    s.target_ticker = None
    s.target_candidates = None
    s.position_status = "IDLE"
    main._f1_result = []
    main._f2_done = False
    main._f3_started = False
    monkeypatch.setattr(main, "_ensure_trading_day", AsyncMock())
    yield
    s.day_skip = False
    s.target_ticker = None
    s.target_candidates = None
    s.position_status = "IDLE"
    main._f1_result = []
    main._f2_done = False
    main._f3_started = False


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

    monkeypatch.setattr(main, "_today", lambda: "20260702")

    await _REAL_ENSURE_TRADING_DAY()

    assert s.trading_date == "20260702"
    assert s.target_ticker is None
    assert main._f1_result == []
    assert main._f2_done is False
    assert main._f3_started is False
