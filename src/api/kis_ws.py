import asyncio
import json
import os
import re
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

# H0STCNT0 응답 필드 수. KIS 공식 저장소 open-trading-api의
# examples_llm/domestic_stock/ccnl_krx/ccnl_krx.py columns 목록 기준 46개
# (0=MKSC_SHRN_ISCD, 1=STCK_CNTG_HOUR, 2=STCK_PRPR, 12=CNTG_VOL,
#  18=CTTR 체결강도, 21=CCLD_DVSN 체결구분, 10/11=ASKP1/BIDP1, 45=VI_STND_PRC).
_CNT_FIELD_COUNT = 46


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
                    # 한 프레임에 체결이 여러 건 올 수 있다. 전부 전달한다 —
                    # 프레임이 묶이는 건 체결 폭주 구간이고, 그 구간이 바로
                    # 트레일링·하드스탑 판정이 가장 민감한 구간이다.
                    for tick in _parse_ticks(raw):
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


def _split_records(body: str) -> list[list[str]]:
    """프레임 데이터부를 체결 레코드(필드 배열) 목록으로 나눈다.

    공식 구현(open-trading-api)은 이 부분을
    ``pd.read_csv(StringIO(body), sep="^", names=<46개 컬럼>)`` 로 읽고
    ``df.iterrows()`` 로 행을 순회한다 — 즉 한 프레임에 체결이 여러 건 온다.
    read_csv 기준이면 레코드는 줄바꿈으로 나뉘지만, 구분자 형태에 의존하지
    않도록 줄바꿈과 ``^``를 모두 값 구분자로 보고 필드 수로 잘라 두 형태를
    같게 처리한다.

    필드 수가 배수로 떨어지지 않으면 쪼개지 않는다 — 오정렬된 레코드를
    만들어 잘못된 가격을 내보내느니 첫 레코드만 쓰는 편이 안전하다.
    """
    body = body.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    if not body:
        return []
    values = re.split(r"[\^\n]", body)
    if len(values) < _CNT_FIELD_COUNT or len(values) % _CNT_FIELD_COUNT != 0:
        return [values]
    return [
        values[i:i + _CNT_FIELD_COUNT]
        for i in range(0, len(values), _CNT_FIELD_COUNT)
    ]


def _tick_from_fields(fields: list[str]) -> dict | None:
    """단일 체결 레코드 → tick dict. 파싱 불가면 None."""
    try:
        exchange_time = fields[1] if len(fields) > 1 else ""
        qty = int(fields[12])
        return {
            "ticker": fields[0],
            "price": float(fields[2]),
            "volume": qty,
            "qty": qty,
            "exchange_time": exchange_time,
            "source_ts": _exchange_iso(exchange_time),
            # 해석하지 않은 필드까지 순서 그대로 넘긴다. 인덱스별 의미는 확인
            # 됐지만(체결강도 18·체결구분 21·최우선호가 10/11 등) 해석은 트랙
            # 작업에서 하고, 여기서는 원본을 그대로 캡처에 남긴다.
            # 매매 판단 경로는 이 값을 읽지 않는다.
            "raw": fields,
        }
    except Exception:
        return None


def _parse_ticks(raw: str) -> list[dict]:
    """KIS 체결 프레임 파싱 → tick 리스트. 한 프레임에 여러 건이 올 수 있다.

    체결시간(거래소 시각)을 오프셋 포함 ISO(``source_ts``)로 함께 제공한다.
    시각이 없거나 유효하지 않으면 ``source_ts=None``으로 표시해 하위에서 naive
    시각을 소리 없이 받아들이지 않게 한다.
    """
    try:
        if raw.startswith("{"):
            return []  # 시스템/PINGPONG 메시지
        parts = raw.split("|")
        if len(parts) < 4:
            return []
        ticks = [_tick_from_fields(f) for f in _split_records(parts[3])]
        return [t for t in ticks if t is not None]
    except Exception:
        return []


def _parse_tick(raw: str) -> dict | None:
    """프레임의 **첫** 체결만 반환하는 편의 래퍼.

    구독 루프는 ``_parse_ticks``를 쓴다 — 이 함수만 쓰면 다건 프레임에서
    나머지 체결을 잃는다.
    """
    ticks = _parse_ticks(raw)
    return ticks[0] if ticks else None
