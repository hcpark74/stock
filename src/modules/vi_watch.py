"""보유 중 VI(변동성완화장치) 감지 — 관측 전용, 매매 로직 불개입.

동작 원리 (2026-07-16 현대약품 VI 인시던트 기반):
VI 발동 중에는 체결이 없어 WS 틱이 끊기고, F4 REST 백업이 마지막 체결가를
반복 조회한다. "REST 가격이 freeze_sec 이상 동결"을 의심 신호로 보고
VI 현황 API(FHPST01390000)로 공식 발동 기록을 확인한다.

핵심 제약 — on_price는 절대 API 응답을 기다리지 않는다. WS stale 상황에서
REST 폴링 루프가 유일한 손절 감시 경로이므로, VI 조회(타임아웃 15초·재시도)를
동기로 기다리면 그동안 스탑 처리가 멈춘다. 조회는 백그라운드 태스크로 띄우고
결과는 다음 폴에서 소비한다.

이벤트는 dict 리스트로 반환만 하고 로그·알림·UI 반영은 호출자(F4)가 한다.
"""

import asyncio
import time
from datetime import datetime
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from src.api import kis_rest

KST = ZoneInfo("Asia/Seoul")

_INFO_KEYS = (
    "vi_kind_code", "cntg_vi_hour", "bsop_date",
    "vi_prc", "vi_stnd_prc", "vi_dprt", "vi_count",
)


async def fetch_vi_status(ticker: str) -> dict:
    """변동성완화장치(VI) 현황 조회 (FHPST01390000). F3 진입 전 체크·F4 감시 공용."""
    resp = await kis_rest.get(
        "/uapi/domestic-stock/v1/quotations/inquire-vi-status",
        tr_id="FHPST01390000",
        params={
            "FID_DIV_CLS_CODE": "0",
            "FID_COND_SCR_DIV_CODE": "20139",
            "FID_MRKT_CLS_CODE": "0",
            "FID_INPUT_ISCD": ticker,
            "FID_RANK_SORT_CLS_CODE": "0",
            "FID_INPUT_DATE_1": datetime.now(KST).strftime("%Y%m%d"),
            "FID_TRGT_CLS_CODE": "",
            "FID_TRGT_EXLS_CLS_CODE": "",
        },
    )
    if str(resp.get("rt_cd", "")) != "0":
        raise RuntimeError(
            f"VI status query failed: {resp.get('msg_cd')} {resp.get('msg1')}")
    return resp


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


