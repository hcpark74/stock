# 멀티 트랙 기반 다지기 구현 계획 (Plan 1/5)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 트랙 B가 올라탈 수 있도록 영속 계층(DB·상태 파일·집계 API)을 트랙 인식으로 바꾼다. 트랙 A의 매매 판단과 실행 경로는 한 줄도 바뀌지 않는다.

**Architecture:** `trades`/`daily_skips`의 유니크 키를 `(date)`에서 `(date, track)`으로 재작성하고, DB 함수에 `track: str = "A"` 기본값을 추가해 기존 호출부 전부를 무변경으로 둔다. 인메모리 상태는 `state.get()`을 트랙 A 전용으로 동결하고 `state.track('B')`를 신설하며, `today_state.json`은 기존 최상위 필드를 유지한 채 `tracks` 섹션만 덧붙여 구버전 파일도 그대로 읽히게 한다.

**Tech Stack:** Python 3.12 / aiosqlite (SQLite, WAL) / pytest (asyncio auto) / ruff

**Spec:** [docs/superpowers/specs/2026-08-25-multi-track-strategy-design.md](../specs/2026-08-25-multi-track-strategy-design.md) — §3.1, §3.2, §4 전체, §6.1

## Global Constraints

- **트랙 A 실행 경로 동결.** `f3_entry.py`, `f4_tracking.py`, `f5_timeout.py`, `exit_recovery.py`의 매매 판단·주문 경로를 수정하지 않는다. 안 C(§2.1)의 전제이자 증명이다.
- **기존 테스트 스위트 무수정 통과가 수용 기준(§8.4).** 특히 `test_f3_entry`, `test_f5_timeout`, `test_restart_guard`, `test_state_daily_reset`, `tests/js/price_flow_checks.js`. 수정이 필요하면 **검증 대상이 바뀐 것인지 비계가 낡은 것인지** 판단한다. 전자면 전제가 무너진 것이므로 멈추고 보고한다.
- **전략 지문 회전은 이 계획 전체에서 정확히 1회.** `src/release.py::_STRATEGY_FILES`에 `main.py`, `src/state.py`, `src/db.py`, `src/api/kis_rest.py`, `src/api/kis_ws.py`, `f1~f5`, `exit_recovery.py`, `paper_fast_probe.py`, `vi_watch.py`, `schedule_times.py`, `scheduler.py`, `src/utils/number.py`, `src/utils/spike_filter.py`가 들어 있다. 이 계획은 `db.py`와 `state.py`를 반드시 건드리므로 지문이 바뀌고 `experiment_id = f"baseline-{fingerprint}"`(`baseline_experiment.py:84`)가 새로 열려 **40거래일 paired 수집이 0부터 다시 시작한다.** 따라서 **브랜치에서 전 과제를 끝내고 한 번만 배포한다.** 과제마다 운영에 올리면 지문이 그만큼 회전한다.
- **DB 재작성 전 파일 백업은 타협 대상이 아니다(§4.3).** WAL 체크포인트 후 복사한다.
- **`SELECT *` 금지(§4.3).** 구 DB는 `name`·`highest_step`이 `ALTER`로 맨 뒤에 붙어 있어 컬럼 순서가 다르다. 타입이 호환되는 자리끼리 조용히 뒤섞인다.
- **KIS 주문 API를 호출하는 코드는 이 계획에 없다.** 전 과제가 로컬 SQLite와 상태 파일만 다룬다.
- 커밋 메시지는 기존 관례를 따른다 — 영문 conventional subject + 한국어 본문, `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>` 트레일러.

---

## File Structure

| 파일 | 책임 | 변경 |
|---|---|---|
| `src/db.py` | 스키마, 마이그레이션, 트랙 스코프 쿼리 | 수정 (Task 1·2·3·4) |
| `src/state.py` | 트랙 A 상태(동결) + `TrackState` 레지스트리 | 수정 (Task 5) |
| `src/modules/exit_recovery.py` | DB 의도 병합에 트랙 전파 | 수정 (Task 4) |
| `src/api/server.py` | 집계·이력 API 트랙 스코프 | 수정 (Task 6) |
| `src/utils/logger.py` | 마이그레이션 이벤트 라벨 | 수정 (Task 1) |
| `tests/test_db_track_migration.py` | 구 DB 재작성·FK 보존·컬럼 정렬 | 신규 (Task 1·2) |
| `tests/test_db_track_scope.py` | 트랙별 멱등성·조회 격리·교차 오염 | 신규 (Task 3·4) |
| `tests/test_track_state.py` | `TrackState`·하위호환 영속화 | 신규 (Task 5) |
| `tests/test_api_track_scope.py` | `/api/stats`·`/api/history` 트랙 스코프 | 신규 (Task 6) |

