"""KIS API 통합 테스트 전체 실행.

  python api_tests/run_all.py                      # auth + ccld + balance + cancel_psbl
  python api_tests/run_all.py --order              # + 매수/매도 (실제 주문)
  python api_tests/run_all.py --cancel             # + 취소 (미체결 주문 생성 후 취소)
  python api_tests/run_all.py --order --cancel     # 전체
  python api_tests/run_all.py --revoke             # + 토큰 폐기

  * cancel_psbl: REAL 모드에서만 동작 (모의투자 미지원), PAPER에서는 자동 skip
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import api_tests._helper as h


async def main() -> None:
    run_order  = "--order"  in sys.argv
    run_cancel = "--cancel" in sys.argv
    run_revoke = "--revoke" in sys.argv
    mode       = h.mode()

    print("=" * 56)
    print("  KIS API Integration Tests")
    print(f"  mode : {mode}")
    if run_order:
        print("  [!] 주문 테스트 활성화 -- 실제 주문이 발생합니다")
    if run_cancel:
        print("  [!] 취소 테스트 활성화 -- 미체결 주문 후 취소합니다")
    if run_revoke:
        print("  [!] 토큰 폐기 테스트 활성화 -- 현재 토큰 무효화됩니다")
    print("=" * 56)

    from api_tests import (
        auth, ccld, balance,
        buy_psbl as buy_psbl_mod,
        cancel_psbl as cancel_psbl_mod,
        order as order_mod,
        cancel as cancel_mod,
    )

    results: dict[str, bool] = {}

    results["auth"]        = await auth.run(revoke=run_revoke)
    results["ccld"]        = await ccld.run()
    results["balance"]     = await balance.run()
    results["buy_psbl"]    = await buy_psbl_mod.run()
    results["cancel_psbl"] = await cancel_psbl_mod.run()
    results["order"]       = await order_mod.run(confirm=run_order)
    results["cancel"]      = await cancel_mod.run(confirm=run_cancel)

    passed = sum(v for v in results.values())
    total  = len(results)

    print("\n" + "=" * 56)
    for name, ok in results.items():
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}]  {name}")
    print(f"\n  결과: {passed}/{total} passed")
    print("=" * 56)

    raise SystemExit(0 if passed == total else 1)


if __name__ == "__main__":
    asyncio.run(main())
