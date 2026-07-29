import asyncio
import time

import httpx
import pytest

import src.api.kis_rest as kis_rest


def test_default_rate_interval_follows_kis_mode(monkeypatch):
    monkeypatch.setenv("KIS_MODE", "PAPER")
    assert kis_rest.default_rate_interval() == 1.1

    monkeypatch.setenv("KIS_MODE", "REAL")
    assert kis_rest.default_rate_interval() == 0.20

    monkeypatch.delenv("KIS_MODE", raising=False)
    assert kis_rest.default_rate_interval() == 1.1


def test_account_helpers_accept_documented_env_names(monkeypatch):
    monkeypatch.delenv("KIS_ACCT_NO", raising=False)
    monkeypatch.delenv("KIS_ACCT_CD", raising=False)
    monkeypatch.setenv("KIS_ACCOUNT_NO", "12345678")
    monkeypatch.setenv("KIS_ACCOUNT_TYPE", "01")

    assert kis_rest.account_no() == "12345678"
    assert kis_rest.account_cd() == "01"

    params = kis_rest.balance_inquiry_params()
    assert params == {
        "CANO": "12345678",
        "ACNT_PRDT_CD": "01",
        "AFHR_FLPR_YN": "N",
        "OFL_YN": "",
        "INQR_DVSN": "01",
        "UNPR_DVSN": "01",
        "FUND_STTL_ICLD_YN": "N",
        "FNCG_AMT_AUTO_RDPT_YN": "N",
        "PRCS_DVSN": "01",
        "CTX_AREA_FK100": "",
        "CTX_AREA_NK100": "",
    }


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


def test_headers_include_continuation_only_when_requested(monkeypatch):
    monkeypatch.setattr(kis_rest.auth, "get", lambda: "token")

    first = kis_rest._headers("VTTC8434R")
    continued = kis_rest._headers("VTTC8434R", "N")

    assert "tr_cont" not in first
    assert continued["tr_cont"] == "N"


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
async def test_get_can_return_continuation_response_metadata(monkeypatch):
    class ContinuationResponse(_FakeResponse):
        headers = {"tr_cont": "M"}

    class ContinuationClient(_FakeAsyncClient):
        async def request(self, *args, **kwargs):
            return ContinuationResponse()

    monkeypatch.setattr(kis_rest, "_RATE_INTERVAL", 0)
    monkeypatch.setattr(kis_rest.httpx, "AsyncClient", ContinuationClient)

    resp = await kis_rest.get("/test", include_response_meta=True)

    assert resp["rt_cd"] == "0"
    assert resp["_response_meta"] == {"tr_cont": "M"}


class _ControlledClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def _timed_client(clock: _ControlledClock, request_seconds: float):
    class TimedClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, *args, **kwargs):
            clock.now += request_seconds
            return _FakeResponse()

    return TimedClient


