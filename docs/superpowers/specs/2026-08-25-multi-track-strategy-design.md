# 멀티 트랙 전략 병행 운용 설계

> **상태**: 설계 확정 — 트랙 모델·DB 스키마·집계 스코프(§3 중 §3.4 제외, §4, §6.1) 구현 완료.
> §3.4(트랙 B 전용 `SpikeFilter` 인스턴스)는 트랙 B 틱 소비자가 아직 없어 미구현. 그림자·승격 대기
> **작성일**: 2026-08-25
> **갱신일**: 2026-08-26 (전체 브랜치 리뷰 반영 — 마이그레이션 FK 게이트 수정·준비도 게이트 트랙 스코프,
> 지문 회전 `d4435896a8a2`→`40d999a0ab66`)
> **관련 문서**: [PRD.md](../../PRD.md), [DB_DESIGN.md](../../DB_DESIGN.md), [UI_DESIGN.md](../../UI_DESIGN.md)

## 1. 목표

F1 종목 선정 프로세스를 그대로 공유하면서, 선정된 **같은 종목**에 대해 기존 전략(트랙 A)과
새 전략(트랙 B)을 **같은 계좌·같은 프로세스**에서 병행 운용한다. 자본은 개장 전에 고정
비율로 분배하며, 목표 배분은 50:50이다. 다만 실제 비율은 트랙 B의 승격 단계를 따른다
(SHADOW 0% → PILOT 5~10% → FULL 50%, §5.1·§6.3).

트랙 B의 매매 규칙은 이 스펙의 범위가 아니다. 이 스펙은 **N개 전략 트랙을 안전하게 병행
운용하는 골격**과 **봉/지표 데이터 계층**을 정의하고, 규칙은 `strategy_configs`에 설정으로
꽂는다.

## 2. 확정된 전제

| 항목 | 결정 |
|---|---|
| 프로세스 분리 | **하지 않는다.** 유량 제한이 앱 키 단위이고 `_rate_waiters`가 프로세스 내 전역 큐 |
| 계좌 분리 | 하지 않는다. 단일 계좌 |
| 종목 | **두 트랙이 같은 종목**을 매매한다 |
| 매수 | 조건이 서로 다르다. **주문 2건**. B가 매수하지 않는 날이 있다 |
| 미사용 자본 | 트랙 B 미진입 시 배정분은 **현금으로 둔다** (고정 분배) |
| 실행 계층 | **안 C** — 기존 F3/F4/F5는 트랙 A 전용으로 동결, 트랙 B는 신규 모듈 |
| B의 입력 | **봉/지표** (캔들, 이동평균, MACD 등) |
| B의 진입 시각 | **09:35 이후.** 지표 정확도를 우선한다 |
| B의 시작 단계 | **SHADOW** (자본 0·주문 0) |

### 2.1 안 C를 택한 이유

기존 `src/modules/f3_entry.py`는 4,000줄이 넘고 `recover_pending_entry()`, VI 대기, 슬리피지
가드 등 사고를 겪으며 쌓인 복구 로직 덩어리다. 실계좌가 도는 봇에서 이를 전면 리팩터링하는
비용과 위험이 이득을 넘는다.

또한 트랙 B의 전략은 아직 정해지지 않았고 실험하며 정한다. 공유 코드였다면 B를 만질
때마다 A가 위험해진다. **격리가 실험 속도를 높인다.**

트랙 B가 A의 변형이 아니라 완전히 새로운 로직이므로 공유할 코드가 거의 없다 — 분리
비용이 사실상 0이다.

## 3. 섹션 ① — 트랙 모델과 관측 계층

### 3.1 상태 구조

기존 `state.get()`은 **트랙 A 전용으로 유지**한다. F3/F4/F5의 호출부가 무변경이다.

```
state.get()            → 트랙 A (현행 State, 무변경)
state.track('B')       → 트랙 B (신규 TrackState)
state.all_tracks()     → 감사·UI용 순회
```

`TrackState`는 `State`에서 트랙 무관 필드(`trading_date`, `target_ticker`,
`target_candidates`, `day_skip`)를 제외한 포지션 관련 필드만 갖는다. 종목과 후보는 F1/F2가
정하는 공유 자산이다.

### 3.2 영속화 — 하위호환 필수

`today_state.json` 한 파일을 유지하되 섹션을 추가한다.

```json
{
  "date": "20260825",
  "ticker": "005930",
  "...기존 트랙 A 필드는 최상위 유지...",
  "tracks": { "B": { "position_status": "IDLE", "entry_price": null } }
}
```

기존 필드를 최상위에 남기는 이유: `src/state.py`의 `restore_from()`이 **구버전 파일을 그대로
읽을 수 있어야** 한다. 재시작 복구 중 상태 파일을 못 읽으면 실포지션을 잃는다. `tracks` 키가
없으면 B는 IDLE로 간주한다.

### 3.3 관측 계층 중립화

현재 `live.push_tick()`은 `f4_tracking.py:742`에서 `position_status != "HOLDING"` 게이트보다
**앞에서** 호출된다. 따라서 틱 팬아웃 지점은 이미 열려 있다.

그러나 상위 게이트 두 곳이 관측을 트랙 A에 종속시킨다.

1. `_price_observation_active()` (f4_tracking.py:134) — `state.get()`(트랙 A)을 읽어 A가
   HOLDING/CLOSED가 아니면 False를 반환한다
2. `run()`의 진입 대기 루프 (f4_tracking.py:339) — A가 HOLDING/CLOSED가 될 때까지 구독을
   시작하지 않는다

**결과: A가 진입하지 않은 날에는 WS 구독도 틱 방송도 일어나지 않아 B가 굶는다.** 하필
"A는 못 샀는데 B는 살 수 있는 날"이 두 전략을 비교하는 가장 의미 있는 날이다.

#### 변경 내용

| 대상 | 변경 |
|---|---|
| `_price_observation_active()` | 게이트를 "A가 보유 중"에서 "**종목이 확정됐고 관측 시간대**"로 |
| `run()` 진입 대기 루프 | "A가 HOLDING"에서 "**종목 확정**"으로 |
| `_rest_backup_allowed()` | IDLE/ENTERING에서 REST 백업 폴링 억제 (§3.5) |
| `_should_attach_capture()` | 체결이 없어도 캡처 부착 (§3.6) |
| `_observation_should_continue()` | 종목이 바뀌면 구독 종료 (§3.7) |

CLOSED 사후 관측 판정(`entry_at` 검증·경고·당일 대조·캡처 연장 컷오프)은 **원본 그대로
보존한다.** 이 변경은 "미진입일 관측"만 여는 것이고, 청산 후 동작까지 바꾸면
`test_f4_capture_wiring`의 기존 계약이 깨진다.

**A의 매매 판단은 무변경이다.** `_process_tick()`(청산 판정)은 `position_status != "HOLDING"`
게이트(f4_tracking.py:761) 뒤에 그대로 남고, `_trigger_close`·`_execute_close`·
`recover_pending_exit`은 손대지 않는다. 넓어지는 것은 관측 부작용뿐이다.

