"""집계·이력 API가 트랙을 섞지 않는다."""
import json

import pytest

from src import db
from src.api import server


@pytest.fixture
async def mem():
    await db.init(":memory:")
    yield
    await db.close()


async def _closed_trade(date, track, pnl_pct):
    trade_id = await db.open_trade(date, "215600", 3000.0, 100, track=track)
    conn = db.get()
    await conn.execute(
        "UPDATE trades SET status='CLOSED', pnl_pct=?, close_reason='TRAILING' "
        "WHERE id=?",
        (pnl_pct, trade_id),
    )
    await conn.commit()
    return trade_id


async def test_stats_counts_only_the_requested_track(mem):
    await _closed_trade("20260824", "A", -2.0)
    await _closed_trade("20260824", "B", 5.0)
    await _closed_trade("20260825", "B", 4.0)

    a_body = json.loads((await server.api_stats(track="A")).body)
    b_body = json.loads((await server.api_stats(track="B")).body)

    assert a_body["total"] == 1
    assert b_body["total"] == 2
    assert b_body["wins"] == 2      # A의 손실이 섞이면 승률이 무너진다


async def test_history_rows_carry_their_track(mem):
    await _closed_trade("20260826", "A", -2.0)
    await _closed_trade("20260826", "B", 3.0)

    rows = json.loads((await server.api_history(track="B")).body)

    assert len(rows) == 1
    assert rows[0]["track"] == "B"


async def test_stats_defaults_to_track_a_for_existing_callers(mem):
    await _closed_trade("20260824", "A", -2.0)
    await _closed_trade("20260824", "B", 5.0)

    default_body = json.loads((await server.api_stats()).body)
    a_body = json.loads((await server.api_stats(track="A")).body)

    assert default_body == a_body
    assert default_body["total"] == 1


async def test_improve_scopes_trades_orders_and_skips_by_track(mem):
    t_a = await db.open_trade("20260810", "005930", 10_000.0, 10, track="A")
    await db.close_trade(
        t_a, 9_800.0, "HARD_STOP", -2.0, 0.0, exit_qty=10, high_price=10_000.0
    )
    t_b = await db.open_trade("20260810", "000660", 10_000.0, 10, track="B")
    await db.close_trade(
        t_b, 10_500.0, "TRAILING", 5.0, 0.025, exit_qty=10, high_price=10_500.0
    )

    o_a = await db.record_order(
        t_a, "KISA1", "BUY", 10, 10_000.0, "FIRST_BUY", "005930"
    )
    await db.update_order_fill(o_a, 10_000.0, 10, 400)
    o_b = await db.record_order(
        t_b, "KISB1", "BUY", 10, 10_000.0, "FIRST_BUY", "000660"
    )
    await db.update_order_fill(o_b, 10_000.0, 10, 400)

    await db.record_skip("20260811", "NO_TARGET", "", track="A")
    await db.record_skip("20260811", "SLIPPAGE_GUARD", "", track="B")

    a_payload = json.loads((await server.api_improve(track="A")).body)
    b_payload = json.loads((await server.api_improve(track="B")).body)

    assert a_payload["overall"]["total"] == 1
    assert a_payload["overall"]["wins"] == 0
    assert b_payload["overall"]["total"] == 1
    assert b_payload["overall"]["wins"] == 1
    assert a_payload["candidates"]["skips"] == {"NO_TARGET": 1}
    assert b_payload["candidates"]["skips"] == {"SLIPPAGE_GUARD": 1}