def _vi_start_from_api(info: dict, fallback: datetime) -> datetime:
    """API의 영업일+발동시각(bsop_date+cntg_vi_hour) → 실제 발동 시각.

    파싱 불가 시 감지 시각으로 대체한다 — 차트가 비는 것보다는 늦은 시작이 낫다.
    """
    raw = f"{info.get('bsop_date') or ''}{info.get('cntg_vi_hour') or ''}"
    try:
        return datetime.strptime(raw, "%Y%m%d%H%M%S").replace(tzinfo=KST)
    except ValueError:
        return fallback


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
        now_fn: Callable[[], datetime] | None = None,
    ):
        self.ticker = ticker
        self.vi_active = False
        self._check_vi = check_vi
        self._freeze_sec = freeze_sec
        self._cooldown_sec = cooldown_sec
        self._monotonic = monotonic
        self._now_fn = now_fn or (lambda: datetime.now(KST))
        self._frozen_price: float | None = None
        self._frozen_since: float | None = None
        self._cooldown_until = 0.0
        self._check_task: asyncio.Task | None = None
        self._pending_frozen_price: float | None = None
        self._active_info: dict = {}
        self._active_start: datetime | None = None
        self._active_price: float | None = None

    async def on_price(self, price: float, source: str) -> list[dict]:
        """가격 관측(source: 'ws' | 'rest'). 발생한 VI 이벤트 목록을 반환.

        API를 기다리지 않으므로 항상 즉시 반환한다.
        """
        now = self._monotonic()

        if self.vi_active:
            # WS 틱 도착 = 체결 재개, REST 가격 변동 = 단일가 해제 체결
            if source == "ws" or price != self._active_price:
                return [self._release(price, source)]
            return []

        if source == "ws":
            # WS 틱이 흐르는 동안은 동결 아님. 진행 중이든 완료든 조회는 그 동결
            # 에피소드에 속한 것이므로 즉시 무효화한다 — 나중에 같은 가격으로
            # 재동결돼도 낡은 결과가 되살아나면 안 된다.
            self._reset_freeze()
            self._invalidate_pending_check()
            return []

        if self._frozen_price is None or price != self._frozen_price:
            self._invalidate_pending_check()  # 동결 깨짐 — 진행 중 조회 무효
            self._frozen_price = price
            self._frozen_since = now

        if self._check_task is not None:
            if not self._check_task.done():
                return []  # 조회 진행 중 — 감시 루프는 계속 돈다
            return self._consume_check()

        assert self._frozen_since is not None
        if now - self._frozen_since < self._freeze_sec or now < self._cooldown_until:
            return []

        # 의심 확정 — 백그라운드 조회 스폰 (여기서 기다리지 않는다)
        self._cooldown_until = now + self._cooldown_sec
        self._pending_frozen_price = price
        self._check_task = asyncio.create_task(self._run_check())
        return []

    async def _run_check(self) -> dict:
        """VI 조회 실행. 예외를 값으로 캡처해 태스크가 절대 예외로 끝나지 않는다."""
        try:
            return {"ok": True, "resp": await self._check_vi()}
        except Exception as e:
            return {"ok": False, "error": repr(e)}

    def _consume_check(self) -> list[dict]:
        assert self._check_task is not None
        result = self._check_task.result()
        self._check_task = None
        pending_price, self._pending_frozen_price = self._pending_frozen_price, None

        if pending_price != self._frozen_price:
            return []  # 조회 중 동결이 깨짐 — 낡은 결과 폐기

        if not result["ok"]:
            return [{"type": "VI_CHECK_FAILED", "error": result["error"]}]

        info = parse_vi_payload(result["resp"], self.ticker)
        detected_at = self._now_fn()
        if info is None:
            frozen_sec = (
                round(self._monotonic() - self._frozen_since, 1)
                if self._frozen_since is not None else None
            )
            return [{
                "type": "VI_CHECK_NEGATIVE",
                "frozen_price": pending_price,
                "frozen_sec": frozen_sec,
            }]

        start = _vi_start_from_api(info, detected_at)
        self.vi_active = True
        self._active_info = info
        self._active_start = start
        self._active_price = pending_price
        return [{
            "type": "VI_DETECTED",
            "ts": start.isoformat(),               # 실제 발동 시각 (API 기록)
            "detected_ts": detected_at.isoformat(),  # 시스템이 알아챈 시각
            "frozen_price": pending_price,
            **info,
        }]

    def _release(self, price: float, source: str) -> dict:
        released_at = self._now_fn()
        start = self._active_start or released_at
        event = {
            "type": "VI_RELEASED",
            "ts": released_at.isoformat(),
            "release_price": price,
            "source": source,
            "duration_sec": round((released_at - start).total_seconds(), 1),
            **self._active_info,
        }
        self.vi_active = False
        self._active_info = {}
        self._active_start = None
        self._active_price = None
        self._reset_freeze()
        return event

    def _invalidate_pending_check(self) -> None:
        """동결 에피소드가 끝났을 때 그에 속한 조회를 취소·폐기한다.

        취소된 태스크는 참조를 지우므로 어떤 경로에서도 소비되지 않는다.
        """
        if self._check_task is not None:
            self._check_task.cancel()
            self._check_task = None
            self._pending_frozen_price = None

    def _reset_freeze(self) -> None:
        self._frozen_price = None
        self._frozen_since = None
