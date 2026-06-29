import ntplib

from src import live
from src.utils.logger import log

NTP_WARN_MS = 200.0
NTP_CRIT_MS = 500.0


def check_ntp(servers: list[str]) -> float:
    """
    NTP 서버 목록을 순서대로 시도해 첫 번째 성공 서버의 오차(ms)를 반환한다.
    전체 실패 시 -1.0 반환.
    PRD §4: ±200ms 허용, 500ms 초과 시 CRIT 알림.
    """
    last_error: Exception | None = None

    for server in servers:
        try:
            resp = ntplib.NTPClient().request(server, version=3)
            offset_ms = abs(resp.offset * 1000)

            live.ntp_offset_ms = round(offset_ms, 1)
            if offset_ms > NTP_CRIT_MS:
                live.ntp_level = "CRIT"
                log("TIME_SYNC_WARN", level="CRIT", ntp_server=server, offset_ms=round(offset_ms, 1))
            elif offset_ms > NTP_WARN_MS:
                live.ntp_level = "WARN"
                log("TIME_SYNC_WARN", level="WARN", ntp_server=server, offset_ms=round(offset_ms, 1))
            else:
                live.ntp_level = "OK"
                log("TIME_SYNC_OK", level="INFO", ntp_server=server, offset_ms=round(offset_ms, 1))

            return offset_ms
        except Exception as e:
            log("TIME_SYNC_FALLBACK", level="WARN", ntp_server=server, error=str(e))
            last_error = e

    live.ntp_offset_ms = -1.0
    live.ntp_level = "ERROR"
    log("TIME_SYNC_ERROR", level="CRIT", ntp_servers=servers, error=str(last_error))
    return -1.0
