"""F4 Step Trailing 로직 유닛 테스트."""
import math
from datetime import datetime as _dt
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from src import state as _state_mod
from src.modules.f4_tracking import (
    HARD_STOP_RATIO,
    STEP_SIZE,
    STEP_TRAIL,
    _execute_close,
    _process_tick,
    _run_dry_ticks,
    _run_rest_price_backup,
)

KST = ZoneInfo("Asia/Seoul")
ENTRY = 10_000.0


# ── 헬퍼 ──────────────────────────────────────────────────────────────

def _kst(h: int, m: int) -> _dt:
    return _dt(2026, 6, 23, h, m, 0, tzinfo=KST)


def _spike_always_pass() -> MagicMock:
    sf = MagicMock()
    sf.is_valid.return_value = True
    return sf


async def _run_tick(
    price: float,
    *,
    hour: int = 9,
    minute: int = 30,
    set_closed_return: bool = False,
) -> AsyncMock:
    """_process_tick 실행, _execute_close mock 반환."""
    mock_close = AsyncMock()
    with (
        patch("src.modules.f4_tracking.datetime") as mock_dt,
        patch("src.modules.f4_tracking._execute_close", mock_close),
        patch("src.state.set_closed", new_callable=AsyncMock, return_value=set_closed_return),
    ):
        mock_dt.now.return_value = _kst(hour, minute)
        await _process_tick(price, _spike_always_pass())
    return mock_close


# ── 픽스처 ────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def holding_state():
    s = _state_mod.get()
    s.position_status = "HOLDING"
    s.entry_price = ENTRY
    s.target_ticker = "005930"
    s.remaining_qty = 100
    s.high_price = ENTRY
    s.trailing_active = False
    s.highest_step = 0.0
    s.trade_id = 0


# ── 스텝 갱신 정확성 ──────────────────────────────────────────────────

async def test_step_update_first_step():
    """2.6% 이익(1스텝 구간) → highest_step = 0.025, trailing_active 활성화.

    정확히 경계(2.5%)는 부동소수점 오차로 floor가 0이 되므로
    구간 안쪽(2.6%) 가격을 사용.
    """
    price = ENTRY * 1.026  # 10260, floor(0.026/0.025)=1 → step=0.025
    await _run_tick(price)
    s = _state_mod.get()
    assert s.highest_step == pytest.approx(STEP_SIZE)
    assert s.trailing_active is True


async def test_step_update_second_step():
    """5.1% 이익(2스텝 구간) → highest_step = 0.050."""
    price = ENTRY * 1.051  # 10510, floor(0.051/0.025)=2 → step=0.050
    await _run_tick(price)
    assert _state_mod.get().highest_step == pytest.approx(STEP_SIZE * 2)


async def test_step_update_third_step():
    """7.6% 이익(3스텝 구간) → highest_step = 0.075."""
    price = ENTRY * 1.076  # 10760, floor(0.076/0.025)=3 → step=0.075
    await _run_tick(price)
    assert _state_mod.get().highest_step == pytest.approx(STEP_SIZE * 3)


async def test_below_first_step_no_trailing():
    """2.4% 이익(스텝 미달) → trailing_active 미활성."""
    price = ENTRY * 1.024  # 10240, 스텝 미달
    await _run_tick(price)
    s = _state_mod.get()
    assert s.highest_step == 0.0
    assert s.trailing_active is False


# ── Hard Stop ─────────────────────────────────────────────────────────

async def test_hard_stop_at_exact_boundary():
    """trailing 미활성 + 정확히 -2.0% → Hard Stop 발동."""
    price = ENTRY * (1 - HARD_STOP_RATIO)  # 9800.0
    mock_close = await _run_tick(price, set_closed_return=True)
    mock_close.assert_awaited_once_with(price, "HARD_STOP")


async def test_hard_stop_not_triggered_above_boundary():
    """trailing 미활성 + -1.99% → Hard Stop 미발동."""
    price = ENTRY * (1 - HARD_STOP_RATIO) + 1  # 9801
    mock_close = await _run_tick(price, set_closed_return=True)
    mock_close.assert_not_awaited()


