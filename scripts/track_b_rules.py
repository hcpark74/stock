"""트랙 B 규칙 후보 — 순수 함수만 둔다.

진입 축 세 개와 청산 한 벌이 들어 있다. I/O도 상태도 없어서 실시간 신호
엔진(2단계)과 백테스트가 같은 코드를 탈 수 있다.

청산은 트랙 A와 같다 — 하드스탑 -2.0%, 스텝 트레일링 +2.5%/-2.0%, 15:15.
B가 A와 다른 점을 진입 시각 하나로 줄여야 비교가 통제된다.
"""

from __future__ import annotations

import math

# 트랙 A와 같은 값. f4_tracking.STEP_SIZE / STEP_TRAIL / HARD_STOP_RATIO 와 일치한다.
STEP_SIZE = 0.025
STEP_TRAIL = 0.020
HARD_STOP = 0.020
TIMEOUT_TIME = "151500"


def _step_of(pnl: float) -> float:
    return max(math.floor(pnl / STEP_SIZE) * STEP_SIZE, 0.0)


def simulate_exit(
    bars: list[dict],
    entry_idx: int,
    entry_price: float,
    *,
    order: str,
) -> dict:
    """진입 봉부터 청산까지 봉으로 훑는다.

    봉 안의 가격 경로는 모른다. ``order`` 로 고가·저가 중 어느 쪽을 먼저 본
    것으로 가정할지 정하고, 호출부가 양쪽을 돌려 답이 갈리는지 본다.
    """
    if entry_price <= 0:
        raise ValueError(f"entry_price must be positive: {entry_price}")
    highest_step = 0.0
    trailing_active = False

    # bars.index(bar) 를 쓰지 않는다 — 값이 같은 봉이 둘이면 앞의 것을 돌려준다.
    for idx in range(entry_idx, len(bars)):
        bar = bars[idx]
        if bar["time"] >= TIMEOUT_TIME:
            return {
                "exit_idx": idx, "exit_time": bar["time"],
                "exit_price": bar["close"], "reason": "TIMEOUT",
                "pct": (bar["close"] / entry_price - 1) * 100,
            }
        prices = (
            [bar["high"], bar["low"]] if order == "high_first"
            else [bar["low"], bar["high"]]
        )
        for price in prices:
            step = _step_of(price / entry_price - 1)
            if step > highest_step:
                highest_step = step
            if highest_step >= STEP_SIZE:
                trailing_active = True

            if not trailing_active and price <= entry_price * (1 - HARD_STOP):
                stop = entry_price * (1 - HARD_STOP)
                return {
                    "exit_idx": idx, "exit_time": bar["time"],
                    "exit_price": stop, "reason": "HARD_STOP",
                    "pct": -HARD_STOP * 100,
                }
            if trailing_active:
                stop = entry_price * (1 + highest_step - STEP_TRAIL)
                if price <= stop:
                    return {
                        "exit_idx": idx, "exit_time": bar["time"],
                        "exit_price": stop, "reason": "TRAILING",
                        "pct": (stop / entry_price - 1) * 100,
                    }

    last = bars[-1]
    return {
        "exit_idx": len(bars) - 1, "exit_time": last["time"],
        "exit_price": last["close"], "reason": "DATA_END",
        "pct": (last["close"] / entry_price - 1) * 100,
    }


def resolve_exit(bars: list[dict], entry_idx: int, entry_price: float) -> dict:
    """양쪽 순서로 돌려 답이 갈리면 AMBIGUOUS.

    갈린 날을 한쪽 답으로 적으면 봉 내부를 안다고 주장하는 것이 된다. 판정에서
    빼되 몇 건이 빠졌는지는 항상 보고한다 — 많이 빠지면 결론 자체가 약하다.
    """
    high_first = simulate_exit(bars, entry_idx, entry_price, order="high_first")
    low_first = simulate_exit(bars, entry_idx, entry_price, order="low_first")
    ambiguous = (
        high_first["reason"] != low_first["reason"]
        or high_first["exit_time"] != low_first["exit_time"]
    )
    return {
        "ambiguous": ambiguous,
        "high_first": high_first,
        "low_first": low_first,
        "pct": None if ambiguous else high_first["pct"],
    }