- `live.push_tick` — UI 가격흐름 차트가 진입 전 구간도 그린다 (개선)
- `tick_capture.enqueue` — 캡처 데이터 증가 (§3.6)

#### 대안을 택하지 않은 이유

| 방법 | A에 미치는 영향 |
|---|---|
| **관측 게이트 중립화** (채택) | 매매 판단 경로 무변경. 관측 창만 확대 |
| B가 별도 WS 연결 | 같은 앱키로 연결 2개. 접속키·구독 슬롯 경합 위험 |
| B가 REST 폴링 | PAPER 초당 1건을 A의 진입 경로와 경합. 가장 간섭적 |

### 3.4 스파이크 필터

`live.push_tick`은 스파이크 필터 **이전**에 호출된다(필터는 `_process_tick` 내부
f4_tracking.py:845). B는 원시 틱을 받으므로 **자체 `SpikeFilter` 인스턴스**를 가져야 한다.
공유하면 A의 필터 내부 상태가 오염된다.

### 3.5 유량 가드 — 미보유 구간에서는 REST 백업을 돌리지 않는다

`should_poll_rest()`의 마지막 줄
(`return not F4_REST_ONLY_WHEN_WS_STALE or is_ws_stale()`)이 IDLE·ENTERING·HOLDING·
EXITING을 전부 담당한다. 관측이 HOLDING일 때만 열리던 동안에는 IDLE/ENTERING이 이 줄에
도달하지 못했다.

§3.3으로 관측 창이 종목 확정 시점부터 열리면 A가 진입하지 않은 날에 IDLE 상태로 폴링
루프에 도달한다. WS가 끊기면 `is_ws_stale()`이 True가 되고, 폴링 간격은 **보유 등급인
`F4_REST_POLL_INTERVAL_SEC=1.0`**, 우선순위도 BACKGROUND가 아닌 기본값이다. **PAPER 초당
1건 예산을 100% 소모하고 그날 늦은 A의 진입까지 막는다.**

가드: `position_status in ("IDLE", "ENTERING")`이면 폴링하지 않는다. REST 백업의 목적은
WS 장애 시 **손절 추적 보호**인데 보유가 없으면 보호할 손절이 없다. WS가 죽은 날 관측
데이터가 일부 비는 편이 A의 진입을 위협하는 것보다 낫다. EXITING은 기존대로 폴링한다.

### 3.6 거래 없는 날의 durable 캡처

캡처 부착이 `if s.trade_id and s.position_status in ("HOLDING", "CLOSED")`로 걸려 있었다.
**거래가 없으면 `trade_id`가 없어 캡처가 시작되지 않는다** — 관측 창만 열면 틱이 `live`로
흘러 B가 실시간으로 쓸 수는 있지만 디스크에 남지 않는다. 데이터 수집이 목적이므로 §3.3만
으로는 목적을 달성하지 못한다.

하부는 이미 준비돼 있다.

- `tick_capture.attach_or_resume(..., trade_id: int | None, ...)` — 이미 Optional
- `price_path_manifests.trade_id INTEGER` — nullable
- `UNIQUE (trade_date, ticker, experiment_id)` — trade_id 없이도 식별 가능

부착 조건을 `bool(s.target_ticker)`로 완화하고 `trade_id`는 `s.trade_id or None`으로 넘긴다.

### 3.7 종목 교체 시 재구독 — 필수

관측이 F2 잠금 시점부터 시작되면서 **F3의 후보 교체가 이미 떠 있는 구독보다 나중에 일어날
수 있게 됐다.** `f3_entry.py`의 후보 재시도 루프는 `s.target_ticker = picked["ticker"]`로
대상을 바꾼다.

```
F2가 X를 잠금  →  F4가 X 구독 시작
F3가 X 거부 → Y로 교체해 체결
F4는 여전히 X 구독 중
→ _process_tick이 X의 가격을 Y의 entry_price와 비교해 손절·트레일링 판정
```

`SpikeFilter.is_valid(price, ticker)`는 `ticker`를 **로깅에만** 쓰므로(`spike_filter.py:33`)
걸러주지 않는다. §3.3 이전에는 `run()`이 HOLDING을 기다렸고 그 시점엔 종목이 확정돼 있어
발생할 수 없던 문제다 — **§3.3이 여는 창이므로 §3.3과 반드시 함께 구현한다.**

`_observation_should_continue(ticker)`가 관측 창과 종목 일치를 함께 판정하고 두 곳에 건다.

1. `kis_ws.subscribe`의 `stop_if` — 종목이 바뀌면 구독 종료 → `run_forever`가 재구독
2. `_handle_price_tick` 진입 가드 — 재구독 전 도착한 낡은 틱이 청산 판정·차트·캡처
   어디에도 들어가지 않는다

## 4. 섹션 ② — DB 스키마와 마이그레이션

### 4.1 스키마 변경

```sql
trades.track TEXT NOT NULL DEFAULT 'A'
UNIQUE (date)  →  UNIQUE (date, track)
```

`daily_skips.date UNIQUE`(db.py:140)도 같은 처리가 필요하다. B가 조건 미충족으로 스킵한
이유를 남겨야 실험 분석이 된다.

`track`과 `experiment_id`는 **다른 축**이므로 컬럼을 분리한다.

- `experiment_id` — 어떤 전략 설정으로 돌았나 (시간 축)
- `track` — 어느 동시 실행 슬롯인가 (동시성 축)

### 4.2 API 하위호환

프로덕션 호출부는 다음이 전부다.

| 함수 | 호출부 |
|---|---|
| `db.open_trade` | `f3_entry.py:1161`, `f3_entry.py:2180` |
| `db.get_trade_by_date` | `f3_entry.py:1152`, `f3_entry.py:2705`, `main.py:307`, `main.py:1078` |

시그니처에 `track: str = "A"` 기본값을 추가하면 **6개 호출부 전부 무변경**이다.

`open_trade()`의 멱등성(`IntegrityError` 포착 후 기존 거래 재사용, db.py:485)은 UNIQUE가
`(date, track)`이 되면 트랙별 멱등성으로 자연히 확장된다. 단, except 절의
`get_trade_by_date(date)`도 **같은 track으로 조회**해야 한다. 그러지 않으면 B의 충돌에서
A의 거래를 반환한다.

### 4.3 마이그레이션 — 이 섹션의 최대 위험

SQLite는 `CREATE TABLE`의 UNIQUE를 `ALTER`로 제거할 수 없어 **테이블 재작성**이 필요하다.
`trades`는 FK 참조가 셋이고 그중 하나가 `ON DELETE CASCADE`다.

```
PRAGMA foreign_keys = ON                                    -- db.py:24
orders.trade_id                REFERENCES trades(id)        -- db.py:60
partial_exits.trade_id         REFERENCES trades(id)        -- db.py:127
trailing_shadow_comparisons    REFERENCES trades(id) ON DELETE CASCADE  -- db.py:169
```

SQLite는 FK가 켜진 상태의 `DROP TABLE`을 암묵적 `DELETE`로 처리하므로 **CASCADE가 실제로
발화**해 청산 비교 이력이 삭제될 수 있다.

