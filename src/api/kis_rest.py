import asyncio
import os
import time
from collections.abc import Callable

import httpx

from src.api import auth
from src.utils.logger import log


def default_rate_interval() -> float:
    # KIS 유량 정책(2026-04-20): 실전 초당 18건 / 모의 초당 1건.
    # 모의는 1.0s 정각 간격 시 서버 도착 시점 jitter로 같은 초에 2건이 몰릴 수 있어 10% 여유.
    return 0.20 if os.getenv("KIS_MODE", "PAPER") == "REAL" else 1.1


_last_call_at: float = 0.0
_RATE_INTERVAL = float(os.getenv("KIS_RATE_INTERVAL_SEC", "") or default_rate_interval())
_MAX_TRANSIENT_RETRIES = int(os.getenv("KIS_MAX_TRANSIENT_RETRIES", "2"))
_TRANSIENT_RETRY_BASE_SEC = float(os.getenv("KIS_TRANSIENT_RETRY_BASE_SEC", "1.0"))
_TRANSIENT_RETRY_MAX_SEC = float(os.getenv("KIS_TRANSIENT_RETRY_MAX_SEC", "8.0"))
_TIMEOUT = 15.0        # 잔고조회 등 느린 API 대응 (문서: "조회속도가 느린 API")
_rate_lock = asyncio.Lock()
_client_lock = asyncio.Lock()
_client: httpx.AsyncClient | None = None
_client_factory: object | None = None
SEND_GUARD_BLOCKED_MSG_CD = "LOCAL_SEND_GUARD_BLOCKED"


def account_no() -> str:
    if "KIS_ACCT_NO" in os.environ:
        return os.getenv("KIS_ACCT_NO", "")
    return os.getenv("KIS_ACCOUNT_NO", "")


def account_cd() -> str:
    if "KIS_ACCT_CD" in os.environ:
        return os.getenv("KIS_ACCT_CD", "")
    return os.getenv("KIS_ACCOUNT_TYPE", "01")


