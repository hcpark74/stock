"""트랙 B 진입 규칙 비교 하네스 — 읽기 전용.

사전 등록한 세 축(R1·R2·R3)을 같은 표본에 돌려 스펙 §5의 관문으로 판정한다.
청산은 세 축 모두 트랙 A와 같은 한 벌이라 진입 시각만 비교된다.

strategy_backtest.py 는 09:00~09:30 장벽 모델에 묶여 있어 재사용하지 않는다.
유니버스 로딩과 봉 캐시 I/O만 가져다 쓴다.
"""

from __future__ import annotations

from scripts.track_b_rules import RULES, build_context, resolve_exit
from src.modules import f1_selector

SIGNAL_START = "093500"
ENTRY_DEADLINE = "140000"
DEPTH = 5


def find_signal(
    bars_by_ticker: dict[str, list[dict]],
    ranked_tickers: list[str],
    rule_key: str,
    params: dict,
) -> dict | None:
    """봉을 시간 순으로 훑어 첫 신호에서 멈춘다.

    같은 봉에서 둘 이상이 신호를 내면 최상위 랭크를 고른다. 랭크 1이 나중에
    신호를 낼지 기다리는 해석은 실시간에 구현 불가라 쓰지 않는다.
    """
    rule = RULES[rule_key]
    contexts = {
        t: build_context(bars, params)
        for t, bars in bars_by_ticker.items()
        if bars
    }
    times = sorted({
        b["time"]
        for t in ranked_tickers
        for b in bars_by_ticker.get(t, [])
        if SIGNAL_START <= b["time"] <= ENTRY_DEADLINE
    })
    for time_ in times:
        for rank, ticker in enumerate(ranked_tickers, start=1):
            bars = bars_by_ticker.get(ticker)
            ctx = contexts.get(ticker)
            if not bars or ctx is None:
                continue
            idx = next((i for i, b in enumerate(bars) if b["time"] == time_), None)
            if idx is None or ctx["gap_block"][idx]:
                continue
            if rule(bars, idx, ctx, params):
                return {
                    "ticker": ticker, "signal_idx": idx,
                    "signal_time": time_, "rank": rank,
                }
    return None


def simulate_day(
    date: str,
    universe: list[dict],
    bars_by_ticker: dict[str, list[dict]],
    rule_key: str,
    params: dict,
    *,
    slippage: float = 0.0,
    depth: int = DEPTH,
) -> dict | None:
    """하루 한 건. 신호가 없거나 진입가가 없으면 None(미진입)이다."""
    ranked = f1_selector.rank_candidates(universe)[:depth]
    ranked_tickers = [str(r["ticker"]) for r in ranked if r.get("ticker")]
    signal = find_signal(bars_by_ticker, ranked_tickers, rule_key, params)
    if signal is None:
        return None

    bars = bars_by_ticker[signal["ticker"]]
    entry_idx = signal["signal_idx"] + 1
    if entry_idx >= len(bars):
        return None
    entry_price = bars[entry_idx]["open"] * (1 + slippage)
    if entry_price <= 0:
        return None

    exit_ = resolve_exit(bars, entry_idx, entry_price)
    return {
        "date": date,
        "ticker": signal["ticker"],
        "rank": signal["rank"],
        "signal_time": signal["signal_time"],
        "entry_time": bars[entry_idx]["time"],
        "entry_price": entry_price,
        "ambiguous": exit_["ambiguous"],
        "reason": exit_["high_first"]["reason"],
        "exit_time": exit_["high_first"]["exit_time"],
        "pct": exit_["pct"],
    }