#### 절차

```sql
PRAGMA foreign_key_check;          -- (1) 재작성 전 위반 다중집합을 떠 둔다
PRAGMA foreign_keys = OFF;         -- 트랜잭션 안에서는 no-op이라 BEGIN 밖에서
BEGIN;
  CREATE TABLE trades_new (... 기존 컬럼 전부 + track, UNIQUE(date, track));
  INSERT INTO trades_new (id, date, ticker, name, ..., experiment_id, track)
       SELECT             id, date, ticker, name, ..., experiment_id, 'A' FROM trades;
  DROP TABLE trades;
  ALTER TABLE trades_new RENAME TO trades;
  CREATE INDEX idx_trades_date ON trades(date);
  PRAGMA foreign_key_check;        -- (2) COMMIT '이전'. (1)보다 늘었으면 ROLLBACK
COMMIT;                            -- 또는 ROLLBACK
PRAGMA foreign_keys = ON;          -- 예외가 나도 반드시 되돌린다(try/finally)
```

`id`를 보존해 넣으므로 FK 참조는 그대로 유효하다.

#### 필수 안전장치

1. **마이그레이션 직전 DB 파일 백업 복사.** 타협 대상이 아니다. WAL을 먼저 접되
   `wal_checkpoint`의 `busy`를 로그에 남긴다 — `busy=1`이면 백업이 (유효하지만) 조금 오래된
   스냅샷이다
2. **필요할 때만 실행.** `sqlite_master`의 `sql`에 `track`이 없을 때만. `daily_skips`
   재구축과 같은 감지 패턴
3. **`foreign_key_check`는 COMMIT 이전에.** 커밋 뒤에 검사하면 위반을 막지 못하고 이미
   durable해진 사실을 보고할 뿐이다. 더구나 조기 반환(2) 때문에 재기동 시에는 검사 자체가
   다시 돌지 않아, "백업으로 복원하라"는 안내가 같은 실패를 되풀이시킨다
4. **판정 기준은 "위반이 있는가"가 아니라 "위반이 늘었는가".** 인자 없는
   `foreign_key_check`는 DB 전체를 훑으므로 이 마이그레이션과 무관한 테이블의 고아까지
   잡힌다. 재작성은 `id`를 보존하므로 위반을 새로 만들 수 없고, 발견되는 위반은 FK 강제
   이전에 쓰인 구 DB의 잔재다. 그대로면 WARN(백업 복원으로 해결되지 않는다는 문구 포함),
   늘어났을 때만 ROLLBACK 후 `RuntimeError`로 기동 중단

#### 컬럼 순서 함정 — `SELECT *` 금지

`name`은 신규 DB에서는 4번째 컬럼이지만, 구 DB에서는
`ALTER TABLE trades ADD COLUMN name`(db.py:262)으로 **맨 뒤에** 붙어 있다.
`highest_step`(db.py:258)도 같다.

따라서 `INSERT INTO trades_new SELECT * FROM trades`는 구 DB의 컬럼을 **조용히 뒤섞는다.**
타입이 호환되는 자리(`name TEXT` ↔ `close_reason TEXT`)면 에러도 나지 않는다. **컬럼명을
명시적으로 나열해야 한다.**

참고: `execution_mode`와 `strategy_fingerprint`는 `CREATE TABLE` 안에 있다(db.py:50-51).
`ALTER`로 붙는 것은 `experiment_id`(db.py:276)와 구 DB 보정용 `highest_step`·`name`뿐이다.

### 4.4 교차 오염 수정 — `get_unresolved_exit_intent`

`db.py:757`은 `date`로만 조인하고 가장 최근 것 하나를 반환한다.

```sql
JOIN trades t ON t.id=o.trade_id
WHERE t.date=? AND o.order_type='SELL' AND o.status IN ('PENDING','PARTIAL_FILL')
ORDER BY o.id DESC LIMIT 1
```

노출 범위는 `main.py:735`의 가드로 좁혀진다. A가 IDLE이면 호출되지 않고, A에 `pending_exit`
dict가 있으면 `exit_recovery.py:92`의 `client_order_id` 비교에서 걸러진다.

**남는 구멍**: `position_status == "EXITING"`이면서 `pending_exit`가 `None`인 상태 —
`state.clear_pending_exit()`이 "다음 재시도를 허용"하려고 만드는 상태다. 이때
`not isinstance(pending, dict)`가 참이 되어 **B의 주문을 무조건 채택**한다.

즉 **A의 청산 재시도 창에서 재시작이 겹치면 A가 B의 매도 주문을 자기 것으로 인수한다.**

수정: `get_unresolved_exit_intent(date, track="A")` — 조인에 `AND t.track=?` 추가,
`merge_db_intent(data, date, track)`로 전파. 호출부는 `main.py:738` 한 곳이다.

### 4.5 `close_reason` CHECK 확장

`db.py:39`의 CHECK는 6개(`TRAILING`, `HARD_STOP`, `TIMEOUT`, `SLIPPAGE_GUARD`, `ENTRY_FAIL`,
`MANUAL`)로 고정이다. 트랙 B가 새 청산 사유를 쓰면 INSERT가 실패한다. 이 코드베이스는 이미
같은 함정을 겪었다 — db.py:308 주석: *"record_skip이 INSERT OR IGNORE라 구 제약에 걸리면
에러 없이 기록만 누락되기 때문에 반드시 맞춰야 한다."* 테이블 재작성 시 함께 넓힌다.

### 4.6 안전 확인된 항목

- `trades` UPDATE 3곳(`mark_pyramided`, `update_trade_progress`, `close_trade`)은 전부
  `WHERE id=?` 기준 — 트랙 안전
- `get_order_by_kis_id`는 **KIS가 주문번호를 발급**하므로 트랙 안전이다. 한 계좌 안의 두
  트랙이 같은 `odno`를 받을 수 없어 트랙 필터 없이도 교차 오염이 생기지 않는다.
  *유니크 인덱스 때문이 아니다* — `db.py`의 `idx_orders_kis_order_id`는 UNIQUE가 아닌
  평범한 인덱스이고(`UNIQUE (date, kis_order_id)`는 `entry_order_attempts`의 제약이다),
  쿼리의 `ORDER BY o.id DESC LIMIT 1` 자체가 중복 행을 전제한다.
  **전제**: 앞으로 `kis_order_id`를 로컬에서 합성하거나 재사용하는 코드가 생기면 이
  안전성은 깨지므로 그때는 `track` 필터가 필수가 된다
- `price_path_manifests`의 `UNIQUE (trade_date, ticker, experiment_id)`(db.py:253)는 B가 다른
  `experiment_id`를 쓰는 한 충돌하지 않는다. **A와 B가 같은 `experiment_id`를 쓰는 구성은
  기동 시 검증으로 금지한다**

### 4.7 실전 전환 게이트는 트랙 A 전용

