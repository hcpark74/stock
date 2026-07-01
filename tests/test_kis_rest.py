import asyncio
import time

import pytest

import src.api.kis_rest as kis_rest


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
