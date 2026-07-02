# DB 설계 문서 — SQLite

> **버전**: 1.0  
> **최종 수정**: 2026-06-23  
> **대상 파일**: `data/db/trading.db`

---

## 1. 설계 원칙

| 원칙 | 내용 |
|------|------|
| 단일 파일 | `data/db/trading.db` — 백업이 파일 복사 한 번 |
| WAL 모드 | 읽기/쓰기 병목 최소화 (`PRAGMA journal_mode=WAL`) |
| 타임스탬프 | 전부 ISO 8601 KST (`2026-06-23T09:00:01+09:00`) |
| 가격 | `REAL` (소수점 허용) |
| 수량 | `INTEGER` |
| Enum 값 | `TEXT` CHECK 제약으로 강제 |
| 운영 상태 | `today_state.json` 유지 — crash recovery 전용 |
| 분석/이력 | SQLite (`trades`, `orders`, `partial_exits`, `daily_skips`) |

---

## 2. ERD (텍스트)

```
trades (1) ──< orders       (N)
trades (1) ──< partial_exits(N)
trades (1) ──  daily_skips  (date 기준 선택적)
```

---

## 3. 테이블 정의

### 3-1. `trades` — 일별 거래 마스터

하루 최대 1건. 진입부터 청산까지 라이프사이클 전체.

```sql
CREATE TABLE IF NOT EXISTS trades (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,

    -- 식별
    date         TEXT NOT NULL UNIQUE,          -- 'YYYYMMDD'
    ticker       TEXT NOT NULL,                 -- 종목코드 (예: '005930')

    -- 진입
    entry_price  REAL,                          -- 가중평균 체결가 (피라미딩 포함)
    entry_qty    INTEGER,                       -- 총 진입 수량
    entry_at     TEXT,                          -- ISO8601 KST

    -- 청산
    exit_price   REAL,                          -- 가중평균 청산가
    exit_qty     INTEGER,                       -- 총 청산 수량
    exit_at      TEXT,                          -- ISO8601 KST
    close_reason TEXT CHECK (close_reason IN (
                     'TRAILING','HARD_STOP','BEP_STOP',
                     'TIMEOUT','SLIPPAGE_GUARD','ENTRY_FAIL',
                     'MANUAL'
                 )),

    -- 손익
    pnl_pct      REAL,                          -- 진입 대비 전체 P&L %
    pnl_amount   REAL,                          -- 손익 원화 (수수료 미포함)

    -- 추적
    high_price   REAL,                          -- 보유 중 최고가 (Trailing 기준)
    pyramided    INTEGER DEFAULT 0,             -- 2차 매수 실행 여부 (0/1)

    -- 상태
    status       TEXT NOT NULL DEFAULT 'OPEN'
                     CHECK (status IN ('OPEN','CLOSED','SKIPPED')),

    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trades_date ON trades(date);
```

#### 컬럼 보충

| 컬럼 | 설명 |
|------|------|
| `entry_price` | 1차+2차 체결가 가중평균. 2차 없으면 1차 그대로 |
| `pnl_amount` | `(exit_price − entry_price) × exit_qty` 단순 계산 |
| `pyramided` | F3에서 2차 30% 매수가 체결됐으면 1 |

---

### 3-2. `orders` — 개별 KIS 주문

진입·청산 모든 주문 1건 = 1행.

```sql
CREATE TABLE IF NOT EXISTS orders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id     INTEGER NOT NULL REFERENCES trades(id),

    -- KIS 주문 식별자
    kis_order_id TEXT,                          -- KIS odno (체결 전 공란 가능)

    -- 구분
    order_type   TEXT NOT NULL CHECK (order_type  IN ('BUY','SELL')),
    order_phase  TEXT NOT NULL CHECK (order_phase IN (
                     'FIRST_BUY',               -- F3 1차 70%
                     'PYRAMID_BUY',             -- F3 2차 30%
                     'PARTIAL_SELL',            -- F4 1차 익절 50%
                     'CLOSE_SELL',              -- F4 청산 (TRAILING/HARD/BEP)
                     'TIMEOUT_SELL',            -- F5 타임아웃 청산
                     'SLIPPAGE_SELL',           -- F3 슬리피지 즉시 청산
                     'CANCEL'                   -- 주문 취소
                 )),

    -- 주문 내용
    ticker       TEXT NOT NULL,
    order_qty    INTEGER NOT NULL,              -- 주문 수량
    order_price  REAL,                          -- 요청 기준가 (시장가=0)

    -- 체결 결과
    fill_price   REAL,                          -- 실제 체결가
    fill_qty     INTEGER,                       -- 실제 체결 수량
    fill_latency_ms INTEGER,                    -- 주문→체결 소요시간

    -- 상태
    status       TEXT NOT NULL DEFAULT 'PENDING'
                     CHECK (status IN (
                         'PENDING','FILLED','PARTIAL_FILL',
                         'CANCELLED','FAILED'
                     )),

    ordered_at   TEXT NOT NULL,                 -- 주문 시각
    filled_at    TEXT,                          -- 체결 시각 (미체결=NULL)
    error_code   TEXT,                          -- KIS 에러코드
    error_msg    TEXT                           -- KIS 에러메시지
);

CREATE INDEX IF NOT EXISTS idx_orders_trade_id    ON orders(trade_id);
CREATE INDEX IF NOT EXISTS idx_orders_kis_order_id ON orders(kis_order_id);
```