---

## Task 0: 어제 배포분 실장 검증 (§12.1)

선행 5개 커밋(`d15a18a`·`6d0fd87`·`3acdac2`·`d7fd48f`·`8909761`)과 2026-08-26의 캡처 재바인딩 수정(`6e0c3bc`)은 **테스트로만 검증됐다.** 코드를 더 쌓기 전에 장중 로그로 확인한다. 이 과제는 코드를 쓰지 않는다.

**Files:**
- 읽기 전용: `data/logs/YYYYMMDD.jsonl`, `data/strategy_ticks/YYYYMMDD/`

**Interfaces:**
- Consumes: 없음
- Produces: 스펙 §12.1에 기록되는 실측값. Plan 2의 봉 집계기가 이 틱 밀도·다건 프레임 빈도를 입력 가정으로 쓴다.

- [ ] **Step 1: 다음 거래일 15:40 이후 관측 창을 확인한다**

```bash
python - <<'PY'
import json, collections, glob
path = sorted(glob.glob("data/logs/*.jsonl"))[-1]
c = collections.Counter()
first = {}
for line in open(path, encoding="utf-8"):
    line = line.strip()
    if not line:
        continue
    d = json.loads(line)
    e = d.get("event")
    c[e] += 1
    first.setdefault(e, d["ts"])
for e in ("WS_CONNECTED", "TICK_CAPTURE_STARTED", "TICK_CAPTURE_TARGET_SWITCHED",
          "F4_REST_BACKUP_START", "TARGET_LOCKED", "F3_FINAL_PICK",
          "ENTRY_EXECUTED", "TICK_CAPTURE_FINALIZED"):
    print(f"{c[e]:5d}  {e:32s} first={first.get(e, '-')}")
PY
```

기대: `WS_CONNECTED`의 first가 09:01 이전(종목 잠금 직후). A가 미진입한 날에도 나타나야 한다(§3.3).

- [ ] **Step 2: 미보유일 REST 백업 억제를 확인한다 (§3.5)**

```bash
python - <<'PY'
import glob, gzip, json, collections
day = sorted(glob.glob("data/strategy_ticks/*"))[-1]
c = collections.Counter()
for gz in glob.glob(f"{day}/*.jsonl.gz"):
    with gzip.open(gz, "rt", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                c[json.loads(line).get("source")] += 1
print(day, dict(c))
PY
```

기대: A가 진입하지 않은 날에는 `rest` 카운트 0. 여기서 `rest`가 나오면 PAPER 초당 1건 예산을 관측이 먹고 있다는 뜻이고, 그날 늦은 A의 진입까지 막는다.

- [ ] **Step 3: 종목 교체일의 재구독·재바인딩을 확인한다 (§3.7)**

`TARGET_LOCKED`의 ticker와 `ENTRY_EXECUTED`의 ticker가 다른 날을 찾아, `TICK_CAPTURE_TARGET_SWITCHED`가 1건 남고 캡처 파일이 **체결 종목** 이름으로 생겼는지 본다. 2026-08-26에는 바로 이 경로에서 하루치가 유실됐다.

- [ ] **Step 4: 다건 프레임을 실측한다 (§11.2)**

캡처의 `raw` 배열 길이 분포와 하루 틱 수를 센다. 다건 프레임이 실제로 오는지, 온다면 하루 몇 건인지가 Plan 2 봉 집계기의 입력 가정이다.

```bash
python - <<'PY'
import glob, gzip, json, collections
day = sorted(glob.glob("data/strategy_ticks/*"))[-1]
lens = collections.Counter()
total = 0
for gz in glob.glob(f"{day}/*.jsonl.gz"):
    with gzip.open(gz, "rt", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            total += 1
            raw = json.loads(line).get("raw")
            lens[len(raw) if isinstance(raw, list) else 0] += 1
print(day, "ticks:", total, "raw 길이 분포:", dict(lens))
PY
```

- [ ] **Step 5: 캡처 완전성을 확인한다**

