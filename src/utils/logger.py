import json
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
_logger: logging.Logger | None = None

EVENT_LABELS = {
    "DAILY_STATE_RESET": "새 거래일 상태 초기화(Daily State Reset)",
    "CATCHUP_START": "누락 작업 보완 시작(Catch-up Start)",
    "PROCESS_RESTART_DETECTED": "프로세스 재시작 감지(Process Restart Detected)",
    "TIME_SYNC_OK": "시각 동기화 정상(Time Sync OK)",
    "TIME_SYNC_WARN": "시각 오차 경고(Time Sync Warning)",
    "TIME_SYNC_FALLBACK": "시각 동기화 서버 재시도(Time Sync Fallback)",
    "TIME_SYNC_ERROR": "시각 동기화 실패(Time Sync Error)",
    "TOKEN_REFRESHED": "KIS 토큰 갱신(Token Refreshed)",
    "TOKEN_LOADED_FROM_CACHE": "KIS 토큰 캐시 로드(Token Loaded From Cache)",
    "TOKEN_REFRESH_HTTP_ERR": "KIS 토큰 갱신 HTTP 오류(Token Refresh HTTP Error)",
    "TOKEN_REFRESH_ATTEMPT_FAIL": "KIS 토큰 갱신 재시도 실패(Token Refresh Attempt Failed)",
    "TOKEN_REFRESH_FAIL": "KIS 토큰 갱신 실패(Token Refresh Failed)",
    "TOKEN_EXPIRED": "KIS 토큰 만료(Token Expired)",
    "TOKEN_REVOKE_SKIP": "KIS 토큰 폐기 생략(Token Revoke Skipped)",
    "TOKEN_REVOKED": "KIS 토큰 폐기(Token Revoked)",
    "TOKEN_REVOKE_FAIL": "KIS 토큰 폐기 실패(Token Revoke Failed)",
    "TOKEN_REVOKE_ERR": "KIS 토큰 폐기 오류(Token Revoke Error)",
    "WS_KEY_REFRESHED": "웹소켓 키 갱신(WebSocket Key Refreshed)",
    "WS_KEY_HTTP_ERR": "웹소켓 키 HTTP 오류(WebSocket Key HTTP Error)",
    "WS_KEY_ATTEMPT_FAIL": "웹소켓 키 재시도 실패(WebSocket Key Attempt Failed)",
    "WS_KEY_REFRESH_FAIL": "웹소켓 키 갱신 실패(WebSocket Key Refresh Failed)",
    "LATENCY_HIGH": "API 지연 감지(High Latency)",
    "RATE_LIMIT_HIT": "API 호출 제한 감지(Rate Limit Hit)",
    "F1_DONE": "F1 필터 완료(F1 Done)",
    "F1_API_ERROR": "F1 API 오류(F1 API Error)",
    "F1_FETCH_DONE": "F1 API 조회 완료(F1 Fetch Done)",
    "F1_MARKET_INTERVAL": "F1 시장 간 대기(F1 Market Interval)",
    "F1_FILTER_EMPTY": "F1 필터 결과 없음(F1 Filter Empty)",
    "F1_RETRY_WAIT": "F1 재시도 대기(F1 Retry Wait)",
    "F1_EXPECTED_QUOTE_ERROR": "F1 예상가 조회 오류(F1 Expected Quote Error)",
    "F1_EXPECTED_COMPARE": "F1 예상체결 비교(F1 Expected Compare)",
    "F1_SNAPSHOT_SAVED": "F1 후보 스냅샷 저장(F1 Snapshot Saved)",
    "F1_SNAPSHOT_SAVE_ERROR": "F1 후보 스냅샷 저장 오류(F1 Snapshot Save Error)",
    "F1_SNAPSHOT_ROTATE_ERROR": "F1 후보 스냅샷 정리 오류(F1 Snapshot Rotate Error)",
    "NO_TARGET": "대상 종목 없음(No Target)",
    "F2_SKIPPED": "F2 종목 잠금 생략(F2 Skipped)",
    "VI_FILTER_ALL_EXCLUDED": "VI 필터 전부 제외(VI Filter All Excluded)",
    "TARGET_LOCKED": "대상 종목 잠금(Target Locked)",
    "F3_SKIPPED": "F3 진입 생략(F3 Skipped)",
    "F3_RECHECK": "F3 진입 전 재검증(F3 Recheck)",
    "F3_ENTRY_BLOCKED": "F3 진입 차단(F3 Entry Blocked)",
    "F3_DEADLINE_PARSE_ERROR": "F3 마감시각 파싱 오류(F3 Deadline Parse Error)",
    "ENTRY_ORDER_SENT": "진입 주문 전송(Entry Order Sent)",
    "ENTRY_PRE_ORDER_WAIT": "진입 주문 전 대기(Entry Pre-order Wait)",
    "ENTRY_RETRY_START": "진입 재시도 시작(Entry Retry Start)",
    "ENTRY_RETRY_SKIPPED": "진입 재시도 생략(Entry Retry Skipped)",
    "ENTRY_FILL_POLL_TIMEOUT": "진입 체결조회 시간초과(Entry Fill Poll Timeout)",
    "ENTRY_CANCEL_SENT": "진입 주문 취소 전송(Entry Cancel Sent)",
    "GAP_CHANGED": "진입 전 갭 변동(Gap Changed)",
    "INSUFFICIENT_BALANCE": "매수 가능 금액 부족(Insufficient Balance)",
    "ENTRY_FAIL": "진입 실패(Entry Failed)",
    "SLIPPAGE_GUARD": "슬리피지 가드 발동(Slippage Guard)",
    "ENTRY_EXECUTED": "진입 체결(Entry Executed)",
    "PYRAMID_TIMEOUT": "피라미딩 체결 시간 초과(Pyramid Timeout)",
    "PYRAMID_EXECUTED": "피라미딩 체결(Pyramid Executed)",
    "PYRAMID_SKIPPED": "피라미딩 생략(Pyramid Skipped)",
    "WS_CONNECTED": "웹소켓 연결(WebSocket Connected)",
    "WS_DISCONNECTED": "웹소켓 연결 끊김(WebSocket Disconnected)",
    "TICK_SPIKE_DROPPED": "이상 틱 제외(Tick Spike Dropped)",
    "F4_SELL_ERROR": "F4 매도 오류(F4 Sell Error)",
    "TRAILING_STOP": "트레일링 스탑 청산(Trailing Stop)",
    "HARD_STOP": "하드 스탑 청산(Hard Stop)",
    "F5_PRECHECK": "F5 청산 사전 확인(F5 Precheck)",
    "F5_PRECHECK_FAIL": "F5 청산 사전 확인 실패(F5 Precheck Failed)",
    "TIMEOUT_CLOSE": "타임아웃 청산(Timeout Close)",
    "TIMEOUT_RETRY": "타임아웃 청산 재시도(Timeout Retry)",
    "TIMEOUT_ORDER_FAILED": "타임아웃 청산 주문 실패(Timeout Order Failed)",
    "NOTIFICATION_FAILED": "알림 전송 실패(Notification Failed)",
    "ORDER_SMOKE_BUY_START": "주문 테스트 매수 시작(Order Smoke Buy Start)",
    "ORDER_SMOKE_BUY_FILLED": "주문 테스트 매수 체결(Order Smoke Buy Filled)",
    "ORDER_SMOKE_SELL_START": "주문 테스트 매도 시작(Order Smoke Sell Start)",
    "ORDER_SMOKE_SELL_FILLED": "주문 테스트 매도 체결(Order Smoke Sell Filled)",
}