---

### 3-3. `partial_exits` — 1차 익절 상세

F4 `_first_partial_exit()` 실행 시 1행 삽입.

```sql
CREATE TABLE IF NOT EXISTS partial_exits (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id     INTEGER NOT NULL REFERENCES trades(id),
    order_id     INTEGER REFERENCES orders(id),

    exit_price   REAL NOT NULL,
    exit_qty     INTEGER NOT NULL,
    remaining_qty INTEGER NOT NULL,             -- 익절 후 잔여 수량
    pnl_pct      REAL,                          -- 해당 시점 진입가 대비 %

    exited_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_partial_exits_trade_id ON partial_exits(trade_id);
```

---

### 3-4. `daily_skips` — 당일 거래 스킵 이력

거래 없이 스킵된 날 기록. F1 NO_TARGET, 슬리피지 즉시 청산 등.

```sql
CREATE TABLE IF NOT EXISTS daily_skips (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    date       TEXT NOT NULL UNIQUE,            -- 'YYYYMMDD'
    reason     TEXT NOT NULL CHECK (reason IN (
                   'NO_TARGET',                 -- F1 필터 통과 종목 없음
                   'GAP_CHANGED',               -- F3 갭 재검증 실패
                   'ENTRY_FAIL',                -- F3 미체결
                   'SLIPPAGE_GUARD',            -- F3 슬리피지 초과
                   'MANUAL'                     -- 수동 스킵
               )),
    detail     TEXT,                            -- 부가 정보 (JSON 문자열)
    created_at TEXT NOT NULL
);
```

---

## 4. PRAGMA 초기 설정

```sql
PRAGMA journal_mode = WAL;       -- 읽기/쓰기 동시성
PRAGMA synchronous   = NORMAL;   -- WAL에서 안전하며 fsync 부담 감소
PRAGMA foreign_keys  = ON;       -- 참조 무결성 강제
PRAGMA cache_size    = -8000;    -- 8 MB 캐시
```

---

## 5. 주요 쿼리 예시

### 5-1. 최근 30일 승률

```sql
SELECT
    COUNT(*)                                      AS total,
    SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) AS wins,
    ROUND(AVG(pnl_pct), 2)                        AS avg_pnl_pct,
    ROUND(MIN(pnl_pct), 2)                        AS worst,
    ROUND(MAX(pnl_pct), 2)                        AS best
FROM trades
WHERE status = 'CLOSED'
  AND date >= strftime('%Y%m%d', date('now','-30 days'));
```

### 5-2. 청산 사유별 집계

```sql
SELECT close_reason,
       COUNT(*)          AS cnt,
       ROUND(AVG(pnl_pct), 2) AS avg_pnl
FROM trades
WHERE status = 'CLOSED'
GROUP BY close_reason
ORDER BY cnt DESC;
```

### 5-3. 특정 날짜 전체 주문 타임라인

```sql
SELECT o.order_phase, o.order_type, o.fill_price, o.fill_qty,
       o.fill_latency_ms, o.ordered_at, o.filled_at
FROM orders o
JOIN trades t ON t.id = o.trade_id
WHERE t.date = '20260623'
ORDER BY o.ordered_at;
```

### 5-4. 1차 익절 발생률

```sql
SELECT
    COUNT(DISTINCT t.id)              AS total_trades,
    COUNT(DISTINCT pe.trade_id)       AS partial_exit_trades,
    ROUND(
        COUNT(DISTINCT pe.trade_id) * 100.0 / COUNT(DISTINCT t.id), 1
    )                                 AS partial_exit_rate_pct
FROM trades t
LEFT JOIN partial_exits pe ON pe.trade_id = t.id
WHERE t.status = 'CLOSED';
```

---

## 6. Python 모듈 구조

```
src/
└── db.py          ← 단일 모듈 (aiosqlite)
```

