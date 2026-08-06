"""매도 주문 전송 의도 영속화와 응답 유실 대사.

KIS 현금주문 API는 클라이언트 주문 ID를 받지 않는다. 따라서 로컬 상관 ID와
주문 시각·종목·수량을 전송 전에 저장하고, 응답이 유실되면 당일 주문조회에서
유일하게 일치하는 매도 주문을 찾는다. 찾지 못하거나 둘 이상이면 재주문하지
않고 EXITING으로 멈춘다.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from src import db, state
from src.api import kis_rest
from src.utils.logger import log

KST = ZoneInfo("Asia/Seoul")
EXIT_RECOVERY_VERSION = 1

_CCLD_TR = {"REAL": "TTTC0081R", "PAPER": "VTTC0081R"}
@dataclass(frozen=True)
class SubmissionResult:
    acknowledged: bool
    uncertain: bool
    order_id: str = ""
    org_no: str = ""
    order_db_id: int = 0
    response: dict | None = None
    matched_order: dict | None = None


def _today() -> str:
    return datetime.now(KST).strftime("%Y%m%d")


def _iso_now() -> str:
    return datetime.now(KST).isoformat()


def _intent_from_order_row(row: dict, *, fallback_reason: str | None = None) -> dict:
    phase = str(row.get("order_phase") or "")
    reason = {
        "TIMEOUT_SELL": "TIMEOUT",
        "SLIPPAGE_SELL": "SLIPPAGE_GUARD",
        # CLOSE_SELL은 TRAILING/HARD_STOP 공용 phase이므로 상태 파일의
        # 구체적인 reason을 우선 사용해야 한다.
        "CLOSE_SELL": fallback_reason or state.get().close_reason or "MANUAL",
    }.get(phase, fallback_reason or state.get().close_reason or "MANUAL")
    return {
        "client_order_id": row.get("client_order_id"),
        "order_db_id": int(row.get("id") or 0),
        "ticker": row.get("ticker"),
        "requested_qty": int(row.get("order_qty") or 0),
        "phase": phase,
        "reason": reason,
        "trigger_price": float(row.get("trigger_price") or 0),
        "prepared_at": row.get("ordered_at"),
        "submitted_at": row.get("submitted_at"),
        "submission_state": row.get("submission_state") or "PREPARED",
        "order_id": row.get("kis_order_id") or "",
        "org_no": "",
    }


async def merge_db_intent(data: dict, date: str) -> dict:
    """DB로 상태 파일의 빈 필드만 보완한다.

    전송 순서는 상태 파일 -> DB이므로 같은 client_order_id에서는 상태 파일이
    항상 같거나 더 최신이다. DB 값이 close reason이나 submission state를
    역행시키지 않게 상태 값을 마지막에 병합한다.
    """
    try:
        row = await db.get_unresolved_exit_intent(date)
    except Exception as exc:
        log(
            "EXIT_ORDER_DB_DEGRADED",
            level="CRIT",
            reason="MERGE_LOOKUP_FAILED",
            error=repr(exc),
        )
        return data
    if not row:
        return data
    pending = data.get("pending_exit")
    if not isinstance(pending, dict) or (
        str(row.get("client_order_id") or "")
        == str(pending.get("client_order_id") or "")
    ):
        merged = dict(data)
        fallback_reason = None
        if isinstance(pending, dict):
            fallback_reason = pending.get("reason")
        fallback_reason = fallback_reason or data.get("close_reason")
        intent = _intent_from_order_row(row, fallback_reason=fallback_reason)
        if isinstance(pending, dict):
            state_values = {
                key: value
                for key, value in pending.items()
                if value not in (None, "")
                and not (key == "order_db_id" and int(value or 0) == 0)
            }
            intent = {**intent, **state_values}
        merged["pending_exit"] = intent
        merged["position_status"] = "EXITING"
        return merged
    return data


async def _prepare_intent(
    *,
    qty: int,
    reason: str,
    phase: str,
    trigger_price: float,
) -> tuple[dict, int]:
    s = state.get()
    if not s.target_ticker or qty <= 0:
        raise RuntimeError("durable exit intent requires ticker and qty")
    intent = {
        "client_order_id": uuid.uuid4().hex,
        "order_db_id": 0,
        "ticker": s.target_ticker,
        "requested_qty": int(qty),
        "phase": phase,
        "reason": reason,
        "trigger_price": float(trigger_price),
        "prepared_at": _iso_now(),
        "submitted_at": None,
        "submission_state": "PREPARED",
        "order_id": "",
        "org_no": "",
    }
    if not await state.begin_exit_intent(intent):
        raise RuntimeError("another exit order is already active")
    # 청산의 필수 감사 원장은 상태 파일이다. SQLite 장애가 실제 보유 포지션의
    # 매도를 막아서는 안 되므로, 네트워크 전송 전에 이것만은 반드시 성공시킨다.
    try:
        await state.persist(os.getenv("STATE_DIR", "data/state"), _today())
    except Exception:
        await state.reject_exit_intent()
        raise

    order_db_id = 0
    if not s.trade_id:
        log(
            "EXIT_ORDER_DB_DEGRADED",
            level="CRIT",
            ticker=s.target_ticker,
            reason="MISSING_TRADE_ID",
        )
        return intent, 0
    try:
        order_db_id = await db.record_order(
            s.trade_id,
            "",
            "SELL",
            qty,
            0.0,
            phase,
            s.target_ticker,
            s.target_name,
            trigger_price=trigger_price,
            client_order_id=intent["client_order_id"],
            submission_state="PREPARED",
        )
        intent["order_db_id"] = order_db_id
        await state.update_pending_exit(order_db_id=order_db_id)
        await state.persist(os.getenv("STATE_DIR", "data/state"), _today())
        return intent, order_db_id
    except Exception as exc:
        if order_db_id:
            try:
                await db.update_order_submission(
                    order_db_id,
                    "REJECTED",
                    error_code="LOCAL_PERSIST_FAILED",
                    error_msg="order was not submitted",
                )
                await db.update_order_status(order_db_id, "FAILED")
            except Exception:
                pass
            await state.reject_exit_intent()
            try:
                await state.persist(os.getenv("STATE_DIR", "data/state"), _today())
            except Exception:
                pass
            raise
        # INSERT 자체가 실패했다면 상태 파일에는 완전한 의도가 남아 있다.
        # 감사 DB만 degraded로 표시하고 청산은 계속한다.
        log(
            "EXIT_ORDER_DB_DEGRADED",
            level="CRIT",
            ticker=s.target_ticker,
            reason="RECORD_ORDER_FAILED",
            error=repr(exc),
        )
        return intent, 0


async def _update_db_submission_best_effort(
    order_db_id: int,
    submission_state: str,
    **kwargs,
) -> None:
    if not order_db_id:
        return
    try:
        await db.update_order_submission(order_db_id, submission_state, **kwargs)
    except Exception as exc:
        log(
            "EXIT_ORDER_DB_DEGRADED",
            level="CRIT",
            order_db_id=order_db_id,
            submission_state=submission_state,
            error=repr(exc),
        )


async def _mark_submitting(intent: dict, order_db_id: int) -> None:
    submitted_at = _iso_now()
    await state.update_pending_exit(
        submission_state="SUBMITTING",
        submitted_at=submitted_at,
    )
    await state.persist(os.getenv("STATE_DIR", "data/state"), _today())
    await _update_db_submission_best_effort(
        order_db_id, "SUBMITTING", submitted=True
    )
    intent.update(submission_state="SUBMITTING", submitted_at=submitted_at)


async def _acknowledge(
    intent: dict,
    order_db_id: int,
    order_id: str,
    org_no: str,
) -> None:
    await state.update_pending_exit(
        submission_state="ACKNOWLEDGED",
        order_id=order_id,
        org_no=org_no,
    )
    await state.persist(os.getenv("STATE_DIR", "data/state"), _today())
    await _update_db_submission_best_effort(
        order_db_id,
        "ACKNOWLEDGED",
        kis_order_id=order_id,
    )
    intent.update(
        submission_state="ACKNOWLEDGED",
        order_id=order_id,
        org_no=org_no,
    )


def _certain_rejection(resp: dict | None) -> bool:
    """주문이 접수되지 않았음이 확실한 응답만 True로 분류한다.

    HTTP 상태가 없거나 2xx가 아닌 응답은 서버가 주문을 전달한 뒤 게이트웨이
    응답만 실패했을 수 있다. 로컬에서 전송 전 차단했음을 명시한 응답과,
    HTTP 200으로 돌아온 KIS 업무 거절만 재시도 가능한 확정 거절이다.
    """
    if not isinstance(resp, dict):
        return False
    meta = resp.get("_response_meta")
    if not isinstance(meta, dict):
        return False
    if meta.get("request_sent") is False:
        return True
    return (
        meta.get("http_status") == 200
        and str(resp.get("rt_cd") or "") == "1"
        and bool(str(resp.get("msg_cd") or "").strip())
    )


def _uncertain_response(resp: dict | None) -> bool:
    return not _certain_rejection(resp)


async def _mark_unknown(intent: dict, order_db_id: int, resp: dict | None) -> None:
    code = str((resp or {}).get("msg_cd") or "UNKNOWN")
    message = str((resp or {}).get("msg1") or "order response lost")[:300]
    await state.update_pending_exit(submission_state="UNKNOWN")
    await state.persist(os.getenv("STATE_DIR", "data/state"), _today())
    await _update_db_submission_best_effort(
        order_db_id,
        "UNKNOWN",
        error_code=code,
        error_msg=message,
    )
    intent["submission_state"] = "UNKNOWN"


async def _mark_rejected(order_db_id: int, resp: dict) -> None:
    await _update_db_submission_best_effort(
        order_db_id,
        "REJECTED",
        error_code=str(resp.get("msg_cd") or ""),
        error_msg=str(resp.get("msg1") or "")[:300],
    )
    if order_db_id:
        try:
            await db.update_order_status(order_db_id, "FAILED")
        except Exception as exc:
            log(
                "EXIT_ORDER_DB_DEGRADED",
                level="CRIT",
                order_db_id=order_db_id,
                submission_state="REJECTED",
                error=repr(exc),
            )
    await state.reject_exit_intent()
    await state.persist(os.getenv("STATE_DIR", "data/state"), _today())


def _parse_order_time(value: object, base: datetime) -> datetime | None:
    raw = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(raw) < 6:
        return None
    try:
        return base.replace(
            hour=int(raw[-6:-4]),
            minute=int(raw[-4:-2]),
            second=int(raw[-2:]),
            microsecond=0,
        )
    except ValueError:
        return None


def _candidate_from_row(row: dict, intent: dict) -> dict | None:
    order_id = str(row.get("odno") or row.get("ODNO") or "").strip()
    ticker = str(row.get("pdno") or row.get("PDNO") or "").strip()
    try:
        order_qty = int(float(row.get("ord_qty") or row.get("tot_ord_qty") or 0))
    except (TypeError, ValueError):
        return None
    side = str(
        row.get("sll_buy_dvsn_cd")
        or row.get("SLL_BUY_DVSN_CD")
        or ""
    ).strip()
    if side != "01":  # KIS 국내주식 주문조회: 01=매도, 02=매수
        return None
    if not order_id or ticker != str(intent.get("ticker") or ""):
        return None
    if order_qty != int(intent.get("requested_qty") or 0):
        return None

    prepared_raw = intent.get("submitted_at") or intent.get("prepared_at")
    try:
        prepared_at = datetime.fromisoformat(str(prepared_raw)).astimezone(KST)
    except (TypeError, ValueError):
        prepared_at = datetime.now(KST)
    order_at = _parse_order_time(
        row.get("ord_tmd") or row.get("ord_time") or row.get("ord_tm"),
        prepared_at,
    )
    if order_at is not None and not (
        prepared_at - timedelta(seconds=2)
        <= order_at
        <= prepared_at + timedelta(seconds=120)
    ):
        return None

    fill_qty = int(float(row.get("tot_ccld_qty") or 0))
    remaining_qty = int(
        float(row.get("rmn_qty") or max(0, order_qty - fill_qty))
    )
    fill_amt = float(row.get("tot_ccld_amt") or 0)
    fill_price = round(fill_amt / fill_qty) if fill_qty and fill_amt else float(
        row.get("avg_prvs") or 0
    )
    return {
        "order_id": order_id,
        "org_no": str(
            row.get("ord_gno_brno")
            or row.get("krx_fwdg_ord_orgno")
            or row.get("KRX_FWDG_ORD_ORGNO")
            or ""
        ),
        "order_qty": order_qty,
        "fill_qty": fill_qty,
        "remaining_qty": max(0, remaining_qty),
        "fill_amt": fill_amt,
        "fill_price": fill_price,
    }


async def find_matching_order(
    intent: dict,
    mode: str,
    *,
    excluded_order_ids: set[str] | None = None,
) -> tuple[str, dict | None]:
    """UNKNOWN 의도와 유일하게 일치하는 KIS 주문을 찾는다."""
    attempts = max(1, int(os.getenv("EXIT_RECONCILE_ATTEMPTS", "3")))
    interval = max(0.0, float(os.getenv("EXIT_RECONCILE_INTERVAL_SEC", "0.5")))
    for attempt in range(1, attempts + 1):
        try:
            resp = await kis_rest.get(
                "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
                tr_id=_CCLD_TR[mode],
                params={
                    "CANO": kis_rest.account_no(),
                    "ACNT_PRDT_CD": kis_rest.account_cd(),
                    "INQR_STRT_DT": _today(),
                    "INQR_END_DT": _today(),
                    "SLL_BUY_DVSN_CD": "01",
                    "INQR_DVSN": "00",
                    "PDNO": str(intent.get("ticker") or ""),
                    "CCLD_DVSN": "00",
                    "ORD_GNO_BRNO": "",
                    "ODNO": "",
                    "INQR_DVSN_3": "00",
                    "INQR_DVSN_1": "",
                    "EXCG_ID_DVSN_CD": "KRX",
                    "CTX_AREA_FK100": "",
                    "CTX_AREA_NK100": "",
                },
            )
            if str(resp.get("rt_cd", "0")) != "0":
                raise RuntimeError(
                    f"rt_cd={resp.get('rt_cd')} msg_cd={resp.get('msg_cd')}"
                )
            rows = resp.get("output1") or []
            if isinstance(rows, dict):
                rows = [rows]
            excluded = excluded_order_ids or set()
            matches = [
                candidate
                for row in rows
                if isinstance(row, dict)
                for candidate in [_candidate_from_row(row, intent)]
                if candidate is not None
                and candidate["order_id"] not in excluded
            ]
            unique = {m["order_id"]: m for m in matches}
            if len(unique) == 1:
                return "MATCHED", next(iter(unique.values()))
            if len(unique) > 1:
                return "AMBIGUOUS", None
        except Exception as exc:
            log(
                "EXIT_ORDER_RECONCILE_QUERY_FAILED",
                level="WARN",
                ticker=intent.get("ticker"),
                attempt=attempt,
                error=repr(exc),
            )
            if attempt == attempts:
                return "QUERY_FAILED", None
        if attempt < attempts:
            await asyncio.sleep(interval)
    return "NOT_FOUND", None


async def fetch_order_status(
    order_id: str,
    mode: str,
    intent: dict | None = None,
) -> dict | None:
    """주문번호가 확인된 매도의 누적 체결 상태를 한 번 조회한다."""
    resp = await kis_rest.get(
        "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
        tr_id=_CCLD_TR[mode],
        params={
            "CANO": kis_rest.account_no(),
            "ACNT_PRDT_CD": kis_rest.account_cd(),
            "INQR_STRT_DT": _today(),
            "INQR_END_DT": _today(),
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
        },
    )
    rows = resp.get("output1") or []
    if isinstance(rows, dict):
        rows = [rows]
    if intent is None:
        intent = dict(state.get().pending_exit or {})
    intent = {
        **intent,
        "ticker": intent.get("ticker") or state.get().target_ticker,
        "requested_qty": int(intent.get("requested_qty") or 0),
    }
    for row in rows:
        if str(row.get("odno") or "") != str(order_id):
            continue
        candidate = _candidate_from_row(row, intent)
        if candidate:
            return candidate
    return None


async def submit_sell(
    *,
    qty: int,
    reason: str,
    phase: str,
    trigger_price: float,
    mode: str,
    send: Callable[[], Awaitable[dict]],
) -> SubmissionResult:
    """내구적 의도 기록 후 매도하고, 응답 유실 시 주문조회로 대사한다."""
    known_order_ids: set[str] = set()
    if state.get().trade_id:
        try:
            known_order_ids = await db.get_known_sell_order_ids(state.get().trade_id)
        except Exception as exc:
            log(
                "EXIT_ORDER_EXCLUSION_QUERY_FAILED",
                level="CRIT",
                ticker=state.get().target_ticker,
                error=repr(exc),
            )
    try:
        intent, order_db_id = await _prepare_intent(
            qty=qty,
            reason=reason,
            phase=phase,
            trigger_price=trigger_price,
        )
        await _mark_submitting(intent, order_db_id)
    except Exception as exc:
        log("EXIT_INTENT_PERSIST_FAILED", level="CRIT", error=repr(exc))
        return SubmissionResult(
            False,
            False,
            response={"msg_cd": "LOCAL_PERSIST_FAILED", "msg1": str(exc)},
        )

    try:
        resp = await send()
    except asyncio.CancelledError:
        try:
            await _mark_unknown(intent, order_db_id, None)
        except Exception as exc:
            log(
                "EXIT_INTENT_PERSIST_FAILED",
                level="CRIT",
                phase="POST_SEND_CANCELLED",
                error=repr(exc),
            )
        raise
    except Exception as exc:
        resp = {
            "rt_cd": "1",
            "msg_cd": "LOCAL_EXCEPTION_UNCERTAIN",
            "msg1": f"{type(exc).__name__}: {exc}",
        }

    output = resp.get("output") if isinstance(resp.get("output"), dict) else {}
    order_id = str(output.get("ODNO") or "")
    org_no = str(output.get("KRX_FWDG_ORD_ORGNO") or "")
    if str(resp.get("rt_cd", "0")) == "0" and order_id:
        try:
            await _acknowledge(intent, order_db_id, order_id, org_no)
        except Exception as exc:
            # 주문번호를 받은 뒤 로컬 영속화가 실패했다. 절대 재주문 가능한
            # 거절로 돌리지 않고 UNKNOWN으로 고정한다.
            log(
                "EXIT_INTENT_PERSIST_FAILED",
                level="CRIT",
                phase="POST_SEND_ACK",
                order_id=order_id,
                error=repr(exc),
            )
            return SubmissionResult(
                False,
                True,
                order_id,
                org_no,
                order_db_id,
                resp,
            )
        return SubmissionResult(
            True, False, order_id, org_no, order_db_id, resp
        )

    if not _uncertain_response(resp):
        await _mark_rejected(order_db_id, resp)
        return SubmissionResult(False, False, order_db_id=order_db_id, response=resp)

    try:
        await _mark_unknown(intent, order_db_id, resp)
    except Exception as exc:
        log(
            "EXIT_INTENT_PERSIST_FAILED",
            level="CRIT",
            phase="POST_SEND_UNKNOWN",
            error=repr(exc),
        )
        return SubmissionResult(
            False, True, order_db_id=order_db_id, response=resp
        )
    # SQLite 강등(trade_id=0) 또는 조회 장애여도 상태 파일의 시각·종목·수량과
    # 매도 구분으로 유일 매칭한다. 모호하면 find_matching_order가 fail-closed한다.
    outcome, matched = await find_matching_order(
        intent,
        mode,
        excluded_order_ids=known_order_ids,
    )
    if outcome == "MATCHED" and matched:
        try:
            await _acknowledge(
                intent,
                order_db_id,
                matched["order_id"],
                matched.get("org_no") or "",
            )
        except Exception as exc:
            log(
                "EXIT_INTENT_PERSIST_FAILED",
                level="CRIT",
                phase="POST_RECONCILE_ACK",
                order_id=matched["order_id"],
                error=repr(exc),
            )
            return SubmissionResult(
                False,
                True,
                matched["order_id"],
                matched.get("org_no") or "",
                order_db_id,
                resp,
                matched,
            )
        log(
            "EXIT_ORDER_RESPONSE_RECOVERED",
            level="CRIT",
            ticker=intent.get("ticker"),
            client_order_id=intent.get("client_order_id"),
            order_id=matched["order_id"],
        )
        return SubmissionResult(
            True,
            True,
            matched["order_id"],
            matched.get("org_no") or "",
            order_db_id,
            resp,
            matched,
        )
    log(
        "EXIT_ORDER_SUBMISSION_UNKNOWN",
        level="CRIT",
        ticker=intent.get("ticker"),
        client_order_id=intent.get("client_order_id"),
        reconcile_outcome=outcome,
    )
    return SubmissionResult(
        False, True, order_db_id=order_db_id, response=resp
    )


async def reconcile_pending_intent(mode: str) -> tuple[str, dict | None]:
    """재시작 시 pending_exit을 주문번호 및 누적 체결 상태와 대사한다."""
    pending = dict(state.get().pending_exit or {})
    if not pending:
        return "NO_INTENT", None
    order_db_id = int(pending.get("order_db_id") or 0)
    order_id = str(pending.get("order_id") or "")
    if not order_db_id and pending.get("client_order_id"):
        try:
            row = await db.get_order_by_client_id(str(pending["client_order_id"]))
            if row:
                order_db_id = int(row["id"])
                await state.update_pending_exit(order_db_id=order_db_id)
        except Exception as exc:
            log(
                "EXIT_ORDER_DB_DEGRADED",
                level="CRIT",
                ticker=pending.get("ticker"),
                reason="CLIENT_ORDER_LOOKUP_FAILED",
                error=repr(exc),
            )

    snapshot: dict | None = None
    if order_id:
        try:
            snapshot = await fetch_order_status(order_id, mode, pending)
        except Exception as exc:
            log(
                "EXIT_ORDER_RECONCILE_QUERY_FAILED",
                level="WARN",
                ticker=pending.get("ticker"),
                error=repr(exc),
            )
            return "QUERY_FAILED", None
    else:
        excluded: set[str] = set()
        try:
            if state.get().trade_id:
                excluded = await db.get_known_sell_order_ids(state.get().trade_id)
        except Exception as exc:
            log(
                "EXIT_ORDER_EXCLUSION_QUERY_FAILED",
                level="CRIT",
                ticker=pending.get("ticker"),
                error=repr(exc),
            )
        outcome, snapshot = await find_matching_order(
            pending,
            mode,
            excluded_order_ids=excluded,
        )
        if outcome != "MATCHED" or snapshot is None:
            return outcome, None
        order_id = str(snapshot["order_id"])
        await _acknowledge(
            pending,
            order_db_id,
            order_id,
            str(snapshot.get("org_no") or ""),
        )

    if snapshot is None:
        return "ORDER_NOT_VISIBLE", None
    if order_db_id and int(snapshot.get("fill_qty") or 0) > 0:
        requested = int(pending.get("requested_qty") or 0)
        fill_qty = int(snapshot.get("fill_qty") or 0)
        try:
            await db.update_order_fill(
                order_db_id,
                float(snapshot.get("fill_price") or 0),
                fill_qty,
                None,
                status="FILLED" if fill_qty >= requested else "PARTIAL_FILL",
            )
        except Exception as exc:
            log(
                "EXIT_ORDER_DB_DEGRADED",
                level="CRIT",
                ticker=pending.get("ticker"),
                reason="FILL_UPDATE_FAILED",
                error=repr(exc),
            )
    return "RECONCILED", snapshot