def balance_inquiry_params() -> dict:
    return {
        "CANO": account_no(),
        "ACNT_PRDT_CD": account_cd(),
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


def _transient_sleep_seconds(retry: int) -> float:
    return min(_TRANSIENT_RETRY_BASE_SEC * (2 ** retry), _TRANSIENT_RETRY_MAX_SEC)


def _headers(tr_id: str = "", tr_cont: str = "") -> dict:
    headers = {
        "authorization": f"Bearer {auth.get()}",
        "appkey": os.getenv("KIS_APP_KEY", ""),
        "appsecret": os.getenv("KIS_APP_SECRET", ""),
        "tr_id": tr_id,
        "custtype": "P",
        "content-type": "application/json; charset=utf-8",
        # 모의투자는 일부 TR ID 앞에 'V' prefix 필요 — 호출 측에서 tr_id 구분
    }
    if tr_cont:
        headers["tr_cont"] = tr_cont
    return headers


async def _get_client() -> httpx.AsyncClient:
    """프로세스 수명 동안 HTTP 연결 풀을 재사용한다."""
    global _client, _client_factory
    factory = httpx.AsyncClient
    async with _client_lock:
        is_closed = bool(getattr(_client, "is_closed", False)) if _client is not None else True
        if _client is None or is_closed or _client_factory is not factory:
            old_client = _client
            if old_client is not None:
                close = getattr(old_client, "aclose", None)
                if close is not None:
                    await close()
            _client = factory()
            _client_factory = factory
        return _client


async def close_client() -> None:
    """공유 REST 클라이언트를 정상 종료한다."""
    global _client, _client_factory
    async with _client_lock:
        client = _client
        _client = None
        _client_factory = None
        if client is not None:
            close = getattr(client, "aclose", None)
            if close is not None:
                await close()


async def _request(
    method: str,
    path: str,
    tr_id: str = "",
    timeout: float = _TIMEOUT,
    _app_retry: int = 0,
    _transient_retry: int = 0,
    _token_retry: int = 0,
    send_guard: Callable[[], bool] | None = None,
    stop_on_rate_limit: bool = False,
    tr_cont: str = "",
    include_response_meta: bool = False,
    **kwargs,
) -> dict:
    """Rate-limited KIS REST 요청.

    조사 모드는 429/EGW00201에서 즉시 중단하며, 401 토큰 갱신은 항상 수행한다.
    """
    global _last_call_at

    base_url = os.getenv("KIS_BASE_URL", "")
    url = base_url + path

    total_start = time.monotonic()
    await _rate_lock.acquire()
    try:
        wait = _RATE_INTERVAL - (time.monotonic() - _last_call_at)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call_at = time.monotonic()
    finally:
        _rate_lock.release()
    request_ready_at = time.monotonic()
    rate_wait_ms = int((request_ready_at - total_start) * 1000)

    try:
        if send_guard is not None and not send_guard():
            return {
                "rt_cd": "1",
                "msg_cd": SEND_GUARD_BLOCKED_MSG_CD,
                "msg1": "request blocked by send guard",
            }
        client = await _get_client()
        client_ready_at = time.monotonic()
        client_setup_ms = int((client_ready_at - request_ready_at) * 1000)
        # 클라이언트 준비 중 주문 마감이 지날 수 있으므로 전송 직전에 다시 검사한다.
        if send_guard is not None and not send_guard():
            return {
                "rt_cd": "1",
                "msg_cd": SEND_GUARD_BLOCKED_MSG_CD,
                "msg1": "request blocked by send guard",
            }
        request_start = time.monotonic()
        resp = await client.request(
            method,
            url,
            headers=_headers(tr_id, tr_cont),
            timeout=timeout,
            **kwargs,
        )
        request_end = time.monotonic()
    except httpx.HTTPError as exc:
        # 주문 등 POST는 서버 도달 후 응답만 유실됐을 수 있어(예: ReadTimeout)
        # 재전송 시 중복 주문 위험 — 요청 미전송이 보장되는 ConnectError만 재시도.
        retry_safe = method == "GET" or isinstance(exc, httpx.ConnectError)
        if retry_safe and _transient_retry < _MAX_TRANSIENT_RETRIES:
            sleep_sec = _transient_sleep_seconds(_transient_retry)
            log(
                "TRANSIENT_ERROR_RETRY",
                level="WARN",
                path=path,
                reason=exc.__class__.__name__,
                retry_after_sec=sleep_sec,
                retry_count=_transient_retry + 1,
                max_retries=_MAX_TRANSIENT_RETRIES,
            )
            await asyncio.sleep(sleep_sec)
            return await _request(
                method,
                path,
                tr_id,
                timeout=timeout,
                _app_retry=_app_retry,
                _transient_retry=_transient_retry + 1,
                _token_retry=_token_retry,
                send_guard=send_guard,
                stop_on_rate_limit=stop_on_rate_limit,
                tr_cont=tr_cont,
                include_response_meta=include_response_meta,
                **kwargs,
            )
        log(
            "KIS_REQUEST_FAILED",
            level="WARN",
            path=path,
            reason=exc.__class__.__name__,
            error=str(exc)[:200],
        )
        # output 키는 넣지 않는다 — 호출부가 resp.get("output", 기본값)으로
        # 각자 올바른 기본형(dict/list)을 쓰도록 위임.
        return {
            "rt_cd": "1",
            "msg_cd": exc.__class__.__name__,
            "msg1": str(exc),
        }
    # Keep latency_ms for existing log consumers, but make it represent the
    # actual HTTP round trip. The old measurement also included the deliberate
    # local rate-limit wait, which made normal PAPER-mode throttling look like
    # upstream API latency.
    network_ms = int((request_end - request_start) * 1000)
    total_ms = int((request_end - total_start) * 1000)
    local_overhead_ms = max(0, total_ms - network_ms - rate_wait_ms)
    latency_fields = {
        "latency_ms": network_ms,
        "network_ms": network_ms,
        "rate_wait_ms": rate_wait_ms,
        "client_setup_ms": client_setup_ms,
        "local_overhead_ms": local_overhead_ms,
        "total_ms": total_ms,
    }

    if network_ms > 500:
        log("LATENCY_HIGH", level="WARN", api_endpoint=path, **latency_fields)
    elif network_ms > 200:
        log("LATENCY_HIGH", level="INFO", api_endpoint=path, **latency_fields)

    # 429 — Rate limit 초과
    if resp.status_code == 429:
        log("RATE_LIMIT_HIT", level="WARN", path=path)
        if stop_on_rate_limit:
            return {
                "rt_cd": "1",
                "msg_cd": "HTTP_429",
                "msg1": "HTTP 429 rate limit",
            }
        await asyncio.sleep(1)
        return await _request(
            method,
            path,
            tr_id,
            timeout=timeout,
            _app_retry=_app_retry,
            _token_retry=_token_retry,
            send_guard=send_guard,
            stop_on_rate_limit=stop_on_rate_limit,
            tr_cont=tr_cont,
            include_response_meta=include_response_meta,
            **kwargs,
        )

    # 401 — 토큰 만료 → 즉시 재발급 후 1회 재시도
    if resp.status_code == 401 and _token_retry < 1:
        log("TOKEN_EXPIRED", level="WARN", path=path)
        new_token = await auth.refresh()
        if new_token:
            return await _request(
                method,
                path,
                tr_id,
                timeout=timeout,
                _app_retry=_app_retry,
                _token_retry=_token_retry + 1,
                send_guard=send_guard,
                stop_on_rate_limit=stop_on_rate_limit,
                tr_cont=tr_cont,
                include_response_meta=include_response_meta,
                **kwargs,
            )

    data = resp.json()
    if data.get("msg_cd") == "EGW00123" and _token_retry < 1:
        log(
            "TOKEN_EXPIRED",
            level="WARN",
            path=path,
            msg_cd=data.get("msg_cd"),
            msg1=data.get("msg1"),
        )
        new_token = await auth.refresh()
        if new_token:
            return await _request(
                method,
                path,
                tr_id,
                timeout=timeout,
                _app_retry=_app_retry,
                _token_retry=_token_retry + 1,
                send_guard=send_guard,
                stop_on_rate_limit=stop_on_rate_limit,
                tr_cont=tr_cont,
                include_response_meta=include_response_meta,
                **kwargs,
            )

    if data.get("msg_cd") == "EGW00201" and stop_on_rate_limit:
        log(
            "RATE_LIMIT_HIT",
            level="WARN",
            path=path,
            msg_cd=data.get("msg_cd"),
            msg1=data.get("msg1"),
        )
        return data

    if data.get("msg_cd") == "EGW00201" and _app_retry < 3:
        log(
            "RATE_LIMIT_HIT",
            level="WARN",
            path=path,
            msg_cd=data.get("msg_cd"),
            msg1=data.get("msg1"),
        )
        await asyncio.sleep(1.0)
        return await _request(
            method,
            path,
            tr_id,
            timeout=timeout,
            _app_retry=_app_retry + 1,
            _token_retry=_token_retry,
            send_guard=send_guard,
            stop_on_rate_limit=stop_on_rate_limit,
            tr_cont=tr_cont,
            include_response_meta=include_response_meta,
            **kwargs,
        )

    if include_response_meta and isinstance(data, dict):
        data = dict(data)
        data["_response_meta"] = {
            "tr_cont": str(resp.headers.get("tr_cont") or ""),
        }
    return data


async def get(
    path: str,
    params: dict | None = None,
    tr_id: str = "",
    timeout: float = _TIMEOUT,
    stop_on_rate_limit: bool = False,
    tr_cont: str = "",
    include_response_meta: bool = False,
) -> dict:
    return await _request(
        "GET",
        path,
        tr_id=tr_id,
        timeout=timeout,
        params=params,
        stop_on_rate_limit=stop_on_rate_limit,
        tr_cont=tr_cont,
        include_response_meta=include_response_meta,
    )


async def post(
    path: str,
    body: dict | None = None,
    tr_id: str = "",
    timeout: float = _TIMEOUT,
    send_guard: Callable[[], bool] | None = None,
) -> dict:
    return await _request(
        "POST",
        path,
        tr_id=tr_id,
        timeout=timeout,
        send_guard=send_guard,
        json=body,
    )
