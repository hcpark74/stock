"""F4 ↔ ViWatch 배선 + live/API 노출 테스트 — VI 감지·가시화 (관측 전용)."""
import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock

import pytest

import src.modules.f4_tracking as f4
from src import live
from src import state as _state_mod

TICKER = "004310"
ENTRY = 6841.0


@pytest.fixture(autouse=True)
def holding_state():
    s = _state_mod.get()
    s.position_status = "HOLDING"
    s.entry_price = ENTRY
    s.target_ticker = TICKER
    s.remaining_qty = 100
    s.high_price = ENTRY
    s.trailing_active = False
    s.highest_step = 0.0
    s.trade_id = 0
    live.clear_tick_history()
    yield
    s.position_status = "IDLE"
    live.clear_tick_history()


def _spike_pass() -> MagicMock:
    sf = MagicMock()
    sf.is_valid.return_value = True
    return sf


DETECTED_EVENT = {
    "type": "VI_DETECTED", "ts": "2026-07-16T09:13:45+09:00",
    "frozen_price": 7690.0, "vi_kind_code": "1", "cntg_vi_hour": "091333",
    "vi_prc": "7700", "vi_stnd_prc": "7000", "vi_dprt": "10.00", "vi_count": "1",
}
RELEASED_EVENT = {
    "type": "VI_RELEASED", "ts": "2026-07-16T09:15:50+09:00",
    "release_price": 7630.0, "source": "rest", "duration_sec": 125.0,
    "vi_prc": "7700", "vi_stnd_prc": "7000",
}


# ── _fetch_vi_status ─────────────────────────────────────────────────

async def test_fetch_vi_status_calls_vi_api(monkeypatch):
    get = AsyncMock(return_value={"rt_cd": "0", "output": {}})
    monkeypatch.setattr(f4.kis_rest, "get", get)

    resp = await f4._fetch_vi_status(TICKER)

    assert resp["rt_cd"] == "0"
    args, kwargs = get.await_args
    assert args[0] == "/uapi/domestic-stock/v1/quotations/inquire-vi-status"
    assert kwargs["tr_id"] == "FHPST01390000"
    assert kwargs["params"]["FID_INPUT_ISCD"] == TICKER