`readiness._clean_paper_trade_count()`는 `KIS_MODE=REAL` 허용 여부를 정하는 돈 게이트다
(하한 20건, 운영자가 낮출 수 없다). 트랙 B가 `trades`에 쓰기 시작하면 B의 PAPER 실적이
A의 실탄 자격으로 흘러들 수 있으므로 쿼리에 `AND t.track='A'`를 **리터럴로** 박는다.
파라미터가 아닌 이유는 이 게이트가 트랙 A 실행 경로의 자격만을 판정하기 때문이다.
트랙 B의 실탄 승격 기준은 §6.3 승격 단계가 따로 정한다.

`src/readiness.py`는 `release._STRATEGY_FILES`에 없으므로 이 수정은 전략 지문을 바꾸지 않는다.

---

## 5. 섹션 ③ — 예산 분배와 불변식 감사

### 5.1 예산 선분배

`F3_ALLOC_RATIO`(기본 0.95, f3_entry.py:61)는 **주문가능현금 대비 안전마진**이지 트랙 배분
비율이 아니다. 두 개념을 섞지 않는다.

```
가용현금 → × F3_ALLOC_RATIO(0.95, 안전마진) → 트랙 예산 풀
        → × TRACK_WEIGHT[B] → 트랙 B 예산 (동결)
        → 나머지 전부        → 트랙 A 예산 (동결)
```

**`TRACK_WEIGHT[B]`는 고정값이 아니라 트랙 B의 현재 승격 단계에서 나온다**(§6.3).

| B의 단계 | `TRACK_WEIGHT[B]` | 트랙 A 배정 |
|---|---|---|
| SHADOW | 0.0 | 예산 풀 전액 — **현행과 동일** |
| PILOT | 0.05 ~ 0.10 | 나머지 |
| FULL | 0.50 | 나머지 |

이 순서(B를 먼저 떼고 나머지를 A에게)가 중요하다. SHADOW 단계에서 트랙 A의 예산이 **현재와
정확히 같아야** 하기 때문이다. 그래야 그림자 기간 동안 A의 성과가 변하지 않고, 트랙 도입
자체가 A에 영향을 주지 않았음을 관측으로 확인할 수 있다.

분배 시점은 **08:59 현금 스냅샷**(`f3_entry.prepare_available_cash_snapshot`, f3_entry.py:458)
이다. 여기서 트랙별 금액을 확정해 동결하면 조회 순서에 따른 경합이 사라진다.

`_fetch_buyable_qty()`는 계좌 전체 기준이라 트랙을 모른다. **상한선으로만** 쓴다.

```
주문수량 = min(트랙예산 기준 수량, buyable)
```

트랙 B가 미진입한 날 배정분은 **현금으로 둔다.** 트랙 A가 흡수하지 않는다 — 흡수하려면 B의
판정을 기다려야 하고, 이는 09시 최속 진입 목표와 충돌하며 날마다 A의 자본이 달라져 성과
비교가 오염된다.

### 5.2 불변식

장부는 새 저장소가 아니라 `orders`의 **파생 뷰**다.

```
장부수량(track) = Σ(BUY fill_qty) − Σ(SELL fill_qty)     -- 그 트랙의 trade_id 소속
불변식:          브로커 hldg_qty(ticker) == Σ_track 장부수량(track)
```

`orders.trade_id`가 `NOT NULL`(db.py:60)이고 `trades`가 트랙당 1행이므로 귀속이 자동으로
성립한다. 재시작 후에도 DB에서 재구성되며, **브로커 총량은 진실의 원천이 아니라 감사
대조값**이다.

검사 시점:

1. 기동 직후 — 재시작 복구 마지막 단계
2. 각 트랙 매도 직전 — 팔려는 수량이 자기 장부 범위 안인지
3. 주기적 — 관측 창 동안 저빈도

### 5.3 위반 시 비대칭 정책

전량 즉시 청산은 채택하지 않는다. 불변식 검사의 입력인 잔고 조회 자체가 불확실하기
때문이다 — 페이지네이션(`_BALANCE_MAX_PAGES = 10`, main.py:66), 체결 반영 지연, 부분체결
진행 중. 전량 청산은 **일시적 스큐를 영구적 손실로 바꾼다.** 09시 급등주를 정상 보유 중인데
조회 타이밍 때문에 전량 시장가 청산되면 이 전략의 핵심 수익 구간을 통째로 잃는다.

이 코드베이스는 이미 잔고 조회의 불확실성을 규약으로 인정하고 있다 —
`state.reset_stale_active_for_trading_day()` 독스트링: *"It must only be called after the
complete paginated balance response has confirmed that the state's ticker is not held."*

| 상황 | 조치 |
|---|---|
| 1차 감지 | **완전 페이지네이션 재조회.** 조치 없음 |
| 재확인에서 해소 | 로그만 남기고 정상 운용 |
| **브로커 < 장부** | 신규 진입 동결 + 클램프 청산 계속 + 오염 플래그 |
| **브로커 > 장부** | **초과분만 즉시 청산** + 신규 진입 동결 |
| 어느 쪽이든 | CRIT 알림 |

**방향별 위험이 다르기 때문에 대응도 다르다.**

- **브로커 < 장부** — 누군가 이미 팔았다. 최악은 "없는 걸 팔려다 주문 거부"이고 실질 금전
  손실은 없다. 매도 수량을 `min(장부수량, 브로커 실제수량)`으로 클램프한다. 선착순으로
  잠식되는 것은 감수하되, 잠식당한 트랙의 거래에 **오염 플래그**를 남겨 실험 분석에서
  제외한다
- **브로커 > 장부** — 미기록 매수 체결 가능성. **어느 트랙도 관리하지 않는 포지션이
  하드스탑 없이 방치**되고 최악의 경우 오버나이트로 넘어간다. 초과분만 즉시 청산한다

청산 자체를 막지 않는 이유: 청산을 막으면 손절이 사라져 더 위험하다.

### 5.4 UNCERTAIN 체결

`entry_order_attempts.status = 'UNCERTAIN'`(db.py:110)은 체결 여부 자체가 미확정인 상태다.
총량도 분할도 모른다. **해당 트랙만 동결**하고 나머지 트랙은 정상 진행한다 — 주문 식별자가
트랙별로 분리돼 있으므로 오염되지 않는다.

## 6. 섹션 ④ — 그림자 → 실자본 승격

### 6.1 집계 쿼리 트랙 스코프 (필수 선행)

`server.py:977`의 `/api/stats`와 `server.py:954`의 `/api/history`는 **트랙 필터도
`execution_mode` 필터도 없다.**

```sql
SELECT COUNT(*), SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) FROM trades WHERE status='CLOSED'
SELECT date, ticker, ... FROM trades ORDER BY date DESC LIMIT ?
```

트랙 B가 생기는 순간 승률·평균손익이 두 전략의 혼합값이 되고, 이력 화면은 같은 날짜 행이
둘씩 나오는데 구분할 방법이 없다. **A/B 비교가 목적인데 통계가 비교를 지운다.** 모든 집계에
트랙 스코프를 명시한다.

### 6.2 그림자 거래는 `trades`에 넣지 않는다

`execution_mode='SHADOW'`로 같은 테이블에 넣지 않는다. 필터 누락의 결과가 비대칭이기
때문이다.

