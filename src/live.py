"""모듈 간 공유 라이브 상태 — UI SSE 및 API에서 읽는다."""

import asyncio
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

from src.utils.logger import event_label

KST = ZoneInfo("Asia/Seoul")
_TICK_HISTORY_MAX = 120

# 마지막 WebSocket 틱 가격 (HOLDING 중에만 갱신)
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
    """F4 on_tick에서 호출 — 틱 가격 갱신 + SSE 브로드캐스트."""
    global last_tick_price, last_tick_ticker
    last_tick_price = price
    last_tick_ticker = ticker
    _tick_history.append({
        "ts": datetime.now(KST).isoformat(),
        "ticker": ticker,
        "price": price,
    })
    _broadcast({"type": "tick", "ticker": ticker, "price": price})


def tick_history(ticker: str | None = None) -> list[dict]:
    if ticker:
        return [row for row in _tick_history if row.get("ticker") == ticker]
    return list(_tick_history)


def clear_tick_history() -> None:
    _tick_history.clear()


def push_status() -> None:
    """상태 변경 시 호출 — SSE 클라이언트에 갱신 신호."""
    _broadcast({"type": "status"})


def push_log(event: str, level: str, **kwargs) -> None:
    """로그 이벤트 발생 시 호출 — SSE 클라이언트에 새 로그 신호."""
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
