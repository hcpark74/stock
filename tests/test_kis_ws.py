from unittest.mock import AsyncMock

import pytest

from src.api import kis_ws


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
