# 코딩 가이드라인 — 데일리 갭업 자동매매 시스템

> **버전**: 2.0  
> **최종 수정**: 2026-06-23  
> **적용 범위**: `d:\Private\stock\` 프로젝트 전체

---

## 1. 실행 환경

| 항목 | 값 |
|------|-----|
| 런타임 | Python 3.12+ (단일 프로세스) |
| OS | Windows 11 (Task Scheduler로 프로세스 감시) |
| 비동기 | `asyncio` 단일 이벤트 루프 |
| 스케줄러 | `APScheduler` (AsyncIOScheduler) |
| HTTP | `httpx.AsyncClient` |
| WebSocket | `websockets` |
| DB | `aiosqlite` (SQLite WAL 모드) |
| 환경 변수 | `.env` + `python-dotenv` |
| 알림 | Telegram Bot API |

---

## 2. 프로젝트 구조 원칙

```
src/
├── api/          ← KIS REST/WebSocket 래퍼 (I/O 전담)
├── modules/      ← F1~F5 비즈니스 로직 (순수 로직 전담)
├── utils/        ← logger, time_sync (도구)
├── db.py         ← SQLite 연결 싱글톤
├── state.py      ← 인메모리 전역 상태
├── notifier.py   ← Telegram 알림 큐
└── scheduler.py  ← APScheduler 빌드
```

**규칙**: `modules/` 코드는 `api/`를 직접 import하지 않는다.  
I/O(API 호출)와 로직(판단)은 반드시 분리한다.

---

## 3. 비동기 프로그래밍

- 모든 I/O 함수는 `async def`로 작성한다.
- `time.sleep()` 사용 금지 → `asyncio.sleep()` 사용.
- `threading` 사용 금지 → asyncio 태스크로 처리.
- CPU 집약 연산(백테스트 등)만 예외적으로 `run_in_executor` 허용.

```python
# Python / asyncio → 올바른 비동기 패턴
async def fetch_price(ticker: str) -> float:
    async with httpx.AsyncClient() as client:
        resp = await client.get(...)
    return resp.json()["price"]
```

---

## 4. 전역 상태 관리 (state.py)

- 인메모리 상태는 `src/state.py`의 함수를 통해서만 읽고 쓴다.
- F4 틱 핸들러 내부에서 직접 dict를 수정하는 것은 허용하되,  
  **청산 결정은 반드시 `position_status` 확인 후 atomic하게 처리**한다.

```python
# Python / state.py → atomic check-and-set 패턴
async def try_close(reason: str) -> bool:
    if state["position_status"] != "HOLDING":
        return False          # 이미 청산됨 — 중복 주문 방지
    state["position_status"] = "CLOSED"
    state["close_reason"] = reason
    return True
```

**주의**: `position_status` 확인과 `"CLOSED"` 세팅 사이에 `await`를 넣지 않는다.  
asyncio는 단일 스레드이므로 `await` 없는 블록 내에서는 선점이 발생하지 않는다.

---

## 5. DB 접근 (aiosqlite)

- `db.get()`으로 연결을 가져오고, 직접 `aiosqlite.connect()`를 호출하지 않는다.
- 트랜잭션이 필요한 쓰기는 `async with conn.execute(...): await conn.commit()` 패턴.
- SELECT는 `async with conn.execute(...) as cur: rows = await cur.fetchall()`.

```python
# Python / aiosqlite → INSERT 패턴
async def record_trade(ticker: str, entry_price: float) -> int:
    conn = db.get()
    async with conn.execute(
        "INSERT INTO trades (ticker, entry_price) VALUES (?, ?)",
        (ticker, entry_price),
    ) as cur:
        trade_id = cur.lastrowid
    await conn.commit()
    return trade_id
```

---

## 6. F4 Step Trailing 구현 규칙

PRD §3-F4 기준. 아래 수식을 그대로 코드로 옮긴다.

```python
# Python / f4_tracking.py → Step Trailing 핵심 로직
STEP_SIZE  = 0.025   # params에서 로드
STEP_TRAIL = 0.015   # params에서 로드
HARD_STOP  = 0.020   # params에서 로드

def on_tick(price: float) -> None:
    E = state["entry_price"]

    # 스텝 갱신
    pnl = price / E - 1
    s   = max(math.floor(pnl / STEP_SIZE) * STEP_SIZE, 0.0)
    if s > state["highest_step"]:
        state["highest_step"] = s
    if state["highest_step"] >= STEP_SIZE:
        state["trailing_active"] = True

    # 고가 갱신
    if price > state["high_price"]:
        state["high_price"] = price

    # 손절 (trailing 미활성 구간에서만)
    if not state["trailing_active"] and price <= E * (1 - HARD_STOP):
        asyncio.create_task(_close("HARD_STOP"))
        return

    # 익절 (Step Trailing)
    if state["trailing_active"]:
        stop = E * (1 + state["highest_step"] - STEP_TRAIL)
        if price <= stop:
            asyncio.create_task(_close("TRAILING"))
