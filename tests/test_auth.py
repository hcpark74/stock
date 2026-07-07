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

class _SuccessResponse:
    status_code = 200

    def json(self):
        return {
            "access_token": "token-value",
            "access_token_token_expired": "2026-07-08 08:30:23",
        }


@pytest.mark.asyncio
async def test_refresh_uses_configurable_auth_timeout_and_logs_diagnostics(monkeypatch, tmp_path):
    captured = {}

    class _SuccessClient:
        def __init__(self, *args, **kwargs):
            captured["timeout"] = kwargs["timeout"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            return _SuccessResponse()

    logs = []
    monkeypatch.setenv("AUTH_DIR", str(tmp_path))
    monkeypatch.setenv("KIS_BASE_URL", "https://openapivts.koreainvestment.com:29443")
    monkeypatch.setenv("KIS_MODE", "PAPER")
    monkeypatch.setenv("KIS_AUTH_CONNECT_TIMEOUT_SEC", "15.5")
    monkeypatch.setattr(auth.httpx, "AsyncClient", _SuccessClient)
    monkeypatch.setattr(auth, "log", lambda event, **kwargs: logs.append((event, kwargs)))

    assert await auth.refresh() == "token-value"

    timeout = captured["timeout"]
    assert timeout.connect == 15.5
    assert timeout.read == 10.0
    assert timeout.write == 10.0
    assert timeout.pool == 5.0

    event, payload = logs[-1]
    assert event == "TOKEN_REFRESHED"
    assert payload["attempt"] == 1
    assert payload["host"] == "openapivts.koreainvestment.com"
    assert payload["mode"] == "PAPER"
    assert "elapsed_ms" in payload
