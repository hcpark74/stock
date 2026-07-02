"""DB CRUD 유닛 테스트 — :memory: SQLite 사용."""
import pytest

from src import db


# ── 픽스처 ────────────────────────────────────────────────────────────

@pytest.fixture
async def mem():
    """각 테스트에 독립적인 :memory: DB 제공."""
    await db.init(":memory:")
    yield
    await db.close()


# ── open_trade ────────────────────────────────────────────────────────

async def test_open_trade_returns_positive_id(mem):
    trade_id = await db.open_trade("20260623", "005930", 75_000.0, 10)
    assert isinstance(trade_id, int)
    assert trade_id > 0


async def test_open_trade_status_is_open(mem):
    trade_id = await db.open_trade("20260623", "005930", 75_000.0, 10)
    conn = db.get()
    async with conn.execute("SELECT status FROM trades WHERE id=?", (trade_id,)) as cur:
        row = await cur.fetchone()
    assert row["status"] == "OPEN"


async def test_open_trade_stores_fields(mem):
    trade_id = await db.open_trade("20260624", "035420", 180_000.0, 5)
    conn = db.get()
    async with conn.execute(
        "SELECT ticker, entry_price, entry_qty FROM trades WHERE id=?", (trade_id,)
    ) as cur:
        row = await cur.fetchone()
    assert row["ticker"] == "035420"
    assert row["entry_price"] == pytest.approx(180_000.0)
    assert row["entry_qty"] == 5


# ── record_order ──────────────────────────────────────────────────────

async def test_record_order_returns_positive_id(mem):
    trade_id = await db.open_trade("20260623", "005930", 75_000.0, 10)
    order_id = await db.record_order(trade_id, "ORD001", "BUY", 10, 75_000.0, "FIRST_BUY", "005930")
    assert isinstance(order_id, int)
    assert order_id > 0


async def test_record_order_status_is_pending(mem):
    trade_id = await db.open_trade("20260623", "005930", 75_000.0, 10)
    order_id = await db.record_order(trade_id, "ORD001", "BUY", 10, 75_000.0, "FIRST_BUY", "005930")
    conn = db.get()
    async with conn.execute("SELECT status FROM orders WHERE id=?", (order_id,)) as cur:
        row = await cur.fetchone()
    assert row["status"] == "PENDING"


# ── update_order_fill ─────────────────────────────────────────────────

async def test_update_order_fill_sets_filled(mem):
    trade_id = await db.open_trade("20260623", "005930", 75_000.0, 10)
    order_id = await db.record_order(trade_id, "ORD001", "BUY", 10, 75_000.0, "FIRST_BUY", "005930")
    await db.update_order_fill(order_id, 75_200.0, 10, 150)
    conn = db.get()
    async with conn.execute(
        "SELECT status, fill_price, fill_qty, fill_latency_ms FROM orders WHERE id=?",
        (order_id,),
    ) as cur:
        row = await cur.fetchone()
    assert row["status"] == "FILLED"
    assert row["fill_price"] == pytest.approx(75_200.0)
    assert row["fill_qty"] == 10
    assert row["fill_latency_ms"] == 150


# ── close_trade ───────────────────────────────────────────────────────

async def test_close_trade_status_is_closed(mem):
    trade_id = await db.open_trade("20260623", "005930", 75_000.0, 10)
    await db.close_trade(trade_id, 78_000.0, "TRAILING", 4.0, 0.025)
    conn = db.get()
    async with conn.execute("SELECT status FROM trades WHERE id=?", (trade_id,)) as cur:
        row = await cur.fetchone()
    assert row["status"] == "CLOSED"


async def test_close_trade_stores_pnl_and_highest_step(mem):
    trade_id = await db.open_trade("20260623", "005930", 75_000.0, 10)
    await db.close_trade(trade_id, 78_750.0, "TRAILING", 5.0, 0.05)
    conn = db.get()
    async with conn.execute(
        "SELECT close_reason, pnl_pct, highest_step, exit_price FROM trades WHERE id=?",
        (trade_id,),
    ) as cur:
        row = await cur.fetchone()
    assert row["close_reason"] == "TRAILING"
    assert row["pnl_pct"] == pytest.approx(5.0)
    assert row["highest_step"] == pytest.approx(0.05)
    assert row["exit_price"] == pytest.approx(78_750.0)


async def test_close_trade_hard_stop(mem):
    trade_id = await db.open_trade("20260623", "005930", 75_000.0, 10)
    await db.close_trade(trade_id, 73_500.0, "HARD_STOP", -2.0, 0.0)
    conn = db.get()
    async with conn.execute("SELECT close_reason FROM trades WHERE id=?", (trade_id,)) as cur:
        row = await cur.fetchone()
    assert row["close_reason"] == "HARD_STOP"


# ── record_skip ───────────────────────────────────────────────────────