- 트랙 B **실거래** 필터 누락 → "합쳐진 진짜 통계". 오해를 부르지만 실재한 거래
- **그림자** 필터 누락 → **일어나지도 않은 거래가 손익에 섞인다.** 조용히 오염되고 발견이
  늦다

별도 테이블 **`shadow_trades`** 를 쓴다. 스키마는 `trades`와 유사하되 주문 관련 컬럼이 없고,
대신 판정 근거(신호 발생 시각, 판정 가격, 가정한 체결가)를 남긴다. 실자본 손익에 섞일 경로가
**구조적으로** 존재하지 않는다.

승격 시 데이터를 옮기지 않는다. 그림자 기간은 그림자 기록으로, 실자본 기간은 `trades`로
남는다 — 성질이 다른 데이터이므로 합치는 것이 오히려 틀리다.

기존 `trailing_shadow_comparisons`(db.py:167)가 이미 "대안 규칙을 그림자로 돌려 비교 기록"
하는 패턴을 쓰고 있다. 같은 사고방식의 확장이다.

### 6.3 3단계 승격

그림자에는 **구조적 낙관 편향**이 있다 — 원하는 가격에 원하는 수량이 체결된다고 가정한다.
이 봇의 전략에서는 **체결 자체가 가장 어려운 부분**이다(F3의 슬리피지 가드·갭 재평가·VI
대기·호가 신선도 검사가 그 증거다). 그림자는 이 난이도를 측정하지 못한다.

트랙 B가 A의 변형이 아니라 완전히 새로운 로직이므로 A가 쌓아온 슬리피지 지식을 빌려올 수
없다. B의 진입 시점(09:35 이후)은 A(09:00~09:11)와 완전히 다른 체결 환경이다.

| 단계 | 자본 | 검증 대상 | 전환 근거 |
|---|---|---|---|
| **1. SHADOW** | 0% | 신호 로직, 발생 빈도, 가정 손익 | 신호가 실제로 발생하는가, 빈도가 분석 가능한가 |
| **2. PILOT** | 5~10% | **체결 현실** — 슬리피지, 미체결, 진입 지연 | 가정 손익 대비 실현 손익의 괴리가 설명 가능한가 |
| **3. FULL** | 50% | 목표 운용 | |

PILOT이 측정하는 것은 수익이 아니라 **그림자 가정과 현실의 차이**다. 이 값이 크면 그림자
기록 전체를 재해석해야 하므로 표본이 적어도 즉시 유의미하다.

### 6.4 승격 게이트는 코드로 잠근다

`app.js:1245`의 `sampleNote()`가 쓰는 기존 기준을 승격 게이트로 쓴다.

- 5건 미만 — 전략 변경 보류
- 20건 미만 — 경향 확인 단계
- **20건 이상 — 전략 비교에 사용 가능**

**표본 20건 미만이면 성과가 좋아도 승격을 거부한다.** 사람의 판단에 맡기면 잘 나온 며칠
뒤에 올리고 싶어진다. 트랙 B는 검증된 전략의 파라미터 변형이 아니라 완전히 새로운 로직이므로
얕은 표본의 위험이 특히 크다.

### 6.5 승격·강등은 코드 배포 없이

기존 스키마를 그대로 쓴다.

```
strategy_configs.kind:      PRIMARY | EXPLORATORY | ACTIVE | RETIRED     -- db.py:201
experiment_registry.status: ACTIVE | PAUSED_DATA | PAUSED_RISK
                            | ROLLED_BACK | RETIRED                       -- db.py:208
experiment_registry.stop_conditions, safety_limits
```

`PAUSED_RISK`와 `ROLLED_BACK`이 자동 강등의 목적지다. 강등 조건(연속 손실, 일일 손실 한도,
그림자 대비 괴리 초과)을 `stop_conditions`에 넣고, 위반 시 트랙 B를 자동으로 SHADOW로
되돌린다. 트랙 A는 영향받지 않는다.

강등은 **신규 진입만 막고 보유 포지션은 자기 규칙대로 청산**시킨다 — §5.3과 같은 이유다.

## 7. 섹션 ⑤ — 봉/지표 계층

### 7.1 제약: 지표는 09시에 설 수 없다

MACD(12,26,9)를 1분봉으로 계산하면 EMA26이 의미를 갖는 데 최소 26봉, 시그널선까지 35봉
이상이 필요하다. 09:00 시작이면 **09:35 전후에야 유효한 신호**가 나온다. 트랙 A의 진입 창은
09:11에 닫힌다(`F3_ENTRY_RETRY_DEADLINE=09:11:00` f3_entry.py:73, `schedule_times.py:13`).

**전일 분봉 시드는 채택하지 않는다.** 09시 최대 상승 종목은 전일 종가 대비 갭이 크다. 전일
EMA를 이어받으면 개장 즉시 MACD가 극단값이 되어 무의미한 신호를 쏟아낸다. 갭이 클수록
심해지는데, 하필 이 전략이 고르는 종목이 갭이 가장 큰 종목이다.

**결론: 지표 기반 트랙 B는 A와 다른 시간대에서 논다.** "09시 최대 상승 종목 진입"이라는 이
봇의 핵심 목표를 B는 수행하지 않으며, 같은 종목의 **후속 구간을 매매하는 다른 성격의
전략**이 된다. 이는 수용된 결정이다(§2).

부수 효과로 **두 트랙이 같은 순간에 주문하지 않으므로 유량 경합이 설계상 사라진다.**

### 7.2 봉의 진실의 원천을 나눈다

틱 집계 봉과 거래소 공식 분봉은 다를 수 있다. WS 유실 구간, REST 백업 폴링 구간
(f4_tracking.py:628)에서는 틱이 성겨 OHLC가 부정확해진다. 지표가 부정확한 봉 위에 서면
실시간과 사후 분석이 어긋난다.

```
WS 체결 틱 (공유)
   ↓
봉 집계기 (1분 OHLCV)        ← 진행 중인 마지막 봉만 담당
   ↓
확정 봉 정정 (분봉 API)       ← 09:11 이후, BACKGROUND, 1분 1회
   ↓                            공식 봉 도착 시 집계본을 대체 → 지표 재계산
지표 엔진 (MA / MACD / ...)
   ↓
트랙 B 전략
```

`live._accumulate_minute`(live.py:55)는 현재 **종가만** 쌓는다(`price`, `tick_count`). OHLCV로
확장한다.

분봉 API는 커서 없이 1페이지가 최근 약 30봉을 주므로(`kis_minute_bar_poc.py:177`) **1분에
1회 호출로 충분**하다. `REQUEST_PRIORITY_BACKGROUND(40)`(kis_rest.py:64)로 주문 경로 뒤에
세운다.

`scripts/kis_minute_bar_poc.py`의 `parse_minute_bars()`가 이미 KIS 응답을 OHLCV dict로
변환한다(`stck_oprc`/`stck_hgpr`/`stck_lwpr`/`stck_prpr`/`cntg_vol`). `src/`로 승격시켜
공용화한다.

### 7.3 09:00~09:11에는 분봉 API를 호출하지 않는다

