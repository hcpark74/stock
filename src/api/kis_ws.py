import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from datetime import datetime
from zoneinfo import ZoneInfo

import websockets

from src.api import auth
from src.utils.logger import log

KST = ZoneInfo("Asia/Seoul")

_RETRY_INTERVAL_BASE = 2     # 최초 재연결 대기 (초)
_RETRY_INTERVAL_MAX  = 30    # 지수 백오프 상한 (초)
_STALE_TIMEOUT       = 30.0  # 수신 중단 감지 기준 (초)
_CRIT_THRESHOLD      = 10    # 연속 실패 N회 이후 CRIT 로그


async def subscribe(
    ticker: str,
    on_tick: Callable[[dict], Awaitable[None]],
    *,
    stop_if: Callable[[], bool] | None = None,
    on_connection_change: Callable[[bool], None] | None = None,
) -> None:
    """
    KIS WebSocket 실시간 체결 구독 (PRD §F4, §6-3).
    지수 백오프로 무한 재연결. stop_if() == True 이면 즉시 반환.
    """
    ws_url = os.getenv("KIS_WS_URL", "ws://ops.koreainvestment.com:31000")
    consec = 0
    interval = _RETRY_INTERVAL_BASE
    ws_key_ready = False

    while True:
        if stop_if and stop_if():
            return

        connected = False
        try:
            if not ws_key_ready:
                # OAuth token과 별개인 WS 전용 접속키 1회 발급.
                # 발급 실패가 subscribe 밖으로 전파되면 F4 모니터링 전체가 죽으므로
                # 재연결 루프 안에서 백오프와 함께 재시도한다.
                await auth.refresh_ws_key()
                ws_key_ready = True
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=10) as ws:
                await _send_subscribe(ws, ticker)
                connected = True
                if on_connection_change is not None:
                    on_connection_change(True)
                log("WS_CONNECTED", level="INFO", ticker=ticker, consec_failures=consec)
                consec = 0
                interval = _RETRY_INTERVAL_BASE

                while True:
                    if stop_if and stop_if():
                        return
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=_STALE_TIMEOUT)
                    except asyncio.TimeoutError:
                        raise TimeoutError(f"데이터 수신 없음 >{_STALE_TIMEOUT}s")
                    tick = _parse_tick(raw)
                    if tick:
                        await on_tick(tick)

        except Exception as e:
            if on_connection_change is not None:
                on_connection_change(False)
            connected = False
            consec += 1
            level = "CRIT" if consec >= _CRIT_THRESHOLD else "WARN"
            log("WS_DISCONNECTED", level=level,
                ticker=ticker, consec=consec, error=repr(e))
            if stop_if and stop_if():
                return
            await asyncio.sleep(interval)
            interval = min(interval * 2, _RETRY_INTERVAL_MAX)
        finally:
            if connected and on_connection_change is not None:
                on_connection_change(False)


async def _send_subscribe(ws: websockets.WebSocketClientProtocol, ticker: str) -> None:
    req = {
        "header": {
            "approval_key": auth.get_ws_key(),
            "custtype": "P",
            "tr_type": "1",
            "content-type": "utf-8",
        },
        "body": {
            "input": {
                "tr_id": "H0STCNT0",   # 주식 체결 실시간 조회
                "tr_key": ticker,
            }
        },
    }
    await ws.send(json.dumps(req, ensure_ascii=False))


def _exchange_iso(hms: str, now: datetime | None = None) -> str | None:
    """체결시간 HHMMSS를 당일 KST 오프셋 포함 ISO8601로 변환. 유효하지 않으면 None."""
    if not hms or len(hms) != 6 or not hms.isdigit():
        return None
    hh, mm, ss = int(hms[0:2]), int(hms[2:4]), int(hms[4:6])
    if hh > 23 or mm > 59 or ss > 59:
        return None
    base = now or datetime.now(KST)
    return base.replace(hour=hh, minute=mm, second=ss, microsecond=0).isoformat()


def _parse_tick(raw: str) -> dict | None:
    """KIS 체결 응답 파싱 → {ticker, price, volume, qty, exchange_time, source_ts}.

    체결시간(거래소 시각)을 오프셋 포함 ISO(``source_ts``)로 함께 제공한다. 시각이
    없거나 유효하지 않으면 ``source_ts=None``으로 표시해 하위에서 naive 시각을
    소리 없이 받아들이지 않게 한다.
    """
    try:
        if raw.startswith("{"):
            return None  # 시스템/PINGPONG 메시지
        parts = raw.split("|")
        if len(parts) < 4:
            return None
        fields = parts[3].split("^")
        exchange_time = fields[1] if len(fields) > 1 else ""
        qty = int(fields[12])
        return {
            "ticker": fields[0],
            "price": float(fields[2]),
            "volume": qty,
            "qty": qty,
            "exchange_time": exchange_time,
            "source_ts": _exchange_iso(exchange_time),
        }
    except Exception:
        return None
