import asyncio
import time

import httpx
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


def test_transient_sleep_seconds_exponential_backoff_with_cap(monkeypatch):
    monkeypatch.setattr(kis_rest, "_TRANSIENT_RETRY_BASE_SEC", 1.0)
    monkeypatch.setattr(kis_rest, "_TRANSIENT_RETRY_MAX_SEC", 8.0)

    assert kis_rest._transient_sleep_seconds(0) == 1.0
    assert kis_rest._transient_sleep_seconds(1) == 2.0
    assert kis_rest._transient_sleep_seconds(2) == 4.0
    assert kis_rest._transient_sleep_seconds(3) == 8.0
    assert kis_rest._transient_sleep_seconds(4) == 8.0


class _TransientErrorClient:
    """지정한 예외를 fail_count번 던진 뒤 성공 응답."""

    calls = 0
    fail_count = 0
    exc_factory = staticmethod(lambda: httpx.ConnectError("connection refused"))

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def request(self, *args, **kwargs):
        cls = self.__class__
        cls.calls += 1
        if cls.calls <= cls.fail_count:
            raise cls.exc_factory()
        return _FakeResponse()


def _patch_transient(monkeypatch, fail_count, exc_factory):
    monkeypatch.setattr(kis_rest, "_RATE_INTERVAL", 0.0)
    monkeypatch.setattr(kis_rest, "_last_call_at", 0.0)
    monkeypatch.setattr(kis_rest, "_TRANSIENT_RETRY_BASE_SEC", 0.0)
    monkeypatch.setattr(kis_rest.httpx, "AsyncClient", _TransientErrorClient)
    _TransientErrorClient.calls = 0
    _TransientErrorClient.fail_count = fail_count
    _TransientErrorClient.exc_factory = staticmethod(exc_factory)


@pytest.mark.asyncio
async def test_get_retries_transient_error_then_succeeds(monkeypatch):
    _patch_transient(monkeypatch, 2, lambda: httpx.ReadTimeout("timed out"))

    resp = await kis_rest.get("/test")

    assert resp == {"rt_cd": "0"}
    assert _TransientErrorClient.calls == 3


@pytest.mark.asyncio
async def test_get_exhausted_retries_returns_error_dict_without_output(monkeypatch):
    monkeypatch.setattr(kis_rest, "_MAX_TRANSIENT_RETRIES", 2)
    _patch_transient(monkeypatch, 99, lambda: httpx.ReadTimeout("timed out"))

    resp = await kis_rest.get("/test")

    assert resp["rt_cd"] == "1"
    assert resp["msg_cd"] == "ReadTimeout"
    assert "output" not in resp
    assert _TransientErrorClient.calls == 3  # 최초 1회 + 재시도 2회


@pytest.mark.asyncio
async def test_post_read_timeout_does_not_retry(monkeypatch):
    """주문 POST가 서버 도달 후 응답만 유실된 경우 재전송하면 중복 주문 위험."""
    _patch_transient(monkeypatch, 99, lambda: httpx.ReadTimeout("timed out"))

    resp = await kis_rest.post("/order", body={"qty": 1})

    assert resp["rt_cd"] == "1"
    assert resp["msg_cd"] == "ReadTimeout"
    assert _TransientErrorClient.calls == 1


@pytest.mark.asyncio
async def test_post_connect_error_retries(monkeypatch):
    """ConnectError는 요청이 서버에 도달하지 않았음이 보장되므로 POST도 재시도."""
    _patch_transient(monkeypatch, 1, lambda: httpx.ConnectError("connection refused"))

    resp = await kis_rest.post("/order", body={"qty": 1})

    assert resp == {"rt_cd": "0"}
    assert _TransientErrorClient.calls == 2