```bash
python - <<'PY'
import asyncio
from src import db
async def main():
    await db.init("data/db/trading.db")
    conn = db.get()
    async with conn.execute(
        "SELECT trade_date, ticker, data_complete, missing_reason "
        "FROM price_path_manifests ORDER BY trade_date DESC LIMIT 10"
    ) as cur:
        for r in await cur.fetchall():
            print(dict(r))
    await db.close()
asyncio.run(main())
PY
```

기대: 새 거래일이 `data_complete=1`. §12 기준 `data_complete=1`인 날은 **0일**이었다. 여기서 1이 나오기 시작해야 트랙 B의 규칙 실험에 쓸 표본이 쌓인다.

- [ ] **Step 6: 결과를 스펙 §12.1에 기록하고 커밋한다**

확인된 값과 빗나간 가정을 스펙에 반영한다. 코드 변경 없음.

```bash
git add docs/superpowers/specs/2026-08-25-multi-track-strategy-design.md
git commit -m "docs: record live verification of the observation-layer changes"
```

---

## Task 1: `trades` 테이블 트랙 재작성

**Files:**
- Modify: `src/db.py` — `init()`의 `CREATE TABLE trades`(28-56행), `daily_skips` 재구축 블록(309-334행) 뒤에 새 마이그레이션 삽입
- Modify: `src/utils/logger.py` — `EVENT_LABELS`
- Test: `tests/test_db_track_migration.py` (신규)

**Interfaces:**
- Consumes: 없음 (이 계획의 첫 코드 과제)
- Produces: `trades.track TEXT NOT NULL DEFAULT 'A'`, `UNIQUE (date, track)`, 확장된 `close_reason` CHECK. Task 3·4·6이 이 컬럼에 의존한다.

- [ ] **Step 1: 실패하는 테스트를 쓴다 — 구 DB 재작성에서 값이 뒤섞이지 않는다**

`tests/test_db_track_migration.py`:

```python
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
```

- [ ] **Step 2: 실패를 확인한다**

Run: `python -m pytest tests/test_db_track_migration.py -q`
Expected: FAIL — `KeyError: 'track'` (아직 컬럼이 없다)

- [ ] **Step 3: 신규 DB 스키마에 track을 넣는다**

`src/db.py`의 `CREATE TABLE IF NOT EXISTS trades`에서 `date TEXT NOT NULL UNIQUE`를 아래로 바꾼다.

```sql
            date         TEXT NOT NULL,
            track        TEXT NOT NULL DEFAULT 'A',
```

`close_reason` CHECK를 넓힌다. 새 사유 3개를 지금 넣는 이유는 §4.5다 — 트랙 B가 쓸 때 재작성을 한 번 더 하지 않기 위해서다.

```sql
            close_reason TEXT CHECK (close_reason IN (
                             'TRAILING','HARD_STOP',
                             'TIMEOUT','SLIPPAGE_GUARD','ENTRY_FAIL','MANUAL',
                             'SIGNAL_EXIT','INDICATOR_STOP','TRACK_HALTED'
                         )),
```

테이블 끝에 유니크 키를 단다.

```sql
            updated_at   TEXT NOT NULL,
            UNIQUE (date, track)
        );
```

- [ ] **Step 4: 구 DB 마이그레이션을 넣는다**

`src/db.py` 상단 import에 `shutil`, `from datetime import datetime`, `from pathlib import Path`가 없으면 추가하고, `daily_skips` 재구축 블록 **뒤에** 아래를 넣는다. `experiment_id`·`name`·`highest_step`은 앞선 `ALTER` 블록에서 이미 보장되므로 여기서 컬럼명을 나열해도 안전하다.

```python
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
    await _conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
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
```

`init()`의 `daily_skips` 블록 뒤에서 호출한다. `init(db_path)`는 이미 경로를 받는다.

```python
    await _migrate_trades_track(db_path)
```

`src/utils/logger.py`의 `EVENT_LABELS`에 두 이벤트를 등록한다.

```python
    "DB_TRACK_MIGRATION_START": "트랙 마이그레이션 시작(DB Track Migration Start)",
    "DB_TRACK_MIGRATION_DONE": "트랙 마이그레이션 완료(DB Track Migration Done)",
```

- [ ] **Step 5: 통과를 확인한다**

Run: `python -m pytest tests/test_db_track_migration.py -q`
Expected: PASS

- [ ] **Step 6: FK 보존·백업·재실행 방지 테스트를 추가한다**

`trailing_shadow_comparisons`는 `ON DELETE CASCADE`라 재작성이 `DROP TABLE`을 잘못 다루면 이력이 조용히 사라진다(§4.3). `tests/test_db_track_migration.py`에 덧붙인다.

