"""F5. 타임아웃 청산 스케줄러 (11:00:00) — PRD §F5"""

import asyncio
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from src import db, notifier, state
from src.api import kis_rest
from src.utils.logger import log

KST = ZoneInfo("Asia/Seoul")
_RETRY = 3
_RETRY_INTERVAL = 2  # 초

_SELL_TR      = {"REAL": "TTTC0011U", "PAPER": "VTTC0011U"}
_BAL_TR       = {"REAL": "TTTC8434R", "PAPER": "VTTC8434R"}
_CCLD_TR      = {"REAL": "TTTC0081R", "PAPER": "VTTC0081R"}
_CANCEL_TR    = {"REAL": "TTTC0013U", "PAPER": "VTTC0013U"}
# 정정취소가능주문조회 — 실전계좌 전용 TR (모의투자 미지원, 최신 공식 샘플 기준)
_PSBL_CNCL_TR = {"REAL": "TTTC0084R"}

# precheck 결과. None=미조회/조회실패(상태 파일 수량 사용), 0=실제 미보유(주문 금지).
# 프로세스는 여러 거래일을 넘겨 살아 있으므로 날짜를 함께 저장해, precheck가
# 누락된 날 전일 값(특히 0)이 11시 청산을 건너뛰게 만드는 것을 막는다.
_prefetch_qty: int | None = None
_prefetch_date: str | None = None


async def _fetch_holding_qty(ticker: str, mode: str) -> int | None:
    """잔고 조회로 실제 보유 수량 확인. 조회 실패 시 None, 미보유 시 0."""
    try:
        resp = await kis_rest.get(
            "/uapi/domestic-stock/v1/trading/inquire-balance",
            tr_id=_BAL_TR[mode],
            params={
                "CANO": kis_rest.account_no(),
                "ACNT_PRDT_CD": kis_rest.account_cd(),
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "",
                "INQR_DVSN": "01",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "01",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
        )
    except Exception as e:
        log("F5_BALANCE_QUERY_FAIL", level="WARN", ticker=ticker, error=str(e))
        return None
    for item in resp.get("output1", []):
        if item.get("pdno") == ticker:
            return int(item.get("hldg_qty") or 0)
    return 0


async def _fetch_current_price(ticker: str) -> float:
    """주문 시점 기준가(현재 체결가) 조회 — 슬리피지 집계용."""
    resp = await kis_rest.get(
        "/uapi/domestic-stock/v1/quotations/inquire-price",
        tr_id="FHKST01010100",
        params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker},
    )
    out = resp.get("output", {}) if isinstance(resp.get("output"), dict) else {}
    return float(out.get("stck_prpr") or out.get("antc_cnpr") or 0)


async def precheck() -> None:
    """10:59:50 — 잔고 조회로 실제 보유 수량 확인."""
    global _prefetch_qty, _prefetch_date
    s = state.get()
    if s.position_status != "HOLDING" or not s.target_ticker:
        return
    mode = os.getenv("KIS_MODE", "PAPER")
    verified = await _fetch_holding_qty(s.target_ticker, mode)
    if verified is None:
        # 조회 실패 — 상태 파일 수량으로 진행
        _prefetch_qty = s.remaining_qty or 0
    else:
        # 확인된 잔고를 그대로 사용한다. 0이면 실제 미보유이므로 매도 주문을 내지 않는다.
        _prefetch_qty = verified
    _prefetch_date = datetime.now(KST).strftime("%Y%m%d")
    log("F5_PRECHECK", level="INFO", ticker=s.target_ticker, prefetch_qty=_prefetch_qty)


