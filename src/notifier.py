import asyncio
import os

import httpx

from src.utils.logger import log

_queue: asyncio.Queue[str] = asyncio.Queue()
_SEND_INTERVAL = 1.1  # Telegram 동일 chat 기준 1건/초 rate limit (PRD §6)


async def send(event: str, level: str = "INFO", message: str = "") -> None:
    """
    Telegram 알림을 비동기 큐에 추가 (non-blocking).
    CRIT 이벤트는 🔴 prefix 추가.
    """
    prefix = "🔴 " if level == "CRIT" else ""
    text = f"{prefix}[{level}] {event}\n{message}"
    await _queue.put(text)


async def worker() -> None:
    """
    백그라운드 태스크 — 큐 드레인 후 Telegram API 전송.
    main.py에서 asyncio.create_task(notifier.worker()) 로 구동.
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
                            "parse_mode": "Markdown",
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
