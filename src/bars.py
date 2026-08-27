"""트랙 B 봉 집계 — 틱 스트림에서 1분 OHLCV를 만든다.

`src/modules/`가 아니라 최상위에 두는 이유는 CODING_GUIDELINES §2의
"modules/ 코드는 api/를 직접 import하지 않는다" 규칙 때문이다. live.py와
같은 층위의 관측 인프라로 본다.

기동·종료 배선이 없다. 첫 틱이 그 (날짜, 종목)의 계열을 시작하고, 봉이
닫힐 때마다 파일로 write-through 한다 — main.py를 건드리지 않기 위해서다
(main.py는 _STRATEGY_FILES에 있어 수정하면 트랙 A의 지문이 돈다).
"""

import json
import os
from collections import deque
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src.modules import tick_capture
from src.utils.logger import log
from src.utils.spike_filter import SpikeFilter

KST = ZoneInfo("Asia/Seoul")

_BARS_DIR = Path(os.getenv("TRACK_B_BARS_DIR", "data/bars"))

# 모스펙 §11.1이 확정한 H0STCNT0 필드 배치
_IDX_CTTR = 18            # 체결강도
_IDX_CCLD_DVSN = 21       # 체결구분 (코드 의미는 해석하지 않는다)
_IDX_ASKP1 = 10           # 최우선 매도호가
_IDX_BIDP1 = 11           # 최우선 매수호가
_IDX_TOTAL_ASKP = 38      # 총 매도호가잔량
_IDX_TOTAL_BIDP = 39      # 총 매수호가잔량

_queue: deque[dict] = deque()
_series: dict[tuple[str, str], dict[str, dict]] = {}
_filters: dict[str, SpikeFilter] = {}
_dirty: set[tuple[str, str]] = set()


def bars_path(date: str, ticker: str) -> Path:
    return _BARS_DIR / f"{date}_{ticker}.json"


def reset() -> None:
    """테스트 전용 — 모든 인메모리 상태를 비운다."""
    _queue.clear()
    _series.clear()
    _filters.clear()
    _dirty.clear()


def install() -> None:
    """틱 스트림에 자신을 등록한다. 여러 번 불러도 한 번만 붙는다."""
    tick_capture.register_tick_listener(on_tick)


def on_tick(tick: dict) -> None:
    """논블로킹. 어떤 예외도 전파하지 않는다(주문 경로 격리).

    A의 손절 판정 경로에서 트랙 B가 하는 일은 이 append 하나뿐이다.
    봉 확정·정정·지표는 전부 drain()에서, 별도 태스크로 돈다.
    """
    try:
        _queue.append(tick)
    except Exception:  # noqa: BLE001 — 관측 실패가 호출부를 흔들면 안 된다
        pass


def _spike_filter_for(ticker: str) -> SpikeFilter:
    """트랙 B 전용 인스턴스. A의 필터를 공유하면 내부 상태가 오염된다."""
    flt = _filters.get(ticker)
    if flt is None:
        flt = SpikeFilter()
        _filters[ticker] = flt
    return flt


def _minute_of(tick: dict) -> datetime | None:
    raw_ts = tick.get("source_ts") if tick.get("valid") else None
    for candidate in (raw_ts, tick.get("received_at")):
        if not candidate:
            continue
        try:
            return datetime.fromisoformat(str(candidate)).astimezone(KST)
        except (TypeError, ValueError):
            continue
    return None


def _float_at(raw, index: int) -> float | None:
    try:
        return float(raw[index])
    except (TypeError, ValueError, IndexError):
        return None


def _new_bar(when: datetime, price: float) -> dict:
    return {
        "date": when.strftime("%Y%m%d"),
        "time": when.strftime("%H%M%S"),
        "open": price, "high": price, "low": price, "close": price,
        "volume": 0.0,
        "confirmed": False,
        "tick_count": 0,
        "spike_dropped": 0,
        "tick_derived": {
            "cttr": None, "askp1": None, "bidp1": None,
            "total_askp_rsqn": None, "total_bidp_rsqn": None,
            "vol_by_ccld": {},
            # 분봉 API는 OHLCV만 준다. 틱 파생값은 정정 대상이 아니다.
            "corrected": False,
        },
    }


def _apply(bar: dict, tick: dict, price: float) -> None:
    bar["high"] = max(bar["high"], price)
    bar["low"] = min(bar["low"], price)
    bar["close"] = price
    bar["tick_count"] += 1

    qty = tick.get("qty")
    volume = float(qty) if isinstance(qty, (int, float)) else 0.0
    bar["volume"] += volume

    raw = tick.get("raw")
    if not isinstance(raw, list):
        return
    derived = bar["tick_derived"]
    for key, index in (
        ("cttr", _IDX_CTTR), ("askp1", _IDX_ASKP1), ("bidp1", _IDX_BIDP1),
        ("total_askp_rsqn", _IDX_TOTAL_ASKP), ("total_bidp_rsqn", _IDX_TOTAL_BIDP),
    ):
        value = _float_at(raw, index)
        if value is not None:
            derived[key] = value        # 봉 구간 마지막 값
    try:
        code = str(raw[_IDX_CCLD_DVSN]).strip()
    except IndexError:
        code = ""
    if code:
        derived["vol_by_ccld"][code] = derived["vol_by_ccld"].get(code, 0.0) + volume


def drain() -> None:
    """큐를 비우며 봉을 갱신하고, 바뀐 계열을 디스크에 쓴다."""
    while _queue:
        tick = _queue.popleft()
        try:
            _consume(tick)
        except Exception as exc:  # noqa: BLE001 — 개별 틱 실패 격리
            log("TRACK_B_BAR_TICK_ERROR", level="WARN", error=repr(exc))
    _flush()


def _consume(tick: dict) -> None:
    ticker = tick.get("ticker")
    if not ticker:
        return
    when = _minute_of(tick)
    if when is None:
        return
    try:
        price = float(tick["price"])
    except (KeyError, TypeError, ValueError):
        return
    if price <= 0:
        return

    key = (when.strftime("%Y%m%d"), str(ticker))
    minute = when.strftime("%H%M%S")[:4] + "00"
    minutes = _series.setdefault(key, {})
    bar = minutes.get(minute)

    if not _spike_filter_for(str(ticker)).is_valid(price, str(ticker)):
        if bar is not None:
            bar["spike_dropped"] += 1
            _dirty.add(key)
        return

    if bar is None:
        bar = _new_bar(when.replace(second=0, microsecond=0), price)
        minutes[minute] = bar
    _apply(bar, tick, price)
    _dirty.add(key)


def series(date: str, ticker: str) -> list[dict]:
    minutes = _series.get((date, ticker), {})
    return [minutes[m] for m in sorted(minutes)]


def _flush() -> None:
    while _dirty:
        date, ticker = _dirty.pop()
        rows = series(date, ticker)
        if not rows:
            continue
        path = bars_path(date, ticker)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(rows, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001 — 쓰기 실패가 집계를 멈추면 안 된다
            log(
                "TRACK_B_BAR_WRITE_ERROR", level="WARN",
                ticker=ticker, date=date, error=repr(exc),
            )