async def execute() -> None:
    """11:00:00 — 미청산 잔여 전량 시장가 청산. Retry 최대 3회.

    중복 매도 방지 원칙:
    - 재시도 전에 직전 주문의 체결/미체결 상태를 먼저 조회하고, 미체결 잔량이
      있으면 취소를 확정한 뒤에만 재주문한다 (잔고 조회만으로는 "접수됐지만
      아직 미체결"을 구분할 수 없다).
    - 이어서 실제 잔고를 재조회해 검증된 잔량만 다시 주문한다.
    - 부분체결은 잔량 기준으로 이어서 청산하고, 전량 확인 후에만 거래를 닫는다.
    - 최종 손익은 총 체결대금 / 총 체결수량의 평균 청산가로 계산한다.
    """
    s = state.get()
    if s.position_status != "HOLDING":
        return  # 이미 F4에서 청산됨

    if not await state.set_closed("TIMEOUT"):
        return

    ticker = s.target_ticker
    # precheck가 "오늘" 잔고 0을 확인했다면 매도할 수량이 없다 — 주문을 보내지 않는다.
    # 날짜가 다른(전일) precheck 값은 무시하고 상태 파일 수량으로 진행한다.
    today_str = datetime.now(KST).strftime("%Y%m%d")
    prefetch_valid = _prefetch_qty is not None and _prefetch_date == today_str
    qty = _prefetch_qty if prefetch_valid else (s.remaining_qty or 0)
    mode = os.getenv("KIS_MODE", "PAPER")
    entry = s.entry_price or 0.0
    remaining = qty
    filled_qty_total = 0
    filled_amt_total = 0.0
    last_sell_id = ""
    last_org_no = ""
    order_db_id = 0
    order_sent_qty = 0
    order_recorded_qty = 0
    order_recorded_amt = 0.0

    async def _apply_order_fill(cum_qty: int, cum_amt: float) -> None:
        """현재 주문의 누적 체결을 총계와 DB에 델타 반영한다."""
        nonlocal remaining, filled_qty_total, filled_amt_total
        nonlocal order_recorded_qty, order_recorded_amt
        delta = cum_qty - order_recorded_qty
        if delta <= 0:
            return
        filled_qty_total += delta
        filled_amt_total += max(0.0, cum_amt - order_recorded_amt)
        remaining = max(0, remaining - delta)
        order_recorded_qty = cum_qty
        order_recorded_amt = cum_amt
        if order_db_id:
            fill_status = "FILLED" if cum_qty >= order_sent_qty else "PARTIAL_FILL"
            avg_price = round(cum_amt / cum_qty) if cum_qty else 0
            try:
                await db.update_order_fill(
                    order_db_id, avg_price, cum_qty, 0, status=fill_status)
            except Exception as e:
                log("TIMEOUT_ORDER_DB_RECORD_FAILED", level="WARN",
                    ticker=ticker, order_id=last_sell_id, error=repr(e))

    for attempt in range(1, _RETRY + 1):
        if attempt > 1:
            order_known = True
            if last_sell_id:
                # 1) 직전 주문 상태 확인 — 체결분 반영, 미체결 잔량은 취소 확정.
                status = await _fetch_order_status(last_sell_id, mode)
                if status is None:
                    order_known = False
                    log("TIMEOUT_ORDER_STATUS_UNKNOWN", level="WARN",
                        ticker=ticker, attempt=attempt, order_id=last_sell_id)
                else:
                    await _apply_order_fill(status["ccld_qty"], status["ccld_amt"])
                    if remaining > 0 and status["rmn_qty"] > 0:
                        status = await _cancel_and_confirm(last_sell_id, last_org_no, mode)
                        if status is None:
                            if attempt < _RETRY:
                                await asyncio.sleep(_RETRY_INTERVAL)
                            continue
                        await _apply_order_fill(status["ccld_qty"], status["ccld_amt"])
                        if order_db_id and order_recorded_qty < order_sent_qty:
                            # 잔량이 취소로 소멸 — 감사 이력을 실제 계좌 상태와 일치시킨다.
                            try:
                                await db.update_order_status(order_db_id, "CANCELLED")
                            except Exception as e:
                                log("TIMEOUT_ORDER_DB_RECORD_FAILED", level="WARN",
                                    ticker=ticker, order_id=last_sell_id, error=repr(e))
            if remaining <= 0:
                break
            # 2) 실제 잔고 재검증 — 검증된 수량만 재주문한다.
            verified = await _fetch_holding_qty(ticker, mode)
            if verified is None:
                log("TIMEOUT_BALANCE_RECHECK_FAILED", level="WARN",
                    ticker=ticker, attempt=attempt)
                if attempt < _RETRY:
                    await asyncio.sleep(_RETRY_INTERVAL)
                continue
            remaining = min(remaining, verified)
            if remaining <= 0:
                break
            if not order_known:
                # 직전 주문 상태 불명 + 잔고 존재 — 미체결 주문이 살아 있을 수 있어
                # 재주문하지 않는다.
                if attempt < _RETRY:
                    await asyncio.sleep(_RETRY_INTERVAL)
                continue

        if remaining <= 0:
            break

        # order_price 기준가는 주문 발송 전에 조회한다 — 발송 후 조회하면
        # 시장가 체결 이후 가격이 기준이 되어 슬리피지가 왜곡된다.
        # 조회 실패 시 0.0 (슬리피지 집계에서 제외될 뿐, 청산 자체는 막지 않는다).
        ref_price = 0.0
        if s.trade_id:
            try:
                ref_price = await _fetch_current_price(ticker)
            except Exception as e:
                log("F5_PRICE_QUERY_FAIL", level="WARN",
                    ticker=ticker, error=str(e))

        try:
            resp = await _send_sell(ticker, remaining, mode)
            output = resp.get("output", {})
            last_sell_id = output.get("ODNO", "")
            last_org_no = output.get("KRX_FWDG_ORD_ORGNO", "")
        except Exception as e:
            log("TIMEOUT_RETRY", level="WARN", ticker=ticker, attempt=attempt, error=str(e))
            if attempt < _RETRY:
                await asyncio.sleep(_RETRY_INTERVAL)
            continue

        order_sent_qty = remaining
        order_recorded_qty = 0
        order_recorded_amt = 0.0
        order_db_id = 0
        if s.trade_id:
            # 주문 발송 직후 PENDING으로 기록. DB 기록 실패는 재주문 사유가 아니므로
            # 주문 실패 경로와 분리해 로그만 남긴다.
            try:
                order_db_id = await db.record_order(
                    s.trade_id, last_sell_id, "SELL", remaining, ref_price,
                    "TIMEOUT_SELL", ticker, s.target_name,
                )
            except Exception as e:
                log("TIMEOUT_ORDER_DB_RECORD_FAILED", level="WARN",
                    ticker=ticker, order_id=last_sell_id, error=repr(e))

        fill = await _poll_fill(last_sell_id, timeout_sec=30, expect_qty=remaining)
        if fill:
            fill_qty = int(fill.get("fill_qty") or 0)
            fill_amt = float(fill.get("fill_amt") or fill["fill_price"] * fill_qty)
            await _apply_order_fill(fill_qty, fill_amt)
            if remaining <= 0:
                break
            # 부분체결 — 잔량은 다음 시도에서 주문 정리·잔고 재검증 후 이어서 매도.
            log("TIMEOUT_PARTIAL_FILL", level="WARN", ticker=ticker,
                attempt=attempt, fill_qty=fill_qty, remaining_qty=remaining)
        else:
            log("TIMEOUT_FILL_UNCONFIRMED", level="WARN", ticker=ticker,
                attempt=attempt, order_id=last_sell_id)
        if attempt < _RETRY:
            await asyncio.sleep(_RETRY_INTERVAL)

    if remaining <= 0:
        if filled_qty_total > 0 and filled_amt_total > 0:
            # 여러 번 나눠 체결돼도 평균 청산가(총 체결대금/총 수량)로 손익을 계산한다.
            exit_avg = round(filled_amt_total / filled_qty_total)
            await _finalize_close(s, ticker, entry, exit_avg, filled_qty_total)
            return
        if qty <= 0:
            # precheck에서 잔고 0 확인 — 애초에 매도할 수량이 없었다.
            log("TIMEOUT_NO_HOLDINGS", level="CRIT", ticker=ticker,
                state_qty=s.remaining_qty)
            await notifier.send(
                "TIMEOUT_NO_HOLDINGS", level="CRIT",
                message=(
                    f"11시 청산: {ticker} 실제 잔고가 0이라 매도 주문을 내지 않았습니다. "
                    "상태 파일과 계좌가 불일치합니다. 보유/체결 내역을 수동 확인하세요."
                ),
                ticker=ticker,
            )
            await state.persist(os.getenv("STATE_DIR", "data/state"),
                                datetime.now(KST).strftime("%Y%m%d"))
            return
        # 잔고는 0인데 체결가를 한 번도 확인하지 못한 경우 — 거래를 임의 가격으로
        # 닫지 않고 운영자 확인을 요청한다 (DB trade는 OPEN으로 남아 대조 가능).
        log("TIMEOUT_CLOSE_UNVERIFIED", level="CRIT", ticker=ticker,
            order_id=last_sell_id, message="잔고 0 확인, 체결가 미확인")
        await notifier.send(
            "TIMEOUT_CLOSE_UNVERIFIED", level="CRIT",
            message=(
                f"11시 청산: {ticker} 잔고 0으로 매도는 완료된 것으로 보이나 "
                "체결가를 확인하지 못했습니다. 주문/체결 내역을 수동 확인하세요."
            ),
            ticker=ticker,
        )
        await state.persist(os.getenv("STATE_DIR", "data/state"),
                            datetime.now(KST).strftime("%Y%m%d"))
        return

    # 의도적으로 state.persist를 호출하지 않는다 — 디스크는 HOLDING으로 남아,
    # 크래시 후 재시작하면 잔고 재검증을 거쳐 F4 추적을 재개할 수 있다.
    log("TIMEOUT_ORDER_FAILED", level="CRIT", ticker=ticker,
        attempt_count=_RETRY, last_error_code="", last_error_msg="Max retries exceeded")
    await notifier.send("TIMEOUT_ORDER_FAILED", level="CRIT",
                        message=f"11시 청산 실패! 수동 청산 필요. {ticker} {remaining}주",
                        ticker=ticker)


