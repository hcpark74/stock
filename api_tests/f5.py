"""F5 타임아웃 청산 테스트.

항상 안전 (잔고 조회만):
  python api_tests/f5.py              # precheck() — HOLDING 상태 합성 후 잔고 확인

실제 청산 주문 포함 (주의):
  python api_tests/f5.py --confirm    # execute() — 시장가 매도 발생!
                                      # 실제 보유 포지션이 있어야 의미 있음
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import api_tests._helper as h

TICKER = "005930"   # 삼성전자


async def run(confirm: bool = False) -> bool:
    h.header("F5. 타임아웃 청산  precheck() + execute()")

    from src.api import auth
    from src import db, state
    import src.modules.f5_timeout as mod

    if not await auth.load_or_refresh():
        h.fail("token")
        return False

    # HOLDING 상태 합성 (precheck 진입 조건)
    s = state.get()
    s.target_ticker = TICKER
    s.position_status = "HOLDING"
    s.entry_price = 50000.0
    s.remaining_qty = 1

    # 1) precheck() — 잔고조회 (read-only, TTTC8434R)
    try:
        await mod.precheck()
    except Exception as e:
        h.fail("f5_precheck", repr(e))
        return False

    prefetch = mod._prefetch_qty
    print(f"  precheck 완료: _prefetch_qty={prefetch}"
          f"  (0이면 {TICKER} 미보유)")
    h.ok("f5_precheck", f"prefetch_qty={prefetch}")

    # 2) execute() — --confirm 필요
    if not confirm:
        print("\n  execute() 스킵 (--confirm 없음)")
        return True

    qty = prefetch or s.remaining_qty or 0
    print(f"\n  [!] execute() — {TICKER} {qty}주 시장가 매도 발생!")
    if qty == 0:
        print("  수량=0 (미보유) — 매도 생략")
        return True

    if db._conn is None:
        await db.init(":memory:")

    # position_status를 HOLDING으로 재설정 (precheck 이후 변경 없음)
    s.position_status = "HOLDING"

    try:
        await mod.execute()
    except Exception as e:
        h.fail("f5_execute", repr(e))
        return False

    final_status = state.get().position_status
    print(f"  position_status={final_status}")
    h.ok("f5_execute", f"status={final_status}")
    return True


if __name__ == "__main__":
    confirm = "--confirm" in sys.argv
    result = asyncio.run(run(confirm=confirm))
    raise SystemExit(0 if result else 1)
