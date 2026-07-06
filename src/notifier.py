import asyncio
import os

import httpx

from src.utils.logger import log

_queue: asyncio.Queue[str] = asyncio.Queue()
_SEND_INTERVAL = 1.1


_ACTIONABLE_ALERT_EVENTS = {
    "ENTRY_EXECUTED",
    "PYRAMID_EXECUTED",
    "TRAILING_STOP",
    "HARD_STOP",
    "TIMEOUT_CLOSE",
    "SLIPPAGE_GUARD",
    "ENTRY_FAIL",
    "NO_TARGET",
    "VI_FILTER_ALL_EXCLUDED",
    "GAP_CHANGED",
    "F2_RETRY_EXHAUSTED",
}
_CRITICAL_ALERT_LEVELS = {"CRIT", "ERROR"}
_LEVEL_LABELS = {
    "CRIT": "긴급",
    "ERROR": "오류",
    "WARN": "확인",
    "INFO": "알림",
}

_ALERT_TITLES = {
    "ENTRY_EXECUTED": "매수 체결",
    "PYRAMID_EXECUTED": "추가 매수 체결",
    "TRAILING_STOP": "매도 체결",
    "HARD_STOP": "손절 매도",
    "TIMEOUT_CLOSE": "11시 청산",
    "SLIPPAGE_GUARD": "슬리피지 초과 청산",
    "ENTRY_FAIL": "진입 실패",
    "NO_TARGET": "오늘 진입 없음",
    "VI_FILTER_ALL_EXCLUDED": "오늘 진입 없음",
    "GAP_CHANGED": "진입 전 갭 변동",
    "F2_RETRY_EXHAUSTED": "오늘 진입 없음",
    "TIMEOUT_ORDER_FAILED": "11시 청산 주문 실패",
    "F4_SELL_ERROR": "매도 주문 오류",
    "TOKEN_REFRESH_FAIL": "KIS 토큰 갱신 실패",
    "WS_KEY_REFRESH_FAIL": "실시간 접속키 갱신 실패",
    "STALE_POSITION_DETECTED": "이전 포지션 상태 확인 필요",
    "PROCESS_RESTART_DETECTED": "프로세스 재시작 감지",
}


async def send(
    event: str,
    level: str = "INFO",
    message: str = "",
    ticker: str | None = None,
    name: str | None = None,
) -> None:
    """Queue a non-blocking Telegram alert when it is actionable."""
    if not _should_send_alert(event, level):
        return
    text = _format_alert_text(event, level, message, ticker=ticker, name=name)
    await _queue.put(text)


def _should_send_alert(event: str, level: str = "INFO") -> bool:
    return event in _ACTIONABLE_ALERT_EVENTS or level in _CRITICAL_ALERT_LEVELS


def _format_alert_text(
    event: str,
    level: str = "INFO",
    message: str = "",
    ticker: str | None = None,
    name: str | None = None,
) -> str:
    severity = _LEVEL_LABELS.get(level, level)
    title = _ALERT_TITLES.get(event) or event.replace("_", " ").title()
    stock = _format_stock(ticker, name)
    content = _clean_message(message) if message else title
    return "\n".join([
        f"[{severity}] {title}",
        f"종목 : {stock}",
        f"내용 : {content}",
    ])


def _format_stock(ticker: str | None = None, name: str | None = None) -> str:
    if ticker and name:
        return f"{ticker} {name}"
    if ticker:
        return ticker
    if name:
        return name
    return "-"


def _clean_message(message: str) -> str:
    return " ".join(str(message).split())


async def worker() -> None:
    """
    Background sender for Telegram API.
    main.py starts this with asyncio.create_task(notifier.worker()).
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    while True:
        text = await _queue.get()

        if token and chat_id:
            for attempt in range(1, 4):
                try:
                    async with httpx.AsyncClient(timeout=5) as client:
                        resp = await client.post(url, json={
                            "chat_id": chat_id,
                            "text": text,
                        })
                    if resp.status_code == 429:
                        retry_after = resp.json().get("parameters", {}).get("retry_after", 1)
                        await asyncio.sleep(float(retry_after))
                        continue
                    break
                except Exception as e:
                    if attempt == 3:
                        log("NOTIFICATION_FAILED", level="WARN", error=str(e))

        _queue.task_done()
        await asyncio.sleep(_SEND_INTERVAL)
