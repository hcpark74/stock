"""구 DB → 트랙 재작성 마이그레이션. 컬럼 정렬·FK 보존·백업."""
import sqlite3

import pytest

from src import db

# 구 DB 모양: name·highest_step이 ALTER로 맨 뒤에 붙어 있다.
_LEGACY_TRADES = """
CREATE TABLE trades (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    date         TEXT NOT NULL UNIQUE,
    ticker       TEXT NOT NULL,
    entry_price  REAL,
    entry_qty    INTEGER,
    entry_at     TEXT,
    exit_price   REAL,
    exit_qty     INTEGER,
    exit_at      TEXT,
    close_reason TEXT CHECK (close_reason IN (
                     'TRAILING','HARD_STOP',
                     'TIMEOUT','SLIPPAGE_GUARD','ENTRY_FAIL','MANUAL'
                 )),
    pnl_pct      REAL,
    pnl_amount   REAL,
    pyramided    INTEGER DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'OPEN'
                     CHECK (status IN ('OPEN','CLOSED','SKIPPED')),
    execution_mode TEXT,
    strategy_fingerprint TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
)
"""


def _make_legacy_db(path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(_LEGACY_TRADES)
    # ALTER로 뒤에 붙는 컬럼들 — 순서가 신규 스키마와 다르다.
    conn.execute("ALTER TABLE trades ADD COLUMN high_price REAL")
    conn.execute("ALTER TABLE trades ADD COLUMN highest_step REAL")
    conn.execute("ALTER TABLE trades ADD COLUMN name TEXT")
    conn.execute("ALTER TABLE trades ADD COLUMN experiment_id TEXT")
    conn.execute(
        """INSERT INTO trades
               (id, date, ticker, entry_price, entry_qty, entry_at,
                close_reason, pnl_pct, status, execution_mode,
                created_at, updated_at, high_price, highest_step, name,
                experiment_id)
           VALUES (7, '20260814', '005930', 71000.0, 10,
                   '2026-08-14T09:01:00+09:00',
                   'HARD_STOP', -2.1, 'CLOSED', 'PAPER',
                   '2026-08-14T09:00:00+09:00', '2026-08-14T09:02:00+09:00',
                   71500.0, 0.05, '삼성전자', 'baseline-old')"""
    )
    conn.commit()
    conn.close()


async def test_legacy_rows_keep_their_columns_after_rewrite(tmp_path):
    path = tmp_path / "trading.db"
    _make_legacy_db(path)

    await db.init(str(path))
    try:
        conn = db.get()
        async with conn.execute("SELECT * FROM trades WHERE id=7") as cur:
            row = dict(await cur.fetchone())
    finally:
        await db.close()

    assert row["name"] == "삼성전자"          # close_reason 자리로 밀리지 않는다
    assert row["close_reason"] == "HARD_STOP"
    assert row["ticker"] == "005930"
    assert row["highest_step"] == 0.05
    assert row["experiment_id"] == "baseline-old"
    assert row["track"] == "A"                # 기존 거래는 전부 트랙 A


async def test_cascade_child_rows_survive_the_rewrite(tmp_path):
    path = tmp_path / "trading.db"
    _make_legacy_db(path)
    conn = sqlite3.connect(path)
    conn.executescript(
        """CREATE TABLE trailing_shadow_comparisons (
               id       INTEGER PRIMARY KEY AUTOINCREMENT,
               trade_id INTEGER NOT NULL
                            REFERENCES trades(id) ON DELETE CASCADE,
               note     TEXT
           );
           INSERT INTO trailing_shadow_comparisons (trade_id, note)
                VALUES (7, 'baseline-vs-recommended');"""
    )
    conn.commit()
    conn.close()

    await db.init(str(path))
    try:
        conn2 = db.get()
        async with conn2.execute(
            "SELECT COUNT(*) AS n FROM trailing_shadow_comparisons"
        ) as cur:
            surviving = (await cur.fetchone())["n"]
        async with conn2.execute("PRAGMA foreign_key_check") as cur:
            violations = await cur.fetchall()
    finally:
        await db.close()

    assert surviving == 1  # CASCADE가 발화하면 0이 된다
    assert violations == []


async def test_migration_backs_up_the_db_file_first(tmp_path):
    path = tmp_path / "trading.db"
    _make_legacy_db(path)

    await db.init(str(path))
    await db.close()

    backups = list(tmp_path.glob("trading.db.pre_track_*"))
    assert len(backups) == 1
    assert backups[0].stat().st_size > 0


async def test_migration_is_skipped_when_already_migrated(tmp_path):
    path = tmp_path / "trading.db"
    _make_legacy_db(path)

    await db.init(str(path))
    await db.close()
    await db.init(str(path))          # 두 번째 기동
    await db.close()

    # 두 번째 기동은 재작성하지 않으므로 백업이 늘지 않는다.
    assert len(list(tmp_path.glob("trading.db.pre_track_*"))) == 1


async def test_two_tracks_can_skip_the_same_day(tmp_path):
    await db.init(":memory:")
    try:
        await db.record_skip("20260826", "NO_TARGET", "A는 갭 미달")
        await db.record_skip("20260826", "NO_TARGET", "B는 신호 없음", track="B")
        conn = db.get()
        async with conn.execute(
            "SELECT track, detail FROM daily_skips WHERE date='20260826' "
            "ORDER BY track"
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()

    assert [r["track"] for r in rows] == ["A", "B"]
    assert rows[1]["detail"] == "B는 신호 없음"


# FK 강제가 켜지기 전에 쓰인 구 DB에는 고아 orders.trade_id가 남아 있을 수 있다.
_LEGACY_ORDERS = """
CREATE TABLE orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id        INTEGER NOT NULL REFERENCES trades(id),
    kis_order_id    TEXT,
    order_type      TEXT NOT NULL CHECK (order_type  IN ('BUY','SELL')),
    order_phase     TEXT NOT NULL CHECK (order_phase IN (
                        'FIRST_BUY','PYRAMID_BUY','PARTIAL_SELL',
                        'CLOSE_SELL','TIMEOUT_SELL','SLIPPAGE_SELL','CANCEL'
                    )),
    ticker          TEXT NOT NULL,
    name            TEXT,
    order_qty       INTEGER NOT NULL,
    order_price     REAL,
    trigger_price   REAL,
    fill_price      REAL,
    fill_qty        INTEGER,
    fill_latency_ms INTEGER,
    status          TEXT NOT NULL DEFAULT 'PENDING'
                        CHECK (status IN (
                            'PENDING','FILLED','PARTIAL_FILL',
                            'CANCELLED','FAILED'
                        )),
    ordered_at      TEXT NOT NULL,
    filled_at       TEXT,
    error_code      TEXT,
    error_msg       TEXT
)
"""


def _add_orphan_order(path, trade_id: int) -> None:
    """구 DB에 주문 1건을 심는다. FK 강제 전이라 고아여도 들어간다."""
    conn = sqlite3.connect(path)
    conn.executescript(_LEGACY_ORDERS)
    conn.execute(
        """INSERT INTO orders
               (trade_id, kis_order_id, order_type, order_phase, ticker,
                order_qty, status, ordered_at)
           VALUES (?, '0000111222', 'BUY', 'FIRST_BUY', '005930',
                   10, 'FILLED', '2026-08-14T09:01:00+09:00')""",
        (trade_id,),
    )
    conn.commit()
    conn.close()


def _trades_schema_sql(path) -> str:
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='trades'"
        ).fetchone()
    finally:
        conn.close()
    return (row[0] if row else "") or ""


async def test_preexisting_fk_violation_does_not_abort_startup(tmp_path):
    """재작성이 만든 게 아닌 위반으로는 기동을 막지 않는다(1회차·2회차 모두)."""
    path = tmp_path / "trading.db"
    _make_legacy_db(path)
    _add_orphan_order(path, trade_id=4242)   # trades에 없는 id

    await db.init(str(path))                 # 1회차 — 재작성이 실제로 일어난다
    try:
        conn = db.get()
        async with conn.execute("SELECT track FROM trades WHERE id=7") as cur:
            assert (await cur.fetchone())["track"] == "A"
        async with conn.execute("PRAGMA foreign_key_check") as cur:
            still_violating = await cur.fetchall()
    finally:
        await db.close()

    # 위반은 그대로 남아 있다(마이그레이션이 고칠 수 있는 대상이 아니다).
    # 그래도 기동은 성공했다.
    assert len(still_violating) == 1
    assert "track" in _trades_schema_sql(path)

    await db.init(str(path))                 # 2회차 — 조기 반환 경로
    await db.close()

    assert len(list(tmp_path.glob("trading.db.pre_track_*"))) == 1


async def test_introduced_fk_violation_rolls_back_and_aborts(monkeypatch, tmp_path):
    """재작성이 새 위반을 만들면 COMMIT 전에 롤백하고 기동을 중단한다."""
    path = tmp_path / "trading.db"
    _make_legacy_db(path)
    _add_orphan_order(path, trade_id=7)       # 정상 참조 — 사전 위반 없음

    sabotage = db._TRADES_REWRITE_STEPS + (
        """INSERT INTO orders
               (trade_id, kis_order_id, order_type, order_phase, ticker,
                order_qty, status, ordered_at)
           VALUES (9999, '0000999999', 'SELL', 'CLOSE_SELL', '005930',
                   10, 'FILLED', '2026-08-14T09:05:00+09:00')""",
    )
    monkeypatch.setattr(db, "_TRADES_REWRITE_STEPS", sabotage)

    with pytest.raises(RuntimeError, match="새로 만들어 롤백했다"):
        await db.init(str(path))
    try:
        conn = db.get()
        # finally 절이 FK 강제를 되돌려 놓았다.
        async with conn.execute("PRAGMA foreign_keys") as cur:
            assert (await cur.fetchone())[0] == 1
    finally:
        await db.close()

    # 롤백됐으므로 구 스키마 그대로다.
    assert "track" not in _trades_schema_sql(path)


async def test_rewrite_failure_still_restores_foreign_key_enforcement(
    monkeypatch, tmp_path
):
    """재작성 중 임의의 예외가 나도 커넥션이 FK 강제 OFF로 남지 않는다."""
    path = tmp_path / "trading.db"
    _make_legacy_db(path)

    broken = db._TRADES_REWRITE_STEPS[:1] + ("SELECT * FROM no_such_table",)
    monkeypatch.setattr(db, "_TRADES_REWRITE_STEPS", broken)

    with pytest.raises(sqlite3.OperationalError):
        await db.init(str(path))
    try:
        conn = db.get()
        async with conn.execute("PRAGMA foreign_keys") as cur:
            assert (await cur.fetchone())[0] == 1
        assert conn.in_transaction is False
    finally:
        await db.close()

    assert "track" not in _trades_schema_sql(path)
