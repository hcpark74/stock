"""트랙 B 상태 — 트랙 A 동결과 구버전 상태 파일 하위호환."""
import json

import pytest

from src import state


@pytest.fixture(autouse=True)
def _reset():
    state._state = state.State()
    state._tracks = {}
    yield
    state._state = state.State()
    state._tracks = {}


def test_track_state_is_isolated_from_track_a():
    state.get().position_status = "HOLDING"
    state.get().entry_price = 3095.0

    b = state.track("B")
    assert b.position_status == "IDLE"
    assert b.entry_price is None

    b.position_status = "ENTERING"
    assert state.get().position_status == "HOLDING"  # A는 영향 없다


def test_track_returns_the_same_instance():
    assert state.track("B") is state.track("B")


async def test_persist_keeps_legacy_fields_at_top_level(tmp_path):
    s = state.get()
    s.target_ticker = "215600"
    s.entry_price = 3095.0
    s.position_status = "HOLDING"
    state.track("B").position_status = "ENTERING"

    await state.persist(str(tmp_path), "20260826")
    data = json.loads((tmp_path / "today_state.json").read_text(encoding="utf-8"))

    # 구버전 복구 경로가 읽는 최상위 필드가 그대로 있어야 한다.
    assert data["ticker"] == "215600"
    assert data["entry_price"] == 3095.0
    assert data["position_status"] == "HOLDING"
    assert data["tracks"]["B"]["position_status"] == "ENTERING"


def test_restore_from_legacy_file_without_tracks_key():
    state.restore_from({
        "date": "20260826",
        "ticker": "215600",
        "entry_price": 3095.0,
        "position_status": "HOLDING",
    })

    assert state.get().position_status == "HOLDING"
    assert state.track("B").position_status == "IDLE"  # tracks 없으면 IDLE


def test_restore_from_reads_the_tracks_section():
    state.restore_from({
        "date": "20260826",
        "ticker": "215600",
        "position_status": "CLOSED",
        "tracks": {"B": {"position_status": "HOLDING", "entry_price": 3200.0,
                         "entry_qty": 300, "trade_id": 41}},
    })

    b = state.track("B")
    assert b.position_status == "HOLDING"
    assert b.entry_price == 3200.0
    assert b.trade_id == 41


def test_restore_ignores_unknown_track_fields():
    # 스키마가 앞서간 상태 파일을 만나도 복구가 죽으면 실포지션을 잃는다.
    state.restore_from({
        "date": "20260826",
        "tracks": {"B": {"position_status": "HOLDING", "future_field": 1}},
    })
    assert state.track("B").position_status == "HOLDING"
