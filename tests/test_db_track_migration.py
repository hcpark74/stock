"""구 DB → 트랙 재작성 마이그레이션. 컬럼 정렬·FK 보존·백업."""
import sqlite3

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