async def _finalize_close(s, ticker: str, entry: float, exit_price: float, exit_qty: int) -> None:
    """전량 체결 확인 후 거래 종료 기록. DB 실패는 재매도 사유가 아니므로 격리한다."""
    pnl_pct = round((exit_price / entry - 1) * 100, 2) if (entry and exit_price) else 0.0
    if s.trade_id:
        try:
            await db.close_trade(s.trade_id, exit_price, "TIMEOUT", pnl_pct, s.highest_step)
        except Exception as e:
            log("TIMEOUT_CLOSE_DB_FAILED", level="CRIT", ticker=ticker, error=repr(e))
            await notifier.send(
                "TIMEOUT_CLOSE_DB_FAILED", level="CRIT",
                message=f"11시 청산 완료 후 DB 기록 실패: {ticker}. 거래 이력을 수동 확인하세요.",
                ticker=ticker,
            )
    log("TIMEOUT_CLOSE", level="INFO", ticker=ticker,
        entry_price=entry, exit_price=exit_price, exit_qty=exit_qty,
        pnl_pct=pnl_pct, fill_latency_ms=0)
    await notifier.send("TIMEOUT_CLOSE", level="INFO",
                        message=f"11시 청산: {ticker} {exit_qty}주 @ {exit_price:,.0f}원",
                        ticker=ticker)
    await state.persist(os.getenv("STATE_DIR", "data/state"),
                        datetime.now(KST).strftime("%Y%m%d"))