이 구간은 A의 진입 창이다. BACKGROUND 우선순위여도 PAPER 초당 1건에서는 슬롯을 소비한다
(`_LOW_PRIORITY_MAX_WAIT_SLOTS`로 무한 기아는 막혀 있어 결국 슬롯을 쓴다). B는 이 구간에
지표가 없으므로 잃는 것이 없다. `kis_minute_bar_poc.py:43`의 `FORBIDDEN_START/END` 규약을
런타임으로 승격시킨다.

### 7.4 지표 엔진은 순수 함수

```
indicators.sma(bars, period)              -> list[float | None]
indicators.ema(bars, period)              -> list[float | None]
indicators.macd(bars, fast, slow, signal) -> list[dict]
```

`bars`를 받아 값을 돌려주는 순수 함수만 둔다. 상태도 I/O도 없으므로 **테스트가 결정적**이고,
오프라인 백테스트와 실시간이 같은 코드를 쓴다. 백테스트에서 좋았던 조합이 실시간에서 다른
계산을 타면 실험 자체가 무의미해진다.

### 7.5 봉 이외의 지표 입력 — 추가 구독 없이 확보된다

§11.1에서 확정된 대로, 체결 프레임에 이미 다음이 들어 있다.

| 값 | idx | 봉 집계 시 |
|---|---|---|
| 체결강도 `CTTR` | 18 | 봉 구간 마지막 값 또는 평균 |
| 체결구분 `CCLD_DVSN` | 21 | 매수/매도 주도 체결량 분리 |
| 1단계 호가 `ASKP1`/`BIDP1` | 10 / 11 | 봉 종료 시점 스프레드 |
| 총 호가잔량 | 38 / 39 | 수급 불균형 비율 |

**별도 호가 구독(`H0STASP0`)이 필요 없다.** 봉 집계기가 OHLCV와 함께 이 값들도 누적하면
트랙 B의 규칙 실험 폭이 넓어진다. 다만 이것들은 **봉이 아니라 틱 파생값**이므로, 분봉 API로
정정되지 않는다(§7.2의 확정 봉 정정 대상은 OHLCV뿐). 정정 불가 값임을 데이터에 표시한다.

## 8. 섹션 ⑥ — 유량·UI·테스트

### 8.1 유량 예산

B의 진입이 09:35 이후로 밀리면서 A의 진입 창(09:00~09:11)과 **시간적으로 분리**됐다.

PAPER 기준(초당 1건 = 분당 60슬롯, kis_rest.py:35):

| 항목 | 분당 소비 | 비고 |
|---|---|---|
| 분봉 폴링 | 1건 (1.7%) | BACKGROUND, 09:11 이후만 |
| B 진입 (09:35경 1회) | 5~10건 | A의 진입 창 밖 |
| A의 F4 REST 백업 | WS stale 시에만 | 기존 동작 |

REAL(초당 18건 = 분당 1080슬롯)은 여유가 압도적이다.

### 8.2 UI — 트랙 인식

- `/api/stats`, `/api/history`에 트랙 스코프 추가 (§6.1)
- 대시보드에 트랙 선택과 A/B 비교 뷰
- 그림자 거래는 별도 테이블이라 실손익에 섞이지 않지만, 화면에서 명확히 구분 표시

### 8.3 UI — 봉/지표 차트

트랙 B의 판정이 지표 기반이므로, **왜 진입했는지 / 왜 안 했는지를 보려면 지표를 봐야
한다.** 규칙을 실험하며 정하는 것이 목표이므로 지표 시각화는 장식이 아니라 **실험 도구
자체**다.

**기존 `drawPriceFlow()`(app.js:615)를 확장하지 않는다.** 요구사항이 다르다.

| | 기존 가격흐름 | 새 지표 차트 |
|---|---|---|
| 단위 | 원시 틱 | 1분 봉 (OHLCV) |
| 창 | 최근 20분 (`PRICE_FLOW_VIEW_MIN`) | 60분 이상 (MACD가 26봉+ 필요) |
| 오버레이 | A의 진입가·트레일스탑·최고가·매매마커·VI밴드 | 이동평균, 캔들 |
| 패널 | 1개 | 2개 (가격 + MACD는 축이 다름) |

`drawPriceFlow`는 200줄 가까이 되고 A의 참조선·마커·VI 밴드가 얽혀 있다. 봉과 지표를
욱여넣으면 둘 다 나빠지고 **A의 차트를 건드리게 된다** — 안 C의 원칙과 어긋난다.

**트랙 B 전용 차트를 신규 작성하고 오늘 화면에 세로로 배치한다.** 위는 기존 틱 가격흐름(A),
아래는 봉·지표 차트(B).

```
┌─ 가격 패널 ─────────────────────────┐
│  캔들 (1분 OHLCV)                    │
│  + 이동평균선 (설정 가능한 기간 N개)  │
│  + B의 진입가 / 청산선 / 매매 마커    │
├─ MACD 패널 ─────────────────────────┤
│  MACD선 · 시그널선 · 히스토그램       │
│  0 기준선                            │
└─────────────────────────────────────┘
```

MACD를 별도 패널로 분리하는 것은 필수다 — 가격은 수만 원대, MACD는 0 근방에서 진동하므로
같은 축에 그릴 수 없다.

**확정 봉과 미확정 봉을 시각적으로 구분한다.** §7.2에서 마지막 봉은 틱 집계본이고 분봉 API
응답이 오면 대체된다. 그 차이가 화면에 보여야 "차트에서 본 값과 지표가 판단한 값이 다른"
상황을 디버깅할 수 있다. 미확정 봉은 흐리게 처리하고 정정 시 그대로 반영한다.

**그림자 단계에서도 마커를 찍는다.** SHADOW 기간에는 실주문이 없지만 "여기서 샀을 것"이라는
가상 진입·청산 지점을 표시한다. 그림자 검증의 대부분이 이 화면에서 이뤄진다.

#### 데이터 경로

```
GET /api/bars?track=B  →  { bars: [OHLCV...], indicators: {...}, marks: [...] }
```

지표는 **서버에서 계산한다.** §7.4에서 지표 엔진을 순수 함수로 격리한 이유가 여기서
살아난다 — 전략 판정과 차트가 **같은 함수**를 타야 "차트는 매수 신호인데 봇은 안 샀다"는
혼란이 없다. 브라우저에서 다시 계산하면 그 보장이 깨진다.

### 8.4 테스트 — 기존 스위트 무수정 통과가 수용 기준

스펙 작성 시점 `tests/test_*.py` 47개(현재 51개 — §12의 선행 작업으로 4개 추가)가 수용
기준이다. 이것이 안 C의 **증명**이다. `test_f3_entry`, `test_f5_timeout`,
`test_exit_recovery`, `test_restart_guard`, `test_state_daily_reset`이 **한 줄도 고치지
않고** 통과해야 한다. 고쳐야 한다면 실행 계층을 건드렸다는 뜻이고 전제가 무너진 것이다.

예상한 예외는 이랬다.

- §3.3 관측 계층 중립화 — `test_f4_capture_wiring`, `test_live`
- §4.4 `get_unresolved_exit_intent` 트랙 인자 추가 — `test_exit_recovery`, `test_db_crud`
- §4.3 마이그레이션 — `test_db_schema_creation`

