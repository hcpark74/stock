"""트랙 스코프 — 같은 날 두 트랙이 서로를 덮지 않는다."""
import pytest

from src import db


@pytest.fixture
async def mem():
    await db.init(":memory:")
    yield
    await db.close()


async def test_both_tracks_open_their_own_trade_on_the_same_day(mem):
    a = await db.open_trade("20260826", "215600", 3095.0, 610, name="신라젠")
    b = await db.open_trade("20260826", "215600", 3200.0, 300, track="B")

    assert a != b
    assert (await db.get_trade_by_date("20260826"))["id"] == a
    assert (await db.get_trade_by_date("20260826", track="B"))["id"] == b


async def test_reopening_the_same_track_is_idempotent(mem):
    first = await db.open_trade("20260826", "215600", 3095.0, 610)
    again = await db.open_trade("20260826", "215600", 3095.0, 610)
    assert first == again


async def test_track_b_conflict_never_returns_track_a_trade(mem):
    a = await db.open_trade("20260826", "215600", 3095.0, 610)
    b_first = await db.open_trade("20260826", "215600", 3200.0, 300, track="B")
    b_again = await db.open_trade("20260826", "215600", 3200.0, 300, track="B")

    assert b_again == b_first
    assert b_again != a  # 멱등 분기가 A의 거래를 돌려주면 B가 A를 청산한다