async def _send_sell(ticker: str, qty: int, mode: str) -> dict:
    return await kis_rest.post(
        "/uapi/domestic-stock/v1/trading/order-cash",
        tr_id=_SELL_TR[mode],
        body={
            "CANO": kis_rest.account_no(),
            "ACNT_PRDT_CD": kis_rest.account_cd(),
            "PDNO": ticker,
            "ORD_DVSN": "01",
            "ORD_QTY": str(qty),
            "ORD_UNPR": "0",
        },
    )


def _ccld_params(order_id: str, today: str) -> dict:
    return {
        "CANO": kis_rest.account_no(),
        "ACNT_PRDT_CD": kis_rest.account_cd(),
        "INQR_STRT_DT": today,
        "INQR_END_DT": today,
        "SLL_BUY_DVSN_CD": "01",
        "INQR_DVSN": "00",
        "PDNO": "",
        "CCLD_DVSN": "00",
        "ORD_GNO_BRNO": "",
        "ODNO": order_id,
        "INQR_DVSN_3": "00",
        "INQR_DVSN_1": "",
        "EXCG_ID_DVSN_CD": "KRX",
        "CTX_AREA_FK100": "",
        "CTX_AREA_NK100": "",
    }


async def _fetch_order_status(order_id: str, mode: str) -> dict | None:
    """일별 체결 조회로 주문의 누적 체결·미체결 잔량 확인. 실패/미발견 시 None."""
    today = datetime.now(KST).strftime("%Y%m%d")
    try:
        resp = await kis_rest.get(
            "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
            tr_id=_CCLD_TR[mode],
            params=_ccld_params(order_id, today),
        )
    except Exception as e:
        log("F5_ORDER_STATUS_QUERY_FAIL", level="WARN", order_id=order_id, error=str(e))
        return None
    for item in resp.get("output1", []):
        if item.get("odno") == order_id:
            return {
                "ccld_qty": int(item.get("tot_ccld_qty") or 0),
                "ccld_amt": float(item.get("tot_ccld_amt") or 0),
                "rmn_qty": int(item.get("rmn_qty") or 0),
            }
    return None


