# 멀티 트랙 전략 병행 운용 설계

> **상태**: 설계 확정 — 구현 계획 대기
> **작성일**: 2026-08-25
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

**A의 매매 판단은 무변경이다.** `_process_tick()`(청산 판정)은 `position_status != "HOLDING"`
게이트(f4_tracking.py:761) 뒤에 그대로 남고, `_trigger_close`·`_execute_close`·
`recover_pending_exit`은 손대지 않는다. 넓어지는 것은 관측 부작용뿐이다.

- `live.push_tick` — UI 가격흐름 차트가 진입 전 구간도 그린다 (개선)
- `tick_capture.enqueue` — 캡처 데이터 증가. 부착은 `trade_id`가 필요하므로 A 진입 전에는
  미부착

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
PRAGMA foreign_keys = OFF;
BEGIN;
  CREATE TABLE trades_new (... 기존 컬럼 전부 + track, UNIQUE(date, track));
  INSERT INTO trades_new (id, date, ticker, name, ..., experiment_id, track)
       SELECT             id, date, ticker, name, ..., experiment_id, 'A' FROM trades;
  DROP TABLE trades;
  ALTER TABLE trades_new RENAME TO trades;
  CREATE INDEX idx_trades_date ON trades(date);
COMMIT;
PRAGMA foreign_key_check;
PRAGMA foreign_keys = ON;
```

`id`를 보존해 넣으므로 FK 참조는 그대로 유효하다.

#### 필수 안전장치

1. **마이그레이션 직전 DB 파일 백업 복사.** 타협 대상이 아니다
2. **필요할 때만 실행.** `sqlite_master`의 `sql`에 `track`이 없을 때만. `daily_skips`
   재구축(db.py:311)과 같은 감지 패턴
3. **`foreign_key_check` 통과 후에만 정상 기동.** 위반 시 백업으로 복원하고 기동 중단

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
- `get_order_by_kis_id`(db.py:527)는 `kis_order_id`가 유니크 인덱스를 가짐 — 트랙 안전
- `price_path_manifests`의 `UNIQUE (trade_date, ticker, experiment_id)`(db.py:253)는 B가 다른
  `experiment_id`를 쓰는 한 충돌하지 않는다. **A와 B가 같은 `experiment_id`를 쓰는 구성은
  기동 시 검증으로 금지한다**

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

### 8.4 테스트 — 기존 47개 무수정 통과가 수용 기준

`tests/test_*.py` 47개가 수용 기준이다. 이것이 안 C의 **증명**이다. `test_f3_entry`,
`test_f4_step_trailing`, `test_f5_timeout`, `test_exit_recovery`, `test_restart_guard`,
`test_state_daily_reset`이 **한 줄도 고치지 않고** 통과해야 한다. 고쳐야 한다면 실행 계층을
건드렸다는 뜻이고 전제가 무너진 것이다.

의도된 예외는 세 곳이다.

- §3.3 관측 계층 중립화 — `test_f4_capture_wiring`, `test_live`
- §4.4 `get_unresolved_exit_intent` 트랙 인자 추가 — `test_exit_recovery`, `test_db_crud`
- §4.3 마이그레이션 — `test_db_schema_creation`

해당 테스트는 보강한다.

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
- **호가·잔량 기반 로직** — 현재 WS는 체결만 구독한다. 필요해지면 별도 스펙
- **State 다중화 리팩터링(안 A)** — 트랙이 3개 이상으로 늘거나 B가 안정화된 뒤 검토

## 10. 미결 사항

없음. §6.3의 PILOT 단계 도입과 §6.4의 표본 20건 하드락은 설계자 권고로 채택했으며, 스펙
검토 단계에서 재고할 수 있다.