async def test_hard_stop_skipped_when_trailing_active():
    """trailing 활성 구간에서 -2.0% → Hard Stop 체크 자체 건너뜀."""
    s = _state_mod.get()
    s.trailing_active = True
    s.highest_step = STEP_SIZE  # 1스텝 달성 후 하락 시나리오
    price = ENTRY * (1 - HARD_STOP_RATIO)  # 9800 — Hard Stop 조건이지만 trailing 우선
    # stop = ENTRY * (1 + 0.025 - 0.015) = 10100 → 9800 <= 10100 → TRAILING 발동
    mock_close = await _run_tick(price, set_closed_return=True)
    # TRAILING으로 닫혀야 함, HARD_STOP이 아님
    mock_close.assert_awaited_once_with(price, "TRAILING")


# ── Step Trailing ─────────────────────────────────────────────────────

async def test_step_trailing_triggers_at_stop():
    """trailing 활성 + stop 가격 이하 → TRAILING 발동."""
    s = _state_mod.get()
    s.trailing_active = True
    s.highest_step = STEP_SIZE  # 0.025
    # stop = ENTRY * (1 + 0.025 - 0.015) = 10100
    stop = ENTRY * (1 + STEP_SIZE - STEP_TRAIL)
    price = stop  # 정확히 stop (<=)
    mock_close = await _run_tick(price, set_closed_return=True)
    mock_close.assert_awaited_once_with(price, "TRAILING")


async def test_step_trailing_not_triggered_above_stop():
    """trailing 활성 + stop 가격 +1원 → 미발동."""
    s = _state_mod.get()
    s.trailing_active = True
    s.highest_step = STEP_SIZE
    stop = ENTRY * (1 + STEP_SIZE - STEP_TRAIL)
    price = stop + 1  # 10101
    mock_close = await _run_tick(price, set_closed_return=True)
    mock_close.assert_not_awaited()


# ── 10:50 강제 발동 ───────────────────────────────────────────────────

async def test_late_force_trailing_active():
    """10:50 이후 → highest_step 0이어도 trailing_active 강제 True."""
    price = ENTRY * 1.01  # 1% 이익, 스텝 미달
    await _run_tick(price, hour=10, minute=50)
    assert _state_mod.get().trailing_active is True


async def test_late_triggers_if_below_zero_step_stop():
    """10:50 강제 활성 후 stop(entry×0.985) 이하 → 청산 발동."""
    price = ENTRY * 0.984  # stop = ENTRY*(1+0-0.015)=9850, 9840 < 9850
    mock_close = await _run_tick(price, hour=10, minute=50, set_closed_return=True)
    mock_close.assert_awaited_once_with(price, "TRAILING")


async def test_before_late_no_force():
    """10:49 → 강제 발동 없음, trailing_active 여전히 False."""
    price = ENTRY * 1.01
    await _run_tick(price, hour=10, minute=49)
    assert _state_mod.get().trailing_active is False


# ── highest_step 단조 증가 ────────────────────────────────────────────

async def test_highest_step_does_not_decrease():
    """2스텝(0.05) 달성 후 가격 후퇴 → highest_step 감소하지 않음."""
    s = _state_mod.get()
    s.trailing_active = True
    s.highest_step = STEP_SIZE * 2  # 0.05
    # 4% 가격(current_step = 0.025) — stop보다 위라서 청산 없음
    # stop = ENTRY*(1+0.05-0.015) = 10350, price=10400 > 10350 → no close
    price = ENTRY * 1.04  # 10400
    await _run_tick(price)
    assert _state_mod.get().highest_step == pytest.approx(STEP_SIZE * 2)


async def test_highest_step_advances_to_new_high():
    """현재 highest_step 0.025 → 5% 신고가 도달 → 0.050으로 갱신."""
    s = _state_mod.get()
    s.trailing_active = True
    s.highest_step = STEP_SIZE  # 0.025
    price = ENTRY * (1 + STEP_SIZE * 2)  # 10500
    await _run_tick(price)
    assert _state_mod.get().highest_step == pytest.approx(STEP_SIZE * 2)


