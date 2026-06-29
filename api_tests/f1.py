"""F1 장전 필터 테스트 — KIS 등락률 순위 API (FHPST01710000).

장전 08:30~09:00 이외 시간에도 실행 가능 (시가 기준 등락률로 조회됨).
갭 필터(3~7%)는 API 파라미터로 서버측에서 선적용.

  python api_tests/f1.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import api_tests._helper as h


async def run() -> tuple[bool, list[dict]]:
    """(성공여부, 유동성 필터 통과 candidates) 반환."""
    h.header("F1. 장전 필터  API=FHPST01710000  KOSPI+KOSDAQ")

    from src.api import auth
    import src.modules.f1_filter as mod

    if not await auth.load_or_refresh():
        h.fail("token")
        return False, []

    # 원시 API 호출 (갭 필터는 API 서버측 적용)
    try:
        raw = await mod._fetch_all_premarket()
    except Exception as e:
        h.fail("_fetch_all_premarket", repr(e))
        return False, []

    print(f"  등락률 API 응답: {len(raw)}종목"
          f"  (갭 {mod.GAP_MIN*100:.0f}%~{mod.GAP_MAX*100:.0f}% 적용)")
    for c in raw[:5]:
        print(f"    {c['ticker']}  gap={c['gap_pct']*100:+.2f}%"
              f"  expected={c['expected_price']:>8,.0f}원"
              f"  avg5d={c['avg_amount_5d']/1e8:.1f}억")
    if len(raw) > 5:
        print(f"    ... 외 {len(raw)-5}종목")

    # 유동성 필터 (run() 내부 로직 재현 — DB/state 없이)
    raw.sort(key=lambda c: c.get("avg_amount_5d", 0.0), reverse=True)
    threshold = max(1, int(len(raw) * mod.LIQUIDITY_TOP_PCT)) if raw else 0
    candidates = raw[:threshold]

    print(f"\n  유동성 상위 {mod.LIQUIDITY_TOP_PCT*100:.0f}% 통과: {len(candidates)}종목")
    for c in candidates:
        print(f"    {c['ticker']}  gap={c['gap_pct']*100:+.2f}%"
              f"  expected={c['expected_price']:>8,.0f}원")

    if not raw:
        print("  (결과 없음 — 장전 시간 이외 또는 해당 갭 범위 종목 없음)")

    h.ok("f1_filter", f"raw={len(raw)}, 통과={len(candidates)}")
    return True, candidates


if __name__ == "__main__":
    ok, _ = asyncio.run(run())
    raise SystemExit(0 if ok else 1)
