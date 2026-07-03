import asyncio
import time

import pytest

import src.api.kis_rest as kis_rest


def test_account_helpers_accept_documented_env_names(monkeypatch):
    monkeypatch.delenv("KIS_ACCT_NO", raising=False)
    monkeypatch.delenv("KIS_ACCT_CD", raising=False)
    monkeypatch.setenv("KIS_ACCOUNT_NO", "12345678")
    monkeypatch.setenv("KIS_ACCOUNT_TYPE", "01")

    assert kis_rest.account_no() == "12345678"
    assert kis_rest.account_cd() == "01"

    params = kis_rest.balance_inquiry_params()
    assert params["CANO"] == "12345678"
    assert params["ACNT_PRDT_CD"] == "01"


def test_account_helpers_prefer_runtime_env_names(monkeypatch):
    monkeypatch.setenv("KIS_ACCT_NO", "87654321")
    monkeypatch.setenv("KIS_ACCOUNT_NO", "12345678")
    monkeypatch.setenv("KIS_ACCT_CD", "02")
    monkeypatch.setenv("KIS_ACCOUNT_TYPE", "01")

    assert kis_rest.account_no() == "87654321"
    assert kis_rest.account_cd() == "02"


def test_account_helpers_do_not_fallback_from_empty_runtime_env_names(monkeypatch):
    monkeypatch.setenv("KIS_ACCT_NO", "")
    monkeypatch.setenv("KIS_ACCOUNT_NO", "12345678")
    monkeypatch.setenv("KIS_ACCT_CD", "")
    monkeypatch.setenv("KIS_ACCOUNT_TYPE", "01")

    assert kis_rest.account_no() == ""
    assert kis_rest.account_cd() == ""

    params = kis_rest.balance_inquiry_params()
    assert params["CANO"] == ""
    assert params["ACNT_PRDT_CD"] == ""


class _FakeResponse:
    status_code = 200

    def json(self):
        return {"rt_cd": "0"}


class _FakeAsyncClient:
    starts: list[float] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def request(self, *args, **kwargs):
        self.starts.append(time.monotonic())
        return _FakeResponse()