async def test_dry_run_execute_close_does_not_touch_order_db(monkeypatch):
    s = _state_mod.get()
    s.trade_id = 123
    s.highest_step = STEP_SIZE
    monkeypatch.setenv("DRY_RUN", "1")

    record_order = AsyncMock()
    update_order_fill = AsyncMock()
    close_trade = AsyncMock()
    persist = AsyncMock()

    monkeypatch.setattr("src.modules.f4_tracking.db.record_order", record_order)
    monkeypatch.setattr("src.modules.f4_tracking.db.update_order_fill", update_order_fill)
    monkeypatch.setattr("src.modules.f4_tracking.db.close_trade", close_trade)
    monkeypatch.setattr("src.modules.f4_tracking.state.persist", persist)
    monkeypatch.setattr("src.modules.f4_tracking.notifier.send", AsyncMock())

    await _execute_close(ENTRY * 1.01, "TRAILING")

    record_order.assert_not_awaited()
    update_order_fill.assert_not_awaited()
    close_trade.assert_not_awaited()
    persist.assert_awaited_once()


async def test_dry_run_ticks_finish_below_trailing_stop(monkeypatch):
    events = []
    s = _state_mod.get()
    s.entry_price = ENTRY
    s.position_status = "HOLDING"

    monkeypatch.setenv("DRY_RUN_STEP_DELAY", "0")
    monkeypatch.setattr("src.modules.f4_tracking.log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr("src.modules.f4_tracking._process_tick", AsyncMock())

    await _run_dry_ticks("005930", _spike_always_pass())

    start_event = [kwargs for event, kwargs in events if event == "DRY_RUN_F4_START"][0]
    prices = start_event["prices"]
    assert prices[-1] < ENTRY * (1 + STEP_SIZE - STEP_TRAIL)


@pytest.mark.asyncio
async def test_rest_backup_skips_poll_when_websocket_is_fresh(monkeypatch):
    fetch = AsyncMock(return_value=ENTRY)

    async def stop_after_sleep(_seconds):
        _state_mod.get().position_status = "CLOSED"

    monkeypatch.setattr("src.modules.f4_tracking._fetch_current_price", fetch)
    monkeypatch.setattr("src.modules.f4_tracking.asyncio.sleep", AsyncMock(side_effect=stop_after_sleep))
    monkeypatch.setattr("src.modules.f4_tracking.log", lambda *args, **kwargs: None)

    await _run_rest_price_backup("005930", _spike_always_pass(), lambda: False)

    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_rest_backup_polls_when_websocket_is_stale(monkeypatch):
    fetch = AsyncMock(return_value=ENTRY)
    process_tick = AsyncMock()

    async def stop_after_sleep(_seconds):
        _state_mod.get().position_status = "CLOSED"

    monkeypatch.setattr("src.modules.f4_tracking._fetch_current_price", fetch)
    monkeypatch.setattr("src.modules.f4_tracking._process_tick", process_tick)
    monkeypatch.setattr("src.modules.f4_tracking.asyncio.sleep", AsyncMock(side_effect=stop_after_sleep))
    monkeypatch.setattr("src.modules.f4_tracking.log", lambda *args, **kwargs: None)

    await _run_rest_price_backup("005930", _spike_always_pass(), lambda: True)

    fetch.assert_awaited_once_with("005930")
    process_tick.assert_awaited_once()


@pytest.mark.asyncio
async def test_rest_backup_survives_fetch_error(monkeypatch):
    events = []
    fetch = AsyncMock(side_effect=[RuntimeError("boom"), ENTRY])
    process_tick = AsyncMock()
    sleep_count = {"n": 0}

    async def stop_after_two_sleeps(_seconds):
        sleep_count["n"] += 1
        if sleep_count["n"] >= 2:
            _state_mod.get().position_status = "CLOSED"

    monkeypatch.setattr("src.modules.f4_tracking._fetch_current_price", fetch)
    monkeypatch.setattr("src.modules.f4_tracking._process_tick", process_tick)
    monkeypatch.setattr("src.modules.f4_tracking.asyncio.sleep", AsyncMock(side_effect=stop_after_two_sleeps))
    monkeypatch.setattr("src.modules.f4_tracking.log", lambda event, **kwargs: events.append((event, kwargs)))

    await _run_rest_price_backup("005930", _spike_always_pass(), lambda: True)

    assert fetch.await_count == 2
    process_tick.assert_awaited_once()
    assert "F4_REST_BACKUP_ERROR" in [event for event, _ in events]


@pytest.mark.asyncio
async def test_run_keeps_monitoring_and_alerts_when_backup_crashes(monkeypatch):
    import asyncio as real_asyncio

    import src.modules.f4_tracking as f4

    events = []
    notify = AsyncMock()
    ws_started = real_asyncio.Event()

    async def fake_subscribe(ticker, on_tick, *, stop_if=None):
        ws_started.set()
        while not (stop_if and stop_if()):
            await real_asyncio.sleep(0.01)

    async def crash_backup(*args, **kwargs):
        raise RuntimeError("backup boom")

    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setattr(f4, "F4_REST_BACKUP_ENABLED", True)
    monkeypatch.setattr(f4.kis_ws, "subscribe", fake_subscribe)
    monkeypatch.setattr(f4, "_run_rest_price_backup", crash_backup)
    monkeypatch.setattr(f4.notifier, "send", notify)
    monkeypatch.setattr(f4, "log", lambda event, **kwargs: events.append((event, kwargs)))

    task = real_asyncio.create_task(f4.run())
    await real_asyncio.wait_for(ws_started.wait(), 1)
    await real_asyncio.sleep(0.05)

    # 백업 태스크가 죽어도 WS 모니터링은 계속되어야 한다
    assert not task.done()
    assert "F4_MONITOR_TASK_ERROR" in [event for event, _ in events]
    notify.assert_awaited()
    assert notify.await_args.args[0] == "F4_MONITOR_TASK_ERROR"

    _state_mod.get().position_status = "CLOSED"
    await real_asyncio.wait_for(task, 1)


@pytest.mark.asyncio
async def test_poll_fill_attempts_cover_timeout_window(monkeypatch):
    import src.modules.f4_tracking as f4

    get = AsyncMock(return_value={"output1": []})
    monkeypatch.setattr(f4.kis_rest, "get", get)
    monkeypatch.setattr(f4.kis_rest, "account_no", lambda: "12345678")
    monkeypatch.setattr(f4.kis_rest, "account_cd", lambda: "01")
    monkeypatch.setattr(f4, "F4_FILL_POLL_INTERVAL_SEC", 0.5)
    monkeypatch.setattr("src.modules.f4_tracking.asyncio.sleep", AsyncMock())

    result = await f4._poll_fill("ORD1", timeout_sec=3)

    assert result is None
    # 3초 창을 0.5초 간격으로 커버 → 6회 조회 (기존 버그: timeout_sec회 = 창 절반)
    assert get.await_count == 6


@pytest.mark.asyncio
async def test_execute_close_sends_critical_alert_on_sell_error(monkeypatch):
    notify = AsyncMock()
    record_order = AsyncMock()
    persist = AsyncMock()

    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setattr("src.modules.f4_tracking._send_sell", AsyncMock(side_effect=RuntimeError("sell failed")))
    monkeypatch.setattr("src.modules.f4_tracking.notifier.send", notify)
    monkeypatch.setattr("src.modules.f4_tracking.db.record_order", record_order)
    monkeypatch.setattr("src.modules.f4_tracking.state.persist", persist)

    await _execute_close(ENTRY * 0.98, "HARD_STOP")

    notify.assert_awaited_once_with(
        "F4_SELL_ERROR",
        level="CRIT",
        message="매도 주문 오류: 005930 RuntimeError('sell failed'). 수동 청산 필요",
        ticker="005930",
    )
    record_order.assert_not_awaited()
    persist.assert_not_awaited()
