import asyncio
import os
import time
from collections.abc import Callable

import httpx

from src import live
from src.api import auth
from src.utils.logger import log

REAL_CASH_BUY_TR = "TTTC0012U"


def _with_response_meta(
    data: dict,
    *,
    enabled: bool,
    http_status: int | None,
    request_sent: bool,
    tr_cont: str = "",
) -> dict:
    if not enabled:
        return data
    result = dict(data)
    result["_response_meta"] = {
        "tr_cont": tr_cont,
        "http_status": http_status,
        "request_sent": request_sent,
    }
    return result


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
_LATENCY_SUMMARY_INTERVAL_SEC = max(
    0.0,
    float(os.getenv("KIS_LATENCY_SUMMARY_INTERVAL_SEC", "60.0")),
)
REQUEST_PRIORITY_CRITICAL = 0
REQUEST_PRIORITY_ORDER_STATUS = 5
REQUEST_PRIORITY_PRICE = 10
REQUEST_PRIORITY_DEFAULT = 20
REQUEST_PRIORITY_VI = 30
_LOW_PRIORITY_MAX_WAIT_SLOTS = max(
    1.0,
    float(os.getenv("KIS_LOW_PRIORITY_MAX_WAIT_SLOTS", "25")),
)
_client_lock = asyncio.Lock()
_client: httpx.AsyncClient | None = None
_client_factory: object | None = None
_latency_windows: dict[tuple[str, str], dict] = {}
_rate_waiters: list[dict] = []
_rate_waiter_seq = 0
SEND_GUARD_BLOCKED_MSG_CD = "LOCAL_SEND_GUARD_BLOCKED"
RATE_LIMIT_CODES = frozenset({"EGW00201"})


def _low_priority_max_wait_sec() -> float:
    """Keep starvation behavior equivalent across PAPER and REAL rate caps."""
    return _RATE_INTERVAL * _LOW_PRIORITY_MAX_WAIT_SLOTS


def _next_rate_waiter(now: float) -> dict | None:
    if not _rate_waiters:
        return None
    critical = [
        item for item in _rate_waiters
        if item["priority"] <= REQUEST_PRIORITY_CRITICAL
    ]
    if critical:
        return min(critical, key=lambda item: item["seq"])
    starved = [
        item for item in _rate_waiters
        if now - item["queued_at"] >= _low_priority_max_wait_sec()
    ]
    if starved:
        return min(starved, key=lambda item: item["seq"])
    return min(_rate_waiters, key=lambda item: (item["priority"], item["seq"]))


def _wake_rate_waiters() -> None:
    for item in _rate_waiters:
        item["wake"].set()


async def _wait_for_rate_slot(priority: int) -> None:
    """Wait for a priority-ordered slot without a fallible central dispatcher.

    Every requester owns its wait. Cancellation removes the waiter synchronously,
    and every wake-up rechecks both priority and the elapsed rate budget.
    """
    global _last_call_at, _rate_waiter_seq
    _rate_waiter_seq += 1
    waiter = {
        "priority": int(priority),
        "seq": _rate_waiter_seq,
        "queued_at": time.monotonic(),
        "wake": asyncio.Event(),
    }
    _rate_waiters.append(waiter)
    _wake_rate_waiters()
    try:
        while True:
            waiter["wake"].clear()
            now = time.monotonic()
            selected = _next_rate_waiter(now)
            if selected is waiter:
                wait = _RATE_INTERVAL - (now - _last_call_at)
                if wait <= 0:
                    _rate_waiters.remove(waiter)
                    _last_call_at = now
                    _wake_rate_waiters()
                    return
                await asyncio.sleep(wait)
                continue
            try:
                await asyncio.wait_for(
                    waiter["wake"].wait(),
                    timeout=max(0.1, _RATE_INTERVAL),
                )
            except TimeoutError:
                # Defensive self-heal if a wake signal is ever missed.
                pass
    finally:
        if waiter in _rate_waiters:
            _rate_waiters.remove(waiter)
            _wake_rate_waiters()


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


def _rate_limit_sleep_seconds(retry: int, path: str) -> float:
    """Use the established one-second delay for each confirmed rate-limit retry."""
    return 1.0


def _percentile(values: list[int], ratio: float) -> int:
    ordered = sorted(values)
    index = int((len(ordered) - 1) * ratio)
    return ordered[index]


