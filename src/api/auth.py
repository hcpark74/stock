import asyncio
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from src.utils.logger import log

KST = ZoneInfo("Asia/Seoul")
_EXPIRY_BUFFER_MIN = 10  # 만료 N분 전 선제 갱신

_token: str = ""
_ws_key: str = ""


def _mask(value: str) -> str:
    return value[:4] + "****" if len(value) > 4 else "****"


def _cache_path() -> Path:
    return Path(os.getenv("AUTH_DIR", "data/auth")) / "token_cache.json"


def _load_cache() -> dict:
    p = _cache_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_cache(token: str, expires_at: str = "") -> None:
    """원자적 쓰기 (tmp → rename)."""
    p = _cache_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"access_token": token, "expires_at": expires_at}, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp.replace(p)


def _is_expiring(cache: dict) -> bool:
    """만료 N분 이내이거나 만료 정보 없으면 True → 선제 갱신 대상."""
    expires_at_str = cache.get("expires_at")
    if not expires_at_str:
        return True
    try:
        expires_at = datetime.strptime(expires_at_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
        return datetime.now(KST) >= expires_at - timedelta(minutes=_EXPIRY_BUFFER_MIN)
    except Exception:
        return True


async def refresh() -> str:
    """
    KIS Access Token 재발급 (PRD §5-1).
    실패 시 2초 간격 3회 재시도. 전부 실패 시 CRIT 알림.
    """
    global _token
    base_url = os.getenv("KIS_BASE_URL", "")
    url = f"{base_url}/oauth2/tokenP"
    payload = {
        "grant_type": "client_credentials",
        "appkey": os.getenv("KIS_APP_KEY", ""),
        "appsecret": os.getenv("KIS_APP_SECRET", ""),
    }
    for attempt in range(1, 4):
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(url, json=payload)
            if resp.status_code == 200:
                data = resp.json()
                _token = data["access_token"]
                expires_at = data.get("access_token_token_expired", "")
                _save_cache(_token, expires_at)
                log("TOKEN_REFRESHED", level="INFO",
                    token_prefix=_mask(_token), expires_at=expires_at)
                return _token
            log("TOKEN_REFRESH_HTTP_ERR", level="WARN",
                attempt=attempt, status=resp.status_code)
        except Exception as e:
            log("TOKEN_REFRESH_ATTEMPT_FAIL", level="WARN", attempt=attempt, error=repr(e))
        if attempt < 3:
            await asyncio.sleep(2)

    log("TOKEN_REFRESH_FAIL", level="CRIT")
    return ""


async def load_or_refresh() -> str:
    """캐시에서 로드. 만료 10분 이내이면 선제 갱신."""
    global _token
    cache = _load_cache()
    if cache.get("access_token") and not _is_expiring(cache):
        _token = cache["access_token"]
        log("TOKEN_LOADED_FROM_CACHE", level="INFO")
        return _token
    return await refresh()


async def refresh_ws_key() -> str:
    """
    WebSocket 전용 접속키 발급 (PRD §6-3).
    OAuth access_token과 별개. /oauth2/Approval 엔드포인트.
    유효기간 24시간 — subscribe() 진입 시 1회 호출로 충분.
    """
    global _ws_key
    base_url = os.getenv("KIS_BASE_URL", "")
    url = f"{base_url}/oauth2/Approval"
    payload = {
        "grant_type": "client_credentials",
        "appkey": os.getenv("KIS_APP_KEY", ""),
        "secretkey": os.getenv("KIS_APP_SECRET", ""),
    }
    for attempt in range(1, 4):
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(url, json=payload)
            if resp.status_code == 200:
                _ws_key = resp.json().get("approval_key", "")
                log("WS_KEY_REFRESHED", level="INFO", key_prefix=_mask(_ws_key))
                return _ws_key
            log("WS_KEY_HTTP_ERR", level="WARN", attempt=attempt, status=resp.status_code)
        except Exception as e:
            log("WS_KEY_ATTEMPT_FAIL", level="WARN", attempt=attempt, error=repr(e))
        if attempt < 3:
            await asyncio.sleep(2)

    log("WS_KEY_REFRESH_FAIL", level="CRIT")
    return ""


async def revoke(token: str = "") -> bool:
    """접근토큰 폐기 [인증-002]. token 생략 시 현재 세션 토큰 폐기."""
    global _token
    target = token or _token
    if not target:
        log("TOKEN_REVOKE_SKIP", level="WARN", reason="no_token")
        return False

    base_url = os.getenv("KIS_BASE_URL", "")
    url = f"{base_url}/oauth2/revokeP"
    payload = {
        "appkey":    os.getenv("KIS_APP_KEY", ""),
        "appsecret": os.getenv("KIS_APP_SECRET", ""),
        "token":     target,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload)
        data = resp.json()
        code = data.get("code", "")
        msg  = data.get("message", "").strip()
        if resp.status_code == 200 and str(code) == "200":
            _token = ""
            p = _cache_path()
            if p.exists():
                p.unlink(missing_ok=True)
            log("TOKEN_REVOKED", level="INFO", code=code, msg=msg)
            return True
        log("TOKEN_REVOKE_FAIL", level="WARN",
            status=resp.status_code, code=code, msg=msg)
        return False
    except Exception as e:
        log("TOKEN_REVOKE_ERR", level="WARN", error=repr(e))
        return False


def get() -> str:
    return _token


def get_ws_key() -> str:
    return _ws_key
