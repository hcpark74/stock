"""SQLite 연결 관리 — DB_DESIGN.md §4 PRAGMA 설정"""

import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import aiosqlite

from src.utils.logger import log

KST = ZoneInfo("Asia/Seoul")
_conn: aiosqlite.Connection | None = None


async def init(db_path: str) -> None:
    """DB 연결 열기 + PRAGMA 설정 + 테이블 생성."""
    global _conn
    _conn = await aiosqlite.connect(db_path)
    _conn.row_factory = aiosqlite.Row
    await _conn.executescript("""
        PRAGMA journal_mode = WAL;
        PRAGMA synchronous   = NORMAL;
        PRAGMA foreign_keys  = ON;
        PRAGMA cache_size    = -8000;
    """)
    await _conn.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            date         TEXT NOT NULL,
            track        TEXT NOT NULL DEFAULT 'A',
            ticker       TEXT NOT NULL,
            name            TEXT,
            entry_price  REAL,
            entry_qty    INTEGER,
            entry_at     TEXT,
            exit_price   REAL,
            exit_qty     INTEGER,
            exit_at      TEXT,
            close_reason TEXT CHECK (close_reason IN (
                             'TRAILING','HARD_STOP',
                             'TIMEOUT','SLIPPAGE_GUARD','ENTRY_FAIL','MANUAL',
                             'SIGNAL_EXIT','INDICATOR_STOP','TRACK_HALTED'
                         )),
            pnl_pct      REAL,
            pnl_amount   REAL,
            high_price   REAL,
            highest_step REAL,
            pyramided    INTEGER DEFAULT 0,
            status       TEXT NOT NULL DEFAULT 'OPEN'
                             CHECK (status IN ('OPEN','CLOSED','SKIPPED')),
            execution_mode TEXT,
            strategy_fingerprint TEXT,
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL,
            UNIQUE (date, track)
        );

        CREATE INDEX IF NOT EXISTS idx_trades_date ON trades(date);

        CREATE TABLE IF NOT EXISTS orders (
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
            error_msg       TEXT,
            client_order_id TEXT,
            submission_state TEXT NOT NULL DEFAULT 'ACKNOWLEDGED',
            submitted_at    TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_orders_trade_id     ON orders(trade_id);
        CREATE INDEX IF NOT EXISTS idx_orders_kis_order_id ON orders(kis_order_id);

        CREATE TABLE IF NOT EXISTS entry_order_attempts (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            date            TEXT NOT NULL,
            kis_order_id    TEXT NOT NULL,
            org_no          TEXT,
            ticker          TEXT NOT NULL,
            name            TEXT,
            entry_attempt   INTEGER NOT NULL,
            max_attempts    INTEGER NOT NULL,
            order_qty       INTEGER NOT NULL,
            order_price     REAL NOT NULL,
            trigger_price   REAL,
            execution_mode  TEXT NOT NULL,
            order_phase     TEXT NOT NULL DEFAULT 'FIRST_BUY'
                                CHECK (order_phase IN ('FIRST_BUY','PYRAMID_BUY')),
            status          TEXT NOT NULL DEFAULT 'PENDING'
                                CHECK (status IN (
                                    'PENDING','FILLED','PARTIAL_FILL',
                                    'CANCELLED','UNCERTAIN'
                                )),
            fill_price      REAL,
            fill_qty        INTEGER,
            fill_latency_ms INTEGER,
            ordered_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL,
            UNIQUE (date, kis_order_id)
        );

        CREATE INDEX IF NOT EXISTS idx_entry_attempts_date
            ON entry_order_attempts(date);
        CREATE INDEX IF NOT EXISTS idx_entry_attempts_kis_order_id
            ON entry_order_attempts(kis_order_id);

        CREATE TABLE IF NOT EXISTS partial_exits (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id      INTEGER NOT NULL REFERENCES trades(id),
            order_id      INTEGER REFERENCES orders(id),
            exit_price    REAL NOT NULL,
            exit_qty      INTEGER NOT NULL,
            remaining_qty INTEGER NOT NULL,
            pnl_pct       REAL,
            exited_at     TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_partial_exits_trade_id ON partial_exits(trade_id);

        CREATE TABLE IF NOT EXISTS daily_skips (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            date       TEXT NOT NULL,
            track      TEXT NOT NULL DEFAULT 'A',
            reason     TEXT NOT NULL CHECK (reason IN (
                           'NO_TARGET','GAP_CHANGED','ENTRY_FAIL',
                           'SLIPPAGE_GUARD','MANUAL','MARKET_CLOSED',
                           'VI_ACTIVE'
                       )),
            detail     TEXT,
            created_at TEXT NOT NULL,
            UNIQUE (date, track)
        );

        CREATE TABLE IF NOT EXISTS asset_snapshots (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            captured_at         TEXT NOT NULL,
            total_asset         REAL,
            cash                REAL,
            buyable_cash        REAL,
            buyable_cash_source TEXT,
            stock_value         REAL,
            pnl_amount          REAL,
            holdings_count      INTEGER,
            source              TEXT NOT NULL DEFAULT 'KIS',
            raw_json            TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_asset_snapshots_captured_at
            ON asset_snapshots(captured_at);

        CREATE TABLE IF NOT EXISTS trailing_shadow_comparisons (
            trade_id                    INTEGER PRIMARY KEY
                                                REFERENCES trades(id) ON DELETE CASCADE,
            baseline_step_trail         REAL NOT NULL,
            recommended_step_trail      REAL NOT NULL,
            entry_price                 REAL NOT NULL,
            exit_qty                    INTEGER,
            baseline_highest_step       REAL,
            recommended_highest_step    REAL,
            baseline_stop_price         REAL,
            recommended_stop_price      REAL,
            baseline_exit_price         REAL,
            recommended_exit_price      REAL,
            actual_exit_price           REAL,
            baseline_exit_at            TEXT,
            recommended_exit_at         TEXT,
            close_reason                TEXT,
            baseline_pnl_pct            REAL,
            recommended_pnl_pct         REAL,
            actual_pnl_pct              REAL,
            pnl_delta_pct               REAL,
            baseline_pnl_amount         REAL,
            recommended_pnl_amount      REAL,
            pnl_delta_amount            REAL,
            finalized                   INTEGER NOT NULL DEFAULT 0,
            created_at                  TEXT NOT NULL,
            updated_at                  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS strategy_configs (
            config_id        TEXT PRIMARY KEY,
            config_json      TEXT NOT NULL,
            config_sha256    TEXT NOT NULL UNIQUE,
            kind             TEXT NOT NULL CHECK (kind IN (
                                 'PRIMARY','EXPLORATORY','ACTIVE','RETIRED'
                             )),
            code_fingerprint TEXT,
            parent_config_id TEXT,
            created_at       TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS experiment_registry (
            experiment_id        TEXT PRIMARY KEY,
            hypothesis           TEXT,
            baseline_config_id   TEXT,
            candidate_config_id  TEXT,
            tested_combos_count  INTEGER,
            learn_start_date     TEXT,
            validate_start_date  TEXT,
            stop_conditions      TEXT,
            primary_metric       TEXT,
            safety_limits        TEXT,
            status               TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN (
                                     'ACTIVE','PAUSED_DATA','PAUSED_RISK',
                                     'ROLLED_BACK','RETIRED'
                                 )),
            strategy_fingerprint TEXT,
            notes                TEXT,
            created_at           TEXT NOT NULL,
            updated_at           TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS price_path_manifests (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date               TEXT NOT NULL,
            ticker                   TEXT NOT NULL,
            trade_id                 INTEGER,
            experiment_id            TEXT NOT NULL DEFAULT '',
            chunks_json              TEXT,
            first_source_ts          TEXT,
            last_source_ts           TEXT,
            first_received_at        TEXT,
            last_received_at         TEXT,
            seq_gaps                 INTEGER,
            source_ts_reversals      INTEGER,
            ws_disconnects           INTEGER,
            rest_backfill_ranges_json TEXT,
            reached_expected_close   INTEGER,
            data_complete            INTEGER,
            missing_reason           TEXT,
            writer_version           TEXT,
            schema_version           TEXT,
            content_hash             TEXT,
            correction_history_json  TEXT,
            created_at               TEXT NOT NULL,
            finalized_at             TEXT,
            UNIQUE (trade_date, ticker, experiment_id)
        );
    """)
    # 기존 DB 마이그레이션: highest_step 컬럼 추가
    try:
        await _conn.execute("ALTER TABLE trades ADD COLUMN highest_step REAL")
    except Exception:
        pass  # 이미 존재하면 무시
    try:
        await _conn.execute("ALTER TABLE trades ADD COLUMN name TEXT")
    except Exception:
        pass  # already exists
    try:
        await _conn.execute("ALTER TABLE orders ADD COLUMN name TEXT")
    except Exception:
        pass  # 이미 존재하면 무시
    try:
        await _conn.execute("ALTER TABLE orders ADD COLUMN trigger_price REAL")
    except Exception:
        pass  # already exists
    for table, column, definition in (
        ("trades", "execution_mode", "TEXT"),
        ("trades", "strategy_fingerprint", "TEXT"),
        ("trades", "experiment_id", "TEXT"),
        ("orders", "client_order_id", "TEXT"),
        (
            "orders",
            "submission_state",
            "TEXT NOT NULL DEFAULT 'ACKNOWLEDGED'",
        ),
        ("orders", "submitted_at", "TEXT"),
        (
            "entry_order_attempts",
            "order_phase",
            "TEXT NOT NULL DEFAULT 'FIRST_BUY'",
        ),
    ):
        try:
            await _conn.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
            )
        except Exception:
            pass
    await _conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_client_order_id
               ON orders(client_order_id) WHERE client_order_id IS NOT NULL"""
    )
    # Older rows used order_price as the decision-time reference even for
    # market orders. Preserve that history while new rows separate the actual
    # submitted price from the trigger/reference price.
    await _conn.execute(
        """UPDATE orders
              SET trigger_price=order_price
            WHERE trigger_price IS NULL
              AND order_price IS NOT NULL"""
    )
    # 기존 DB 마이그레이션: daily_skips.reason CHECK 확장 — SQLite는 CHECK 변경이
    # 불가하므로 재구축. record_skip이 INSERT OR IGNORE라 구 제약에 걸리면
    # 에러 없이 기록만 누락되기 때문에 반드시 맞춰야 한다.
    async with _conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='daily_skips'"
    ) as cur:
        row = await cur.fetchone()
    sql = (row["sql"] or "") if row else ""
    if row and ("VI_ACTIVE" not in sql or "track" not in sql):
        await _conn.executescript("""
            BEGIN;
            CREATE TABLE daily_skips_migrated (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                date       TEXT NOT NULL,
                track      TEXT NOT NULL DEFAULT 'A',
                reason     TEXT NOT NULL CHECK (reason IN (
                               'NO_TARGET','GAP_CHANGED','ENTRY_FAIL',
                               'SLIPPAGE_GUARD','MANUAL','MARKET_CLOSED',
                               'VI_ACTIVE'
                           )),
                detail     TEXT,
                created_at TEXT NOT NULL,
                UNIQUE (date, track)
            );
            INSERT INTO daily_skips_migrated
                (id, date, track, reason, detail, created_at)
                SELECT id, date, 'A', reason, detail, created_at FROM daily_skips;
            DROP TABLE daily_skips;
            ALTER TABLE daily_skips_migrated RENAME TO daily_skips;
            COMMIT;
        """)

    await _migrate_trades_track(db_path)

    # Backfill legacy CLOSED rows that predate complete close summaries.
    # Filled sell orders are the durable source of truth; cancelled orders can
    # still contain a valid partial fill, so the predicate is fill_qty > 0
    # rather than status='FILLED'. Existing non-NULL summaries are preserved.
    await _conn.execute("""
        UPDATE trades
           SET exit_qty = (
               SELECT SUM(o.fill_qty)
                 FROM orders AS o
                WHERE o.trade_id = trades.id
                  AND o.order_type = 'SELL'
                  AND o.fill_qty > 0
           )
         WHERE status = 'CLOSED'
           AND exit_qty IS NULL
           AND EXISTS (
               SELECT 1
                 FROM orders AS o
                WHERE o.trade_id = trades.id
                  AND o.order_type = 'SELL'
                  AND o.fill_qty > 0
           )
    """)
    # pyramided rows are excluded: trades.entry_price holds only the first
    # fill price, so the flat formula would misstate pnl by the second-buy
    # price difference — and the IS NULL idempotency guard would then freeze
    # that wrong value forever. Left NULL until a weighted-average backfill
    # from BUY fills exists.
    await _conn.execute("""
        UPDATE trades
           SET pnl_amount = (exit_price - entry_price) * exit_qty
         WHERE status = 'CLOSED'
           AND pnl_amount IS NULL
           AND pyramided = 0
           AND exit_price IS NOT NULL
           AND entry_price IS NOT NULL
           AND exit_qty IS NOT NULL
    """)
    await _conn.commit()


def _backup_db_file(db_path: str) -> str | None:
    """재작성 전 DB 파일을 복사한다. WAL을 먼저 접어 최근 쓰기를 포함시킨다."""
    if db_path == ":memory:":
        return None
    src_path = Path(db_path)
    if not src_path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = src_path.with_name(f"{src_path.name}.pre_track_{stamp}")
    shutil.copy2(src_path, dst)
    return str(dst)


async def _migrate_trades_track(db_path: str) -> None:
    """UNIQUE(date) → UNIQUE(date, track). SQLite는 ALTER로 못 바꿔 재작성한다.

    구 DB는 name·highest_step이 ALTER로 맨 뒤에 붙어 있어 컬럼 순서가 신규
    스키마와 다르다. SELECT *로 옮기면 타입이 호환되는 자리끼리 조용히 뒤섞이므로
    컬럼명을 전부 명시한다(§4.3).
    """
    async with _conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='trades'"
    ) as cur:
        row = await cur.fetchone()
    if not row or "track" in (row["sql"] or ""):
        return
    # 앞선 마이그레이션 블록(예: orders.trigger_price 백필)이 열어 둔 쓰기
    # 트랜잭션이 남아 있으면 이후 DDL이 "database table is locked"로 실패한다.
    # 체크포인트 전에 먼저 커밋해 비운다.
    await _conn.commit()
    # 커서를 끝까지 소비하지 않고 남겨 두면 그 자체가 미종결 statement로 남아
    # 뒤이은 CREATE/DROP TABLE을 "database table is locked"로 막는다(SQLite는
    # 같은 커넥션에 열린 statement가 있으면 스키마 변경을 거부한다). async with로
    # 확실히 finalize한다.
    async with _conn.execute("PRAGMA wal_checkpoint(TRUNCATE)"):
        pass
    backup = _backup_db_file(db_path)
    log("DB_TRACK_MIGRATION_START", level="WARN", backup=backup)
    await _conn.executescript("""
        PRAGMA foreign_keys = OFF;
        BEGIN;
        CREATE TABLE trades_track_migrated (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            date         TEXT NOT NULL,
            track        TEXT NOT NULL DEFAULT 'A',
            ticker       TEXT NOT NULL,
            name         TEXT,
            entry_price  REAL,
            entry_qty    INTEGER,
            entry_at     TEXT,
            exit_price   REAL,
            exit_qty     INTEGER,
            exit_at      TEXT,
            close_reason TEXT CHECK (close_reason IN (
                             'TRAILING','HARD_STOP',
                             'TIMEOUT','SLIPPAGE_GUARD','ENTRY_FAIL','MANUAL',
                             'SIGNAL_EXIT','INDICATOR_STOP','TRACK_HALTED'
                         )),
            pnl_pct      REAL,
            pnl_amount   REAL,
            high_price   REAL,
            highest_step REAL,
            pyramided    INTEGER DEFAULT 0,
            status       TEXT NOT NULL DEFAULT 'OPEN'
                             CHECK (status IN ('OPEN','CLOSED','SKIPPED')),
            execution_mode       TEXT,
            strategy_fingerprint TEXT,
            experiment_id        TEXT,
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL,
            UNIQUE (date, track)
        );
        INSERT INTO trades_track_migrated
            (id, date, track, ticker, name, entry_price, entry_qty, entry_at,
             exit_price, exit_qty, exit_at, close_reason, pnl_pct, pnl_amount,
             high_price, highest_step, pyramided, status, execution_mode,
             strategy_fingerprint, experiment_id, created_at, updated_at)
        SELECT
             id, date, 'A', ticker, name, entry_price, entry_qty, entry_at,
             exit_price, exit_qty, exit_at, close_reason, pnl_pct, pnl_amount,
             high_price, highest_step, pyramided, status, execution_mode,
             strategy_fingerprint, experiment_id, created_at, updated_at
          FROM trades;
        DROP TABLE trades;
        ALTER TABLE trades_track_migrated RENAME TO trades;
        CREATE INDEX IF NOT EXISTS idx_trades_date ON trades(date);
        COMMIT;
        PRAGMA foreign_keys = ON;
    """)
    async with _conn.execute("PRAGMA foreign_key_check") as cur:
        violations = await cur.fetchall()
    if violations:
        raise RuntimeError(
            f"trades 트랙 마이그레이션 후 FK 위반 {len(violations)}건. "
            f"백업({backup})으로 복원하고 기동을 중단한다."
        )
    log("DB_TRACK_MIGRATION_DONE", level="WARN", backup=backup)


def get() -> aiosqlite.Connection:
    """현재 연결 반환. init() 전에 호출하면 RuntimeError."""
    if _conn is None:
        raise RuntimeError("DB not initialised — call db.init() first")
    return _conn


async def close() -> None:
    """연결 닫기. 프로세스 종료 전 호출."""
    global _conn
    if _conn is not None:
        await _conn.close()
        _conn = None


# ── CRUD ──────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(KST).isoformat()


def _num(value):
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _asset_holdings_from_raw(raw_json: str | None) -> list[dict]:
    if not raw_json:
        return []
    try:
        raw = json.loads(raw_json)
    except Exception:
        return []
    output1 = raw.get("output1") or []
    if not isinstance(output1, list):
        return []
    holdings: list[dict] = []
    for item in output1:
        if not isinstance(item, dict):
            continue
        qty = int(_num(item.get("hldg_qty")) or 0)
        if qty <= 0:
            continue
        holdings.append({
            "ticker": str(item.get("pdno") or "").strip(),
            "name": str(item.get("prdt_name") or "").strip(),
            "qty": qty,
            "orderable_qty": int(_num(item.get("ord_psbl_qty")) or 0),
            "current_price": _num(item.get("prpr")),
            "avg_price": _num(item.get("pchs_avg_pric")),
            "purchase_amount": _num(item.get("pchs_amt")),
            "evaluation_amount": _num(item.get("evlu_amt")),
            "pnl_amount": _num(item.get("evlu_pfls_amt")),
            "pnl_pct": _num(item.get("evlu_pfls_rt")),
        })
    return holdings

async def open_trade(
    date: str, ticker: str, entry_price: float, entry_qty: int,
    name: str | None = None, track: str = "A",
) -> int:
    """Insert a trade and return its id. Existing same-day trades are reused."""
    import os

    from src.release import strategy_fingerprint

    now = _now()
    execution_mode = os.getenv("KIS_MODE", "PAPER")
    fingerprint = strategy_fingerprint()
    # 활성 기준선 실험 ID를 best-effort로 기록한다. 실험이 아직 등록되지 않았거나
    # 조회가 실패해도 거래 기록을 막지 않는다(주문 안전 우선).
    experiment_id: str | None = None
    try:
        from src.modules import baseline_experiment

        experiment_id = baseline_experiment.active_experiment_id()
    except Exception:
        experiment_id = None
    conn = get()
    try:
        async with conn.execute(
            """INSERT INTO trades
                   (date, track, ticker, name, entry_price, entry_qty, entry_at,
                    status, execution_mode, strategy_fingerprint, experiment_id,
                    created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?)""",
            (
                date,
                track,
                ticker,
                name,
                entry_price,
                entry_qty,
                now,
                execution_mode,
                fingerprint,
                experiment_id,
                now,
                now,
            ),
        ) as cur:
            trade_id = cur.lastrowid
        await conn.commit()
        return trade_id
    except sqlite3.IntegrityError:
        existing = await get_trade_by_date(date, track=track)
        if existing is None:
            raise
        if existing.get("ticker") != ticker:
            log(
                "TRADE_ALREADY_EXISTS",
                level="WARN",
                ticker=ticker,
                existing_ticker=existing.get("ticker"),
                trade_id=int(existing["id"]),
            )
        elif name and not existing.get("name"):
            await conn.execute(
                "UPDATE trades SET name=?, updated_at=? WHERE id=?",
                (name, now, int(existing["id"])),
            )
            await conn.commit()
        return int(existing["id"])


async def get_trade_by_date(date: str, track: str = "A") -> dict | None:
    conn = get()
    async with conn.execute(
        """SELECT t.*,
                  COALESCE(
                      (SELECT SUM(o.fill_qty)
                         FROM orders o
                        WHERE o.trade_id=t.id
                          AND o.order_type='BUY'
                          AND o.fill_qty > 0),
                      t.entry_qty,
                      0
                  ) AS confirmed_entry_qty
             FROM trades t
            WHERE t.date=? AND t.track=?""",
        (date, track),
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def get_order_by_kis_id(
    kis_order_id: str,
    *,
    date: str | None = None,
    ticker: str | None = None,
) -> dict | None:
    """재시작 복구 시 같은 KIS 주문을 DB에 중복 기록하지 않도록 조회한다."""
    conn = get()
    filters = ["o.kis_order_id=?"]
    params: list[object] = [kis_order_id]
    if date is not None:
        filters.append("t.date=?")
        params.append(date)
    if ticker is not None:
        filters.append("o.ticker=?")
        params.append(ticker)
    async with conn.execute(
        f"""SELECT o.*
              FROM orders AS o
              JOIN trades AS t ON t.id=o.trade_id
             WHERE {' AND '.join(filters)}
             ORDER BY o.id DESC
             LIMIT 1""",
        tuple(params),
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def record_order(
    trade_id: int,
    kis_order_id: str,
    side: str,
    qty: int,
    price: float,
    phase: str,
    ticker: str,
    name: str | None = None,
    *,
    trigger_price: float | None = None,
    client_order_id: str | None = None,
    submission_state: str = "ACKNOWLEDGED",
) -> int:
    """Insert a pending order and return its local id.

    ``price`` is the price submitted to KIS (zero for market orders), while
    ``trigger_price`` is the decision-time quote used for slippage analysis.
    """
    now = _now()
    conn = get()
    async with conn.execute(
        """INSERT INTO orders
               (trade_id, kis_order_id, order_type, order_phase,
                ticker, name, order_qty, order_price, trigger_price,
                status, ordered_at, client_order_id, submission_state)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?)""",
        (
            trade_id,
            kis_order_id,
            side,
            phase,
            ticker,
            name,
            qty,
            price,
            trigger_price,
            now,
            client_order_id,
            submission_state,
        ),
    ) as cur:
        order_db_id = cur.lastrowid
    await conn.commit()
    return order_db_id


async def record_entry_order_attempt(
    date: str,
    kis_order_id: str,
    ticker: str,
    qty: int,
    price: float,
    trigger_price: float,
    attempt: int,
    max_attempts: int,
    mode: str,
    *,
    org_no: str | None = None,
    name: str | None = None,
    order_phase: str = "FIRST_BUY",
    status: str = "PENDING",
    fill_price: float | None = None,
    fill_qty: int | None = None,
    fill_latency_ms: int | None = None,
) -> int:
    """Upsert an acknowledged entry attempt by its KIS natural key.

    Entry attempts cannot safely create a ``trades`` row before a fill because
    the daily unique trade would block candidate retries. This independent audit
    row preserves accepted-then-cancelled orders without changing position truth.
    A late asynchronous ``PENDING`` write never downgrades a reconciled row.
    """
    now = _now()
    conn = get()
    async with conn.execute(
        """INSERT INTO entry_order_attempts
               (date, kis_order_id, org_no, ticker, name,
                entry_attempt, max_attempts, order_qty, order_price,
                trigger_price, execution_mode, order_phase, status, fill_price, fill_qty,
                fill_latency_ms, ordered_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(date, kis_order_id) DO UPDATE SET
               org_no=excluded.org_no,
               ticker=excluded.ticker,
               name=COALESCE(excluded.name, entry_order_attempts.name),
               entry_attempt=excluded.entry_attempt,
               max_attempts=excluded.max_attempts,
               order_qty=excluded.order_qty,
               order_price=excluded.order_price,
               trigger_price=excluded.trigger_price,
               execution_mode=excluded.execution_mode,
               order_phase=excluded.order_phase,
               status=CASE
                   WHEN excluded.status = 'PENDING'
                    AND entry_order_attempts.status <> 'PENDING'
                   THEN entry_order_attempts.status
                   ELSE excluded.status
               END,
               fill_price=COALESCE(excluded.fill_price, entry_order_attempts.fill_price),
               fill_qty=COALESCE(excluded.fill_qty, entry_order_attempts.fill_qty),
               fill_latency_ms=COALESCE(
                   excluded.fill_latency_ms,
                   entry_order_attempts.fill_latency_ms
               ),
               updated_at=CASE
                   WHEN excluded.status = 'PENDING'
                    AND entry_order_attempts.status <> 'PENDING'
                   THEN entry_order_attempts.updated_at
                   ELSE excluded.updated_at
               END
           RETURNING id""",
        (
            date,
            kis_order_id,
            org_no,
            ticker,
            name,
            attempt,
            max_attempts,
            qty,
            price,
            trigger_price,
            mode,
            order_phase,
            status,
            fill_price,
            fill_qty,
            fill_latency_ms,
            now,
            now,
        ),
    ) as cur:
        row = await cur.fetchone()
    await conn.commit()
    return int(row["id"])


async def update_order_submission(
    order_db_id: int,
    submission_state: str,
    *,
    kis_order_id: str | None = None,
    error_code: str | None = None,
    error_msg: str | None = None,
    submitted: bool = False,
) -> None:
    """매도 주문의 전송 경계를 내구적으로 기록한다."""
    conn = get()
    assignments = [
        "submission_state=?",
        "error_code=?",
        "error_msg=?",
    ]
    params: list[object] = [submission_state, error_code, error_msg]
    if kis_order_id is not None:
        assignments.append("kis_order_id=?")
        params.append(kis_order_id)
    if submitted:
        assignments.append("submitted_at=?")
        params.append(_now())
    params.append(order_db_id)
    await conn.execute(
        f"UPDATE orders SET {', '.join(assignments)} WHERE id=?",
        tuple(params),
    )
    await conn.commit()


async def get_order_by_client_id(client_order_id: str) -> dict | None:
    conn = get()
    async with conn.execute(
        "SELECT * FROM orders WHERE client_order_id=? LIMIT 1",
        (client_order_id,),
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def get_known_sell_order_ids(trade_id: int) -> set[str]:
    """같은 거래에서 이미 식별된 매도 주문번호를 반환한다.

    응답 유실 대사 시 이전 재시도의 취소·체결 주문을 새 주문으로 오인하지
    않도록, 현재 전송 전에 알고 있던 주문번호를 제외하는 데 사용한다.
    """
    if trade_id <= 0:
        return set()
    conn = get()
    async with conn.execute(
        """SELECT kis_order_id
             FROM orders
            WHERE trade_id=?
              AND order_type='SELL'
              AND kis_order_id IS NOT NULL
              AND kis_order_id <> ''""",
        (trade_id,),
    ) as cur:
        rows = await cur.fetchall()
    return {str(row["kis_order_id"]) for row in rows}


async def get_unresolved_exit_intent(date: str, track: str = "A") -> dict | None:
    """상태 파일 갱신 직전 장애를 DB 주문 의도에서 복구한다.

    트랙 스코프가 없으면 A의 청산 재시도 창(EXITING + pending_exit=None)에서
    재시작이 겹칠 때 A가 B의 매도 주문을 인수한다(§4.4).
    """
    conn = get()
    async with conn.execute(
        """SELECT o.*
               FROM orders o
               JOIN trades t ON t.id=o.trade_id
              WHERE t.date=?
                AND t.track=?
                AND o.order_type='SELL'
                AND o.client_order_id IS NOT NULL
                AND o.status IN ('PENDING','PARTIAL_FILL')
                AND o.submission_state IN (
                    'PREPARED','SUBMITTING','UNKNOWN','ACKNOWLEDGED'
                )
              ORDER BY o.id DESC
              LIMIT 1""",
        (date, track),
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def sell_fill_summary(trade_id: int) -> dict:
    """확인된 모든 매도 체결의 수량·대금 합계."""
    conn = get()
    async with conn.execute(
        """SELECT COALESCE(SUM(fill_qty), 0) AS fill_qty,
                  COALESCE(SUM(fill_qty * fill_price), 0) AS fill_amt
             FROM orders
            WHERE trade_id=? AND order_type='SELL' AND fill_qty > 0""",
        (trade_id,),
    ) as cur:
        row = await cur.fetchone()
    return {
        "fill_qty": int(row["fill_qty"] or 0),
        "fill_amt": float(row["fill_amt"] or 0),
    }


async def update_order_fill(
    order_db_id: int,
    fill_price: float,
    fill_qty: int,
    fill_latency_ms: int | None,
    status: str = "FILLED",
) -> None:
    """orders 체결 정보 갱신 (기본 FILLED, 부분체결은 PARTIAL_FILL)."""
    now = _now()
    conn = get()
    await conn.execute(
        """UPDATE orders
           SET fill_price=?, fill_qty=?, fill_latency_ms=?,
               status=?, filled_at=?
           WHERE id=?""",
        (fill_price, fill_qty, fill_latency_ms, status, now, order_db_id),
    )
    await conn.commit()


async def update_order_status(order_db_id: int, status: str) -> None:
    """orders 상태만 갱신 (예: 취소 확정 → CANCELLED). 체결 정보는 보존한다."""
    conn = get()
    await conn.execute(
        "UPDATE orders SET status=? WHERE id=?",
        (status, order_db_id),
    )
    await conn.commit()


async def mark_pyramided(trade_id: int) -> None:
    """Mark a trade as having a filled pyramid buy."""
    now = _now()
    conn = get()
    await conn.execute(
        """UPDATE trades
           SET pyramided=1, updated_at=?
           WHERE id=?""",
        (now, trade_id),
    )
    await conn.commit()



async def update_trade_progress(
    trade_id: int,
    high_price: float | None,
    highest_step: float,
) -> None:
    """Update in-trade high/trailing progress for restart recovery."""
    now = _now()
    conn = get()
    await conn.execute(
        """UPDATE trades
           SET high_price=?, highest_step=?, updated_at=?
           WHERE id=? AND status='OPEN'""",
        (high_price, highest_step, now, trade_id),
    )
    await conn.commit()


async def record_trailing_shadow_baseline(
    trade_id: int,
    *,
    baseline_step_trail: float,
    recommended_step_trail: float,
    entry_price: float,
    highest_step: float,
    baseline_stop_price: float,
    recommended_stop_price: float,
    baseline_exit_price: float,
) -> bool:
    """Persist the first tick where the legacy trailing rule would exit.

    Returns ``True`` only for the first observation. Repeated ticks and process
    restarts are idempotent because ``trade_id`` is the primary key.
    """
    now = _now()
    conn = get()
    cursor = await conn.execute(
        """INSERT OR IGNORE INTO trailing_shadow_comparisons (
               trade_id, baseline_step_trail, recommended_step_trail,
               entry_price, baseline_highest_step, baseline_stop_price,
               recommended_stop_price, baseline_exit_price, baseline_exit_at,
               finalized, created_at, updated_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
        (
            trade_id,
            baseline_step_trail,
            recommended_step_trail,
            entry_price,
            highest_step,
            baseline_stop_price,
            recommended_stop_price,
            baseline_exit_price,
            now,
            now,
            now,
        ),
    )
    await conn.commit()
    return cursor.rowcount == 1


async def finalize_trailing_shadow_comparison(
    trade_id: int,
    *,
    baseline_step_trail: float,
    recommended_step_trail: float,
    entry_price: float,
    exit_qty: int,
    highest_step: float,
    baseline_stop_price: float | None,
    recommended_stop_price: float | None,
    recommended_exit_price: float,
    actual_exit_price: float,
    actual_pnl_pct: float,
    close_reason: str,
) -> dict:
    """Finalize a legacy-vs-recommended trailing comparison for one trade.

    Exit prices are decision-tick prices, so the strategy delta is not polluted
    by fill latency. ``actual_exit_price`` and ``actual_pnl_pct`` retain the
    confirmed execution result alongside that counterfactual comparison.
    """
    if exit_qty <= 0:
        raise ValueError("exit_qty must be positive when finalizing trailing shadow")
    if entry_price <= 0 or recommended_exit_price <= 0 or actual_exit_price <= 0:
        raise ValueError("shadow comparison prices must be positive")

    now = _now()
    conn = get()
    async with conn.execute(
        "SELECT * FROM trailing_shadow_comparisons WHERE trade_id=?",
        (trade_id,),
    ) as cur:
        existing_row = await cur.fetchone()
    existing = dict(existing_row) if existing_row else {}

    baseline_exit_price = float(
        existing.get("baseline_exit_price") or recommended_exit_price
    )
    baseline_exit_at = existing.get("baseline_exit_at") or now
    baseline_highest_step = float(
        existing.get("baseline_highest_step")
        if existing.get("baseline_highest_step") is not None
        else highest_step
    )
    stored_baseline_stop = existing.get("baseline_stop_price")
    if stored_baseline_stop is None:
        stored_baseline_stop = baseline_stop_price

    baseline_pnl_pct = round((baseline_exit_price / entry_price - 1) * 100, 4)
    recommended_pnl_pct = round(
        (recommended_exit_price / entry_price - 1) * 100,
        4,
    )
    pnl_delta_pct = round(recommended_pnl_pct - baseline_pnl_pct, 4)
    baseline_pnl_amount = round(
        (baseline_exit_price - entry_price) * exit_qty,
        2,
    )
    recommended_pnl_amount = round(
        (recommended_exit_price - entry_price) * exit_qty,
        2,
    )
    pnl_delta_amount = round(recommended_pnl_amount - baseline_pnl_amount, 2)

    await conn.execute(
        """INSERT INTO trailing_shadow_comparisons (
               trade_id, baseline_step_trail, recommended_step_trail,
               entry_price, exit_qty, baseline_highest_step,
               recommended_highest_step, baseline_stop_price,
               recommended_stop_price, baseline_exit_price,
               recommended_exit_price, actual_exit_price, baseline_exit_at,
               recommended_exit_at, close_reason, baseline_pnl_pct,
               recommended_pnl_pct, actual_pnl_pct, pnl_delta_pct,
               baseline_pnl_amount, recommended_pnl_amount, pnl_delta_amount,
               finalized, created_at, updated_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
           ON CONFLICT(trade_id) DO UPDATE SET
               baseline_step_trail=excluded.baseline_step_trail,
               recommended_step_trail=excluded.recommended_step_trail,
               entry_price=excluded.entry_price,
               exit_qty=excluded.exit_qty,
               baseline_highest_step=excluded.baseline_highest_step,
               recommended_highest_step=excluded.recommended_highest_step,
               baseline_stop_price=excluded.baseline_stop_price,
               recommended_stop_price=excluded.recommended_stop_price,
               baseline_exit_price=excluded.baseline_exit_price,
               recommended_exit_price=excluded.recommended_exit_price,
               actual_exit_price=excluded.actual_exit_price,
               baseline_exit_at=excluded.baseline_exit_at,
               recommended_exit_at=excluded.recommended_exit_at,
               close_reason=excluded.close_reason,
               baseline_pnl_pct=excluded.baseline_pnl_pct,
               recommended_pnl_pct=excluded.recommended_pnl_pct,
               actual_pnl_pct=excluded.actual_pnl_pct,
               pnl_delta_pct=excluded.pnl_delta_pct,
               baseline_pnl_amount=excluded.baseline_pnl_amount,
               recommended_pnl_amount=excluded.recommended_pnl_amount,
               pnl_delta_amount=excluded.pnl_delta_amount,
               finalized=1,
               updated_at=excluded.updated_at""",
        (
            trade_id,
            baseline_step_trail,
            recommended_step_trail,
            entry_price,
            exit_qty,
            baseline_highest_step,
            highest_step,
            stored_baseline_stop,
            recommended_stop_price,
            baseline_exit_price,
            recommended_exit_price,
            actual_exit_price,
            baseline_exit_at,
            now,
            close_reason,
            baseline_pnl_pct,
            recommended_pnl_pct,
            actual_pnl_pct,
            pnl_delta_pct,
            baseline_pnl_amount,
            recommended_pnl_amount,
            pnl_delta_amount,
            existing.get("created_at") or now,
            now,
        ),
    )
    await conn.commit()
    return await get_trailing_shadow_comparison(trade_id)


async def get_trailing_shadow_comparison(trade_id: int) -> dict:
    conn = get()
    async with conn.execute(
        "SELECT * FROM trailing_shadow_comparisons WHERE trade_id=?",
        (trade_id,),
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else {}

async def close_trade(
    trade_id: int,
    exit_price: float,
    close_reason: str,
    pnl_pct: float,
    highest_step: float,
    *,
    exit_qty: int,
    high_price: float | None,
) -> None:
    """Persist the complete, fee-exclusive trade close summary atomically.

    ``exit_qty`` must be the confirmed cumulative sell fill quantity. The
    caller supplies the latest in-memory ``high_price`` because the final tick
    can trigger a close before the throttled progress writer reaches SQLite.
    ``pnl_amount`` deliberately excludes fees and taxes, matching DB_DESIGN.
    """
    if exit_qty <= 0:
        raise ValueError("exit_qty must be positive when closing a trade")

    now = _now()
    conn = get()
    cursor = await conn.execute(
        """UPDATE trades
           SET exit_price=?, exit_qty=?, exit_at=?, close_reason=?,
               pnl_pct=?,
               pnl_amount=CASE
                   WHEN entry_price IS NULL THEN NULL
                   ELSE (? - entry_price) * ?
               END,
               high_price=CASE
                   WHEN ? IS NULL THEN high_price
                   WHEN high_price IS NULL OR ? > high_price THEN ?
                   ELSE high_price
               END,
               highest_step=?, status='CLOSED', updated_at=?
           WHERE id=? AND status='OPEN'""",
        (
            exit_price,
            exit_qty,
            now,
            close_reason,
            pnl_pct,
            exit_price,
            exit_qty,
            high_price,
            high_price,
            high_price,
            highest_step,
            now,
            trade_id,
        ),
    )
    if cursor.rowcount != 1:
        # No rollback: a zero-row UPDATE changed nothing, and rolling back the
        # shared connection could discard another coroutine's uncommitted write.
        raise RuntimeError(f"open trade not found while closing trade_id={trade_id}")
    await conn.commit()


def _canonical_config_hash(config: dict) -> str:
    """정규화 SHA-256 — release.py 지문 직렬화 규약과 동일."""
    import hashlib

    payload = json.dumps(
        config,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def upsert_strategy_config(
    config: dict,
    *,
    kind: str,
    code_fingerprint: str | None = None,
    parent_config_id: str | None = None,
) -> str:
    """정규화 해시로 config를 고정한다. 동일 해시는 기존 config_id 재사용.

    값이 하나라도 다르면 새 해시 → 새 config_id. 기존 결과를 덮어쓰지 않고
    추가만 하므로 상수·설정 변경은 항상 새 config로 전량 재생된다.
    """
    sha = _canonical_config_hash(config)
    conn = get()
    async with conn.execute(
        "SELECT config_id FROM strategy_configs WHERE config_sha256=?",
        (sha,),
    ) as cur:
        row = await cur.fetchone()
    if row is not None:
        return str(row["config_id"])

    config_id = f"cfg-{sha[:12]}"
    now = _now()
    config_json = json.dumps(config, ensure_ascii=False, sort_keys=True)
    await conn.execute(
        """INSERT OR IGNORE INTO strategy_configs
               (config_id, config_json, config_sha256, kind,
                code_fingerprint, parent_config_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (config_id, config_json, sha, kind, code_fingerprint, parent_config_id, now),
    )
    await conn.commit()
    return config_id


async def get_strategy_config(config_id: str) -> dict | None:
    conn = get()
    async with conn.execute(
        "SELECT * FROM strategy_configs WHERE config_id=?", (config_id,)
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def register_experiment(
    experiment_id: str,
    *,
    hypothesis: str,
    baseline_config_id: str,
    candidate_config_id: str,
    primary_metric: str,
    learn_start_date: str,
    stop_conditions: dict | None = None,
    safety_limits: dict | None = None,
    strategy_fingerprint: str | None = None,
    tested_combos_count: int | None = None,
    validate_start_date: str | None = None,
    status: str = "ACTIVE",
    notes: str | None = None,
) -> dict | None:
    """실험 정의를 사전 등록한다. 이미 있으면 시작값을 보존한다(INSERT OR IGNORE).

    사후에 유리한 기간·지표를 고르는 일을 막기 위해 등록 후에는 시작 설정을
    덮어쓰지 않는다.
    """
    now = _now()
    conn = get()
    await conn.execute(
        """INSERT OR IGNORE INTO experiment_registry
               (experiment_id, hypothesis, baseline_config_id, candidate_config_id,
                tested_combos_count, learn_start_date, validate_start_date,
                stop_conditions, primary_metric, safety_limits, status,
                strategy_fingerprint, notes, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            experiment_id,
            hypothesis,
            baseline_config_id,
            candidate_config_id,
            tested_combos_count,
            learn_start_date,
            validate_start_date,
            json.dumps(stop_conditions or {}, ensure_ascii=False),
            primary_metric,
            json.dumps(safety_limits or {}, ensure_ascii=False),
            status,
            strategy_fingerprint,
            notes,
            now,
            now,
        ),
    )
    await conn.commit()
    return await get_experiment(experiment_id)


async def get_experiment(experiment_id: str) -> dict | None:
    conn = get()
    async with conn.execute(
        "SELECT * FROM experiment_registry WHERE experiment_id=?", (experiment_id,)
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def get_active_experiment() -> dict | None:
    conn = get()
    async with conn.execute(
        """SELECT * FROM experiment_registry
            WHERE status='ACTIVE'
            ORDER BY created_at DESC, rowid DESC
            LIMIT 1"""
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def upsert_price_path_manifest(
    *,
    trade_date: str,
    ticker: str,
    experiment_id: str | None,
    trade_id: int | None = None,
    chunks_json: str = "[]",
    first_source_ts: str | None = None,
    last_source_ts: str | None = None,
    first_received_at: str | None = None,
    last_received_at: str | None = None,
    seq_gaps: int | None = None,
    source_ts_reversals: int | None = None,
    ws_disconnects: int | None = None,
    rest_backfill_ranges_json: str = "[]",
    reached_expected_close: int | None = None,
    data_complete: int | None = None,
    missing_reason: str | None = None,
    writer_version: str | None = None,
    schema_version: str | None = None,
    content_hash: str | None = None,
    finalized_at: str | None = None,
) -> dict:
    """가격 경로 manifest를 (거래일,종목,실험) 자연키로 UPSERT한다.

    ``experiment_id``가 없으면 빈 문자열 sentinel로 정규화한다 — SQLite UNIQUE는
    NULL을 서로 다르게 취급해 UPSERT가 실패하기 때문이다. content_hash가 기존과
    다르면 조용히 덮어쓰지 않고 correction_history에 이전 해시·시각을 남긴 뒤 갱신한다.
    """
    experiment_id = experiment_id or ""
    now = _now()
    conn = get()
    existing = await get_price_path_manifest(trade_date, ticker, experiment_id)

    correction_history: list[dict] = []
    created_at = now
    if existing is not None:
        created_at = existing.get("created_at") or now
        try:
            correction_history = json.loads(
                existing.get("correction_history_json") or "[]"
            )
        except (TypeError, json.JSONDecodeError):
            correction_history = []
        prev_hash = existing.get("content_hash")
        if content_hash is not None and prev_hash is not None and prev_hash != content_hash:
            correction_history.append(
                {"prev_content_hash": prev_hash, "at": now}
            )

    await conn.execute(
        """INSERT INTO price_path_manifests
               (trade_date, ticker, trade_id, experiment_id, chunks_json,
                first_source_ts, last_source_ts, first_received_at, last_received_at,
                seq_gaps, source_ts_reversals, ws_disconnects, rest_backfill_ranges_json,
                reached_expected_close, data_complete, missing_reason,
                writer_version, schema_version, content_hash,
                correction_history_json, created_at, finalized_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(trade_date, ticker, experiment_id) DO UPDATE SET
               trade_id=excluded.trade_id,
               chunks_json=excluded.chunks_json,
               first_source_ts=excluded.first_source_ts,
               last_source_ts=excluded.last_source_ts,
               first_received_at=excluded.first_received_at,
               last_received_at=excluded.last_received_at,
               seq_gaps=excluded.seq_gaps,
               source_ts_reversals=excluded.source_ts_reversals,
               ws_disconnects=excluded.ws_disconnects,
               rest_backfill_ranges_json=excluded.rest_backfill_ranges_json,
               reached_expected_close=excluded.reached_expected_close,
               data_complete=excluded.data_complete,
               missing_reason=excluded.missing_reason,
               writer_version=excluded.writer_version,
               schema_version=excluded.schema_version,
               content_hash=excluded.content_hash,
               correction_history_json=excluded.correction_history_json,
               finalized_at=excluded.finalized_at""",
        (
            trade_date,
            ticker,
            trade_id,
            experiment_id,
            chunks_json,
            first_source_ts,
            last_source_ts,
            first_received_at,
            last_received_at,
            seq_gaps,
            source_ts_reversals,
            ws_disconnects,
            rest_backfill_ranges_json,
            reached_expected_close,
            data_complete,
            missing_reason,
            writer_version,
            schema_version,
            content_hash,
            json.dumps(correction_history, ensure_ascii=False),
            created_at,
            finalized_at,
        ),
    )
    await conn.commit()
    return await get_price_path_manifest(trade_date, ticker, experiment_id)


async def get_price_path_manifest(
    trade_date: str, ticker: str, experiment_id: str | None
) -> dict | None:
    conn = get()
    experiment_id = experiment_id or ""  # NULL 정규화 — UNIQUE/UPSERT 일관성 유지
    async with conn.execute(
        """SELECT * FROM price_path_manifests
            WHERE trade_date=? AND ticker=? AND experiment_id=?""",
        (trade_date, ticker, experiment_id),
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def get_skip_by_date(date: str, track: str = "A") -> dict | None:
    """해당 날짜·트랙의 daily_skips 행 반환. 없으면 None."""
    conn = get()
    async with conn.execute(
        "SELECT * FROM daily_skips WHERE date=? AND track=?", (date, track)
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def record_skip(
    date: str, reason: str, detail: str = "", track: str = "A"
) -> None:
    """daily_skips INSERT. 같은 (날짜, 트랙) 중복 시 무시 (OR IGNORE)."""
    now = _now()
    conn = get()
    await conn.execute(
        """INSERT OR IGNORE INTO daily_skips (date, track, reason, detail, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (date, track, reason, detail, now),
    )
    await conn.commit()


async def record_asset_snapshot(snapshot: dict, raw: dict | None = None) -> int:
    """asset_snapshots 테이블에 KIS 잔고 조회 결과를 저장하고 id를 반환."""
    now = _now()
    conn = get()
    async with conn.execute(
        """INSERT INTO asset_snapshots
               (captured_at, total_asset, cash, buyable_cash, buyable_cash_source,
                stock_value, pnl_amount, holdings_count, source, raw_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            now,
            snapshot.get("total_asset"),
            snapshot.get("cash"),
            snapshot.get("buyable_cash"),
            snapshot.get("buyable_cash_source"),
            snapshot.get("stock_value"),
            snapshot.get("pnl_amount"),
            snapshot.get("holdings_count"),
            snapshot.get("source") or "KIS",
            json.dumps(raw, ensure_ascii=False, separators=(",", ":")) if raw is not None else None,
        ),
    ) as cur:
        snapshot_id = cur.lastrowid
    await conn.commit()
    return snapshot_id


async def latest_asset_snapshot() -> dict | None:
    """가장 최근 asset_snapshots 행을 UI/API 응답 형태로 반환."""
    conn = get()
    async with conn.execute(
        """SELECT captured_at, total_asset, cash, buyable_cash, buyable_cash_source,
                  stock_value, pnl_amount, holdings_count, source, raw_json
           FROM asset_snapshots
           ORDER BY captured_at DESC, id DESC
           LIMIT 1"""
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    result = dict(row)
    raw_json = result.pop("raw_json", None)
    result["source"] = result.get("source") or "KIS"
    result["snapshot_source"] = "DB"
    result["holdings"] = _asset_holdings_from_raw(raw_json)
    if result.get("holdings_count") is None:
        result["holdings_count"] = len(result["holdings"])
    return result
