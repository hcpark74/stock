"""ViWatch — 보유 중 VI(변동성완화장치) 감지 로직 유닛 테스트.

관측 전용 모듈: REST 백업 가격이 일정 시간 동결되면 VI 현황 API로 1회 확인,
VI_DETECTED / VI_RELEASED 이벤트를 반환한다. 매매 로직에는 개입하지 않는다.
"""
from unittest.mock import AsyncMock

from src.modules.vi_watch import ViWatch, parse_vi_payload

TICKER = "004310"

# 2026-07-16 실제 VI 현황 API 응답 (해제 완료 상태)
RELEASED_PAYLOAD = {
    "rt_cd": "0",
    "output": {
        "hts_kor_isnm": "현대약품", "mksc_shrn_iscd": "004310",
        "vi_cls_code": "N", "bsop_date": "20260716",
        "cntg_vi_hour": "091333", "vi_cncl_hour": "091550",
        "vi_kind_code": "1", "vi_prc": "7700", "vi_stnd_prc": "7000",
        "vi_dprt": "10.00", "vi_dmc_stnd_prc": "0", "vi_dmc_dprt": "0.00",
        "vi_count": "1",
    },
}

# 발동 중 상태 (해제 시각 없음)
ACTIVE_PAYLOAD = {
    "rt_cd": "0",
    "output": {
        "hts_kor_isnm": "현대약품", "mksc_shrn_iscd": "004310",
        "vi_cls_code": "Y", "bsop_date": "20260716",
        "cntg_vi_hour": "091333", "vi_cncl_hour": "",
        "vi_kind_code": "1", "vi_prc": "7700", "vi_stnd_prc": "7000",
        "vi_dprt": "10.00", "vi_count": "1",
    },
}


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, sec: float) -> None:
        self.t += sec


def _watch(check_vi, clock, **kwargs) -> ViWatch:
    return ViWatch(TICKER, check_vi, freeze_sec=10.0, cooldown_sec=60.0,
                   monotonic=clock, **kwargs)


# ── parse_vi_payload ─────────────────────────────────────────────────

def test_parse_returns_none_for_released_vi():
    assert parse_vi_payload(RELEASED_PAYLOAD, TICKER) is None


def test_parse_returns_info_for_active_vi():
    info = parse_vi_payload(ACTIVE_PAYLOAD, TICKER)
    assert info is not None
    assert info["vi_prc"] == "7700"
    assert info["vi_stnd_prc"] == "7000"
    assert info["vi_kind_code"] == "1"
    assert info["cntg_vi_hour"] == "091333"


def test_parse_ignores_other_ticker():
    assert parse_vi_payload(ACTIVE_PAYLOAD, "005930") is None


def test_parse_handles_list_output():
    payload = {"rt_cd": "0", "output": [ACTIVE_PAYLOAD["output"]]}
    assert parse_vi_payload(payload, TICKER) is not None


# ── 동결 감지 → VI 확인 ──────────────────────────────────────────────

async def test_no_check_before_freeze_threshold():
    clock = FakeClock()
    check = AsyncMock(return_value=ACTIVE_PAYLOAD)
    w = _watch(check, clock)

    assert await w.on_price(7690.0, source="rest") == []
    clock.advance(5)
    assert await w.on_price(7690.0, source="rest") == []
    check.assert_not_awaited()


async def test_frozen_price_triggers_check_and_detects_vi():
    clock = FakeClock()
    check = AsyncMock(return_value=ACTIVE_PAYLOAD)
    w = _watch(check, clock)

    await w.on_price(7690.0, source="rest")
    clock.advance(10)
    events = await w.on_price(7690.0, source="rest")

    check.assert_awaited_once()
    assert len(events) == 1
    assert events[0]["type"] == "VI_DETECTED"
    assert events[0]["vi_prc"] == "7700"
    assert events[0]["frozen_price"] == 7690.0
    assert w.vi_active is True


