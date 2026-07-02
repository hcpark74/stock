import asyncio
import os
import time

import httpx

from src.api import auth
from src.utils.logger import log

_last_call_at: float = 0.0
_RATE_INTERVAL = float(os.getenv("KIS_RATE_INTERVAL_SEC", "0.10"))
_TIMEOUT = 15.0        # 잔고조회 등 느린 API 대응 (문서: "조회속도가 느린 API")
_rate_lock = asyncio.Lock()


def balance_inquiry_params() -> dict:
    return {
        "CANO": os.getenv("KIS_ACCT_NO", ""),
        "ACNT_PRDT_CD": os.getenv("KIS_ACCT_CD", "01"),
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


def _headers(tr_id: str = "") -> dict:
    return {
        "authorization": f"Bearer {auth.get()}",
        "appkey": os.getenv("KIS_APP_KEY", ""),
        "appsecret": os.getenv("KIS_APP_SECRET", ""),
        "tr_id": tr_id,
        "custtype": "P",
        "content-type": "application/json; charset=utf-8",
        # 모의투자는 일부 TR ID 앞에 'V' prefix 필요 — 호출 측에서 tr_id 구분
    }


async def _request(
    method: str,
    path: str,
    tr_id: str = "",
    timeout: float = _TIMEOUT,
    _app_retry: int = 0,
    **kwargs,
) -> dict:
    """Rate-limited KIS REST 요청. 401/429 자동 처리."""
    global _last_call_at

    base_url = os.getenv("KIS_BASE_URL", "")
    url = base_url + path

    start = time.monotonic()
    await _rate_lock.acquire()
    try:
        wait = _RATE_INTERVAL - (time.monotonic() - _last_call_at)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call_at = time.monotonic()
    finally:
        _rate_lock.release()

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.request(method, url, headers=_headers(tr_id), **kwargs)
    latency_ms = int((time.monotonic() - start) * 1000)

    if latency_ms > 500:
        log("LATENCY_HIGH", level="WARN", api_endpoint=path, latency_ms=latency_ms)
    elif latency_ms > 200:
        log("LATENCY_HIGH", level="INFO", api_endpoint=path, latency_ms=latency_ms)

    # 429 — Rate limit 초과
    if resp.status_code == 429:
        log("RATE_LIMIT_HIT", level="WARN", path=path)
        await asyncio.sleep(1)
        return await _request(method, path, tr_id, timeout=timeout, _app_retry=_app_retry, **kwargs)

    # 401 — 토큰 만료 → 즉시 재발급 후 1회 재시도
    if resp.status_code == 401:
        log("TOKEN_EXPIRED", level="WARN", path=path)
        new_token = await auth.refresh()
        if new_token:
            return await _request(method, path, tr_id, timeout=timeout, _app_retry=_app_retry, **kwargs)

    data = resp.json()
    if data.get("msg_cd") == "EGW00201" and _app_retry < 3:
        log("RATE_LIMIT_HIT", level="WARN", path=path, msg_cd=data.get("msg_cd"), msg1=data.get("msg1"))
        await asyncio.sleep(1.0)
        return await _request(
            method,
            path,
            tr_id,
            timeout=timeout,
            _app_retry=_app_retry + 1,
            **kwargs,
        )

    return data


async def get(path: str, params: dict | None = None, tr_id: str = "", timeout: float = _TIMEOUT) -> dict:
    return await _request("GET", path, tr_id=tr_id, timeout=timeout, params=params)


async def post(path: str, body: dict | None = None, tr_id: str = "", timeout: float = _TIMEOUT) -> dict:
    return await _request("POST", path, tr_id=tr_id, timeout=timeout, json=body)
