"""보유 중 VI(변동성완화장치) 감지 — 관측 전용, 매매 로직 불개입.

동작 원리 (2026-07-16 현대약품 VI 인시던트 기반):
VI 발동 중에는 체결이 없어 WS 틱이 끊기고, F4 REST 백업이 마지막 체결가를
반복 조회한다. "REST 가격이 freeze_sec 이상 동결"을 의심 신호로 보고
VI 현황 API(FHPST01390000)를 1회 조회해 공식 발동 기록을 확인한다.

이벤트는 dict 리스트로 반환만 하고 로그·알림·UI 반영은 호출자(F4)가 한다.
"""

import time
from datetime import datetime
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

_INFO_KEYS = ("vi_kind_code", "cntg_vi_hour", "vi_prc", "vi_stnd_prc", "vi_dprt", "vi_count")


def parse_vi_payload(resp: dict, ticker: str) -> dict | None:
    """VI 현황 응답에서 해당 종목의 '발동 중' 레코드를 찾는다. 없으면 None.

    해제 시각(vi_cncl_hour)이 채워진 레코드는 이미 끝난 VI이므로 무시한다.
    """
    rows: Any = resp.get("output") or resp.get("output1") or []
    if isinstance(rows, dict):
        rows = [rows]
    for row in rows:
        if str(row.get("mksc_shrn_iscd", "")) != ticker:
            continue
        if not str(row.get("cntg_vi_hour") or "").strip():
            continue
        cncl = str(row.get("vi_cncl_hour") or "").strip()
        if cncl and cncl not in ("0", "000000"):
            continue
        return {k: row.get(k) for k in _INFO_KEYS}
    return None


class ViWatch:
    """단일 종목 VI 감시. F4가 관측한 가격을 on_price로 전달받는다."""

    def __init__(
        self,
        ticker: str,
        check_vi: Callable[[], Awaitable[dict]],
        *,
        freeze_sec: float = 10.0,
        cooldown_sec: float = 60.0,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.ticker = ticker
        self.vi_active = False
        self._check_vi = check_vi
        self._freeze_sec = freeze_sec
        self._cooldown_sec = cooldown_sec
        self._monotonic = monotonic
        self._frozen_price: float | None = None
        self._frozen_since: float | None = None
        self._cooldown_until = 0.0
        self._active_info: dict = {}
        self._active_since = 0.0
        self._active_price: float | None = None

    async def on_price(self, price: float, source: str) -> list[dict]:
        """가격 관측(source: 'ws' | 'rest'). 발생한 VI 이벤트 목록을 반환."""
        now = self._monotonic()

        if self.vi_active:
            # WS 틱 도착 = 체결 재개, REST 가격 변동 = 단일가 해제 체결
            if source == "ws" or price != self._active_price:
                return [self._release(price, source, now)]
            return []

        if source == "ws":
            # WS 틱이 흐르는 동안은 동결 아님
            self._reset_freeze()
            return []

        if self._frozen_price is None or price != self._frozen_price:
            self._frozen_price = price
            self._frozen_since = now
            return []

        assert self._frozen_since is not None
        if now - self._frozen_since < self._freeze_sec or now < self._cooldown_until:
            return []

        self._cooldown_until = now + self._cooldown_sec
        try:
            resp = await self._check_vi()
        except Exception as e:
            return [{"type": "VI_CHECK_FAILED", "error": repr(e)}]

        info = parse_vi_payload(resp, self.ticker)
        if info is None:
            return [{
                "type": "VI_CHECK_NEGATIVE",
                "frozen_price": price,
                "frozen_sec": round(now - self._frozen_since, 1),
            }]

        self.vi_active = True
        self._active_info = info
        self._active_since = now
        self._active_price = price
        return [{
            "type": "VI_DETECTED",
            "ts": datetime.now(KST).isoformat(),
            "frozen_price": price,
            **info,
        }]

    def _release(self, price: float, source: str, now: float) -> dict:
        event = {
            "type": "VI_RELEASED",
            "ts": datetime.now(KST).isoformat(),
            "release_price": price,
            "source": source,
            "duration_sec": round(now - self._active_since, 1),
            **self._active_info,
        }
        self.vi_active = False
        self._active_info = {}
        self._active_price = None
        self._reset_freeze()
        return event

    def _reset_freeze(self) -> None:
        self._frozen_price = None
        self._frozen_since = None
