import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src import live

KST = ZoneInfo("Asia/Seoul")


@dataclass
class State:
    trading_date: str | None = None
    target_ticker: str | None = None
    target_name: str | None = None
    target_candidates: list[dict] | None = None
    entry_price: float | None = None
    entry_at: str | None = None
    entry_qty: int | None = None
    remaining_qty: int | None = None
    high_price: float | None = None
    position_status: str = "IDLE"       # IDLE | ENTERING | HOLDING | CLOSED
    close_reason: str | None = None     # TRAILING | HARD_STOP | TIMEOUT
                                        # ENTRY_FAIL | SLIPPAGE_GUARD | GAP_CHANGED
                                        # VI_ACTIVE
    order_id: str | None = None
    trailing_active: bool = False
    highest_step: float = 0.0           # 마지막으로 통과한 이익 스텝 (0.025 단위, 예: 0.075)
    trade_id: int = 0                   # DB trades.id (0 = 미기록)
    daily_pnl_pct: float = 0.0
    day_skip: bool = False


_state = State()
_lock = asyncio.Lock()


def get() -> State:
    return _state


def _clear_for_trading_day(date_str: str) -> None:
    live.clear_tick_history()
    _state.trading_date = date_str
    _state.target_ticker = None
    _state.target_name = None
    _state.target_candidates = None
    _state.entry_price = None
    _state.entry_at = None
    _state.entry_qty = None
    _state.remaining_qty = None
    _state.high_price = None
    _state.position_status = "IDLE"
    _state.close_reason = None
    _state.order_id = None
    _state.trailing_active = False
    _state.highest_step = 0.0
    _state.trade_id = 0
    _state.daily_pnl_pct = 0.0
    _state.day_skip = False


async def ensure_trading_day(date_str: str) -> bool:
    """Reset in-memory daily state when a new trading date starts."""
    async with _lock:
        if _state.trading_date == date_str:
            return False
        if _state.position_status in {"ENTERING", "HOLDING"}:
            return False
        _clear_for_trading_day(date_str)
        return True


# ── 상태 전이 (atomic) ────────────────────────────────────────────────

async def set_entering() -> bool:
    """IDLE → ENTERING. 성공 시 True, 이미 전이 불가 상태면 False."""
    async with _lock:
        if _state.position_status != "IDLE":
            return False
        _state.position_status = "ENTERING"
        return True


async def set_holding(entry_price: float, entry_qty: int, order_id: str) -> None:
    """ENTERING → HOLDING. F3 1차 체결 확인 후 호출."""
    async with _lock:
        _state.entry_price = entry_price
        _state.entry_at = datetime.now(KST).isoformat()
        _state.entry_qty = entry_qty
        _state.remaining_qty = entry_qty
        _state.high_price = entry_price
        _state.position_status = "HOLDING"
        _state.order_id = order_id
        _state.trailing_active = False
        _state.highest_step = 0.0
        _state.trade_id = 0


async def set_closed(reason: str) -> bool:
    """HOLDING → CLOSED (atomic). 이중 청산 방지. 성공 시 True.

    tick 이력은 지우지 않는다 — 청산 후에도 UI 가격흐름 차트를 유지하고,
    다음 거래일 시작 시 _clear_for_trading_day가 정리한다.
    """
    async with _lock:
        if _state.position_status != "HOLDING":
            return False
        _state.position_status = "CLOSED"
        _state.close_reason = reason
        return True


async def reset_to_idle(reason: str) -> None:
    """ENTERING → IDLE. F3 미체결 확정 시 호출."""
    async with _lock:
        _state.position_status = "IDLE"
        _state.close_reason = reason
        _state.target_ticker = None
        _state.target_name = None
        _state.target_candidates = None
        _state.entry_at = None
        _state.order_id = None
        live.clear_tick_history()


def update_high_price(price: float) -> None:
    if _state.high_price is None or price > _state.high_price:
        _state.high_price = price


# ── 영속화 ───────────────────────────────────────────────────────────

async def persist(state_dir: str, date_str: str) -> None:
    """today_state.json 원자적 쓰기 (tmp → rename). PRD §6-7."""
    path = Path(state_dir)
    tmp = path / "today_state.tmp"
    dst = path / "today_state.json"
    data = {
        "date": date_str,
        "ticker": _state.target_ticker,
        "name": _state.target_name,
        "target_candidates": _state.target_candidates or [],
        "entry_price": _state.entry_price,
        "entry_at": _state.entry_at,
        "entry_qty": _state.entry_qty,
        "remaining_qty": _state.remaining_qty,
        "high_price": _state.high_price,
        "trailing_active": _state.trailing_active,
        "highest_step": _state.highest_step,
        "trade_id": _state.trade_id,
        "position_status": _state.position_status,
        "close_reason": _state.close_reason,
    }
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(dst)


def discard(state_dir: str) -> None:
    """지난 거래일의 today_state.json 폐기. 파일이 없으면 무시."""
    (Path(state_dir) / "today_state.json").unlink(missing_ok=True)


def backup_stale(state_dir: str, stale_date: str) -> bool:
    """지난 거래일 상태 파일을 증거 사본으로 격리 (today_state.stale_<날짜>.json).

    이후 당일 DB OPEN 거래 복구의 persist가 원본을 덮어써도 증거가 남는다.
    같은 stale 날짜면 같은 이름에 덮어쓰므로 재시작이 반복돼도 사본이 쌓이지 않는다.
    날짜는 YYYYMMDD만 신뢰한다 — 손상된 값(경로 문자 포함 가능)은 해시로 치환해
    파일명 오류를 막는다. 사본 확보 여부를 반환하며 예외는 전파하지 않는다
    (증거 백업 실패가 포지션 복구를 중단시키면 안 된다).
    """
    if not re.fullmatch(r"\d{8}", stale_date or ""):
        digest = hashlib.md5((stale_date or "").encode("utf-8")).hexdigest()[:8]
        stale_date = f"unknown_{digest}"
    try:
        src = Path(state_dir) / "today_state.json"
        if not src.exists():
            return True
        dst = Path(state_dir) / f"today_state.stale_{stale_date}.json"
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        return True
    except Exception:
        return False


def load(state_dir: str) -> dict | None:
    """today_state.json 읽기. 없거나 손상 시 None 반환."""
    dst = Path(state_dir) / "today_state.json"
    if not dst.exists():
        return None
    try:
        return json.loads(dst.read_text(encoding="utf-8"))
    except Exception:
        return None


def restore_from(data: dict) -> None:
    """재시작 복구: today_state.json → 인메모리 State 복원. PRD §6-7."""
    _state.trading_date = data.get("date")
    _state.target_ticker = data.get("ticker")
    _state.target_name = data.get("name")
    _state.target_candidates = data.get("target_candidates") or None
    _state.entry_price = data.get("entry_price")
    _state.entry_at = data.get("entry_at")
    _state.entry_qty = data.get("entry_qty")
    _state.remaining_qty = data.get("remaining_qty")
    _state.high_price = data.get("high_price")
    _state.trailing_active = data.get("trailing_active", False)
    _state.highest_step = data.get("highest_step", 0.0)
    _state.trade_id = data.get("trade_id", 0)
    _state.position_status = data.get("position_status", "IDLE")
    _state.close_reason = data.get("close_reason")