async def test_record_skip_inserts_row(mem):
    await db.record_skip("20260623", "NO_TARGET", "gap_filtered=0")
    conn = db.get()
    async with conn.execute(
        "SELECT reason, detail FROM daily_skips WHERE date='20260623'"
    ) as cur:
        row = await cur.fetchone()
    assert row["reason"] == "NO_TARGET"
    assert row["detail"] == "gap_filtered=0"


async def test_record_skip_duplicate_ignored(mem):
    """같은 날짜 중복 INSERT → INSERT OR IGNORE, 1행만 존재."""
    await db.record_skip("20260623", "NO_TARGET", "first")
    await db.record_skip("20260623", "NO_TARGET", "second")  # 무시됨
    conn = db.get()
    async with conn.execute(
        "SELECT COUNT(*) as cnt FROM daily_skips WHERE date='20260623'"
    ) as cur:
        row = await cur.fetchone()
    assert row[0] == 1


# ── 전체 거래 생명주기 ────────────────────────────────────────────────

async def test_full_lifecycle_open_buy_close(mem):
    """open_trade → record_order → update_order_fill → close_trade 흐름."""
    trade_id = await db.open_trade("20260623", "005930", 75_000.0, 10)

    buy_id = await db.record_order(
        trade_id, "ORD001", "BUY", 10, 75_000.0, "FIRST_BUY", "005930"
    )
    await db.update_order_fill(buy_id, 75_200.0, 10, 120)

    sell_id = await db.record_order(
        trade_id, "ORD002", "SELL", 10, 78_000.0, "CLOSE_SELL", "005930"
    )
    await db.update_order_fill(sell_id, 78_000.0, 10, 200)

    await db.close_trade(trade_id, 78_000.0, "TRAILING", 3.72, 0.025)

    conn = db.get()
    async with conn.execute(
        "SELECT COUNT(*) as cnt FROM orders WHERE trade_id=?", (trade_id,)
    ) as cur:
        assert (await cur.fetchone())[0] == 2  # BUY + SELL

    async with conn.execute("SELECT status FROM trades WHERE id=?", (trade_id,)) as cur:
        assert (await cur.fetchone())["status"] == "CLOSED"


async def test_pyramid_buy_creates_two_buy_orders(mem):
    """1차 매수 + 피라미딩 매수 → orders 테이블에 BUY 2행."""
    trade_id = await db.open_trade("20260623", "035420", 180_000.0, 7)

    b1 = await db.record_order(trade_id, "ORD001", "BUY", 7, 180_000.0, "FIRST_BUY", "035420")
    b2 = await db.record_order(trade_id, "ORD002", "BUY", 3, 182_000.0, "PYRAMID_BUY", "035420")
    await db.update_order_fill(b1, 180_000.0, 7, 100)
    await db.update_order_fill(b2, 182_000.0, 3, 80)

    conn = db.get()
    async with conn.execute(
        "SELECT COUNT(*) as cnt FROM orders WHERE trade_id=? AND order_type='BUY'",
        (trade_id,),
    ) as cur:
        assert (await cur.fetchone())[0] == 2


async def test_timeout_close_reason(mem):
    """TIMEOUT으로 close_trade → close_reason 정상 기록."""
    trade_id = await db.open_trade("20260623", "000660", 130_000.0, 5)
    await db.close_trade(trade_id, 129_000.0, "TIMEOUT", -0.77, 0.0)
    conn = db.get()
    async with conn.execute(
        "SELECT close_reason, pnl_pct FROM trades WHERE id=?", (trade_id,)
    ) as cur:
        row = await cur.fetchone()
    assert row["close_reason"] == "TIMEOUT"
    assert row["pnl_pct"] == pytest.approx(-0.77)


async def test_record_asset_snapshot_inserts_row(mem):
    snapshot_id = await db.record_asset_snapshot(
        {
            "total_asset": 1_500_000.0,
            "cash": 1_000_000.0,
            "buyable_cash": 800_000.0,
            "buyable_cash_source": "ord_psbl_cash",
            "stock_value": 500_000.0,
            "pnl_amount": 12_000.0,
            "holdings_count": 1,
            "source": "KIS",
        },
        raw={"rt_cd": "0"},
    )
    conn = db.get()
    async with conn.execute(
        "SELECT total_asset, buyable_cash_source, raw_json FROM asset_snapshots WHERE id=?",
        (snapshot_id,),
    ) as cur:
        row = await cur.fetchone()

    assert row["total_asset"] == pytest.approx(1_500_000.0)
    assert row["buyable_cash_source"] == "ord_psbl_cash"
    assert row["raw_json"] == '{"rt_cd":"0"}'


async def test_latest_asset_snapshot_returns_newest(mem):
    await db.record_asset_snapshot({"total_asset": 1.0, "source": "KIS"})
    await db.record_asset_snapshot({"total_asset": 2.0, "source": "KIS"})

    latest = await db.latest_asset_snapshot()

    assert latest["total_asset"] == pytest.approx(2.0)
    assert latest["snapshot_source"] == "DB"
