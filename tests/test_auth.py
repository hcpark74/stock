from unittest.mock import AsyncMock

import pytest

import src.api.auth as auth


class _FakeResponse:
    status_code = 500

    def json(self):
        return {}


class _FakeClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, *args, **kwargs):
        return _FakeResponse()


@pytest.mark.asyncio
async def test_refresh_sends_critical_alert_after_exhausted_attempts(monkeypatch):
    notify = AsyncMock()

    monkeypatch.setattr(auth.httpx, "AsyncClient", lambda *args, **kwargs: _FakeClient())
    monkeypatch.setattr(auth.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(auth.notifier, "send", notify)

    assert await auth.refresh() == ""
    notify.assert_awaited_once_with(
        "TOKEN_REFRESH_FAIL",
        level="CRIT",
        message="KIS 토큰 갱신 실패",
    )


@pytest.mark.asyncio
async def test_refresh_ws_key_sends_critical_alert_after_exhausted_attempts(monkeypatch):
    notify = AsyncMock()

    monkeypatch.setattr(auth.httpx, "AsyncClient", lambda *args, **kwargs: _FakeClient())
    monkeypatch.setattr(auth.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(auth.notifier, "send", notify)

    assert await auth.refresh_ws_key() == ""
    notify.assert_awaited_once_with(
        "WS_KEY_REFRESH_FAIL",
        level="CRIT",
        message="실시간 접속키 갱신 실패",
    )