```python
# src/db.py — 공개 인터페이스 (예정)

async def init(db_path: str) -> None: ...
    # CREATE TABLE IF NOT EXISTS + PRAGMA 설정

async def open_trade(date: str, ticker: str) -> int: ...
    # trades INSERT → id 반환

async def record_order(trade_id: int, ...) -> int: ...
    # orders INSERT → id 반환

async def update_order_fill(order_id: int, fill_price: float,
                            fill_qty: int, latency_ms: int) -> None: ...

async def record_partial_exit(trade_id: int, order_id: int, ...) -> None: ...

async def close_trade(trade_id: int, exit_price: float,
                      exit_qty: int, close_reason: str, pnl_pct: float) -> None: ...

async def record_skip(date: str, reason: str, detail: dict) -> None: ...
```

---

## 7. 패키지 추가

```text
# requirements.txt 추가
aiosqlite==0.20.0
```

---

## 8. 데이터 흐름 (모듈 → DB)

```
F1  run()               → daily_skips (NO_TARGET)
F2  run()               → (없음, 선택 종목은 state에)
F3  run()               → trades.open_trade()
                           orders.record_order(FIRST_BUY)
                           orders.update_order_fill()
                           orders.record_order(PYRAMID_BUY) [조건부]
                           orders.record_order(SLIPPAGE_SELL) [조건부]
                           daily_skips (SLIPPAGE_GUARD / ENTRY_FAIL)
F4  _first_partial_exit()  → partial_exits.record()
                             orders.record_order(PARTIAL_SELL)
    _execute_close()        → orders.record_order(CLOSE_SELL)
                             trades.close_trade()
F5  execute()           → orders.record_order(TIMEOUT_SELL)
                           trades.close_trade(close_reason='TIMEOUT')
```

---

## 9. 파일 위치 및 백업

| 항목 | 경로 |
|------|------|
| DB 파일 | `data/db/trading.db` |
| WAL 파일 | `data/db/trading.db-wal` (자동 생성) |
| 백업 스크립트 | `scripts/backup_db.py` (미구현) |

> `.gitignore`에 `data/db/` 추가 필요 (실거래 데이터 노출 방지).

---

## 10. 주의사항

1. **aiosqlite 단일 연결** — 멀티스레드 아님. `asyncio` 이벤트 루프 1개에서 단일 `aiosqlite.Connection` 공유.
2. **WAL 체크포인트** — 프로세스 정상 종료 시 자동 체크포인트. 비정상 종료 후 재시작해도 WAL에서 복구됨.
3. **today_state.json 병행 유지** — DB는 분석/이력용. 운영 중 빠른 상태 읽기는 여전히 `state.py` 인메모리 + `today_state.json`.
4. **마이그레이션** — 스키마 변경 시 `ALTER TABLE` 또는 버전 테이블(`schema_version`) 관리 필요 (현재 미구현).
---

## 2026-07-01 기록 정책 업데이트

### DRY_RUN 데이터 분리

- `DRY_RUN=1` 실행 시 운영 DB와 분리된 `DRY_RUN_DB_DIR` 경로를 사용한다.
- 기본값은 `data/dry_run/db`이며, 운영 DB(`data/db/trading.db`)를 오염시키지 않는다.
- DRY_RUN에서 상태 충돌로 F3가 생략되면 `daily_skips`에 `DRY_RUN_F3_SKIPPED` 사유를 기록한다.

### F3 실패 기록

- 진입 주문이 최종 미체결이면 `daily_skips.reason='ENTRY_FAIL'`로 기록한다.
- `detail`에는 가능한 경우 주문번호, 실패 사유, 체결조회 요약을 포함한다.
- 주문 전송 후 미체결이면 실제 미체결 주문 취소 이벤트는 로그(`ENTRY_CANCEL_SENT`)에 남긴다.

### 로그와 DB 역할 구분

- 주문/체결의 영속 기록은 `orders`, `trades`, `daily_skips`가 담당한다.
- 재시도 시도 횟수, 체결조회 타임아웃, KIS 응답 코드처럼 진단용 세부 정보는 JSONL 이벤트 로그에 남긴다.
- UI의 하단 파이프라인 진행 단계는 DB가 아니라 당일 JSONL 로그를 기준으로 계산한다.

### UI 메뉴와 데이터 원천

- 자산 메뉴는 DB가 아니라 KIS 잔고 조회(`/api/assets`)의 현재 스냅샷을 원천으로 삼는다.
- 주문 메뉴는 `orders` 테이블을 주 원천으로 삼고, 체결조회 타임아웃/취소 전송/실패 사유 같은 진단 정보는 JSONL 이벤트 로그를 보조로 사용한다.
- `주문가능금액`은 자산 메뉴 데이터로 분류한다. 주문 메뉴에서 표시할 경우 주문 판단용 참조값으로만 사용한다.
