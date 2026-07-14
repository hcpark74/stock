"""모듈 간 공유 라이브 상태. UI SSE 및 API에서 읽는다."""

import asyncio
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

from src.utils.logger import event_label, normalize_level

KST = ZoneInfo("Asia/Seoul")
_TICK_HISTORY_MAX = 5000
_MINUTE_HISTORY_MAX = 180

# 마지막 WebSocket 틱 가격. HOLDING 중에만 갱신된다.
last_tick_price: float | None = None
last_tick_ticker: str | None = None
_tick_history: deque[dict] = deque(maxlen=_TICK_HISTORY_MAX)
# 분봉은 tick 버퍼에서 파생하지 않고 별도 누적한다 — 거래가 활발해 tick 버퍼(5,000건)가
# 20분을 못 담아도 분 단위 가격흐름(최대 180분)은 유지된다.
_minute_history: deque[dict] = deque(maxlen=_MINUTE_HISTORY_MAX)

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
    now = datetime.now(KST)
    ts = now.isoformat()
    _tick_history.append({
        "ts": ts,
        "ticker": ticker,
        "price": price,
    })
    _accumulate_minute(now, ticker, price)
    _broadcast({"type": "tick", "ticker": ticker, "price": price, "ts": ts})


def _accumulate_minute(ts_dt: datetime, ticker: str | None, price: float) -> None:
    """같은 분이면 마지막 가격 갱신, 새 분이면 행 추가."""
    minute_ts = ts_dt.replace(second=0, microsecond=0).isoformat()
    last = _minute_history[-1] if _minute_history else None
    if last and last.get("ts") == minute_ts and last.get("ticker") == ticker:
        last["price"] = price
        last["tick_count"] = int(last.get("tick_count", 0)) + 1
    else:
        _minute_history.append({
            "ts": minute_ts,
            "ticker": ticker,
            "price": price,
            "tick_count": 1,
        })


def _parse_since(since: datetime | str | None) -> datetime | None:
    if isinstance(since, str):
        try:
            return datetime.fromisoformat(since)
        except ValueError:
            return None
    if isinstance(since, datetime):
        return since
    return None


def tick_history(ticker: str | None = None, since: datetime | str | None = None) -> list[dict]:
    since_dt = _parse_since(since)
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


def minute_price_history(
    ticker: str | None = None, since: datetime | str | None = None,
) -> list[dict]:
    """별도 누적된 분봉 조회. since는 분 단위로 내림해 진입 분을 포함한다."""
    since_dt = _parse_since(since)
    if since_dt is not None:
        since_dt = since_dt.replace(second=0, microsecond=0)
    rows: list[dict] = []
    for row in _minute_history:
        if ticker and row.get("ticker") != ticker:
            continue
        if since_dt is not None:
            try:
                row_ts = datetime.fromisoformat(str(row.get("ts")))
            except ValueError:
                continue
            if row_ts < since_dt:
                continue
        rows.append(dict(row))
    return rows


def clear_tick_history() -> None:
    _tick_history.clear()
    _minute_history.clear()


def push_status() -> None:
    """상태 변경 시 호출. SSE 클라이언트에 갱신 신호를 보낸다."""
    _broadcast({"type": "status"})


def push_log(event: str, level: str, **kwargs) -> None:
    """로그 이벤트 발생 시 호출. SSE 클라이언트에 새 로그 신호를 보낸다."""
    _broadcast({
        "type": "log",
        "event": event,
        "event_label": event_label(event),
        "level": normalize_level(level),
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