```python
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
```

Run: `python -m pytest tests/test_db_track_migration.py -q`
Expected: 네 테스트 모두 PASS. 하나라도 실패하면 Step 4의 구현을 고친다 — 테스트를 고치지 않는다.

- [ ] **Step 7: 전체 스위트로 기존 계약을 확인한다**

Run: `python -m pytest -q`
Expected: 전부 통과. `test_db_schema_creation`·`test_db_crud`가 깨지면 스키마 변경이 기존 계약을 건드린 것이므로 멈추고 원인을 본다.

- [ ] **Step 8: 커밋**

```bash
git add src/db.py src/utils/logger.py tests/test_db_track_migration.py
git commit -F <메시지 파일>
```

제목: `feat(db): rewrite trades with a track column and (date, track) unique key`

---

## Task 2: `daily_skips` 트랙 확장

B가 조건 미충족으로 스킵한 이유를 남겨야 실험 분석이 된다. 현재 `daily_skips.date`가 UNIQUE라 하루에 한 트랙만 기록된다.

**Files:**
- Modify: `src/db.py` — `daily_skips` CREATE(138행), 재구축 블록(309-334행), `get_skip_by_date`(1353행), `record_skip`(1362행)
- Test: `tests/test_db_track_migration.py` (Task 1에서 만든 파일에 추가)

**Interfaces:**
- Consumes: Task 1의 `sqlite_master` 기반 마이그레이션 감지 패턴
- Produces: `daily_skips.track TEXT NOT NULL DEFAULT 'A'`, `UNIQUE (date, track)`, `record_skip(date, reason, detail="", track="A")`, `get_skip_by_date(date, track="A")`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
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
```

- [ ] **Step 2: 실패를 확인한다**

Run: `python -m pytest tests/test_db_track_migration.py::test_two_tracks_can_skip_the_same_day -q`
Expected: FAIL — `record_skip() got an unexpected keyword argument 'track'`

- [ ] **Step 3: 최소 구현**

`CREATE TABLE IF NOT EXISTS daily_skips`에서 `date TEXT NOT NULL UNIQUE`를 `date TEXT NOT NULL`로 바꾸고 그 아래에 `track TEXT NOT NULL DEFAULT 'A',`를 넣은 뒤, 테이블 끝에 `UNIQUE (date, track)`을 단다.

기존 재구축 블록의 감지 조건을 넓힌다.

```python
    sql = (row["sql"] or "") if row else ""
    if row and ("VI_ACTIVE" not in sql or "track" not in sql):
```

재구축 테이블 정의에 `track TEXT NOT NULL DEFAULT 'A'`와 `UNIQUE (date, track)`을 넣고, INSERT를 컬럼 명시로 바꾼다.

```sql
            INSERT INTO daily_skips_migrated
                (id, date, track, reason, detail, created_at)
                SELECT id, date, 'A', reason, detail, created_at FROM daily_skips;
```

```python
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
```

`get_skip_by_date(date: str, track: str = "A")`도 `WHERE date=? AND track=?`로 바꾼다.

- [ ] **Step 4: 통과를 확인한다**

Run: `python -m pytest tests/test_db_track_migration.py -q`
Expected: PASS

- [ ] **Step 5: 전체 스위트**

Run: `python -m pytest -q`
Expected: 전부 통과. `record_skip` 호출부는 전부 위치 인자 3개까지만 쓰므로 무변경이어야 한다.

- [ ] **Step 6: 커밋**

```bash
git add src/db.py tests/test_db_track_migration.py
git commit -F <메시지 파일>
```

제목: `feat(db): scope daily_skips by track`

---

## Task 3: `open_trade` / `get_trade_by_date` 트랙 스코프

**Files:**
- Modify: `src/db.py:439-504`(`open_trade`), `src/db.py:506-524`(`get_trade_by_date`)
- Test: `tests/test_db_track_scope.py` (신규)

**Interfaces:**
- Consumes: Task 1의 `UNIQUE (date, track)`
- Produces: `open_trade(date, ticker, entry_price, entry_qty, name=None, track="A") -> int`, `get_trade_by_date(date, track="A") -> dict | None`. 기존 호출부 6곳(`f3_entry.py:1152·1161·2180·2705`, `main.py:307·1078`)은 **무변경**이다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_db_track_scope.py`:

```python
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
```

- [ ] **Step 2: 실패를 확인한다**

