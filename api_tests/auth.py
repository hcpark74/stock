"""KIS OAuth 인증 테스트 — access token + WebSocket key + token revoke.

  python api_tests/auth.py             # 발급 테스트만
  python api_tests/auth.py --revoke    # 발급 + 폐기 (현재 토큰 무효화됨)
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import api_tests._helper as h


async def run(revoke: bool = False) -> bool:
    passed = True
    from src.api import auth

    # 1. Access Token 발급 [인증-001]
    h.header("1. OAuth - Access Token [인증-001]")
    try:
        token = await auth.load_or_refresh()
        if token:
            h.ok("load_or_refresh()", f"prefix={token[:10]}...")
        else:
            h.fail("load_or_refresh()", "빈 토큰 반환")
            passed = False
    except Exception as e:
        h.fail("load_or_refresh()", repr(e))
        passed = False

    # 2. WebSocket Key 발급 [인증-003]
    h.header("2. OAuth - WebSocket Key [인증-003]")
    try:
        ws_key = await auth.refresh_ws_key()
        if ws_key:
            h.ok("refresh_ws_key()", f"prefix={ws_key[:10]}...")
        else:
            h.fail("refresh_ws_key()", "빈 키 반환")
            passed = False
    except Exception as e:
        h.fail("refresh_ws_key()", repr(e))
        passed = False

    # 3. Access Token 폐기 [인증-002] — --revoke 플래그 필수
    h.header("3. OAuth - Token Revoke [인증-002]")
    if not revoke:
        print("  --revoke 플래그 없음. 폐기 테스트 건너뜁니다.")
        print("  실행: python api_tests/auth.py --revoke")
    else:
        print("  [!] 현재 세션 토큰이 폐기됩니다. 이후 자동 재발급됩니다.")
        try:
            ok = await auth.revoke()
            if ok:
                h.ok("revoke()", "토큰 폐기 완료 (캐시 삭제)")
                # 폐기 후 새 토큰 재발급 확인
                new_token = await auth.refresh()
                if new_token:
                    h.ok("refresh() 재발급", f"prefix={new_token[:10]}...")
                else:
                    h.fail("refresh() 재발급", "폐기 후 재발급 실패")
                    passed = False
            else:
                h.fail("revoke()", "폐기 실패")
                passed = False
        except Exception as e:
            h.fail("revoke()", repr(e))
            passed = False

    return passed


if __name__ == "__main__":
    do_revoke = "--revoke" in sys.argv
    result = asyncio.run(run(revoke=do_revoke))
    raise SystemExit(0 if result else 1)