class _JsonLinesHandler(logging.Handler):
    def __init__(self, log_dir: str) -> None:
        super().__init__()
        self._log_dir = Path(log_dir)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            extra: dict = getattr(record, "_extra", {})
            entry = {
                "ts": datetime.now(KST).isoformat(),
                "event": extra.get("event", record.getMessage()),
                "event_label": extra.get("event_label"),
                "level": extra.get("level", record.levelname),
                "ticker": extra.get("ticker"),
                **{
                    k: v
                    for k, v in extra.items()
                    if k not in ("event", "event_label", "ticker", "level")
                },
            }
            date_str = datetime.now(KST).strftime("%Y%m%d")
            log_file = self._log_dir / f"{date_str}.jsonl"
            with log_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            self.handleError(record)


def setup(log_dir: str) -> logging.Logger:
    global _logger
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    _logger = logging.getLogger("stock")
    _logger.setLevel(logging.DEBUG)
    if not _logger.handlers:
        _logger.addHandler(_JsonLinesHandler(log_dir))
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", "%H:%M:%S"))
        _logger.addHandler(ch)
    return _logger


_LEVEL_ALIAS = {"CRIT": "CRITICAL", "WARN": "WARNING"}


def event_label(event: str) -> str:
    return EVENT_LABELS.get(event, f"{event}({event})")


def log(event: str, level: str = "INFO", ticker: str | None = None, **kwargs) -> None:
    logger = logging.getLogger("stock")
    py_level = _LEVEL_ALIAS.get(level, level)
    lvl = getattr(logging, py_level, logging.INFO)
    label = event_label(event)
    extra = {"event": event, "event_label": label, "ticker": ticker, "level": level, **kwargs}
    logger.log(lvl, label, extra={"_extra": extra})