Run: `python -m pytest tests/test_db_track_scope.py -q`
Expected: FAIL — `open_trade() got an unexpected keyword argument 'track'`

- [ ] **Step 3: 최소 구현**

```python
async def open_trade(
    date: str, ticker: str, entry_price: float, entry_qty: int,
    name: str | None = None, track: str = "A",
) -> int:
```

INSERT 컬럼 목록에 `track`을, VALUES에 자리표시자 하나를, 파라미터 튜플에 `track`을 더한다.

```sql
               INSERT INTO trades
                   (date, track, ticker, name, entry_price, entry_qty, entry_at,
                    status, execution_mode, strategy_fingerprint, experiment_id,
                    created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?)
```

`except sqlite3.IntegrityError` 분기의 조회를 **같은 트랙으로** 바꾼다. 이걸 놓치면 B의 충돌에서 A의 거래를 반환한다(§4.2).

```python
    except sqlite3.IntegrityError:
        existing = await get_trade_by_date(date, track=track)
```

```python
async def get_trade_by_date(date: str, track: str = "A") -> dict | None:
```

쿼리의 `WHERE t.date=?`를 `WHERE t.date=? AND t.track=?`로, 파라미터를 `(date, track)`으로 바꾼다.

- [ ] **Step 4: 통과를 확인한다**

Run: `python -m pytest tests/test_db_track_scope.py -q`
Expected: PASS

- [ ] **Step 5: 전체 스위트 — 여기가 안 C의 첫 관문이다**

Run: `python -m pytest -q`
Expected: `test_f3_entry`·`test_db_crud`가 **무수정**으로 통과. 깨지면 기본값 `track="A"`가 어딘가에서 전파되지 않은 것이다.

- [ ] **Step 6: 커밋**

```bash
git add src/db.py tests/test_db_track_scope.py
git commit -F <메시지 파일>
```

제목: `feat(db): scope trade lookup and idempotency by track`

---

## Task 4: 청산 의도 교차 오염 수정 (§4.4)

`get_unresolved_exit_intent`는 `date`로만 조인해 가장 최근 미해결 매도를 돌려준다. A가 `EXITING`이면서 `pending_exit`가 `None`인 창(`state.clear_pending_exit()`이 재시도를 허용하려고 만드는 상태)에서 재시작이 겹치면 `exit_recovery.py:91`의 `not isinstance(pending, dict)`가 참이 되어 **A가 B의 매도 주문을 자기 것으로 인수한다.**

**Files:**
- Modify: `src/db.py:757-776`(`get_unresolved_exit_intent`)
- Modify: `src/modules/exit_recovery.py:71-94`(`merge_db_intent`)
- Test: `tests/test_db_track_scope.py` (Task 3 파일에 추가)

**Interfaces:**
- Consumes: Task 1의 `trades.track`, Task 3의 트랙 스코프 관례
- Produces: `get_unresolved_exit_intent(date, track="A")`, `merge_db_intent(data, date, track="A")`. 호출부 `main.py:738`은 무변경(기본값 A).

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`record_order`(`db.py:556`)는 `status='PENDING'`으로 INSERT하고 `submission_state`의 기본값이 `'ACKNOWLEDGED'`이므로, 미해결 매도 조건은 `client_order_id`만 주면 성립한다.

```python
async def test_track_a_recovery_never_adopts_track_b_sell_order(mem):
    a = await db.open_trade("20260826", "215600", 3095.0, 610)
    b = await db.open_trade("20260826", "215600", 3200.0, 300, track="B")
    await db.record_order(
        b, "B-SELL-1", "SELL", 300, 3150.0, "CLOSE_SELL", "215600", "신라젠",
        client_order_id="cli-b-1",
    )

    assert await db.get_unresolved_exit_intent("20260826") is None
    row = await db.get_unresolved_exit_intent("20260826", track="B")
    assert row is not None and row["client_order_id"] == "cli-b-1"
    assert a != b
```

- [ ] **Step 2: 실패를 확인한다**

Run: `python -m pytest tests/test_db_track_scope.py::test_track_a_recovery_never_adopts_track_b_sell_order -q`
Expected: FAIL — 트랙 인자가 없어 `TypeError`, 또는 A 조회가 B의 행을 반환

- [ ] **Step 3: 최소 구현**

