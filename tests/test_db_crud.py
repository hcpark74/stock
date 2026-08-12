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


async def test_open_trade_stores_name(mem):
    trade_id = await db.open_trade("20260624", "035420", 180_000.0, 5, name="NAVER")
    conn = db.get()
    async with conn.execute("SELECT name FROM trades WHERE id=?", (trade_id,)) as cur:
        row = await cur.fetchone()
    assert row["name"] == "NAVER"


async def test_open_trade_reuse_backfills_missing_name(mem):
    first_id = await db.open_trade("20260623", "005930", 75_000.0, 10)
    second_id = await db.open_trade("20260623", "005930", 75_000.0, 10, name="삼성전자")
    conn = db.get()
    async with conn.execute("SELECT name FROM trades WHERE id=?", (first_id,)) as cur:
        row = await cur.fetchone()
    assert second_id == first_id
    assert row["name"] == "삼성전자"


async def test_get_trade_by_date_includes_all_confirmed_buy_fills(mem):
    trade_id = await db.open_trade("20260625", "005930", 75_000.0, 70)
    first = await db.record_order(
        trade_id, "BUY-1", "BUY", 70, 75_000.0, "FIRST_BUY", "005930"
    )
    pyramid = await db.record_order(
        trade_id, "BUY-2", "BUY", 30, 76_000.0, "PYRAMID_BUY", "005930"
    )
    await db.update_order_fill(first, 75_000.0, 70, 100)
    await db.update_order_fill(pyramid, 76_000.0, 30, 100)

    trade = await db.get_trade_by_date("20260625")

    assert trade["entry_qty"] == 70
    assert trade["confirmed_entry_qty"] == 100


# ── record_order ──────────────────────────────────────────────────────


async def test_entry_order_attempt_audits_cancelled_order_without_trade(mem):
    attempt_id = await db.record_entry_order_attempt(
        "20260807",
        "0000000839",
        "064400",
        103,
        75_300.0,
        74_500.0,
        1,
        2,
        "PAPER",
        org_no="00950",
        name="LG씨엔에스",
        status="CANCELLED",
    )

    conn = db.get()
    async with conn.execute(
        "SELECT * FROM entry_order_attempts WHERE id=?",
        (attempt_id,),
    ) as cur:
        row = await cur.fetchone()

    assert row["kis_order_id"] == "0000000839"
    assert row["ticker"] == "064400"
    assert row["status"] == "CANCELLED"
    assert row["order_phase"] == "FIRST_BUY"
    assert row["order_qty"] == 103
    assert row["order_price"] == pytest.approx(75_300.0)
    assert row["trigger_price"] == pytest.approx(74_500.0)


async def test_entry_order_attempt_records_fill_reconciliation(mem):
    attempt_id = await db.record_entry_order_attempt(
        "20260807",
        "0000000947",
        "064400",
        103,
        75_100.0,
        74_500.0,
        2,
        2,
        "PAPER",
        status="FILLED",
        fill_price=74_400.0,
        fill_qty=103,
        fill_latency_ms=4_552,
    )

    conn = db.get()
    async with conn.execute(
        "SELECT status, fill_price, fill_qty, fill_latency_ms "
        "FROM entry_order_attempts WHERE id=?",
        (attempt_id,),
    ) as cur:
        row = await cur.fetchone()

    assert dict(row) == {
        "status": "FILLED",
        "fill_price": 74_400.0,
        "fill_qty": 103,
        "fill_latency_ms": 4_552,
    }