async def test_fetch_vi_status_raises_on_error_rt_cd(monkeypatch):
    get = AsyncMock(return_value={"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "err"})
    monkeypatch.setattr(f4.kis_rest, "get", get)

    with pytest.raises(RuntimeError):
        await f4._fetch_vi_status(TICKER)


# ── live 기록 ────────────────────────────────────────────────────────

def test_live_records_vi_detected_and_released():
    live.record_vi_detected(DETECTED_EVENT)
    assert len(live.vi_events) == 1
    row = live.vi_events[0]
    assert row["ts"] == DETECTED_EVENT["ts"]
    assert row["vi_prc"] == "7700"
    assert row.get("released_ts") is None

    live.record_vi_released(RELEASED_EVENT)
    row = live.vi_events[0]
    assert row["released_ts"] == RELEASED_EVENT["ts"]
    assert row["release_price"] == 7630.0
    assert row["duration_sec"] == 125.0


def test_clear_tick_history_clears_vi_events():
    live.record_vi_detected(DETECTED_EVENT)
    live.clear_tick_history()
    assert live.vi_events == []


# ── _handle_vi_events: 로그·알림·live 반영 ───────────────────────────

async def test_handle_detected_logs_notifies_and_records(monkeypatch):
    events = []
    notify = AsyncMock()
    monkeypatch.setattr(f4, "log", lambda event, **kw: events.append((event, kw)))
    monkeypatch.setattr(f4.notifier, "send", notify)

    await f4._handle_vi_events([dict(DETECTED_EVENT)], TICKER)

    assert [e for e, _ in events] == ["VI_DETECTED"]
    assert events[0][1]["vi_prc"] == "7700"
    assert len(live.vi_events) == 1
    notify.assert_awaited_once()
    assert notify.await_args.args[0] == "VI_DETECTED"


async def test_handle_released_updates_live_and_notifies(monkeypatch):
    live.record_vi_detected(DETECTED_EVENT)
    notify = AsyncMock()
    monkeypatch.setattr(f4, "log", lambda *a, **kw: None)
    monkeypatch.setattr(f4.notifier, "send", notify)

    await f4._handle_vi_events([dict(RELEASED_EVENT)], TICKER)

    assert live.vi_events[0]["released_ts"] == RELEASED_EVENT["ts"]
    notify.assert_awaited_once()
    assert notify.await_args.args[0] == "VI_RELEASED"


async def test_handle_negative_and_failed_log_only(monkeypatch):
    events = []
    notify = AsyncMock()
    monkeypatch.setattr(f4, "log", lambda event, **kw: events.append(event))
    monkeypatch.setattr(f4.notifier, "send", notify)

    await f4._handle_vi_events(
        [{"type": "VI_CHECK_NEGATIVE", "frozen_price": 7690.0, "frozen_sec": 10.0},
         {"type": "VI_CHECK_FAILED", "error": "RuntimeError('x')"}],
        TICKER,
    )

    assert events == ["VI_CHECK_NEGATIVE", "VI_CHECK_FAILED"]
    notify.assert_not_awaited()
    assert live.vi_events == []


# ── REST 백업 → ViWatch 배선 ─────────────────────────────────────────

async def test_rest_backup_feeds_vi_watch(monkeypatch):
    prices = [7690.0, 7690.0]
    calls = []

    class FakeWatch:
        async def on_price(self, price, source):
            calls.append((price, source))
            if len(calls) == 2:
                _state_mod.get().position_status = "CLOSED"
                return [dict(DETECTED_EVENT)]
            return []

    notify = AsyncMock()
    monkeypatch.setattr(f4, "F4_REST_POLL_INTERVAL_SEC", 0.01)
    monkeypatch.setattr(f4, "_fetch_current_price", AsyncMock(side_effect=lambda t: prices.pop(0)))
    monkeypatch.setattr(f4, "_process_tick", AsyncMock())
    monkeypatch.setattr(f4.notifier, "send", notify)
    monkeypatch.setattr(f4, "log", lambda *a, **kw: None)

    await asyncio.wait_for(
        f4._run_rest_price_backup(TICKER, _spike_pass(), None, vi_watch=FakeWatch()), 2)

    assert calls == [(7690.0, "rest"), (7690.0, "rest")]
    assert len(live.vi_events) == 1  # DETECTED가 live에 반영됨


# ── run() → WS 틱 배선 ───────────────────────────────────────────────

async def test_run_feeds_ws_ticks_to_vi_watch(monkeypatch):
    calls = []
    seen = asyncio.Event()

    class FakeWatch:
        async def on_price(self, price, source):
            calls.append((price, source))
            seen.set()
            return []

    async def fake_subscribe(_ticker, on_tick, *, stop_if=None):
        await on_tick({"price": 7000.0})
        while not (stop_if and stop_if()):
            await asyncio.sleep(0.01)

    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setattr(f4, "_close_in_progress", False)
    monkeypatch.setattr(f4, "_closing_task", None)
    monkeypatch.setattr(f4, "F4_REST_BACKUP_ENABLED", False)
    monkeypatch.setattr(f4, "_make_vi_watch", lambda ticker: FakeWatch())
    monkeypatch.setattr(f4, "_process_tick", AsyncMock())
    monkeypatch.setattr(f4.kis_ws, "subscribe", fake_subscribe)
    monkeypatch.setattr(f4, "log", lambda *a, **kw: None)

    task = asyncio.create_task(f4.run())
    try:
        await asyncio.wait_for(seen.wait(), 1)
    finally:
        _state_mod.get().position_status = "CLOSED"
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(task, 1)
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    assert calls == [(7000.0, "ws")]


async def test_make_vi_watch_disabled_by_env(monkeypatch):
    monkeypatch.setattr(f4, "VI_WATCH_ENABLED", False)
    assert f4._make_vi_watch(TICKER) is None