```python
async def get_unresolved_exit_intent(date: str, track: str = "A") -> dict | None:
    """상태 파일 갱신 직전 장애를 DB 주문 의도에서 복구한다.

    트랙 스코프가 없으면 A의 청산 재시도 창(EXITING + pending_exit=None)에서
    재시작이 겹칠 때 A가 B의 매도 주문을 인수한다(§4.4).
    """
```

조인 조건에 `AND t.track=?`를 더하고 파라미터를 `(date, track)`으로 바꾼다.

```sql
              WHERE t.date=?
                AND t.track=?
                AND o.order_type='SELL'
```

`exit_recovery.merge_db_intent`:

```python
async def merge_db_intent(data: dict, date: str, track: str = "A") -> dict:
```

```python
        row = await db.get_unresolved_exit_intent(date, track=track)
```

- [ ] **Step 4: 통과를 확인한다**

Run: `python -m pytest tests/test_db_track_scope.py -q`
Expected: PASS

- [ ] **Step 5: 전체 스위트**

Run: `python -m pytest -q`
Expected: `test_exit_recovery`가 **무수정**으로 통과. 기본값이 A이므로 기존 계약이 그대로다.

- [ ] **Step 6: 커밋**

```bash
git add src/db.py src/modules/exit_recovery.py tests/test_db_track_scope.py
git commit -F <메시지 파일>
```

제목: `fix(db): scope unresolved exit intent by track`

---

## Task 5: `TrackState`와 하위호환 영속화 (§3.1·§3.2)

`TrackState`는 트랙 무관 필드(`trading_date`, `target_ticker`, `target_name`, `target_candidates`, `day_skip`)를 갖지 않는다 — 종목과 후보는 F1/F2가 정하는 트랙 공유 자산이다.

**Files:**
- Modify: `src/state.py` — `State`(15-40행) 아래 `TrackState` 추가, `_clear_for_trading_day`, `persist`(276행), `restore_from`(343행)
- Test: `tests/test_track_state.py` (신규)

**Interfaces:**
- Consumes: 없음
- Produces: `state.TrackState`, `state.track(name: str) -> TrackState`, `state.all_tracks() -> dict[str, TrackState]`, `today_state.json`의 `tracks` 섹션. Plan 3의 트랙 B 모듈이 이것을 쓴다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_track_state.py`:

```python
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
```

- [ ] **Step 2: 실패를 확인한다**

Run: `python -m pytest tests/test_track_state.py -q`
Expected: FAIL — `module 'src.state' has no attribute 'track'`

- [ ] **Step 3: 최소 구현**

`src/state.py`의 `State` 정의 아래에 넣는다. `import dataclasses`와 `from dataclasses import asdict`가 없으면 추가한다.

```python
@dataclass
class TrackState:
    """트랙 A 이외 트랙의 포지션 상태.

    trading_date·target_ticker·target_candidates·day_skip은 갖지 않는다 —
    종목과 후보는 F1/F2가 정하는 트랙 공유 자산이다(§3.1).
    """
    entry_price: float | None = None
    entry_at: str | None = None
    entry_qty: int | None = None
    remaining_qty: int | None = None
    high_price: float | None = None
    position_status: str = "IDLE"
    close_reason: str | None = None
    order_id: str | None = None
    trade_id: int = 0
    pending_entry: dict | None = None
    pending_exit: dict | None = None


_tracks: dict[str, TrackState] = {}


def track(name: str) -> TrackState:
    """트랙 상태를 반환한다. 없으면 IDLE로 만든다."""
    return _tracks.setdefault(name, TrackState())


def all_tracks() -> dict[str, TrackState]:
    """감사·UI용 순회. 트랙 A는 get()이며 여기 포함되지 않는다."""
    return dict(_tracks)
```

`persist()`의 `data` dict 마지막에 한 줄을 더한다. 기존 최상위 필드는 **그대로 둔다** — 구버전 `restore_from()`이 읽어야 한다(§3.2).

```python
        "tracks": {
            name: asdict(track_state) for name, track_state in _tracks.items()
        },
```

`restore_from()` 끝에 트랙 복원을 더한다.

```python
    _tracks.clear()
    tracks = data.get("tracks")
    if isinstance(tracks, dict):
        allowed = {f.name for f in dataclasses.fields(TrackState)}
        for name, payload in tracks.items():
            if not isinstance(payload, dict):
                continue
            _tracks[name] = TrackState(
                **{k: v for k, v in payload.items() if k in allowed}
            )