async def test_entry_order_attempt_natural_key_upsert_resolves_uncertain(mem):
    audit = {
        "date": "20260807",
        "kis_order_id": "0000000951",
        "ticker": "064400",
        "qty": 3,
        "price": 75_100.0,
        "trigger_price": 74_500.0,
        "attempt": 1,
        "max_attempts": 2,
        "mode": "PAPER",
    }
    attempt_id = await db.record_entry_order_attempt(**audit, status="UNCERTAIN")
    resolved_id = await db.record_entry_order_attempt(
        **audit,
        status="FILLED",
        fill_price=75_000.0,
        fill_qty=3,
        fill_latency_ms=800,
    )
    # A late detached initial write must not downgrade the resolved row.
    await db.record_entry_order_attempt(**audit, status="PENDING")

    async with db.get().execute(
        "SELECT * FROM entry_order_attempts WHERE date=? AND kis_order_id=?",
        (audit["date"], audit["kis_order_id"]),
    ) as cur:
        row = await cur.fetchone()

    assert resolved_id == attempt_id
    assert row["status"] == "FILLED"
    assert row["fill_price"] == pytest.approx(75_000.0)
    assert row["fill_qty"] == 3


async def test_entry_order_attempt_status_update_preserves_fill_values(mem):
    await db.record_entry_order_attempt(
        "20260807",
        "0000000952",
        "064400",
        3,
        75_100.0,
        74_500.0,
        1,
        2,
        "PAPER",
        status="PARTIAL_FILL",
        fill_price=75_000.0,
        fill_qty=1,
    )

    await db.record_entry_order_attempt(
        "20260807",
        "0000000952",
        "064400",
        3,
        75_100.0,
        74_500.0,
        1,
        2,
        "PAPER",
        status="UNCERTAIN",
    )

    async with db.get().execute(
        "SELECT status, fill_price, fill_qty FROM entry_order_attempts "
        "WHERE date=? AND kis_order_id=?",
        ("20260807", "0000000952"),
    ) as cur:
        row = await cur.fetchone()
    assert dict(row) == {
        "status": "UNCERTAIN",
        "fill_price": 75_000.0,
        "fill_qty": 1,
    }


async def test_entry_order_attempt_preserves_pyramid_phase(mem):
    await db.record_entry_order_attempt(
        "20260807",
        "PYRAMID-CANCELLED-1",
        "064400",
        2,
        75_500.0,
        75_300.0,
        1,
        1,
        "PAPER",
        order_phase="PYRAMID_BUY",
        status="CANCELLED",
    )

    async with db.get().execute(
        "SELECT order_phase, status FROM entry_order_attempts "
        "WHERE date=? AND kis_order_id=?",
        ("20260807", "PYRAMID-CANCELLED-1"),
    ) as cur:
        row = await cur.fetchone()

    assert dict(row) == {
        "order_phase": "PYRAMID_BUY",
        "status": "CANCELLED",
    }


async def test_init_migrates_legacy_entry_attempt_phase(tmp_path):
    import aiosqlite

    path = str(tmp_path / "legacy-entry-attempt.db")
    async with aiosqlite.connect(path) as conn:
        await conn.execute("""
            CREATE TABLE entry_order_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                kis_order_id TEXT NOT NULL,
                org_no TEXT,
                ticker TEXT NOT NULL,
                name TEXT,
                entry_attempt INTEGER NOT NULL,
                max_attempts INTEGER NOT NULL,
                order_qty INTEGER NOT NULL,
                order_price REAL NOT NULL,
                trigger_price REAL,
                execution_mode TEXT NOT NULL,
                status TEXT NOT NULL,
                fill_price REAL,
                fill_qty INTEGER,
                fill_latency_ms INTEGER,
                ordered_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (date, kis_order_id)
            )
        """)
        await conn.execute(
            "INSERT INTO entry_order_attempts "
            "(date, kis_order_id, ticker, entry_attempt, max_attempts, "
            "order_qty, order_price, execution_mode, status, ordered_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "20260806",
                "LEGACY-ENTRY-1",
                "064400",
                1,
                2,
                3,
                75_100.0,
                "PAPER",
                "CANCELLED",
                "2026-08-06T09:00:00+09:00",
                "2026-08-06T09:00:01+09:00",
            ),
        )
        await conn.commit()

    await db.init(path)
    async with db.get().execute(
        "SELECT order_phase FROM entry_order_attempts WHERE kis_order_id=?",
        ("LEGACY-ENTRY-1",),
    ) as cur:
        row = await cur.fetchone()
    await db.close()

    assert row["order_phase"] == "FIRST_BUY"


