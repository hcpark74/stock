"""SQLite 연결 관리 — DB_DESIGN.md §4 PRAGMA 설정"""

import json
import sqlite3
from datetime import datetime
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
            date         TEXT NOT NULL UNIQUE,
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
                             'TIMEOUT','SLIPPAGE_GUARD','ENTRY_FAIL','MANUAL'
                         )),
            pnl_pct      REAL,
            pnl_amount   REAL,
            high_price   REAL,
            highest_step REAL,
            pyramided    INTEGER DEFAULT 0,
            status       TEXT NOT NULL DEFAULT 'OPEN'
                             CHECK (status IN ('OPEN','CLOSED','SKIPPED')),
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL
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
        );

        CREATE INDEX IF NOT EXISTS idx_orders_trade_id     ON orders(trade_id);
        CREATE INDEX IF NOT EXISTS idx_orders_kis_order_id ON orders(kis_order_id);

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
            date       TEXT NOT NULL UNIQUE,
            reason     TEXT NOT NULL CHECK (reason IN (
                           'NO_TARGET','GAP_CHANGED','ENTRY_FAIL',
                           'SLIPPAGE_GUARD','MANUAL'
                       )),
            detail     TEXT,
            created_at TEXT NOT NULL
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
    await _conn.commit()


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

async def open_trade(date: str, ticker: str, entry_price: float, entry_qty: int) -> int:
    """Insert a trade and return its id. Existing same-day trades are reused."""
    now = _now()
    conn = get()
    try:
        async with conn.execute(
            """INSERT INTO trades
                   (date, ticker, entry_price, entry_qty, entry_at,
                    status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'OPEN', ?, ?)""",
            (date, ticker, entry_price, entry_qty, now, now, now),
        ) as cur:
            trade_id = cur.lastrowid
        await conn.commit()
        return trade_id
    except sqlite3.IntegrityError:
        existing = await get_trade_by_date(date)
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
        return int(existing["id"])


async def get_trade_by_date(date: str) -> dict | None:
    conn = get()
    async with conn.execute("SELECT * FROM trades WHERE date=?", (date,)) as cur:
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
) -> int:
    """orders 테이블에 주문 INSERT. order_db_id 반환.

    side: 'BUY' | 'SELL'
    phase: 'FIRST_BUY' | 'PYRAMID_BUY' | 'CLOSE_SELL' | 'TIMEOUT_SELL' | 'SLIPPAGE_SELL'
    """
    now = _now()
    conn = get()
    async with conn.execute(
        """INSERT INTO orders
               (trade_id, kis_order_id, order_type, order_phase,
                ticker, name, order_qty, order_price, status, ordered_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?)""",
        (trade_id, kis_order_id, side, phase, ticker, name, qty, price, now),
    ) as cur:
        order_db_id = cur.lastrowid
    await conn.commit()
    return order_db_id


async def update_order_fill(
    order_db_id: int,
    fill_price: float,
    fill_qty: int,
    fill_latency_ms: int,
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

async def close_trade(
    trade_id: int,
    exit_price: float,
    close_reason: str,
    pnl_pct: float,
    highest_step: float,
) -> None:
    """trades 청산 정보 갱신 (status → CLOSED)."""
    now = _now()
    conn = get()
    await conn.execute(
        """UPDATE trades
           SET exit_price=?, exit_at=?, close_reason=?,
               pnl_pct=?, highest_step=?, status='CLOSED', updated_at=?
           WHERE id=?""",
        (exit_price, now, close_reason, pnl_pct, highest_step, now, trade_id),
    )
    await conn.commit()


async def record_skip(date: str, reason: str, detail: str = "") -> None:
    """daily_skips INSERT. 같은 날짜 중복 시 무시 (OR IGNORE)."""
    now = _now()
    conn = get()
    await conn.execute(
        """INSERT OR IGNORE INTO daily_skips (date, reason, detail, created_at)
           VALUES (?, ?, ?, ?)""",
        (date, reason, detail, now),
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