@pytest.mark.asyncio
async def test_latency_warning_excludes_deliberate_rate_limit_wait(monkeypatch):
    clock = _ControlledClock()
    events = []

    monkeypatch.setattr(kis_rest.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(kis_rest.asyncio, "sleep", clock.sleep)
    monkeypatch.setattr(kis_rest, "_RATE_INTERVAL", 1.1)
    monkeypatch.setattr(kis_rest, "_last_call_at", clock.now)
    monkeypatch.setattr(kis_rest.httpx, "AsyncClient", _timed_client(clock, 0.1))
    monkeypatch.setattr(kis_rest, "log", lambda event, **fields: events.append((event, fields)))

    resp = await kis_rest.get("/test")

    assert resp == {"rt_cd": "0"}
    assert not [event for event, _ in events if event == "LATENCY_HIGH"]


@pytest.mark.asyncio
async def test_latency_warning_reports_network_wait_and_total_times(monkeypatch):
    clock = _ControlledClock()
    events = []

    monkeypatch.setattr(kis_rest.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(kis_rest.asyncio, "sleep", clock.sleep)
    monkeypatch.setattr(kis_rest, "_RATE_INTERVAL", 1.1)
    monkeypatch.setattr(kis_rest, "_last_call_at", clock.now)
    monkeypatch.setattr(kis_rest.httpx, "AsyncClient", _timed_client(clock, 0.6))
    monkeypatch.setattr(kis_rest, "log", lambda event, **fields: events.append((event, fields)))

    await kis_rest.get("/test")

    latency_events = [fields for event, fields in events if event == "LATENCY_HIGH"]
    assert len(latency_events) == 1
    fields = latency_events[0]
    assert fields["level"] == "WARN"
    assert fields["api_endpoint"] == "/test"
    assert 599 <= fields["latency_ms"] <= 600
    assert fields["network_ms"] == fields["latency_ms"]
    assert 1099 <= fields["rate_wait_ms"] <= 1100
    assert fields["client_setup_ms"] == 0
    assert fields["local_overhead_ms"] == 0
    assert 1699 <= fields["total_ms"] <= 1700


@pytest.mark.asyncio
async def test_kis_rest_reuses_client_and_closes_it(monkeypatch):
    class ReusableClient:
        instances = 0
        requests = 0
        closes = 0
        is_closed = False

        def __init__(self, *args, **kwargs):
            self.__class__.instances += 1

        async def request(self, *args, **kwargs):
            self.__class__.requests += 1
            return _FakeResponse()

        async def aclose(self):
            self.__class__.closes += 1
            self.is_closed = True

    monkeypatch.setattr(kis_rest, "_RATE_INTERVAL", 0.0)
    monkeypatch.setattr(kis_rest, "_last_call_at", 0.0)
    monkeypatch.setattr(kis_rest, "_client", None)
    monkeypatch.setattr(kis_rest, "_client_factory", None)
    monkeypatch.setattr(kis_rest.httpx, "AsyncClient", ReusableClient)

    await kis_rest.get("/first")
    await kis_rest.get("/second")
    await kis_rest.close_client()

    assert ReusableClient.instances == 1
    assert ReusableClient.requests == 2
    assert ReusableClient.closes == 1


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


@pytest.mark.asyncio
async def test_post_send_guard_runs_after_rate_limit_wait(monkeypatch):
    """A deadline that passes during rate-limit sleep must block HTTP dispatch."""
    allowed = True
    request_calls = 0

    async def fake_sleep(_seconds):
        nonlocal allowed
        allowed = False

    class NoDispatchClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, *args, **kwargs):
            nonlocal request_calls
            request_calls += 1
            return _FakeResponse()

    monkeypatch.setattr(kis_rest, "_RATE_INTERVAL", 1.0)
    monkeypatch.setattr(kis_rest, "_last_call_at", time.monotonic())
    monkeypatch.setattr(kis_rest.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(kis_rest.httpx, "AsyncClient", NoDispatchClient)

    resp = await kis_rest.post(
        "/order",
        body={"qty": 1},
        send_guard=lambda: allowed,
    )

    assert resp["msg_cd"] == kis_rest.SEND_GUARD_BLOCKED_MSG_CD
    assert request_calls == 0


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


class _RateLimitClient:
    calls = 0
    status_code = 429
    body = {"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "rate limit"}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def request(self, *args, **kwargs):
        self.__class__.calls += 1
        outer = self

        class Response:
            status_code = outer.status_code

            def json(self):
                return dict(outer.body)

        return Response()


@pytest.mark.asyncio
async def test_kis_rest_investigation_mode_stops_on_http_429(monkeypatch):
    monkeypatch.setattr(kis_rest, "_RATE_INTERVAL", 0.0)
    monkeypatch.setattr(kis_rest, "_last_call_at", 0.0)
    monkeypatch.setattr(kis_rest.httpx, "AsyncClient", _RateLimitClient)
    _RateLimitClient.calls = 0
    _RateLimitClient.status_code = 429

    resp = await kis_rest.get("/test", stop_on_rate_limit=True)

    assert resp == {
        "rt_cd": "1",
        "msg_cd": "HTTP_429",
        "msg1": "HTTP 429 rate limit",
    }
    assert _RateLimitClient.calls == 1


@pytest.mark.asyncio
async def test_kis_rest_investigation_mode_stops_on_egw00201(monkeypatch):
    monkeypatch.setattr(kis_rest, "_RATE_INTERVAL", 0.0)
    monkeypatch.setattr(kis_rest, "_last_call_at", 0.0)
    monkeypatch.setattr(kis_rest.httpx, "AsyncClient", _RateLimitClient)
    _RateLimitClient.calls = 0
    _RateLimitClient.status_code = 200

    resp = await kis_rest.get("/test", stop_on_rate_limit=True)

    assert resp["msg_cd"] == "EGW00201"
    assert _RateLimitClient.calls == 1


class _BodyRateLimitThenOkClient:
    calls = 0
    code = "EGW00215"

    def __init__(self, *args, **kwargs):
        pass

    async def request(self, *args, **kwargs):
        self.__class__.calls += 1
        call_no = self.__class__.calls
        code = self.__class__.code

        class Response:
            status_code = 200

            def json(self):
                if call_no == 1:
                    return {"rt_cd": "1", "msg_cd": code, "msg1": "rate limit"}
                return {"rt_cd": "0", "output": {"ok": True}}

        return Response()


@pytest.mark.asyncio
async def test_get_does_not_classify_unconfirmed_egw00215_as_rate_limit(monkeypatch):
    events = []
    monkeypatch.setattr(kis_rest, "_RATE_INTERVAL", 0.0)
    monkeypatch.setattr(kis_rest, "_last_call_at", 0.0)
    monkeypatch.setattr(kis_rest, "_rate_limit_sleep_seconds", lambda *_: 0.0)
    monkeypatch.setattr(kis_rest.httpx, "AsyncClient", _BodyRateLimitThenOkClient)
    monkeypatch.setattr(
        kis_rest,
        "log",
        lambda event, **fields: events.append((event, fields)),
    )
    _BodyRateLimitThenOkClient.calls = 0

    resp = await kis_rest.get("/balance")

    assert resp["msg_cd"] == "EGW00215"
    assert _BodyRateLimitThenOkClient.calls == 1
    assert not any(event.startswith("RATE_LIMIT_") for event, _ in events)


@pytest.mark.asyncio
async def test_post_does_not_retry_application_rate_limit(monkeypatch):
    monkeypatch.setattr(kis_rest, "_RATE_INTERVAL", 0.0)
    monkeypatch.setattr(kis_rest, "_last_call_at", 0.0)
    monkeypatch.setattr(kis_rest.httpx, "AsyncClient", _BodyRateLimitThenOkClient)
    _BodyRateLimitThenOkClient.calls = 0

    resp = await kis_rest.post("/order", body={"qty": 1})

    assert resp["msg_cd"] == "EGW00215"
    assert _BodyRateLimitThenOkClient.calls == 1


def test_transient_sleep_seconds_exponential_backoff_with_cap(monkeypatch):
    monkeypatch.setattr(kis_rest, "_TRANSIENT_RETRY_BASE_SEC", 1.0)
    monkeypatch.setattr(kis_rest, "_TRANSIENT_RETRY_MAX_SEC", 8.0)

    assert kis_rest._transient_sleep_seconds(0) == 1.0
    assert kis_rest._transient_sleep_seconds(1) == 2.0
    assert kis_rest._transient_sleep_seconds(2) == 4.0
    assert kis_rest._transient_sleep_seconds(3) == 8.0
    assert kis_rest._transient_sleep_seconds(4) == 8.0


def test_confirmed_rate_limit_retry_delay_remains_one_second():
    assert [kis_rest._rate_limit_sleep_seconds(i, "/test") for i in range(3)] == [
        1.0,
        1.0,
        1.0,
    ]


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
    monkeypatch.setattr(kis_rest, "_MAX_TRANSIENT_RETRIES", 2)
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