async def test_open_trade_reuses_existing_same_day_trade(mem):
    first_id = await db.open_trade("20260623", "005930", 75_000.0, 10)
    second_id = await db.open_trade("20260623", "000660", 120_000.0, 1)

    conn = db.get()
    async with conn.execute(
        "SELECT COUNT(*) AS cnt FROM trades WHERE date=?", ("20260623",)
    ) as cur:
        row = await cur.fetchone()

    assert second_id == first_id
    assert row["cnt"] == 1

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


async def test_record_order_separates_submitted_and_trigger_price(mem):
    trade_id = await db.open_trade("20260623", "005930", 75_000.0, 10)
    order_id = await db.record_order(
        trade_id,
        "ORD001",
        "BUY",
        10,
        0.0,
        "FIRST_BUY",
        "005930",
        trigger_price=75_000.0,
    )
    conn = db.get()
    async with conn.execute(
        "SELECT order_price, trigger_price FROM orders WHERE id=?",
        (order_id,),
    ) as cur:
        row = await cur.fetchone()
    assert row["order_price"] == 0.0
    assert row["trigger_price"] == pytest.approx(75_000.0)


async def test_get_order_by_kis_id_filters_trading_date_and_ticker(mem):
    trade_id = await db.open_trade("20260623", "005930", 75_000.0, 10)
    order_id = await db.record_order(
        trade_id,
        "0000000937",
        "BUY",
        10,
        75_000.0,
        "FIRST_BUY",
        "005930",
    )

    found = await db.get_order_by_kis_id(
        "0000000937",
        date="20260623",
        ticker="005930",
    )

    assert found["id"] == order_id
    assert await db.get_order_by_kis_id(
        "0000000937",
        date="20260624",
        ticker="005930",
    ) is None
    assert await db.get_order_by_kis_id(
        "0000000937",
        date="20260623",
        ticker="000660",
    ) is None


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



async def test_update_trade_progress_stores_high_and_step(mem):
    trade_id = await db.open_trade("20260623", "005930", 75_000.0, 10)

    await db.update_trade_progress(trade_id, 78_500.0, 0.05)

    conn = db.get()
    async with conn.execute(
        "SELECT high_price, highest_step, status FROM trades WHERE id=?",
        (trade_id,),
    ) as cur:
        row = await cur.fetchone()
    assert row["high_price"] == pytest.approx(78_500.0)
    assert row["highest_step"] == pytest.approx(0.05)
    assert row["status"] == "OPEN"


async def test_update_trade_progress_ignores_closed_trade(mem):
    trade_id = await db.open_trade("20260623", "005930", 75_000.0, 10)
    await db.close_trade(
        trade_id, 76_000.0, "TRAILING", 1.33, 0.025,
        exit_qty=10, high_price=None,
    )

    await db.update_trade_progress(trade_id, 80_000.0, 0.075)

    conn = db.get()
    async with conn.execute(
        "SELECT high_price, highest_step, status FROM trades WHERE id=?",
        (trade_id,),
    ) as cur:
        row = await cur.fetchone()
    assert row["high_price"] is None
    assert row["highest_step"] == pytest.approx(0.025)
    assert row["status"] == "CLOSED"


# ── trailing shadow comparison ────────────────────────────────────────