def _log_latency(
    path: str,
    level: str,
    fields: dict,
    *,
    context: str | None,
    aggregate: bool,
) -> None:
    """반복 백그라운드 지연은 창 단위로 집계하고 주문 경로는 건별 유지한다."""
    enriched = dict(fields)
    if context:
        enriched["context"] = context
    if not aggregate or _LATENCY_SUMMARY_INTERVAL_SEC <= 0:
        log("LATENCY_HIGH", level=level, api_endpoint=path, **enriched)
        return

    now = time.monotonic()
    key = (path, context or "")
    window = _latency_windows.get(key)
    if window is None:
        _latency_windows[key] = {
            "started_at": now,
            "values": [int(fields["network_ms"])],
            "warn_count": 1 if level == "WARN" else 0,
        }
        log("LATENCY_HIGH", level=level, api_endpoint=path, **enriched)
        return

    values = window["values"]
    values.append(int(fields["network_ms"]))
    if level == "WARN":
        window["warn_count"] += 1
    if now - window["started_at"] < _LATENCY_SUMMARY_INTERVAL_SEC:
        return

    count = len(values)
    log(
        "LATENCY_SUMMARY",
        level="WARN" if window["warn_count"] else "INFO",
        api_endpoint=path,
        context=context,
        count=count,
        warn_count=window["warn_count"],
        p50_ms=_percentile(values, 0.50),
        p95_ms=_percentile(values, 0.95),
        max_ms=max(values),
        suppressed_count=max(0, count - 1),
        window_sec=round(now - window["started_at"], 3),
    )
    _latency_windows.pop(key, None)


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
    latency_context: str | None = None,
    aggregate_latency: bool = False,
    request_priority: int = REQUEST_PRIORITY_DEFAULT,
    allow_real_smoke_buy: bool = False,
    **kwargs,
) -> dict:
    """Rate-limited KIS REST 요청.

    조사 모드는 429/EGW00201에서 즉시 중단하며, 401 토큰 갱신은 항상 수행한다.
    """
    base_url = os.getenv("KIS_BASE_URL", "")
    url = base_url + path

    is_gated_real_buy = (
        method == "POST"
        and os.getenv("KIS_MODE", "PAPER") == "REAL"
        and tr_id == REAL_CASH_BUY_TR
        and not live.real_entry_enabled
    )
    if is_gated_real_buy and not allow_real_smoke_buy:
        log(
            "REAL_ENTRY_BLOCKED",
            level="CRIT",
            path=path,
            reason="REAL_READINESS_BELOW_100",
        )
        return _with_response_meta({
            "rt_cd": "1",
            "msg_cd": "LOCAL_REAL_READINESS_BLOCKED",
            "msg1": "REAL entry blocked until readiness reaches 100%",
        }, enabled=include_response_meta, http_status=None, request_sent=False)
    if is_gated_real_buy and allow_real_smoke_buy and _transient_retry == 0:
        log(
            "REAL_SMOKE_BUY_AUTHORIZED",
            level="CRIT",
            path=path,
            reason="EXPLICIT_SMOKE_TEST_CONFIRMATION",
        )

    total_start = time.monotonic()
    await _wait_for_rate_slot(request_priority)
    request_ready_at = time.monotonic()
    rate_wait_ms = int((request_ready_at - total_start) * 1000)

    try:
        if send_guard is not None and not send_guard():
            return _with_response_meta({
                "rt_cd": "1",
                "msg_cd": SEND_GUARD_BLOCKED_MSG_CD,
                "msg1": "request blocked by send guard",
            }, enabled=include_response_meta, http_status=None, request_sent=False)
        client = await _get_client()
        client_ready_at = time.monotonic()
        client_setup_ms = int((client_ready_at - request_ready_at) * 1000)
        # 클라이언트 준비 중 주문 마감이 지날 수 있으므로 전송 직전에 다시 검사한다.
        if send_guard is not None and not send_guard():
            return _with_response_meta({
                "rt_cd": "1",
                "msg_cd": SEND_GUARD_BLOCKED_MSG_CD,
                "msg1": "request blocked by send guard",
            }, enabled=include_response_meta, http_status=None, request_sent=False)
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
                latency_context=latency_context,
                aggregate_latency=aggregate_latency,
                request_priority=request_priority,
                allow_real_smoke_buy=allow_real_smoke_buy,
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
        return _with_response_meta({
            "rt_cd": "1",
            "msg_cd": exc.__class__.__name__,
            "msg1": str(exc),
        }, enabled=include_response_meta, http_status=None,
            request_sent=not isinstance(exc, httpx.ConnectError))
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
        _log_latency(
            path,
            "WARN",
            latency_fields,
            context=latency_context,
            aggregate=aggregate_latency,
        )
    elif network_ms > 200:
        _log_latency(
            path,
            "INFO",
            latency_fields,
            context=latency_context,
            aggregate=aggregate_latency,
        )

    # 429 — Rate limit 초과. GET만 재시도한다. POST는 서버 도달 후 반환된
    # 응답이므로 자동 재전송하지 않고 request_sent=True 메타와 함께 돌려준다.
    # 매도 호출부는 이를 확정 거절이 아닌 UNKNOWN으로 저장하고 주문 대사하며,
    # 끝내 확인되지 않으면 EXITING을 유지한다(중복 매도 방지 우선 정책).
    if resp.status_code == 429:
        log("RATE_LIMIT_HIT", level="WARN", path=path)
        if stop_on_rate_limit or method != "GET" or _app_retry >= 3:
            return _with_response_meta({
                "rt_cd": "1",
                "msg_cd": "HTTP_429",
                "msg1": "HTTP 429 rate limit",
            }, enabled=include_response_meta, http_status=429, request_sent=True)
        await asyncio.sleep(_rate_limit_sleep_seconds(_app_retry, path))
        result = await _request(
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
            latency_context=latency_context,
            aggregate_latency=aggregate_latency,
            request_priority=request_priority,
            allow_real_smoke_buy=allow_real_smoke_buy,
            **kwargs,
        )
        if _app_retry == 0 and str(result.get("rt_cd", "1")) == "0":
            log(
                "RATE_LIMIT_RECOVERED",
                level="INFO",
                path=path,
                initial_code="HTTP_429",
            )
        return result

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
                latency_context=latency_context,
                aggregate_latency=aggregate_latency,
                request_priority=request_priority,
                allow_real_smoke_buy=allow_real_smoke_buy,
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
                latency_context=latency_context,
                aggregate_latency=aggregate_latency,
                request_priority=request_priority,
                allow_real_smoke_buy=allow_real_smoke_buy,
                **kwargs,
            )

    msg_cd = str(data.get("msg_cd") or "")
    if msg_cd in RATE_LIMIT_CODES and stop_on_rate_limit:
        log(
            "RATE_LIMIT_HIT",
            level="WARN",
            path=path,
            msg_cd=msg_cd,
            msg1=data.get("msg1"),
        )
        return data

    if msg_cd in RATE_LIMIT_CODES:
        log(
            "RATE_LIMIT_HIT",
            level="WARN",
            path=path,
            msg_cd=msg_cd,
            msg1=data.get("msg1"),
        )
    if msg_cd in RATE_LIMIT_CODES and method == "GET" and _app_retry < 3:
        await asyncio.sleep(_rate_limit_sleep_seconds(_app_retry, path))
        result = await _request(
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
            latency_context=latency_context,
            aggregate_latency=aggregate_latency,
            request_priority=request_priority,
            allow_real_smoke_buy=allow_real_smoke_buy,
            **kwargs,
        )
        if _app_retry == 0 and str(result.get("rt_cd", "1")) == "0":
            log(
                "RATE_LIMIT_RECOVERED",
                level="INFO",
                path=path,
                initial_code=msg_cd,
            )
        return result

    if not isinstance(data, dict):
        return data
    return _with_response_meta(
        data,
        enabled=include_response_meta,
        http_status=int(resp.status_code),
        request_sent=True,
        tr_cont=str(getattr(resp, "headers", {}).get("tr_cont") or ""),
    )


async def get(
    path: str,
    params: dict | None = None,
    tr_id: str = "",
    timeout: float = _TIMEOUT,
    stop_on_rate_limit: bool = False,
    tr_cont: str = "",
    include_response_meta: bool = False,
    latency_context: str | None = None,
    aggregate_latency: bool = False,
    request_priority: int = REQUEST_PRIORITY_DEFAULT,
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
        latency_context=latency_context,
        aggregate_latency=aggregate_latency,
        request_priority=request_priority,
    )


async def post(
    path: str,
    body: dict | None = None,
    tr_id: str = "",
    timeout: float = _TIMEOUT,
    send_guard: Callable[[], bool] | None = None,
    latency_context: str | None = None,
    aggregate_latency: bool = False,
    include_response_meta: bool = False,
    allow_real_smoke_buy: bool = False,
    request_priority: int = REQUEST_PRIORITY_CRITICAL,
) -> dict:
    return await _request(
        "POST",
        path,
        tr_id=tr_id,
        timeout=timeout,
        send_guard=send_guard,
        latency_context=latency_context,
        aggregate_latency=aggregate_latency,
        include_response_meta=include_response_meta,
        allow_real_smoke_buy=allow_real_smoke_buy,
        request_priority=request_priority,
        json=body,
    )