```

`_clear_for_trading_day()`에 `_tracks.clear()`를 더한다.

- [ ] **Step 4: 통과를 확인한다**

Run: `python -m pytest tests/test_track_state.py -q`
Expected: PASS

- [ ] **Step 5: 재시작·리셋 계약을 확인한다**

Run: `python -m pytest tests/test_state_daily_reset.py tests/test_restart_guard.py -q`
Expected: PASS (무수정)

- [ ] **Step 6: 전체 스위트**

Run: `python -m pytest -q`
Expected: 전부 통과

- [ ] **Step 7: 커밋**

```bash
git add src/state.py tests/test_track_state.py
git commit -F <메시지 파일>
```

제목: `feat(state): add per-track state with backward-compatible persistence`

---

## Task 6: 집계 API 트랙 스코프 (§6.1)

`/api/stats`와 `/api/history`에는 트랙 필터가 없다. 트랙 B가 생기는 순간 승률·평균손익이 두 전략의 혼합값이 되고 이력 화면에 같은 날짜가 둘씩 뜬다. **A/B 비교가 목적인데 통계가 비교를 지운다.**

**Files:**
- Modify: `src/api/server.py:954-970`(`api_history`), `src/api/server.py:977-`(`api_stats`)
- Test: `tests/test_api_track_scope.py` (신규)

**Interfaces:**
- Consumes: Task 1의 `trades.track`, Task 3의 `open_trade(track=...)`
- Produces: `GET /api/history?track=A`, `GET /api/stats?track=A` (기본값 `"A"`). `/api/history` 응답 행에 `track` 필드가 포함된다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`api_stats`의 실제 응답 키는 구현을 읽고 맞춘다. 요지는 **트랙별로 집계가 갈리는 것**이다.

```python
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
```

- [ ] **Step 2: 실패를 확인한다**

Run: `python -m pytest tests/test_api_track_scope.py -q`
Expected: FAIL — `api_stats() got an unexpected keyword argument 'track'`

- [ ] **Step 3: 최소 구현**

```python
@app.get("/api/history")
async def api_history(limit: int = 60, track: str = "A") -> JSONResponse:
```

```sql
            SELECT date, track, ticker, name, entry_price, exit_price,
                   pnl_pct, close_reason, highest_step, pyramided, status
              FROM trades
             WHERE track=?
             ORDER BY date DESC
             LIMIT ?
```

파라미터는 `(track, limit)`.

```python
@app.get("/api/stats")
async def api_stats(track: str = "A") -> JSONResponse:
```

`api_stats` 안의 집계 쿼리는 **4개**다 — 전체 집계(`agg`), 사유별(`by_reason`), 월별(`monthly`), 불타기별(`by_pyramided`). **네 개 모두**에 트랙 조건을 넣는다. 하나라도 빠지면 그 지표만 혼합값이 되고, 화면에서는 구분되지 않는다.

```sql
               FROM trades WHERE status='CLOSED' AND track=?
```

응답 키(`total`, `wins`, `losses`, `win_rate`, `avg_pnl`, `max_loss`, `max_gain`, `by_reason`, `monthly`, `by_pyramided`)는 그대로 두고 값만 트랙 스코프로 좁힌다.

- [ ] **Step 4: 통과를 확인한다**

Run: `python -m pytest tests/test_api_track_scope.py -q`
Expected: PASS

- [ ] **Step 5: 전체 스위트 + JS 회귀선**

Run: `python -m pytest -q`
Expected: `test_api_server`·`test_api_status_logic` 무수정 통과

Run: `node tests\js\price_flow_checks.js` (`docs/DEV_ENV.md:660`의 실행 방식)
Expected: PASS — A의 차트를 건드리지 않았다는 증거다(§8.4)

- [ ] **Step 6: 커밋**

```bash
git add src/api/server.py tests/test_api_track_scope.py
git commit -F <메시지 파일>
```

제목: `feat(api): scope stats and history queries by track`

---

## Task 7: 배포 리허설과 지문 회전 기록

**Files:**
- 읽기 전용 + `docs/superpowers/specs/2026-08-25-multi-track-strategy-design.md` 갱신

**Interfaces:**
- Consumes: Task 1~6 전부
- Produces: 새 `strategy_fingerprint`와 `experiment_id`. Plan 3의 승격 기록이 이 실험 ID 위에서 시작한다.

- [ ] **Step 1: 운영 DB 사본으로 마이그레이션을 리허설한다**

봇이 멈춘 시간대(15:40 이후)에 실행한다.

```bash
python - <<'PY'
import asyncio, shutil
from pathlib import Path
shutil.copy2(Path("data/db/trading.db"), Path("data/db/rehearsal.db"))
from src import db
async def main():
    await db.init("data/db/rehearsal.db")
    conn = db.get()
    async with conn.execute(
        "SELECT COUNT(*) AS n, SUM(track='A') AS a FROM trades"
    ) as cur:
        print(dict(await cur.fetchone()))
    async with conn.execute("PRAGMA foreign_key_check") as cur:
        print("fk violations:", await cur.fetchall())
    async with conn.execute(
        "SELECT COUNT(*) AS n FROM trailing_shadow_comparisons"
    ) as cur:
        print("shadow rows:", dict(await cur.fetchone()))
    await db.close()
