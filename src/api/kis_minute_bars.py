"""당일 분봉 API — scripts/kis_minute_bar_poc.py에서 승격.

트랙 B의 확정 봉 정정과 재시작 복구에 쓴다. 주문 경로 뒤에 서도록
BACKGROUND 우선순위를 쓰고, A의 진입 창(09:00~09:11)에는 호출하지 않는다.
"""

from datetime import datetime, time

from src.api import kis_rest

MINUTE_PATH = "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
MINUTE_TR = "FHKST03010200"

# A의 F1 선정(09:00)부터 F3 체결 마감(09:11)까지. B는 이 구간에 지표가 없어
# 잃는 것이 없고, PAPER 초당 1건 예산을 A의 진입과 다투면 안 된다.
FORBIDDEN_START = time(9, 0)
FORBIDDEN_END = time(9, 11)

_MAX_PAGES = 20


class MinuteBarError(Exception):
    """분봉 조회·파싱 실패. 호출부가 그날 정정을 건너뛰게 한다."""


def in_forbidden_window(now: datetime) -> bool:
    current = now.timetz().replace(tzinfo=None)
    return FORBIDDEN_START <= current < FORBIDDEN_END


def parse_minute_bars(response: dict) -> tuple[list[dict], dict]:
    """분봉 응답을 (정렬된 bar 리스트, 이슈 카운트)로 파싱한다.

    시각/OHLC 필드가 없는 봉은 추정하지 않고 이슈로 세고 제외한다.
    """
    rows = response.get("output2")
    if rows is None:
        rows = response.get("output")
    if not isinstance(rows, list):
        raise MinuteBarError("MINUTE_OUTPUT_MISSING")

    issues = {"empty_bar": 0, "field_missing": 0}
    bars: list[dict] = []
    for row in rows:
        if not isinstance(row, dict) or not row:
            issues["empty_bar"] += 1
            continue
        try:
            bars.append({
                "date": str(row["stck_bsop_date"]),
                "time": str(row["stck_cntg_hour"]),
                "open": float(row["stck_oprc"]),
                "high": float(row["stck_hgpr"]),
                "low": float(row["stck_lwpr"]),
                "close": float(row["stck_prpr"]),
                "volume": float(row.get("cntg_vol") or 0),
            })
        except (KeyError, TypeError, ValueError):
            issues["field_missing"] += 1
            continue

    bars.sort(key=lambda b: (b["date"], b["time"]))
    return bars, issues


async def fetch_minute_bars(ticker: str, *, hour_cursor: str = "") -> dict:
    """당일 분봉 한 페이지. 1페이지가 최근 약 30봉을 준다."""
    response = await kis_rest.get(
        MINUTE_PATH,
        tr_id=MINUTE_TR,
        params={
            "FID_ETC_CLS_CODE": "",
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": ticker,
            "FID_INPUT_HOUR_1": hour_cursor,
            "FID_PW_DATA_INCU_YN": "N",
        },
        stop_on_rate_limit=True,
        request_priority=kis_rest.REQUEST_PRIORITY_BACKGROUND,
    )
    if str(response.get("rt_cd") or "") != "0":
        raise MinuteBarError(
            f"MINUTE_PRICE_FAILED msg_cd={response.get('msg_cd')!r}"
        )
    return response


async def fetch_day_bars(
    ticker: str,
    *,
    max_pages: int = _MAX_PAGES,
) -> tuple[list[dict], dict]:
    """시간 커서로 당일 분봉을 역방향 페이지네이션한다.

    새 봉이 0인 페이지에서 멈춘다 — 커서가 진전하지 않는다는 뜻이다.
    """
    bars: list[dict] = []
    seen: set[tuple[str, str]] = set()
    issues = {"empty_bar": 0, "field_missing": 0}
    cursor = ""

    for _ in range(max_pages):
        response = await fetch_minute_bars(ticker, hour_cursor=cursor)
        page, page_issues = parse_minute_bars(response)
        issues["empty_bar"] += page_issues["empty_bar"]
        issues["field_missing"] += page_issues["field_missing"]

        fresh = [b for b in page if (b["date"], b["time"]) not in seen]
        if not fresh:
            break
        for bar in fresh:
            seen.add((bar["date"], bar["time"]))
        bars.extend(fresh)
        cursor = min(b["time"] for b in fresh)

    bars.sort(key=lambda b: (b["date"], b["time"]))
    return bars, issues
