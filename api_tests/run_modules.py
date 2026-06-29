"""F1~F5 모듈 통합 테스트.

  python api_tests/run_modules.py              # F1 + F2 + F3헬퍼 + F5precheck
  python api_tests/run_modules.py --confirm    # + F3 주문 + F5 청산 (실제 주문!)

  F4는 WebSocket + 실제 보유 포지션 필요 → 별도 테스트 불가, 단독 실행으로 검증.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import api_tests._helper as h


async def main() -> None:
    confirm = "--confirm" in sys.argv
    mode = h.mode()

    print("=" * 56)
    print("  Module Tests  F1 → F2 → F3 → F5")
    print(f"  mode : {mode}")
    if confirm:
        print("  [!] --confirm: 실제 주문이 발생합니다!")
    print("=" * 56)

    from api_tests.f1 import run as f1_run
    from api_tests.f2 import run as f2_run
    from api_tests.f3 import run as f3_run
    from api_tests.f5 import run as f5_run

    results: dict[str, bool] = {}

    # F1 — API 호출 + 필터 파이프라인
    ok, candidates = await f1_run()
    results["F1_filter"] = ok

    # F2 — 정렬/VI 필터/락업 (F1 결과 또는 합성 데이터)
    results["F2_lockup"] = await f2_run(candidates if ok else None)

    # F3 — 가격·잔고 조회 헬퍼 (+ --confirm 시 주문)
    results["F3_entry"] = await f3_run(confirm=confirm)

    # F5 — precheck 잔고 확인 (+ --confirm 시 청산)
    results["F5_timeout"] = await f5_run(confirm=confirm)

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