async def test_trailing_shadow_preserves_first_baseline_exit_and_finalizes(mem):
    trade_id = await db.open_trade("20260623", "005930", 10_000.0, 100)

    inserted = await db.record_trailing_shadow_baseline(
        trade_id,
        baseline_step_trail=0.015,
        recommended_step_trail=0.020,
        entry_price=10_000.0,
        highest_step=0.025,
        baseline_stop_price=10_100.0,
        recommended_stop_price=10_050.0,
        baseline_exit_price=10_090.0,
    )
    repeated = await db.record_trailing_shadow_baseline(
        trade_id,
        baseline_step_trail=0.015,
        recommended_step_trail=0.020,
        entry_price=10_000.0,
        highest_step=0.025,
        baseline_stop_price=10_100.0,
        recommended_stop_price=10_050.0,
        baseline_exit_price=10_080.0,
    )

    assert inserted is True
    assert repeated is False

    comparison = await db.finalize_trailing_shadow_comparison(
        trade_id,
        baseline_step_trail=0.015,
        recommended_step_trail=0.020,
        entry_price=10_000.0,
        exit_qty=100,
        highest_step=0.025,
        baseline_stop_price=10_100.0,
        recommended_stop_price=10_050.0,
        recommended_exit_price=10_040.0,
        actual_exit_price=10_030.0,
        actual_pnl_pct=0.3,
        close_reason="TRAILING",
    )

    assert comparison["finalized"] == 1
    assert comparison["baseline_exit_price"] == pytest.approx(10_090.0)
    assert comparison["recommended_exit_price"] == pytest.approx(10_040.0)
    assert comparison["actual_exit_price"] == pytest.approx(10_030.0)
    assert comparison["baseline_pnl_pct"] == pytest.approx(0.9)
    assert comparison["recommended_pnl_pct"] == pytest.approx(0.4)
    assert comparison["pnl_delta_pct"] == pytest.approx(-0.5)
    assert comparison["baseline_pnl_amount"] == pytest.approx(9_000.0)
    assert comparison["recommended_pnl_amount"] == pytest.approx(4_000.0)
    assert comparison["pnl_delta_amount"] == pytest.approx(-5_000.0)


async def test_trailing_shadow_without_earlier_baseline_uses_same_exit(mem):
    trade_id = await db.open_trade("20260623", "005930", 10_000.0, 100)

    comparison = await db.finalize_trailing_shadow_comparison(
        trade_id,
        baseline_step_trail=0.015,
        recommended_step_trail=0.020,
        entry_price=10_000.0,
        exit_qty=100,
        highest_step=0.0,
        baseline_stop_price=None,
        recommended_stop_price=None,
        recommended_exit_price=9_800.0,
        actual_exit_price=9_790.0,
        actual_pnl_pct=-2.1,
        close_reason="HARD_STOP",
    )

    assert comparison["baseline_exit_price"] == pytest.approx(9_800.0)
    assert comparison["recommended_exit_price"] == pytest.approx(9_800.0)
    assert comparison["pnl_delta_pct"] == pytest.approx(0.0)
    assert comparison["pnl_delta_amount"] == pytest.approx(0.0)

# ── close_trade ───────────────────────────────────────────────────────

async def test_close_trade_status_is_closed(mem):
    trade_id = await db.open_trade("20260623", "005930", 75_000.0, 10)
    await db.close_trade(
        trade_id, 78_000.0, "TRAILING", 4.0, 0.025,
        exit_qty=10, high_price=78_000.0,
    )
    conn = db.get()
    async with conn.execute("SELECT status FROM trades WHERE id=?", (trade_id,)) as cur:
        row = await cur.fetchone()
    assert row["status"] == "CLOSED"


