"""F1. 장전 데이터 파싱 모듈 (08:40 ~ 08:58) — PRD §F1"""

from datetime import datetime
from zoneinfo import ZoneInfo

from src import db, notifier, state
from src.api import kis_rest
from src.utils.logger import log

KST = ZoneInfo("Asia/Seoul")

GAP_MIN = 0.030     # 갭 하단 필터
GAP_MAX = 0.070     # 갭 상단 필터
LIQUIDITY_TOP_PCT = 0.10  # 유동성 상위 10%

_EXCLUDED_PRODUCT_KEYWORDS = (
    "ETF",
    "ETN",
    "KODEX",
    "TIGER",
    "ACE",
    "SOL",
    "KBSTAR",
    "KOSEF",
    "HANARO",
    "ARIRANG",
    "TIMEFOLIO",
    "TREX",
    "인버스",
    "레버리지",
    "선물",
)


async def run() -> list[dict]:
    """
    KIS 예상 체결가 API로 전 종목 조회 후 갭·유동성 필터 적용.
    통과 종목 리스트 반환. day_skip 상태면 즉시 빈 리스트 반환.
    """
    s = state.get()
    if s.day_skip:
        return []

    # TODO: KIS 예상 체결가 API 페이지네이션 조회
    # (FHKST03010100 또는 동급 API, 50ms 간격 Rate Limit 준수)
    raw_candidates: list[dict] = await _fetch_all_premarket()

    # ── 갭 필터 ─────────────────────────────────────────────────────
    gap_filtered = [
        c for c in raw_candidates
        if GAP_MIN <= c.get("gap_pct", 0.0) < GAP_MAX
    ]

    if not gap_filtered:
        log("NO_TARGET", level="INFO", filter_count=0, reason="GAP_FILTER_EMPTY")
        await notifier.send(
            "NO_TARGET", level="INFO", message="당일 필터 통과 종목 없음. 거래 스킵.",
        )
        s.day_skip = True
        today = datetime.now(KST).strftime("%Y%m%d")
        await db.record_skip(today, "NO_TARGET", f"gap_filtered=0")
        return []

    # ── 유동성 필터 (5일 평균 거래대금 상위 10%) ─────────────────────
    total = len(gap_filtered)
    gap_filtered.sort(key=lambda c: c.get("avg_amount_5d", 0.0), reverse=True)
    threshold = max(1, int(total * LIQUIDITY_TOP_PCT))
    result = gap_filtered[:threshold]  # noqa: E501

    log("F1_DONE", level="INFO", total_candidates=total, passed=len(result))
    return result


async def _fetch_all_premarket() -> list[dict]:
    """
    KIS 등락률 순위 API (FHPST01710000) — KOSPI/KOSDAQ 각각 조회.
    장전 08:30~09:00 구간에는 예상 체결가 기준 등락률이 반영됨.
    각 종목 dict 필드: ticker, expected_price, prev_close, gap_pct,
                       avg_amount_5d, expected_amount, expected_qty
    """
    results: list[dict] = []
    for mkt in ("J", "Q"):  # J=KOSPI, Q=KOSDAQ
        try:
            resp = await kis_rest.get(
                "/uapi/domestic-stock/v1/ranking/fluctuation",
                tr_id="FHPST01710000",
                params={
                    "fid_cond_mrkt_div_code": mkt,
                    "fid_cond_scr_div_code": "20171",
                    "fid_input_iscd": "0000",
                    "fid_rank_sort_cls_code": "0",
                    "fid_input_cnt_1": "0",
                    "fid_prc_cls_code": "0",
                    "fid_input_price_1": "",
                    "fid_input_price_2": "",
                    "fid_vol_cnt": "",
                    "fid_trgt_cls_code": "0",
                    "fid_trgt_exls_cls_code": "0",
                    "fid_div_cls_code": "0",
                    "fid_rsfl_rate1": f"{GAP_MIN * 100:.1f}",
                    "fid_rsfl_rate2": f"{GAP_MAX * 100:.1f}",
                },
            )
        except Exception as e:
            log("F1_API_ERROR", level="WARN", market=mkt, error=repr(e))
            continue

        for item in resp.get("output", []):
            try:
                ticker = item.get("stck_shrn_iscd") or item.get("mksc_shrn_iscd")
                name = item.get("hts_kor_isnm", "")
                if not _is_common_stock_candidate(ticker, name):
                    continue
                prdy_ctrt = float(item.get("prdy_ctrt") or 0)
                expected_price = float(item.get("stck_prpr") or 0)
                if expected_price <= 0:
                    continue
                prev_close = expected_price / (1 + prdy_ctrt / 100)
                avrg_vol = float(item.get("avrg_vol") or 0)
                results.append({
                    "ticker": ticker,
                    "name": name,
                    "expected_price": expected_price,
                    "prev_close": round(prev_close),
                    "gap_pct": prdy_ctrt / 100,
                    "avg_amount_5d": avrg_vol * expected_price,
                    "expected_amount": float(item.get("acml_tr_pbmn") or 0),
                    "expected_qty": int(item.get("acml_vol") or 0),
                })
            except (KeyError, ValueError, ZeroDivisionError):
                continue
    return results


def _is_common_stock_candidate(ticker: str | None, name: str = "") -> bool:
    """Exclude ETF/ETN/leveraged/inverse products from F1 candidates."""
    if not ticker or len(ticker) != 6 or not ticker.isdigit():
        return False

    upper_name = name.upper()
    return not any(keyword in upper_name for keyword in _EXCLUDED_PRODUCT_KEYWORDS)
