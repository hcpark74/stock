from datetime import datetime
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from src.api import kis_ws

KST = ZoneInfo("Asia/Seoul")


def _cnt_frame(ticker="005930", hms="091015", price="10300", vol="7"):
    # H0STCNT0: 0=ticker, 1=체결시간(HHMMSS), 2=현재가, 12=체결량
    fields = [""] * 13
    fields[0] = ticker
    fields[1] = hms
    fields[2] = price
    fields[12] = vol
    return "0|H0STCNT0|001|" + "^".join(fields)


def test_parse_tick_includes_exchange_time_and_qty():
    tick = kis_ws._parse_tick(_cnt_frame(hms="091015", price="10300", vol="7"))
    assert tick["ticker"] == "005930"
    assert tick["price"] == 10300.0
    assert tick["qty"] == 7
    assert tick["exchange_time"] == "091015"
    assert tick["source_ts"].endswith("+09:00")
    assert "T09:10:15" in tick["source_ts"]


def test_parse_tick_invalid_time_marks_source_ts_none():
    tick = kis_ws._parse_tick(_cnt_frame(hms="999999"))
    assert tick["source_ts"] is None


def test_exchange_iso_rejects_naive_and_bad_values():
    assert kis_ws._exchange_iso("") is None
    assert kis_ws._exchange_iso("0910") is None
    assert kis_ws._exchange_iso("abcdef") is None
    now = datetime(2026, 8, 13, 0, 0, tzinfo=KST)
    assert kis_ws._exchange_iso("091015", now).endswith("+09:00")


@pytest.mark.asyncio
async def test_subscribe_reports_transport_connection_changes(monkeypatch):
    stopped = False
    changes = []

    class FakeSocket:
        async def recv(self):
            raise RuntimeError("connection lost")

    class FakeConnection:
        async def __aenter__(self):
            return FakeSocket()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def stop_retry(_seconds):
        nonlocal stopped
        stopped = True

    monkeypatch.setattr(kis_ws.auth, "refresh_ws_key", AsyncMock())
    monkeypatch.setattr(kis_ws, "_send_subscribe", AsyncMock())
    monkeypatch.setattr(kis_ws.websockets, "connect", lambda *a, **kw: FakeConnection())
    monkeypatch.setattr(kis_ws.asyncio, "sleep", stop_retry)
    monkeypatch.setattr(kis_ws, "log", lambda *a, **kw: None)

    await kis_ws.subscribe(
        "005930",
        AsyncMock(),
        stop_if=lambda: stopped,
        on_connection_change=changes.append,
    )

    assert changes == [True, False]