asyncio.run(main())
PY
```

기대: 전체 행 수가 마이그레이션 전과 같고, 전부 `track='A'`, FK 위반 0건, `trailing_shadow_comparisons` 행 수 보존. 확인 후 `data/db/rehearsal.db`와 그 백업 파일을 지운다.

- [ ] **Step 2: 새 전략 지문을 확인한다**

```bash
python -c "from src import release; print(release.strategy_fingerprint())"
```

이 값이 배포 후의 `experiment_id = baseline-<fingerprint>`가 된다. **이전 실험의 paired 수집은 여기서 끊긴다.**

- [ ] **Step 3: 스펙 §12를 갱신하고 커밋한다**

`미착수: §4(DB 스키마·마이그레이션)`을 완료로 옮기고, 회전한 지문·새 `experiment_id`·리허설 결과를 남긴다.

```bash
git add docs/superpowers/specs/2026-08-25-multi-track-strategy-design.md
git commit -m "docs: mark section 4 implemented and record the fingerprint rotation"
```

- [ ] **Step 4: 브랜치를 한 번에 배포한다**

`main`으로 병합하고 **봇을 1회 재시작한다.** 첫 기동에서 `DB_TRACK_MIGRATION_START`/`DONE`이 로그에 남고 `data/db/trading.db.pre_track_*` 백업이 생긴다. 다음 거래일 09시 전에 끝낸다. 기동 로그에 `RuntimeError`가 뜨면 백업으로 복원하고 원인을 먼저 본다 — 그 상태로 장을 맞지 않는다.

---

## 후속 계획 경계

스펙 §5~§8은 각각 독립적으로 동작·테스트 가능한 산출물이라 별도 계획으로 나눈다. 순서는 의존성 순이다.

| 계획 | 범위 | 산출물 | 선행 |
|---|---|---|---|
| **Plan 2 — 봉/지표 계층** | §7 전체 | `live._accumulate_minute`의 OHLCV 확장, 체결강도·1단계 호가·총잔량 누적(§7.5), 분봉 API 확정 봉 정정(§7.2), 09:00~09:11 호출 금지 런타임 승격(§7.3), 순수 함수 지표 엔진(§7.4) | 없음 — Plan 1과 병행 가능 |
| **Plan 3 — 트랙 B SHADOW** | §6.2·§6.4·§6.5, §4.6 기동 검증 | `shadow_trades` 테이블, 트랙 B 신호 모듈, 표본 20건 하드락, `strategy_configs` 기반 승격·강등, A/B 동일 `experiment_id` 금지 | Plan 1·2 |
| **Plan 4 — 트랙 UI** | §8.2·§8.3 | 트랙 선택·A/B 비교 뷰, 신규 봉/지표 차트(`drawPriceFlow` 미변경), 미확정 봉 구분 표시, 그림자 마커 | Plan 1·2·3 |
| **Plan 5 — PILOT 승격** | §5 전체, §6.3 | 08:59 예산 선분배·동결, 장부 불변식 감사, §5.3 비대칭 위반 정책, UNCERTAIN 트랙 격리 | Plan 3·4 |

**Plan 5까지 가기 전에는 실자본이 트랙 B로 가지 않는다.** SHADOW 단계에서 `TRACK_WEIGHT[B] = 0.0`이므로 트랙 A의 예산은 현재와 정확히 같고(§5.1), 그래서 그림자 기간 동안 A의 성과 변화가 트랙 도입 탓인지 아닌지를 관측으로 가를 수 있다.

범위 밖은 스펙 §9를 따른다 — 트랙 B의 매매 규칙 자체, 트랙 3개 이상, 다른 종목 트랙, 호가창 깊이 구독, VI의 WS 대체, State 다중화 리팩터링(안 A).
