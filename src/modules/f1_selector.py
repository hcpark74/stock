"""F1 candidate scoring and selection.

The selector ranks candidates by the goal we actually care about: early-session
upside potential. Environment variables tune thresholds and weights; the
selection shape remains a list of candidate dictionaries for the existing F2/F3
pipeline.
"""

import math
import os

EXCLUDED_PRODUCT_KEYWORDS = (
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


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def is_excluded_product(name: str) -> bool:
    upper_name = str(name or "").upper()
    return any(keyword in upper_name for keyword in EXCLUDED_PRODUCT_KEYWORDS)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


GAP_MIN = _env_float("F1_GAP_MIN", 0.030)
GAP_CORE_MAX = _env_float("F1_GAP_CORE_MAX", 0.080)
GAP_HARD_MAX = _env_float("F1_GAP_HARD_MAX", 0.100)
# 2026-07-20: 예상 체결대금 수백만원짜리 후보가 통과해 미체결(VI)·유동성 제로
# 진입 시도로 이어졌다. 주문금액(약 900만원)이 예상 체결대금을 넘지 않도록 1억 하한.
MIN_EXPECTED_AMOUNT = _env_float("F1_MIN_EXPECTED_AMOUNT", 100_000_000)
HIGH_GAP_MIN_EXPECTED_AMOUNT = _env_float(
    "F1_HIGH_GAP_MIN_EXPECTED_AMOUNT",
    5_000_000_000,
)
MIN_VI_GAP = _env_float("F1_MIN_VI_GAP", 0.010)
SAFE_VI_GAP = _env_float("F1_SAFE_VI_GAP", 0.030)
MIN_VOLUME_SURGE = _env_float("F1_MIN_VOLUME_SURGE", 0.0)

LIQUIDITY_TOP_PCT = _env_float("F1_SELECTION_TOP_PCT", 0.10)
MIN_CANDIDATES = max(1, _env_int("F1_MIN_CANDIDATES", 10))

GAP_WEIGHT = _env_float("F1_SCORE_GAP_WEIGHT", 25)
AMOUNT_WEIGHT = _env_float("F1_SCORE_AMOUNT_WEIGHT", 25)
SURGE_WEIGHT = _env_float("F1_SCORE_SURGE_WEIGHT", 25)
BUY_PRESSURE_WEIGHT = _env_float("F1_SCORE_BUY_PRESSURE_WEIGHT", 0)
VI_WEIGHT = _env_float("F1_SCORE_VI_WEIGHT", 10)
OVERHEAT_PENALTY = _env_float("F1_SCORE_OVERHEAT_PENALTY", 30)


def gap_allowed(candidate: dict) -> bool:
    gap = float(candidate.get("gap_pct") or 0.0)
    return candidate.get("gap_allowed") is not False and GAP_MIN <= gap < GAP_HARD_MAX


def expected_amount(candidate: dict) -> float:
    return float(candidate.get("expected_amount") or candidate.get("avg_amount_5d") or 0.0)


def avg_amount_5d(candidate: dict) -> float:
    return float(candidate.get("avg_amount_5d") or 0.0)


def volume_surge(candidate: dict) -> float:
    avg_amount = avg_amount_5d(candidate)
    if avg_amount <= 0:
        return 0.0
    return expected_amount(candidate) / avg_amount


def _score_gap(gap: float) -> float:
    if gap < GAP_MIN or gap >= GAP_HARD_MAX:
        return 0.0
    core_span = max(0.0001, GAP_CORE_MAX - GAP_MIN)
    if gap <= GAP_CORE_MAX:
        return min(1.0, (gap - GAP_MIN) / core_span)
    return max(
        0.45,
        1.0 - ((gap - GAP_CORE_MAX) / max(0.0001, GAP_HARD_MAX - GAP_CORE_MAX)),
    )


def _score_amount(amount: float) -> float:
    if amount <= 0:
        return 0.0
    # 2B is useful, 10B is strong, 30B+ is saturated.
    return max(0.0, min(1.0, math.log10(max(1.0, amount) / 200_000_000) / math.log10(150)))


def _score_surge(surge: float) -> float:
    if surge <= 0:
        return 0.0
    # 1x is ordinary, 5x is strong, 10x+ is saturated.
    return min(1.0, math.log2(max(1.0, surge)) / math.log2(10))


def _score_buy_pressure(ratio: float) -> float:
    if ratio <= 1.0:
        return 0.0
    return min(1.0, (ratio - 1.0) / 2.0)


def _score_vi(vi_gap: float | None) -> float:
    if vi_gap is None:
        return 0.35
    if vi_gap < MIN_VI_GAP:
        return 0.0
    return min(1.0, vi_gap / max(0.0001, SAFE_VI_GAP))


def f1_score(candidate: dict) -> float:
    gap = float(candidate.get("gap_pct") or 0.0)
    amount = expected_amount(candidate)
    surge = volume_surge(candidate)
    vi_gap = candidate.get("vi_gap")
    buy_sell_ratio = float(candidate.get("buy_sell_ratio") or 0.0)

    score = (
        _score_gap(gap) * GAP_WEIGHT
        + _score_amount(amount) * AMOUNT_WEIGHT
        + _score_surge(surge) * SURGE_WEIGHT
        + _score_buy_pressure(buy_sell_ratio) * BUY_PRESSURE_WEIGHT
        + _score_vi(vi_gap) * VI_WEIGHT
    )
    if GAP_CORE_MAX <= gap < GAP_HARD_MAX:
        score -= OVERHEAT_PENALTY * max(
            0.0,
            (gap - GAP_CORE_MAX) / (GAP_HARD_MAX - GAP_CORE_MAX),
        )
    return round(score, 4)


def _passes_selection_floor(candidate: dict) -> bool:
    gap = float(candidate.get("gap_pct") or 0.0)
    amount = expected_amount(candidate)
    surge = volume_surge(candidate)
    vi_gap = candidate.get("vi_gap")

    if not gap_allowed(candidate):
        return False
    if amount < MIN_EXPECTED_AMOUNT:
        return False
    if surge < MIN_VOLUME_SURGE:
        return False
    if vi_gap is not None and vi_gap < MIN_VI_GAP:
        return False
    if gap >= GAP_CORE_MAX:
        return high_gap_allowed(candidate)
    return True


def high_gap_allowed(candidate: dict) -> bool:
    vi_gap = candidate.get("vi_gap")
    return (
        expected_amount(candidate) >= HIGH_GAP_MIN_EXPECTED_AMOUNT
        and vi_gap is not None
        and vi_gap >= MIN_VI_GAP
    )


def rank_candidates(candidates: list[dict]) -> list[dict]:
    ranked = []
    for candidate in candidates:
        if not _passes_selection_floor(candidate):
            continue
        enriched = {**candidate}
        enriched["volume_surge"] = volume_surge(enriched)
        enriched["f1_score"] = f1_score(enriched)
        ranked.append(enriched)

    return sorted(
        ranked,
        key=lambda c: (
            c.get("f1_score", 0.0),
            c.get("volume_surge", 0.0),
            expected_amount(c),
            c.get("gap_pct", 0.0),
        ),
        reverse=True,
    )


def select_candidates(candidates: list[dict]) -> list[dict]:
    ranked = rank_candidates(candidates)
    if not ranked:
        return []
    threshold = max(MIN_CANDIDATES, int(len(ranked) * LIQUIDITY_TOP_PCT))
    return ranked[:threshold]