async def _fetch_cancelable_qty(order_id: str, mode: str) -> int | None:
    """정정취소가능주문조회로 해당 주문의 취소가능수량 확인. 실패/미발견 시 None.

    KIS 공식 가이드는 취소 전 이 조회를 권고한다. 실전계좌 전용 API이므로
    모의투자(PAPER)에서는 호출 없이 None을 반환하고, 호출부는 바로 취소
    요청으로 진행한다.
    """
    if mode not in _PSBL_CNCL_TR:
        return None
    try:
        resp = await kis_rest.get(
            "/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl",
            tr_id=_PSBL_CNCL_TR[mode],
            params={
                "CANO": kis_rest.account_no(),
                "ACNT_PRDT_CD": kis_rest.account_cd(),
                "INQR_DVSN_1": "0",
                "INQR_DVSN_2": "0",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
        )
    except Exception as e:
        log("F5_PSBL_CANCEL_QUERY_FAIL", level="WARN", order_id=order_id, error=str(e))
        return None
    rows = resp.get("output") or resp.get("output1") or []
    for item in rows:
        if item.get("odno") == order_id:
            return int(item.get("psbl_qty") or 0)
    return None


async def _cancel_order(order_id: str, org_no: str, mode: str) -> bool:
    """미체결 잔량 전량 취소 요청 (RVSE_CNCL_DVSN_CD=02). 요청 성공 여부 반환."""
    try:
        await kis_rest.post(
            "/uapi/domestic-stock/v1/trading/order-rvsecncl",
            tr_id=_CANCEL_TR[mode],
            body={
                "CANO": kis_rest.account_no(),
                "ACNT_PRDT_CD": kis_rest.account_cd(),
                "KRX_FWDG_ORD_ORGNO": org_no,
                "ORGN_ODNO": order_id,
                "ORD_DVSN": "01",
                "RVSE_CNCL_DVSN_CD": "02",
                "ORD_QTY": "0",
                "ORD_UNPR": "0",
                "QTY_ALL_ORD_YN": "Y",
                # KIS 공식 샘플 필수 필드 — 누락 시 실전에서 취소가 거부될 수 있다
                "EXCG_ID_DVSN_CD": "KRX",
            },
        )
        return True
    except Exception as e:
        log("TIMEOUT_CANCEL_FAILED", level="WARN", order_id=order_id, error=str(e))
        return False


async def _cancel_and_confirm(order_id: str, org_no: str, mode: str) -> dict | None:
    """미체결 잔량 취소 후 확정된 주문 상태를 반환. 미확정이면 None.

    취소 전 취소가능수량을 조회해(best-effort) 0이면 — 그 사이 체결/취소된
    것이므로 — 취소 요청 없이 상태만 재조회한다.
    """
    psbl = await _fetch_cancelable_qty(order_id, mode)
    if psbl is None or psbl > 0:
        if not await _cancel_order(order_id, org_no, mode):
            return None
        await asyncio.sleep(_RETRY_INTERVAL)
    status = await _fetch_order_status(order_id, mode)
    if status is None or status["rmn_qty"] > 0:
        log("TIMEOUT_CANCEL_UNCONFIRMED", level="WARN", order_id=order_id)
        return None
    return status


async def _poll_fill(order_id: str, timeout_sec: int = 30, expect_qty: int = 0) -> dict | None:
    """매도 주문 체결을 timeout_sec 초 내에 폴링.

    expect_qty가 주어지면 누적 체결 수량이 전량에 도달할 때까지 대기하고,
    타임아웃 시 마지막으로 확인된 부분체결을 반환한다 — 부분체결을 전량으로
    오인해 조기 반환하지 않는다.
    """
    mode = os.getenv("KIS_MODE", "PAPER")
    today = datetime.now(KST).strftime("%Y%m%d")
    last_partial: dict | None = None
    for _ in range(timeout_sec):
        try:
            resp = await kis_rest.get(
                "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
                tr_id=_CCLD_TR[mode],
                params=_ccld_params(order_id, today),
            )
            for item in resp.get("output1", []):
                if item.get("odno") == order_id:
                    tot_qty = int(item.get("tot_ccld_qty") or 0)
                    tot_amt = float(item.get("tot_ccld_amt") or 0)
                    if tot_qty > 0:
                        last_partial = {
                            "fill_price": round(tot_amt / tot_qty),
                            "fill_qty": tot_qty,
                            "fill_amt": tot_amt,
                        }
                        if expect_qty <= 0 or tot_qty >= expect_qty:
                            return last_partial
        except Exception:
            pass
        await asyncio.sleep(1)
    return last_partial