**실제로는 빗나갔다.** §3.3 구현에서 수정이 필요했던 것은 `test_f4_capture_wiring`이 아니라
`test_f4_step_trailing`이었다. 두 가지 이유였고, 둘 다 **테스트 비계이지 검증 대상이 아니다.**

1. 세 테스트가 `position_status = "IDLE"`을 **관측 루프 종료 장치**로 썼다. 관측이 A의
   포지션에서 분리되면서 이 방법이 통하지 않아 전체 스위트가 무한 루프에 걸렸다. 프로덕션이
   실제로 관측을 끝내는 방식(종목 잠금 해제 = 일일 리셋 경로)으로 바꿨다.
2. 두 테스트가 `_price_observation_active`를 **인자 없는 람다**로 stub했다. 실제 시그니처는
   `(now=None)`이라 원래도 어긋나 있었고, `test_f4_capture_wiring`이 이미 쓰던
   `lambda *a, **k` 관례로 정리했다.

**교훈**: "무수정 통과"를 수용 기준으로 삼되, 어떤 파일이 걸릴지는 미리 맞히기 어렵다.
수정이 필요할 때는 **검증 대상이 바뀐 것인지 비계가 낡은 것인지**를 구분해서 판단한다.
전자면 전제가 무너진 것이고, 후자면 고쳐도 된다.

JS 테스트도 있다(`tests/js/`). `price_flow_checks.js`는 기존 가격흐름 차트를 검증하므로
**§8.3에서 `drawPriceFlow`를 건드리지 않기로 한 결정의 회귀 방지선**이다. 이것이 무수정으로
통과해야 A의 차트가 안전하다는 뜻이다. 새 봉/지표 차트는 별도 JS 테스트를 추가한다.

#### 신규 테스트

| 대상 | 내용 |
|---|---|
| **마이그레이션** | 구 DB(컬럼이 `ALTER`로 뒤에 붙은 상태) 픽스처에서 재작성 후 값이 뒤섞이지 않는지 |
| **FK 보존** | 재작성 후 `foreign_key_check` 통과, `trailing_shadow_comparisons` 행 생존 |
| **교차 오염** | A가 `EXITING` + `pending_exit=None`일 때 B의 미해결 매도를 인수하지 않는지 |
| **불변식 양방향** | 브로커 < 장부, 브로커 > 장부 각각의 조치가 §5.3 정책대로인지 |
| **재확인 단계** | 1차 위반이 재조회로 해소되면 조치가 없는지 |
| **예산 동결** | 08:59 스냅샷 이후 트랙 예산이 조회 순서에 영향받지 않는지 |
| **승격 게이트** | 표본 20건 미만에서 성과가 좋아도 승격이 거부되는지 |
| **지표 결정성** | 같은 봉 입력에 같은 출력. 백테스트와 실시간이 같은 함수를 타는지 |
| **봉 정정** | 분봉 API 응답이 틱 집계본을 대체하고 지표가 재계산되는지 |
| **관측 중립성** | A가 미진입한 날에도 B가 틱을 받는지 |

## 9. 범위 밖

- **트랙 B의 매매 규칙** — `strategy_configs`에 설정으로 꽂는다. 실험하며 정한다
- **트랙 3개 이상** — 스키마는 N개를 수용하지만 이번 구현은 A/B 2개
- **다른 종목 트랙** — 두 트랙은 같은 종목만 매매한다
- **호가창 깊이(2~10단계)** — `H0STASP0` 별도 구독이 필요하다. **1단계 호가·잔량은 체결
  프레임에 이미 포함되므로 범위 밖이 아니다** (§11.1). 초판에서 "WS는 체결만 구독"으로 적은
  것은 사실 오인이었다
- **VI 판정을 WS로 대체** — `VI_STND_PRC`(idx 45)가 매 체결에 오므로 현재 REST 기반
  `vi_watch`를 줄일 여지가 있다. 별도 검토 (§11.1)
- **State 다중화 리팩터링(안 A)** — 트랙이 3개 이상으로 늘거나 B가 안정화된 뒤 검토

## 10. 미결 사항

없음. §6.3의 PILOT 단계 도입과 §6.4의 표본 20건 하드락은 설계자 권고로 채택했으며, 스펙
검토 단계에서 재고할 수 있다.

## 11. KIS WS 체결 프레임 명세 (확정)

출처: KIS 공식 저장소 `koreainvestment/open-trading-api`

- `examples_llm/domestic_stock/ccnl_krx/ccnl_krx.py` — `columns` 목록
- `examples_llm/domestic_stock/ccnl_krx/chk_ccnl_krx.py` — 필드 한글명
- `examples_llm/kis_auth.py` — 프레임 파싱
- `backtester/kis_backtest/providers/kis/websocket.py` — 다건 순회

### 11.1 필드 배치 — `H0STCNT0`, 46개 (idx 0~45)

기존 파서가 쓰던 인덱스(0·1·2·12)는 **전부 정확했다.** 새로 쓸 수 있게 된 것들:

| idx | 필드 | 의미 | 용도 |
|---|---|---|---|
| 0 | `MKSC_SHRN_ISCD` | 종목코드 | 기존 |
| 1 | `STCK_CNTG_HOUR` | 체결시간 | 기존 |
| 2 | `STCK_PRPR` | 현재가 | 기존 |
| **10 / 11** | `ASKP1` / `BIDP1` | 매도호가1 / 매수호가1 | **1단계 호가** |
| 12 | `CNTG_VOL` | 체결거래량 | 기존 |
| 15 / 16 | `SELN_CNTG_CSNU` / `SHNU_CNTG_CSNU` | 매도/매수 체결건수 | 체결강도 자체 산식 |
| 17 | `NTBY_CNTG_CSNU` | 순매수 체결건수 | |
| **18** | `CTTR` | **체결강도** | 지표 입력 (계산 불필요) |
| 19 / 20 | `SELN_CNTG_SMTN` / `SHNU_CNTG_SMTN` | 총 매도/매수 수량 | |
| **21** | `CCLD_DVSN` | **체결구분** | 매수·매도 주도 판정 |
| 22 | `SHNU_RATE` | 매수비율 | |
| 33 | `BSOP_DATE` | 영업일자 | |
| 35 | `TRHT_YN` | 거래정지 여부 | |
| **36 / 37** | `ASKP_RSQN1` / `BIDP_RSQN1` | **호가잔량1** | 1단계 잔량 |
| **38 / 39** | `TOTAL_ASKP_RSQN` / `TOTAL_BIDP_RSQN` | **총 호가잔량** | 수급 불균형 |
| 43 | `HOUR_CLS_CODE` | 시간 구분 코드 | |
| **45** | `VI_STND_PRC` | **정적VI발동기준가** | VI 판정 대체 후보 |

**§7(봉/지표 계층)에 대한 함의**: 체결강도·1단계 호가·총잔량이 **추가 구독 없이** 지표
입력으로 쓸 수 있다. 봉 집계기가 이 값들도 함께 누적하면 트랙 B의 규칙 실험 폭이 넓어진다.

