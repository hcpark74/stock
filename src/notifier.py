import asyncio
import os

import httpx

from src.utils.logger import log

_queue: asyncio.Queue[str] = asyncio.Queue()
_SEND_INTERVAL = 1.1  # Telegram 동일 chat 기준 1건/초 rate limit (PRD §6)

_LEVEL_LABELS = {
    "CRIT": "긴급",
    "WARN": "확인",
    "INFO": "알림",
}

_ALERT_RULES = {
    "STALE_POSITION_DETECTED": {
        "title": "전일 포지션 잔류 의심",
        "situation": "이전 거래일 상태 파일에 포지션 정보가 남아 있습니다.",
        "action": "계좌 보유 수량과 미체결 주문을 확인하고, 필요하면 수동 정리 후 재시작하세요.",
    },
    "TIMEOUT_ORDER_FAILED": {
        "title": "10시 청산 주문 실패",
        "situation": "자동 청산 주문이 실패했거나 체결 확인이 끝나지 않았습니다.",
        "action": "즉시 계좌에서 보유 수량을 확인하고 수동 청산하세요.",
    },
    "TOKEN_REFRESH_FAIL": {
        "title": "KIS 토큰 갱신 실패",
        "situation": "API 인증 토큰을 갱신하지 못해 자동매매를 계속하기 어렵습니다.",
        "action": "KIS 인증 정보와 네트워크 상태를 확인하고 프로세스를 재시작하세요.",
    },
    "WS_KEY_REFRESH_FAIL": {
        "title": "웹소켓 키 갱신 실패",
        "situation": "실시간 시세 연결용 키를 발급받지 못했습니다.",
        "action": (
            "KIS 인증 상태를 확인하세요. 보유 중이면 REST 시세와 계좌 상태를 직접 확인하세요."
        ),
    },
    "TIME_SYNC_WARN": {
        "title": "시스템 시각 오차 감지",
        "situation": "PC 시각과 기준 시각의 차이가 허용 범위를 넘었습니다.",
        "action": "Windows 시간 동기화를 확인하세요. 장 시작 전이면 자동매매 재시작을 권장합니다.",
    },
    "PROCESS_RESTART_DETECTED": {
        "title": "프로세스 재시작 감지",
        "situation": "프로그램이 재시작되어 이전 상태 복구를 시도했습니다.",
        "action": "대시보드의 현재 포지션과 실제 계좌 상태가 일치하는지 확인하세요.",
    },
    "ENTRY_FAIL": {
        "title": "진입 실패",
        "situation": "매수 주문이 체결되지 않아 오늘 진입을 중단했습니다.",
        "action": "미체결 주문이 남아 있지 않은지 확인하세요.",
    },
    "ENTRY_EXECUTED": {
        "title": "진입 체결",
        "situation": "매수 주문이 체결되어 포지션 추적을 시작합니다.",
        "action": "대시보드에서 수량, 진입가, 손절 기준을 확인하세요.",
    },
    "TARGET_LOCKED": {
        "title": "대상 종목 확정",
        "situation": "F2에서 오늘 매매 후보가 확정되었습니다.",
        "action": "09:00 진입 전까지 종목과 예상 갭을 확인하세요.",
    },
    "NO_TARGET": {
        "title": "오늘 매매 대상 없음",
        "situation": "F1 필터를 통과한 종목이 없습니다.",
        "action": "오늘 자동 진입은 건너뜁니다.",
    },
}


async def send(event: str, level: str = "INFO", message: str = "") -> None:
    """
    Telegram 알림을 비동기 큐에 추가 (non-blocking).
    운영자가 바로 이해할 수 있도록 제목, 상황, 조치, 세부 순서로 구성한다.
    """
    text = _format_alert_text(event, level, message)
    await _queue.put(text)


def _format_alert_text(event: str, level: str = "INFO", message: str = "") -> str:
    rule = _ALERT_RULES.get(event, {})
    severity = _LEVEL_LABELS.get(level, level)
    prefix = "🔴 " if level == "CRIT" else "🟡 " if level == "WARN" else "ℹ️ "
    title = rule.get("title") or event.replace("_", " ").title()
    situation = rule.get("situation")
    action = rule.get("action")

    lines = [f"{prefix}{severity}: {title}"]
    if situation:
        lines.append(f"상황: {situation}")
    if action:
        lines.append(f"조치: {action}")
    if message:
        lines.append(f"세부: {_clean_message(message)}")
    lines.append(f"코드: {event}")
    return "\n".join(lines)


def _clean_message(message: str) -> str:
    return " ".join(str(message).split())


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