@pytest.mark.asyncio
async def test_kis_rest_rate_limiter_serializes_concurrent_requests(monkeypatch):
    monkeypatch.setattr(kis_rest, "_RATE_INTERVAL", 0.05)
    monkeypatch.setattr(kis_rest, "_last_call_at", 0.0)
    monkeypatch.setattr(kis_rest.httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.starts = []

    await asyncio.gather(*(kis_rest.get("/test") for _ in range(5)))

    starts = sorted(_FakeAsyncClient.starts)
    gaps = [b - a for a, b in zip(starts, starts[1:])]

    assert len(starts) == 5
    assert min(gaps) >= 0.040


class _TokenExpiredThenOkClient:
    calls = 0

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def request(self, *args, **kwargs):
        self.__class__.calls += 1

        class Response:
            status_code = 200

            def json(self_inner):
                if _TokenExpiredThenOkClient.calls == 1:
                    return {
                        "rt_cd": "1",
                        "msg_cd": "EGW00123",
                        "msg1": "기간이 만료된 token 입니다.",
                    }
                return {"rt_cd": "0", "output": {"ok": True}}

        return Response()


@pytest.mark.asyncio
async def test_kis_rest_refreshes_on_kis_token_expired_body(monkeypatch):
    refresh_calls = 0

    async def fake_refresh():
        nonlocal refresh_calls
        refresh_calls += 1
        return "new-token"

    monkeypatch.setattr(kis_rest, "_RATE_INTERVAL", 0.0)
    monkeypatch.setattr(kis_rest, "_last_call_at", 0.0)
    monkeypatch.setattr(kis_rest.httpx, "AsyncClient", _TokenExpiredThenOkClient)
    monkeypatch.setattr(kis_rest.auth, "refresh", fake_refresh)
    _TokenExpiredThenOkClient.calls = 0

    resp = await kis_rest.get("/test")

    assert resp == {"rt_cd": "0", "output": {"ok": True}}
    assert refresh_calls == 1
    assert _TokenExpiredThenOkClient.calls == 2


_TOKEN_EXPIRED_BODY = {
    "rt_cd": "1",
    "msg_cd": "EGW00123",
    "msg1": "기간이 만료된 token 입니다.",
}


class _AlwaysTokenExpiredClient:
    calls = 0

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def request(self, *args, **kwargs):
        self.__class__.calls += 1

        class Response:
            status_code = 200

            def json(self_inner):
                return dict(_TOKEN_EXPIRED_BODY)

        return Response()


@pytest.mark.asyncio
async def test_kis_rest_persistent_token_expired_body_refreshes_only_once(monkeypatch):
    refresh_calls = 0

    async def fake_refresh():
        nonlocal refresh_calls
        refresh_calls += 1
        return "new-token"

    monkeypatch.setattr(kis_rest, "_RATE_INTERVAL", 0.0)
    monkeypatch.setattr(kis_rest, "_last_call_at", 0.0)
    monkeypatch.setattr(kis_rest.httpx, "AsyncClient", _AlwaysTokenExpiredClient)
    monkeypatch.setattr(kis_rest.auth, "refresh", fake_refresh)
    _AlwaysTokenExpiredClient.calls = 0

    resp = await kis_rest.get("/test")

    assert resp["msg_cd"] == "EGW00123"
    assert refresh_calls == 1
    assert _AlwaysTokenExpiredClient.calls == 2


@pytest.mark.asyncio
async def test_kis_rest_token_expired_body_without_new_token_does_not_retry(monkeypatch):
    async def fake_refresh():
        return ""

    monkeypatch.setattr(kis_rest, "_RATE_INTERVAL", 0.0)
    monkeypatch.setattr(kis_rest, "_last_call_at", 0.0)
    monkeypatch.setattr(kis_rest.httpx, "AsyncClient", _AlwaysTokenExpiredClient)
    monkeypatch.setattr(kis_rest.auth, "refresh", fake_refresh)
    _AlwaysTokenExpiredClient.calls = 0

    resp = await kis_rest.get("/test")

    assert resp["msg_cd"] == "EGW00123"
    assert _AlwaysTokenExpiredClient.calls == 1


class _Always401Client:
    calls = 0

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def request(self, *args, **kwargs):
        self.__class__.calls += 1

        class Response:
            status_code = 401

            def json(self_inner):
                return {
                    "rt_cd": "1",
                    "msg_cd": "EGW00121",
                    "msg1": "유효하지 않은 token 입니다.",
                }

        return Response()


@pytest.mark.asyncio
async def test_kis_rest_persistent_401_refreshes_only_once(monkeypatch):
    refresh_calls = 0

    async def fake_refresh():
        nonlocal refresh_calls
        refresh_calls += 1
        return "new-token"

    monkeypatch.setattr(kis_rest, "_RATE_INTERVAL", 0.0)
    monkeypatch.setattr(kis_rest, "_last_call_at", 0.0)
    monkeypatch.setattr(kis_rest.httpx, "AsyncClient", _Always401Client)
    monkeypatch.setattr(kis_rest.auth, "refresh", fake_refresh)
    _Always401Client.calls = 0

    resp = await kis_rest.get("/test")

    assert resp["msg_cd"] == "EGW00121"
    assert refresh_calls == 1
    assert _Always401Client.calls == 2


class _TokenExpired429TokenExpiredClient:
    """1번째 EGW00123 → 2번째 HTTP 429 → 3번째 EGW00123."""

    calls = 0

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def request(self, *args, **kwargs):
        self.__class__.calls += 1
        call_no = self.__class__.calls

        class Response:
            status_code = 429 if call_no == 2 else 200

            def json(self_inner):
                return dict(_TOKEN_EXPIRED_BODY)

        return Response()


@pytest.mark.asyncio
async def test_kis_rest_429_retry_preserves_token_refresh_budget(monkeypatch):
    refresh_calls = 0

    async def fake_refresh():
        nonlocal refresh_calls
        refresh_calls += 1
        return "new-token"

    monkeypatch.setattr(kis_rest, "_RATE_INTERVAL", 0.0)
    monkeypatch.setattr(kis_rest, "_last_call_at", 0.0)
    monkeypatch.setattr(kis_rest.httpx, "AsyncClient", _TokenExpired429TokenExpiredClient)
    monkeypatch.setattr(kis_rest.auth, "refresh", fake_refresh)
    _TokenExpired429TokenExpiredClient.calls = 0

    resp = await kis_rest.get("/test")

    assert resp["msg_cd"] == "EGW00123"
    assert refresh_calls == 1
    assert _TokenExpired429TokenExpiredClient.calls == 3