### 11.2 다건 프레임 — 한 프레임에 체결이 여러 건 온다

공식 구현 두 곳 모두 `parts[3]`을 `pd.read_csv(sep="^", names=<46개>)`로 읽고
`for _, row in df.iterrows()`로 **행을 순회**한다. 둘 다 `parts[2]`(데이터 건수)는 쓰지
않는다 — 따라서 문제는 건수 미사용이 아니라 **데이터부를 레코드 단위로 나누지 않은 것**이다.

기존 파서는 `parts[3].split("^")` 후 첫 레코드의 인덱스만 읽어 나머지를 조용히 버렸다.
프레임이 묶이는 건 **체결 폭주 구간**이고, 그 구간이 바로 트레일링·하드스탑 판정이 가장
민감한 구간이다.

파싱은 줄바꿈과 `^`를 모두 값 구분자로 보고 **46필드 단위로 자른다.** 구분자 형태에
의존하지 않으므로 두 형태 모두 같게 처리된다. 배수로 떨어지지 않으면 쪼개지 않는다 —
오정렬된 레코드로 잘못된 가격을 손절 판정에 넣느니 첫 레코드만 쓰는 편이 안전하다.

### 11.3 원시 필드 보존

인덱스 의미가 확인되기 전에도 `raw`(미해석 필드 배열)를 캡처에 남기도록 해뒀다. 명세가
확정된 지금은 소급 해석이 가능하다 — `raw` 도입 이후 쌓인 데이터에서 체결강도·체결구분·
호가를 되살릴 수 있다. 그 이전 데이터는 복원 불가다.

## 12. 구현 상태

트랙 구현 전에 **데이터 수집 선행 작업**을 먼저 넣었다. 트랙 B의 규칙을 실험으로 정하려면
표본이 필요한데, 기존 구조로는 표본이 쌓이지 않았기 때문이다(전 구간 확보일 2일, 그중
`data_complete=1`인 날 0일).

| 커밋 | 내용 | 관련 절 |
|---|---|---|
| `d15a18a` | 추적 종료 버튼 데이터 손실 경고 | — |
| `6d0fd87` | 미해석 WS 원시 필드 보존 | §11.3 |
| `3acdac2` | 관측 계층 중립화 + 유량 가드 + 캡처 부착 완화 | §3.3·§3.5·§3.6 |
| `d7fd48f` | 종목 교체 시 낡은 구독 중단 | §3.7 |
| `8909761` | 다건 프레임 전량 파싱 | §11.2 |
| `7f5362a` | trades 재작성 — `track` 컬럼 + `(date, track)` UNIQUE + `close_reason` CHECK 확장 | §4.1·§4.3·§4.5 |
| `06b615a` | `daily_skips` 트랙 스코프 | §4.1 |
| `1b73639` | 거래 조회·주문 멱등성 트랙 스코프 | §4.2 |
| `8ee0086` | `get_unresolved_exit_intent` 트랙 교차 오염 수정 | §4.4 |
| `6db1599` | 트랙별 상태 + 하위호환 영속화 | §3.1·§3.2 |
| `67be433` | `/api/stats`·`/api/history`·개선 쿼리 트랙 스코프 | §6.1 |
| `cf9481a` | 리뷰 수정 — 재작성 FK 게이트를 COMMIT 이전으로·증분 판정, 준비도 게이트 트랙 A 한정 | §4.3·§4.7 |

완료: §3.1·§3.2(트랙 모델·영속화), §3.3·§3.5~§3.7(관측 계층 중립화·재구독), §4(DB 스키마·마이그레이션),
§6.1(집계 쿼리 트랙 스코프).

미착수: §3.4(트랙 B 전용 `SpikeFilter` — 트랙 B 틱 소비자가 없어 붙일 곳이 없다),
§5(예산·불변식), §6.2~§6.5(그림자·승격), §7(봉/지표), §8(트랙 UI).

다음 덩어리는 [트랙 B 그림자 가동 설계](2026-08-27-track-b-shadow-design.md)가 이어받는다 —
§3.4·§6.2·§7을 다루고, §5·§6.3~§6.5·§8은 그 다음(PILOT)으로 미룬다. 그 스펙은 §7.2의
"`live._accumulate_minute`를 OHLCV로 확장한다"에서 의도적으로 이탈한다. `src/live.py`가
`_STRATEGY_FILES`에 있어 손댈 때마다 트랙 A의 지문이 돌기 때문이다.

### 12.1 아직 실장 검증되지 않았다

위 변경들은 테스트로만 검증됐다. 장중 로그에서 확인할 것:

- WS 구독 시작 시각이 09:00 전후로 앞당겨졌는지
- A 미진입일에 `F4_REST_BACKUP_START` 이후 실제 REST 호출이 없는지 (§3.5)
- 후보 교체가 일어난 날 재구독이 기록되는지 (§3.7)
- 다건 프레임이 실제로 오는지, 온다면 하루 몇 건인지 (§11.2)

### 12.2 배포 리허설과 지문 회전 (Task 7, 2026-08-26)

운영 DB(`data/db/trading.db`, `-wal`·`-shm` 포함)를 사본으로 떠서 `db.init()` 마이그레이션을
리허설했다. 사본은 워크트리 안(`.superpowers/sdd/2026-08-26-multi-track-foundation/`)에서만
다뤘고, 운영 파일은 열지 않았다 — 리허설 전후 `trading.db`의 MD5가 동일함을 확인했다.

| 항목 | 마이그레이션 전 | 마이그레이션 후 |
|---|---|---|
| `trades` 총 행 수 | 31 | 31 |
| `track='A'` 행 수 | — (컬럼 없음) | 31 |
| `PRAGMA foreign_key_check` | — | 위반 0건 |
| `trailing_shadow_comparisons` 행 수 | 6 | 6 |

행 수 보존, 전량 `track='A'`, FK 위반 0건, `ON DELETE CASCADE` 대상인 shadow 비교 행도 그대로
보존됐다 — §4.3 마이그레이션이 운영 데이터에 안전하게 적용됨을 확인했다. 리허설 사본과
`rehearsal.db.pre_track_*` 백업, 사이드카 파일은 검증 직후 전부 삭제했다.

`strategy_fingerprint()`는 `src/db.py`·`src/state.py`를 포함한 전략 파일 해시라 이번
마이그레이션 코드 자체가 지문을 회전시킨다: `main`의 `f864e8a95ba2` → 리허설 시점
`d4435896a8a2` → 리뷰 수정 반영 후 **`40d999a0ab66`**(브랜치 최종). 배포 후 `experiment_id`는
`baseline-40d999a0ab66`으로 바뀐다. **이전 실험의 40거래일 paired 수집은 여기서 끊기고 0부터
다시 시작한다** — Plan 3의 그림자 승격 기록은 이 새 실험 ID 위에서 쌓인다. 준비도의 무결
PAPER 20건 근거도 같은 이유로 0부터 다시 쌓인다(`src/readiness.py`는 전략 파일이 아니므로
§4.7의 트랙 스코프 수정 자체는 지문을 건드리지 않는다).
