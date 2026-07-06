"""모듈 간 공유 라이브 상태. UI SSE 및 API에서 읽는다."""

import asyncio
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

from src.utils.logger import event_label

KST = ZoneInfo("Asia/Seoul")
_TICK_HISTORY_MAX = 5000
_MINUTE_HISTORY_MAX = 180

# 마지막 WebSocket 틱 가격. HOLDING 중에만 갱신된다.
last_tick_price: float | None = None
last_tick_ticker: str | None = None
_tick_history: deque[dict] = deque(maxlen=_TICK_HISTORY_MAX)

# WebSocket 연결 상태
ws_connected: bool = False

# NTP 마지막 측정값
ntp_offset_ms: float = -1.0
ntp_level: str = "OK"  # OK | WARN | CRIT | ERROR

# SSE 구독 큐 목록
_sse_queues: list[asyncio.Queue] = []


def push_tick(price: float, ticker: str | None = None) -> None:
    """F4 on_tick에서 호출. 틱 가격 갱신과 SSE 브로드캐스트를 수행한다."""
    global last_tick_price, last_tick_ticker
    last_tick_price = price
    last_tick_ticker = ticker
    ts = datetime.now(KST).isoformat()
    _tick_history.append({
        "ts": ts,
        "ticker": ticker,
        "price": price,
    })
    _broadcast({"type": "tick", "ticker": ticker, "price": price, "ts": ts})


def tick_history(ticker: str | None = None, since: datetime | str | None = None) -> list[dict]:
    since_dt: datetime | None = None
    if isinstance(since, str):
        try:
            since_dt = datetime.fromisoformat(since)
        except ValueError:
            since_dt = None
    elif isinstance(since, datetime):
        since_dt = since

    rows = [row for row in _tick_history if not ticker or row.get("ticker") == ticker]
    if since_dt is None:
        return rows

    filtered = []
    for row in rows:
        try:
            row_ts = datetime.fromisoformat(str(row.get("ts")))
        except ValueError:
            continue
        if row_ts >= since_dt:
            filtered.append(row)
    return filtered


def minute_price_history(ticker: str | None = None, since: datetime | str | None = None) -> list[dict]:
    rows = tick_history(ticker, since=since)
    buckets: dict[str, dict] = {}
    for row in rows:
        try:
            row_ts = datetime.fromisoformat(str(row.get("ts")))
        except ValueError:
            continue
        minute_ts = row_ts.replace(second=0, microsecond=0).isoformat()
        previous = buckets.get(minute_ts)
        buckets[minute_ts] = {
            "ts": minute_ts,
            "ticker": row.get("ticker"),
            "price": row.get("price"),
            "tick_count": int((previous or {}).get("tick_count", 0)) + 1,
        }
    return list(buckets.values())[-_MINUTE_HISTORY_MAX:]


def clear_tick_history() -> None:
    _tick_history.clear()


def push_status() -> None:
    """상태 변경 시 호출. SSE 클라이언트에 갱신 신호를 보낸다."""
    _broadcast({"type": "status"})


def push_log(event: str, level: str, **kwargs) -> None:
    """로그 이벤트 발생 시 호출. SSE 클라이언트에 새 로그 신호를 보낸다."""
    _broadcast({
        "type": "log",
        "event": event,
        "event_label": event_label(event),
        "level": level,
        **kwargs,
    })


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=64)
    _sse_queues.append(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    try:
        _sse_queues.remove(q)
    except ValueError:
        pass


def _broadcast(data: dict) -> None:
    dead = []
    for q in _sse_queues:
        try:
            q.put_nowait(data)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        _sse_queues.remove(q)