async def test_close_trade_stores_pnl_and_highest_step(mem):
    trade_id = await db.open_trade("20260623", "005930", 75_000.0, 10)
    await db.close_trade(
        trade_id, 78_750.0, "TRAILING", 5.0, 0.05,
        exit_qty=10, high_price=79_000.0,
    )
    conn = db.get()
    async with conn.execute(
        """SELECT close_reason, pnl_pct, pnl_amount, highest_step,
                  exit_price, exit_qty, high_price
           FROM trades WHERE id=?""",
        (trade_id,),
    ) as cur:
        row = await cur.fetchone()
    assert row["close_reason"] == "TRAILING"
    assert row["pnl_pct"] == pytest.approx(5.0)
    assert row["highest_step"] == pytest.approx(0.05)
    assert row["exit_price"] == pytest.approx(78_750.0)
    assert row["exit_qty"] == 10
    assert row["pnl_amount"] == pytest.approx(37_500.0)
    assert row["high_price"] == pytest.approx(79_000.0)


async def test_close_trade_preserves_higher_persisted_high(mem):
    trade_id = await db.open_trade("20260623", "005930", 75_000.0, 10)
    await db.update_trade_progress(trade_id, 80_000.0, 0.05)

    await db.close_trade(
        trade_id, 78_750.0, "TRAILING", 5.0, 0.05,
        exit_qty=10, high_price=79_000.0,
    )

    conn = db.get()
    async with conn.execute(
        "SELECT high_price FROM trades WHERE id=?", (trade_id,)
    ) as cur:
        row = await cur.fetchone()
    assert row["high_price"] == pytest.approx(80_000.0)


async def test_init_backfills_legacy_closed_trade_summary(tmp_path):
    db_path = str(tmp_path / "legacy-close.db")
    await db.init(db_path)
    trade_id = await db.open_trade("20260623", "005930", 75_000.0, 10)
    order_id = await db.record_order(
        trade_id, "SELL001", "SELL", 10, 78_000.0, "CLOSE_SELL", "005930"
    )
    await db.update_order_fill(order_id, 78_000.0, 10, 200)
    await db.close_trade(
        trade_id, 78_000.0, "TRAILING", 4.0, 0.025,
        exit_qty=10, high_price=79_000.0,
    )
    conn = db.get()
    await conn.execute(
        "UPDATE trades SET exit_qty=NULL, pnl_amount=NULL WHERE id=?", (trade_id,)
    )
    await conn.commit()
    await db.close()

    await db.init(db_path)
    conn = db.get()
    async with conn.execute(
        "SELECT exit_qty, pnl_amount FROM trades WHERE id=?", (trade_id,)
    ) as cur:
        row = await cur.fetchone()
    async with conn.execute(
        "SELECT order_price, trigger_price FROM orders WHERE id=?", (order_id,)
    ) as cur:
        order_row = await cur.fetchone()
    assert row["exit_qty"] == 10
    assert row["pnl_amount"] == pytest.approx(30_000.0)
    assert order_row["trigger_price"] == pytest.approx(order_row["order_price"])
    await db.close()


async def test_init_backfill_leaves_pyramided_pnl_amount_null(tmp_path):
    db_path = str(tmp_path / "legacy-pyramided.db")
    await db.init(db_path)
    trade_id = await db.open_trade("20260623", "005930", 10_000.0, 100)
    await db.mark_pyramided(trade_id)
    order_id = await db.record_order(
        trade_id, "SELL001", "SELL", 130, 10_200.0, "CLOSE_SELL", "005930"
    )
    await db.update_order_fill(order_id, 10_200.0, 130, 200)
    await db.close_trade(
        trade_id, 10_200.0, "TRAILING", 2.0, 0.02,
        exit_qty=130, high_price=None,
    )
    conn = db.get()
    await conn.execute(
        "UPDATE trades SET exit_qty=NULL, pnl_amount=NULL WHERE id=?", (trade_id,)
    )
    await conn.commit()
    await db.close()

    await db.init(db_path)
    conn = db.get()
    async with conn.execute(
        "SELECT exit_qty, pnl_amount FROM trades WHERE id=?", (trade_id,)
    ) as cur:
        row = await cur.fetchone()
    # exit_qty comes from summed sell fills and is correct even when
    # pyramided; pnl_amount must stay NULL because entry_price holds only
    # the first fill price.
    assert row["exit_qty"] == 130
    assert row["pnl_amount"] is None
    await db.close()


