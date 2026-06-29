"""F3 진입 주문 테스트.

항상 안전 (조회만):
  python api_tests/f3.py              # 예상체결가 + 예수금 조회

실제 주문 포함 (주의):
  python api_tests/f3.py --confirm    # force=True 모드 — 시장가 매수 발생!
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import api_tests._helper as h

TICKER = "005930"   # 삼성전자


async def run(confirm: bool = False) -> bool:
    h.header(f"F3. 진입 주문  ticker={TICKER}"
             + ("  [!] --confirm 활성화" if confirm else ""))

    from src.api import auth
    from src import db, state
    import src.modules.f3_entry as mod

    if not await auth.load_or_refresh():
        h.fail("token")
        return False

    # 1) 예상 체결가 조회 (FHKST01010100)
    try:
        expected, prev_close = await mod._fetch_expected_price(TICKER)
    except Exception as e:
        h.fail("_fetch_expected_price", repr(e))
        return False

    gap = (expected / prev_close - 1) * 100 if prev_close else 0.0
    print(f"  [{TICKER}]  expected={expected:>8,.0f}원"
          f"  prev_close={prev_close:>8,.0f}원  gap={gap:+.2f}%")
    if expected <= 0:
        h.fail("_fetch_expected_price", "expected_price=0")
        return False
    h.ok("_fetch_expected_price", f"expected={expected:,.0f}원")

    # 2) 예수금 조회 (TTTC8434R)
    try:
        cash = await mod._fetch_available_cash()
    except Exception as e:
        h.fail("_fetch_available_cash", repr(e))
        return False

    alloc = int(cash * mod.ALLOC_RATIO)
    qty   = int(alloc / expected) if expected > 0 else 0
    print(f"  예수금={cash:>12,.0f}원"
          f"  배분({mod.ALLOC_RATIO*100:.0f}%)={alloc:>10,.0f}원"
          f"  가능수량={qty}주")
    if cash < 0:
        h.fail("_fetch_available_cash", "cash<0")
        return False
    h.ok("_fetch_available_cash", f"cash={cash:,.0f}원")

    # 3) 전체 run() — --confirm 필요
    if not confirm:
        print("\n  전체 run() 스킵 (--confirm 없음)")
        return True

    print(f"\n  [!] force=True 모드 — {TICKER} 시장가 매수 주문 발생!")
    if qty == 0:
        print("  수량=0 (예수금 부족) — 주문 생략")
        return True

    if db._conn is None:
        await db.init(":memory:")

    s = state.get()
    s.day_skip = False
    s.target_ticker = TICKER
    s.position_status = "IDLE"

    try:
        await mod.run(force=True)
    except Exception as e:
        h.fail("f3_entry.run", repr(e))
        return False

    final_status = state.get().position_status
    print(f"  position_status={final_status}")
    h.ok("f3_entry.run", f"status={final_status}")
    return True


if __name__ == "__main__":
    confirm = "--confirm" in sys.argv
    result = asyncio.run(run(confirm=confirm))
    raise SystemExit(0 if result else 1)
