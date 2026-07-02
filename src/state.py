import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

from src import live


@dataclass
class State:
    trading_date: str | None = None
    target_ticker: str | None = None
    target_candidates: list[dict] | None = None
    entry_price: float | None = None
    entry_qty: int | None = None
    remaining_qty: int | None = None
    high_price: float | None = None
    position_status: str = "IDLE"       # IDLE | ENTERING | HOLDING | CLOSED
    close_reason: str | None = None     # TRAILING | HARD_STOP | TIMEOUT
                                        # ENTRY_FAIL | SLIPPAGE_GUARD | GAP_CHANGED
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
    _state.target_candidates = None
    _state.entry_price = None
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
        _state.entry_qty = entry_qty
        _state.remaining_qty = entry_qty
        _state.high_price = entry_price
        _state.position_status = "HOLDING"
        _state.order_id = order_id
        _state.trailing_active = False
        _state.highest_step = 0.0
        _state.trade_id = 0


async def set_closed(reason: str) -> bool:
    """HOLDING → CLOSED (atomic). 이중 청산 방지. 성공 시 True."""
    async with _lock:
        if _state.position_status != "HOLDING":
            return False
        _state.position_status = "CLOSED"
        _state.close_reason = reason
        live.clear_tick_history()
        return True


async def reset_to_idle(reason: str) -> None:
    """ENTERING → IDLE. F3 미체결 확정 시 호출."""
    async with _lock:
        _state.position_status = "IDLE"
        _state.close_reason = reason
        _state.target_ticker = None
        _state.target_candidates = None
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
        "target_candidates": _state.target_candidates or [],
        "entry_price": _state.entry_price,
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
    _state.target_candidates = data.get("target_candidates") or None
    _state.entry_price = data.get("entry_price")
    _state.entry_qty = data.get("entry_qty")
    _state.remaining_qty = data.get("remaining_qty")
    _state.high_price = data.get("high_price")
    _state.trailing_active = data.get("trailing_active", False)
    _state.highest_step = data.get("highest_step", 0.0)
    _state.trade_id = data.get("trade_id", 0)
    _state.position_status = data.get("position_status", "IDLE")
    _state.close_reason = data.get("close_reason")
