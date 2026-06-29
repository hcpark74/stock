"""F2 VI 필터 + 복합 정렬 유닛 테스트."""
from unittest.mock import AsyncMock, patch

import pytest

from src import state as _state_mod
from src.modules.f2_lockup import VI_GAP_MIN, run


# ── 헬퍼 ──────────────────────────────────────────────────────────────

def _candidate(
    ticker: str,
    gap_pct: float,
    prev_close: float = 10_000.0,
    expected_amount: float = 1e12,
    buy_sell_ratio: float = 1.0,
) -> dict:
    ep = prev_close * (1 + gap_pct)
    return {
        "ticker": ticker,
        "expected_price": ep,
        "prev_close": prev_close,
        "gap_pct": gap_pct,
        "expected_amount": expected_amount,
        "buy_sell_ratio": buy_sell_ratio,
        "avg_amount_5d": expected_amount,
    }


async def _run(candidates: list[dict]) -> None:
    with patch("src.notifier.send", new_callable=AsyncMock):
        await run(candidates)


# ── VI 이격 계산 근거 ──────────────────────────────────────────────────
# static_vi_upper = prev_close * 1.10
# vi_gap = (static_vi_upper - expected_price) / expected_price
# gap=5%  → vi_gap = (11000-10500)/10500 ≈ 0.0476 ≥ 0.03  ✓
# gap=6.8%→ vi_gap = (11000-10680)/10680 ≈ 0.0300 ≥ 0.03  ✓ (경계)
# gap=6.85%→vi_gap = (11000-10685)/10685 ≈ 0.0296 < 0.03  ✗


# ── 픽스처 ────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clean_state():
    s = _state_mod.get()
    s.day_skip = False
    s.target_ticker = None
    yield
    s.day_skip = False
    s.target_ticker = None


# ── VI 이격 필터 ──────────────────────────────────────────────────────

async def test_vi_safe_candidate_locked():
    """VI 이격 충분(5% 갭) → 타겟 확정."""
    await _run([_candidate("005930", gap_pct=0.05)])
    assert _state_mod.get().target_ticker == "005930"


async def test_vi_near_candidate_excluded():
    """VI 근접(6.85% 갭) → 제외, day_skip=True."""
    await _run([_candidate("VI_NEAR", gap_pct=0.0685)])
    s = _state_mod.get()
    assert s.day_skip is True
    assert s.target_ticker is None


async def test_vi_gap_exactly_at_min():
    """VI 이격이 정확히 VI_GAP_MIN(3%) → 통과 (>= 조건)."""
    # gap=6.796...% 일 때 vi_gap ≈ 0.03
    # (11000 - ep) / ep = 0.03 → ep = 11000/1.03 ≈ 10679.6
    # gap = (10679.6 - 10000) / 10000 ≈ 0.06796
    # vi_gap = (11000 - 10679.6) / 10679.6 ≈ 0.030 (통과)
    prev_close = 10_000.0
    ep = 11_000.0 / (1 + VI_GAP_MIN)  # 정확히 vi_gap=0.03
    gap_pct = (ep - prev_close) / prev_close
    cand = {
        "ticker": "VIEDGE",
        "expected_price": ep,
        "prev_close": prev_close,
        "gap_pct": gap_pct,
        "expected_amount": 1e12,
        "buy_sell_ratio": 1.0,
        "avg_amount_5d": 1e12,
    }
    await _run([cand])
    assert _state_mod.get().target_ticker == "VIEDGE"


async def test_all_vi_near_day_skip():
    """전 종목 VI 근접 → day_skip=True."""
    candidates = [
        _candidate("A", gap_pct=0.09),  # 9% — too near VI (10%)
        _candidate("B", gap_pct=0.085),
    ]
    await _run(candidates)
    assert _state_mod.get().day_skip is True


# ── 복합 정렬 ─────────────────────────────────────────────────────────

async def test_sort_picks_highest_expected_amount():
    """expected_amount 높은 종목이 타겟 선정."""
    candidates = [
        _candidate("LOW_AMT",  gap_pct=0.05, expected_amount=1e11),
        _candidate("HIGH_AMT", gap_pct=0.05, expected_amount=1e12),
    ]
    await _run(candidates)
    assert _state_mod.get().target_ticker == "HIGH_AMT"


async def test_sort_tiebreak_by_buy_sell_ratio():
    """expected_amount 동일 → buy_sell_ratio 높은 종목 선정."""
    candidates = [
        _candidate("LOW_RATIO",  gap_pct=0.05, expected_amount=1e12, buy_sell_ratio=0.8),
        _candidate("HIGH_RATIO", gap_pct=0.05, expected_amount=1e12, buy_sell_ratio=2.5),
    ]
    await _run(candidates)
    assert _state_mod.get().target_ticker == "HIGH_RATIO"


async def test_vi_filter_then_sort():
    """VI 필터 후 남은 종목 중 가장 높은 거래대금 선정."""
    candidates = [
        _candidate("NEAR_VI",  gap_pct=0.09, expected_amount=9e12),   # 제외
        _candidate("SAFE_LOW",  gap_pct=0.05, expected_amount=1e11),
        _candidate("SAFE_HIGH", gap_pct=0.05, expected_amount=5e11),
    ]
    await _run(candidates)
    assert _state_mod.get().target_ticker == "SAFE_HIGH"


# ── 엣지 케이스 ───────────────────────────────────────────────────────

async def test_empty_candidates_returns_early():
    """빈 candidates → target_ticker 없음, day_skip 유지."""
    await _run([])
    assert _state_mod.get().target_ticker is None
    assert _state_mod.get().day_skip is False


async def test_day_skip_returns_early():
    """day_skip=True 시 처리 없이 즉시 반환."""
    _state_mod.get().day_skip = True
    await _run([_candidate("SKIP", gap_pct=0.05)])
    # target_ticker 설정되지 않음
    assert _state_mod.get().target_ticker is None


async def test_missing_prev_close_excluded():
    """prev_close=0 종목 → VI 계산 불가, 제외."""
    cand = _candidate("BAD", gap_pct=0.05)
    cand["prev_close"] = 0.0
    await _run([cand])
    assert _state_mod.get().day_skip is True