async def test_price_change_resets_freeze_anchor():
    clock = FakeClock()
    check = AsyncMock(return_value=ACTIVE_PAYLOAD)
    w = _watch(check, clock)

    await w.on_price(7690.0, source="rest")
    clock.advance(8)
    await w.on_price(7700.0, source="rest")  # 가격 변동 → 동결 아님
    clock.advance(8)
    events = await w.on_price(7700.0, source="rest")  # 새 anchor 기준 8초뿐

    check.assert_not_awaited()
    assert events == []


async def test_ws_tick_resets_freeze_anchor():
    clock = FakeClock()
    check = AsyncMock(return_value=ACTIVE_PAYLOAD)
    w = _watch(check, clock)

    await w.on_price(7690.0, source="rest")
    clock.advance(8)
    await w.on_price(7690.0, source="ws")  # WS 틱 존재 → 동결 아님
    clock.advance(8)
    events = await w.on_price(7690.0, source="rest")

    check.assert_not_awaited()
    assert events == []


# ── 해제 감지 ────────────────────────────────────────────────────────

async def _activate(w: ViWatch, clock: FakeClock, price: float = 7690.0):
    await w.on_price(price, source="rest")
    clock.advance(10)
    events = await w.on_price(price, source="rest")
    assert events and events[0]["type"] == "VI_DETECTED"


async def test_rest_price_change_releases_vi():
    clock = FakeClock()
    w = _watch(AsyncMock(return_value=ACTIVE_PAYLOAD), clock)
    await _activate(w, clock)

    clock.advance(120)
    events = await w.on_price(7630.0, source="rest")

    assert len(events) == 1
    assert events[0]["type"] == "VI_RELEASED"
    assert events[0]["release_price"] == 7630.0
    assert events[0]["duration_sec"] == 120.0
    assert w.vi_active is False


async def test_ws_tick_releases_vi_even_at_same_price():
    clock = FakeClock()
    w = _watch(AsyncMock(return_value=ACTIVE_PAYLOAD), clock)
    await _activate(w, clock)

    clock.advance(130)
    events = await w.on_price(7690.0, source="ws")  # 체결 재개 = 해제

    assert len(events) == 1
    assert events[0]["type"] == "VI_RELEASED"
    assert w.vi_active is False


async def test_same_frozen_rest_price_while_active_emits_nothing():
    clock = FakeClock()
    w = _watch(AsyncMock(return_value=ACTIVE_PAYLOAD), clock)
    await _activate(w, clock)

    clock.advance(30)
    assert await w.on_price(7690.0, source="rest") == []
    assert w.vi_active is True


# ── 미발동·오류 처리 ─────────────────────────────────────────────────

async def test_negative_check_emits_once_and_cools_down():
    clock = FakeClock()
    check = AsyncMock(return_value=RELEASED_PAYLOAD)  # 발동 중 아님
    w = _watch(check, clock)

    await w.on_price(7690.0, source="rest")
    clock.advance(10)
    events = await w.on_price(7690.0, source="rest")
    assert [e["type"] for e in events] == ["VI_CHECK_NEGATIVE"]

    clock.advance(30)  # 쿨다운(60초) 이내
    assert await w.on_price(7690.0, source="rest") == []
    assert check.await_count == 1

    clock.advance(31)  # 쿨다운 경과
    events = await w.on_price(7690.0, source="rest")
    assert [e["type"] for e in events] == ["VI_CHECK_NEGATIVE"]
    assert check.await_count == 2


async def test_check_failure_emits_event_and_cools_down():
    clock = FakeClock()
    check = AsyncMock(side_effect=RuntimeError("api down"))
    w = _watch(check, clock)

    await w.on_price(7690.0, source="rest")
    clock.advance(10)
    events = await w.on_price(7690.0, source="rest")

    assert [e["type"] for e in events] == ["VI_CHECK_FAILED"]
    assert "api down" in events[0]["error"]
    assert w.vi_active is False

    clock.advance(30)
    assert await w.on_price(7690.0, source="rest") == []
    assert check.await_count == 1