async def test_close_trade_rejects_non_positive_exit_qty(mem):
    trade_id = await db.open_trade("20260623", "005930", 75_000.0, 10)
    with pytest.raises(ValueError):
        await db.close_trade(
            trade_id, 78_000.0, "TRAILING", 4.0, 0.025,
            exit_qty=0, high_price=None,
        )


async def test_close_trade_raises_when_already_closed(mem):
    trade_id = await db.open_trade("20260623", "005930", 75_000.0, 10)
    await db.close_trade(
        trade_id, 78_000.0, "TRAILING", 4.0, 0.025,
        exit_qty=10, high_price=None,
    )
    with pytest.raises(RuntimeError):
        await db.close_trade(
            trade_id, 77_000.0, "TIMEOUT", 2.67, 0.025,
            exit_qty=10, high_price=None,
        )


async def test_close_trade_hard_stop(mem):
    trade_id = await db.open_trade("20260623", "005930", 75_000.0, 10)
    await db.close_trade(
        trade_id, 73_500.0, "HARD_STOP", -2.0, 0.0,
        exit_qty=10, high_price=75_000.0,
    )
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


async def test_record_skip_market_closed_persists(mem):
    """MARKET_CLOSED가 CHECK에 막혀 OR IGNORE로 조용히 누락되면 안 된다."""
    await db.record_skip("20260717", "MARKET_CLOSED", "msg_cd=40100000")
    conn = db.get()
    async with conn.execute(
        "SELECT reason FROM daily_skips WHERE date='20260717'"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row["reason"] == "MARKET_CLOSED"


async def test_init_migrates_legacy_daily_skips_check(tmp_path):
    """구 CHECK 제약(MARKET_CLOSED 없음) DB는 init()에서 재구축돼야 한다."""
    import aiosqlite

    path = str(tmp_path / "legacy.db")
    async with aiosqlite.connect(path) as conn:
        await conn.execute("""
            CREATE TABLE daily_skips (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                date       TEXT NOT NULL UNIQUE,
                reason     TEXT NOT NULL CHECK (reason IN (
                               'NO_TARGET','GAP_CHANGED','ENTRY_FAIL',
                               'SLIPPAGE_GUARD','MANUAL'
                           )),
                detail     TEXT,
                created_at TEXT NOT NULL
            )
        """)
        await conn.execute(
            "INSERT INTO daily_skips (date, reason, detail, created_at) "
            "VALUES ('20260701', 'NO_TARGET', 'legacy', '2026-07-01T09:00:00+09:00')"
        )
        await conn.commit()

    await db.init(path)
    await db.record_skip("20260717", "MARKET_CLOSED", "msg_cd=40100000")
    conn = db.get()
    async with conn.execute(
        "SELECT date, reason FROM daily_skips ORDER BY date"
    ) as cur:
        rows = await cur.fetchall()
    await db.close()

    assert [(r["date"], r["reason"]) for r in rows] == [
        ("20260701", "NO_TARGET"),
        ("20260717", "MARKET_CLOSED"),
    ]


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

    await db.close_trade(
        trade_id, 78_000.0, "TRAILING", 3.72, 0.025,
        exit_qty=10, high_price=78_000.0,
    )

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


async def test_mark_pyramided_updates_trade_flag(mem):
    trade_id = await db.open_trade("20260623", "035420", 180_000.0, 7)

    await db.mark_pyramided(trade_id)

    conn = db.get()
    async with conn.execute("SELECT pyramided FROM trades WHERE id=?", (trade_id,)) as cur:
        row = await cur.fetchone()
    assert row["pyramided"] == 1


async def test_timeout_close_reason(mem):
    """TIMEOUT으로 close_trade → close_reason 정상 기록."""
    trade_id = await db.open_trade("20260623", "000660", 130_000.0, 5)
    await db.close_trade(
        trade_id, 129_000.0, "TIMEOUT", -0.77, 0.0,
        exit_qty=5, high_price=130_000.0,
    )
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

async def test_latest_asset_snapshot_restores_holdings_from_raw(mem):
    await db.record_asset_snapshot(
        {"total_asset": 2.0, "holdings_count": 1, "source": "KIS"},
        raw={
            "rt_cd": "0",
            "output1": [
                {
                    "pdno": "365660",
                    "prdt_name": "Lemon Healthcare",
                    "hldg_qty": "9",
                    "ord_psbl_qty": "9",
                    "prpr": "11170",
                    "evlu_amt": "100530",
                    "evlu_pfls_amt": "4370",
                    "evlu_pfls_rt": "4.54",
                },
                {"pdno": "000660", "hldg_qty": "0"},
            ],
            "output2": [{}],
        },
    )

    latest = await db.latest_asset_snapshot()

    assert latest["holdings"] == [
        {
            "ticker": "365660",
            "name": "Lemon Healthcare",
            "qty": 9,
            "orderable_qty": 9,
            "current_price": 11170.0,
            "avg_price": None,
            "purchase_amount": None,
            "evaluation_amount": 100530.0,
            "pnl_amount": 4370.0,
            "pnl_pct": 4.54,
        }
    ]


async def test_record_order_stores_name(tmp_path):
    await db.init(str(tmp_path / "orders_name.db"))
    trade_id = await db.open_trade("20260702", "005930", 75_000.0, 10)
    order_id = await db.record_order(
        trade_id, "ORD001", "BUY", 10, 75_000.0, "FIRST_BUY", "005930", "삼성전자"
    )

    async with db.get().execute("SELECT ticker, name FROM orders WHERE id=?", (order_id,)) as cur:
        row = await cur.fetchone()

    assert dict(row) == {"ticker": "005930", "name": "삼성전자"}
    await db.close()

async def test_record_skip_vi_active_persists(mem):
    """VI_ACTIVE가 CHECK에 막혀 OR IGNORE로 조용히 누락되면 안 된다."""
    await db.record_skip("20260720", "VI_ACTIVE", "cntg_vi_hour=090032,vi_kind=2")
    conn = db.get()
    async with conn.execute(
        "SELECT reason FROM daily_skips WHERE date='20260720'"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row["reason"] == "VI_ACTIVE"


async def test_init_migrates_market_closed_era_daily_skips_check(tmp_path):
    """MARKET_CLOSED까지만 있는 CHECK 제약 DB도 init()에서 재구축돼야 한다."""
    import aiosqlite

    path = str(tmp_path / "market_closed_era.db")
    async with aiosqlite.connect(path) as conn:
        await conn.execute("""
            CREATE TABLE daily_skips (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                date       TEXT NOT NULL UNIQUE,
                reason     TEXT NOT NULL CHECK (reason IN (
                               'NO_TARGET','GAP_CHANGED','ENTRY_FAIL',
                               'SLIPPAGE_GUARD','MANUAL','MARKET_CLOSED'
                           )),
                detail     TEXT,
                created_at TEXT NOT NULL
            )
        """)
        await conn.execute(
            "INSERT INTO daily_skips (date, reason, detail, created_at) "
            "VALUES ('20260717', 'MARKET_CLOSED', 'legacy', '2026-07-17T09:00:00+09:00')"
        )
        await conn.commit()

    await db.init(path)
    await db.record_skip("20260720", "VI_ACTIVE", "cntg_vi_hour=090032")
    conn = db.get()
    async with conn.execute(
        "SELECT date, reason FROM daily_skips ORDER BY date"
    ) as cur:
        rows = await cur.fetchall()
    await db.close()

    assert [(r["date"], r["reason"]) for r in rows] == [
        ("20260717", "MARKET_CLOSED"),
        ("20260720", "VI_ACTIVE"),
    ]