```

**규칙**:
- `highest_step`은 절대 감소시키지 않는다.
- `stop_price`는 `high_price`가 아닌 `highest_step`에서 계산한다.
- 청산 주문은 `asyncio.create_task()`로 분리한다 (틱 핸들러 블로킹 금지).

---

## 7. 주문 전송 규칙

- 모든 매도 주문 전송 전에 `state["position_status"] == "HOLDING"` 확인.
- 주문 ID는 `state["order_id"]`에 저장, 중복 전송 방지에 활용.
- 체결 확인은 KIS REST API 폴링 또는 WebSocket 체결 이벤트로 수행.
- 미체결 상태로 `FILLED_TIMEOUT_MS`(기본 3,000ms) 초과 시 재시도 로직 진입.

---

## 8. 환경 변수

- 모든 민감 정보는 `.env`에 저장, 코드에 하드코딩 금지.
- 코드 내에서는 `os.getenv("KEY", "default")` 패턴으로 접근.
- `.env`는 `.gitignore`에 포함, `.env.example`에 키 목록만 유지.

```python
# Python → 환경 변수 접근 패턴
KIS_APP_KEY = os.getenv("KIS_APP_KEY")
if not KIS_APP_KEY:
    raise EnvironmentError("KIS_APP_KEY 미설정")
```

---

## 9. 로깅 규칙

- 모든 이벤트 로그는 `src/utils/logger.py`의 `log()` 함수를 통한다.
- 형식: `data/logs/YYYYMMDD.jsonl` (1일 1파일, JSONL)
- 로그 레벨: `INFO`, `WARN`, `CRIT`
- 거래 관련 이벤트는 PRD §5의 이벤트 코드를 그대로 사용한다.

```python
# Python / logger.py → 로그 작성 패턴
logger.log("TRAILING_STOP", level="INFO",
           ticker=ticker, exit_price=price,
           highest_step=state["highest_step"],
           stop_price=stop, pnl_pct=pnl_pct)
```

---

## 10. 에러 핸들링

- KIS API 호출은 `try/except` + 재시도 (최대 3회, 지수 백오프).
- 청산 주문 실패(`SELL_ORDER_REJECTED`)는 즉시 CRIT 알림 + 수동 처리 요청.
- 처리되지 않은 예외가 `main()` 이벤트 루프를 죽이지 않도록  
  `asyncio.create_task()` 결과에 `.add_done_callback(handle_exception)` 부착.

```python
# Python / asyncio → 태스크 예외 누락 방지 패턴
def _guard(task: asyncio.Task) -> None:
    if task.exception():
        logger.log("UNHANDLED_TASK_ERROR", level="CRIT",
                   error=str(task.exception()))

task = asyncio.create_task(some_coro())
task.add_done_callback(_guard)
```

---

## 11. 코드 컨벤션

| 항목 | 규칙 |
|------|------|
| 포매터 | `ruff format` (Black 호환, 줄 길이 100) |
| 린터 | `ruff check` |
| 타입 힌트 | 모든 공개 함수에 필수 |
| 주석 | WHY가 불명확한 곳에만 한 줄. 코드 설명 주석 금지 |
| 상수 | 모듈 상단 `UPPER_SNAKE_CASE`로 선언 |
| 매직 넘버 | 직접 사용 금지 — 상수 또는 params로 분리 |

---

## 12. 파라미터 관리

- 전략 파라미터(`STEP_SIZE`, `STEP_TRAIL`, `HARD_STOP` 등)는  
  `data/params/params.json`에서 로드한다.
- 코드에 기본값을 하드코딩하는 것은 `params.json` 부재 시 폴백용으로만 허용.
- 파라미터 변경 이력은 `data/params/history.json`에 버전으로 기록한다.

---

## 13. 테스트 가이드라인

- `pytest` 사용, `tests/` 디렉터리에 모듈별 파일 작성.
- KIS API 호출은 반드시 `unittest.mock.AsyncMock`으로 대체한다.
- F4 Step Trailing 로직은 틱 시퀀스 단위 유닛 테스트를 필수로 작성한다.

```python
# Python / pytest → Step Trailing 유닛 테스트 패턴
def test_step_trailing_triggers_at_stop():
    state["entry_price"]  = 100_000
    state["highest_step"] = 0.075   # +7.5% 스텝 달성 상태
    state["trailing_active"] = True
    # stop = 100_000 × (1 + 0.075 - 0.015) = 106_000
    on_tick(106_001)   # 발동 안 됨
    on_tick(105_999)   # 발동
    assert state["close_reason"] == "TRAILING"
```

---

## 14. 금지 사항

| 금지 | 이유 |
|------|------|
| `pandas`, `numpy` import (거래 루프 내) | 틱 처리 지연 |
| `time.sleep()` | 이벤트 루프 블로킹 |
| `threading.Thread` | asyncio와 충돌 가능 |
| `print()` (로거 대체) | 로그 누락 |
| DB 직접 `aiosqlite.connect()` | 연결 누수 |
| 청산 주문 중복 전송 | 이중 청산 |
| `state["position_status"]` 직접 문자열 비교 없이 주문 전송 | 중복 청산 |
