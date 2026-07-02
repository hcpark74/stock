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
