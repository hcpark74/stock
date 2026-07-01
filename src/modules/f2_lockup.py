"""F2. 타겟 락업 엔진 (08:58:00 ~ 08:59:30) — PRD §F2"""

from src import notifier, state
from src.utils.logger import log

VI_GAP_MIN = 0.03   # 정적 VI 상단까지 최소 이격 3%
F2_MAX_TARGET_CANDIDATES = 3


async def run(candidates: list[dict]) -> None:
    """
    복합 정렬 → VI 필터 → 1위 종목 타겟 락업.
    candidates: F1 통과 종목 리스트.
    """
    s = state.get()
    if s.day_skip or not candidates:
        log("F2_SKIPPED", level="WARN",
            reason="DAY_SKIP" if s.day_skip else "NO_CANDIDATES")
        return

    # ── 복합 정렬 (내림차순): 1순위 예상 체결대금, 2순위 매수잔량/매도잔량 ──
    sorted_list = sorted(
        candidates,
        key=lambda c: (c.get("expected_amount", 0.0), c.get("buy_sell_ratio", 0.0)),
        reverse=True,
    )

    # ── VI 근접 종목 제외 (PRD §F2) ──────────────────────────────────
    vi_filtered: list[dict] = []
    for c in sorted_list:
        expected_price = c.get("expected_price", 0.0)
        prev_close = c.get("prev_close", 0.0)
        if not prev_close or not expected_price:
            continue
        static_vi_upper = prev_close * 1.10
        vi_gap = (static_vi_upper - expected_price) / expected_price
        if vi_gap >= VI_GAP_MIN:
            vi_filtered.append(c)

    if not vi_filtered:
        log("VI_FILTER_ALL_EXCLUDED", level="INFO", filter_count=0, reason="ALL_VI_NEAR")
        await notifier.send(
            "VI_FILTER_ALL_EXCLUDED", level="WARN",
            message="VI 필터로 전 종목 제외. 거래 스킵.",
        )
        s.day_skip = True
        return

    # ── 락업 ─────────────────────────────────────────────────────────
    locked_candidates = vi_filtered[:F2_MAX_TARGET_CANDIDATES]
    target = locked_candidates[0]
    s.target_ticker = target["ticker"]
    s.target_candidates = locked_candidates

    log(
        "TARGET_LOCKED", level="INFO", ticker=s.target_ticker,
        target_count=len(locked_candidates),
        target_tickers=[c.get("ticker") for c in locked_candidates],
        gap_pct=round(target.get("gap_pct", 0.0) * 100, 2),
        expected_price=target.get("expected_price"),
        expected_amount=target.get("expected_amount"),
        buy_sell_ratio=target.get("buy_sell_ratio"),
    )
    await notifier.send(
        "TARGET_LOCKED", level="INFO",
        message=(
            f"타겟 확정: {s.target_ticker}, "
            f"예상갭 {target.get('gap_pct', 0.0)*100:.1f}%"
        ),
    )
