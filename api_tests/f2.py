"""F2 타겟 락업 테스트 — VI 필터 + 복합 정렬 + 1위 종목 확정.

F2는 API 호출 없이 candidates 리스트만으로 동작.
candidates 없을 때는 합성 데이터로 로직 검증.

  python api_tests/f2.py              # F1 실행 후 candidates 전달
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import api_tests._helper as h

# 합성 데이터 — 장전 시간 이외 또는 갭 종목 없을 때 사용
_SYNTHETIC: list[dict] = [
    {
        "ticker": "005930", "expected_price": 58000.0,
        "prev_close": 55000.0, "gap_pct": 0.054,
        "avg_amount_5d": 500_000_000_000.0, "expected_amount": 100_000_000.0,
    },
    {
        "ticker": "000660", "expected_price": 120000.0,
        "prev_close": 114000.0, "gap_pct": 0.053,
        "avg_amount_5d": 200_000_000_000.0, "expected_amount": 50_000_000.0,
    },
    {
        "ticker": "035720", "expected_price": 110000.0,
        "prev_close": 104500.0, "gap_pct": 0.052,
        "avg_amount_5d": 80_000_000_000.0, "expected_amount": 20_000_000.0,
    },
]


async def run(candidates: list[dict] | None = None) -> bool:
    """candidates=None 이면 F1 먼저 실행, 없으면 합성 데이터 사용."""
    h.header("F2. 타겟 락업  VI_GAP_MIN=3%  복합정렬")

    from src import state
    import src.modules.f2_lockup as mod

    # candidates 확보
    if candidates is None:
        from api_tests.f1 import run as f1_run
        ok, candidates = await f1_run()
        if not ok:
            return False

    synthetic_used = False
    if not candidates:
        print("  F1 candidates 없음 — 합성 데이터로 로직 검증")
        candidates = _SYNTHETIC
        synthetic_used = True
        print(f"  합성 candidates: {[c['ticker'] for c in candidates]}")

    # 상태 초기화
    s = state.get()
    s.day_skip = False
    s.target_ticker = None

    # F2 실행
    await mod.run(candidates)

    target = s.target_ticker
    if target:
        h.ok("f2_lockup", f"타겟 확정: {target}"
             + (" (합성데이터)" if synthetic_used else ""))
        return True

    if s.day_skip:
        # VI 필터로 전종목 제외 — 정상 동작
        h.ok("f2_lockup", "VI 필터 전종목 제외 (day_skip=True)")
        return True

    h.fail("f2_lockup", "target_ticker 미설정, day_skip=False")
    return False


if __name__ == "__main__":
    result = asyncio.run(run())
    raise SystemExit(0 if result else 1)
