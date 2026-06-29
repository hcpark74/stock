import asyncio
import json
import os
from collections.abc import Awaitable, Callable

import websockets

from src.api import auth
from src.utils.logger import log

_RETRY_INTERVAL_BASE = 2     # 최초 재연결 대기 (초)
_RETRY_INTERVAL_MAX  = 30    # 지수 백오프 상한 (초)
_STALE_TIMEOUT       = 30.0  # 수신 중단 감지 기준 (초)
_CRIT_THRESHOLD      = 10    # 연속 실패 N회 이후 CRIT 로그


async def subscribe(
    ticker: str,
    on_tick: Callable[[dict], Awaitable[None]],
    *,
    stop_if: Callable[[], bool] | None = None,
) -> None:
    """
    KIS WebSocket 실시간 체결 구독 (PRD §F4, §6-3).
    지수 백오프로 무한 재연결. stop_if() == True 이면 즉시 반환.
    """
    ws_url = os.getenv("KIS_WS_URL", "ws://ops.koreainvestment.com:31000")
    consec = 0
    interval = _RETRY_INTERVAL_BASE

    await auth.refresh_ws_key()  # OAuth token과 별개인 WS 전용 접속키 1회 발급

    while True:
        if stop_if and stop_if():
            return

        try:
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=10) as ws:
                log("WS_CONNECTED", level="INFO", ticker=ticker, consec_failures=consec)
                consec = 0
                interval = _RETRY_INTERVAL_BASE

                await _send_subscribe(ws, ticker)

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
            consec += 1
            level = "CRIT" if consec >= _CRIT_THRESHOLD else "WARN"
            log("WS_DISCONNECTED", level=level,
                ticker=ticker, consec=consec, error=repr(e))
            if stop_if and stop_if():
                return
            await asyncio.sleep(interval)
            interval = min(interval * 2, _RETRY_INTERVAL_MAX)


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


def _parse_tick(raw: str) -> dict | None:
    """KIS 체결 응답 파싱 → {"ticker", "price", "volume"}"""
    try:
        if raw.startswith("{"):
            return None  # 시스템/PINGPONG 메시지
        parts = raw.split("|")
        if len(parts) < 4:
            return None
        fields = parts[3].split("^")
        return {
            "ticker": fields[0],
            "price": float(fields[2]),
            "volume": int(fields[12]),
        }
    except Exception:
        return None
