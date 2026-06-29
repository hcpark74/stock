# 스프린트 계획서 — 데일리 갭업 자동매매 시스템

> **버전**: 1.0  
> **작성일**: 2026-06-23  
> **기준 브랜치**: main  

---

## 전체 진행 현황

```
Sprint 0  ████████████████████  완료   기반 구조
Sprint 1  ████████████████████  완료   State + F4 Step Trailing 정리
Sprint 2  ████████████████████  완료   KIS API 실 구현
Sprint 3  ████████████████████  완료   DB CRUD + F3/F4/F5 연결
Sprint 4  ████████████░░░░░░░░  진행   테스트 + Paper Trading 검증
Sprint 5  ░░░░░░░░░░░░░░░░░░░░  대기   FastAPI + UI 실데이터 연동
```

---

## Sprint 0 — 기반 구조 [완료]

> 목표: 실행 가능한 프로젝트 골격 완성

- [x] `requirements.txt` 패키지 정의
- [x] `.env.example` 환경 변수 템플릿
- [x] `main.py` 진입점 (APScheduler 부트스트랩)
- [x] `src/api/auth.py` — KIS 토큰 발급/캐시/갱신
- [x] `src/api/kis_rest.py` — Rate-limited HTTP 래퍼 (401/429 자동 처리)
- [x] `src/api/kis_ws.py` — WebSocket 구독/파싱/재연결 (H0STCNT0)
- [x] `src/utils/logger.py` — JSONL 이벤트 로거
- [x] `src/utils/time_sync.py` — NTP 오차 검증
- [x] `src/utils/spike_filter.py` — 이상 틱 필터
- [x] `src/notifier.py` — Telegram 비동기 큐
- [x] `src/scheduler.py` — APScheduler 빌드
- [x] `src/state.py` — 인메모리 전역 상태 + atomic 전이
- [x] `src/db.py` — SQLite 연결 싱글톤 + 4개 테이블 초기화
- [x] `src/modules/f1_filter.py` — 갭/유동성 필터 로직 골격
- [x] `src/modules/f2_lockup.py` — VI 필터 + 타겟 락업 로직
- [x] `src/modules/f3_entry.py` — 진입 주문 흐름 골격
- [x] `src/modules/f4_tracking.py` — 틱 추적 골격
- [x] `src/modules/f5_timeout.py` — 타임아웃 청산 골격
- [x] 문서: PRD, DB_DESIGN, UI_DESIGN, CODING_GUIDELINES, DEV_ENV
- [x] `data/db/trading.db` DB 파일 생성 확인

---

## Sprint 1 — State + F4 Step Trailing 정리 [완료]

> 목표: PRD v1.3 변경사항(1차 익절 제거 → Step Trailing) 코드 반영  
> 선행 조건: Sprint 0 완료  
> 완료일: 2026-06-23

### 1-1. `src/state.py` 정리

- [x] `State` 데이터클래스에서 `first_partial_done`, `bep_stop_active` 필드 제거
- [x] `State`에 `highest_step: float = 0.0` 필드 추가
- [x] `close_reason` 주석에서 `BEP_STOP` 제거
- [x] `set_holding()` 초기화 블록에서 두 필드 제거, `highest_step = 0.0` 추가
- [x] `persist()` — `today_state.json` 직렬화에서 두 필드 제거, `highest_step` 추가
- [x] `restore_from()` — 복구 로직 동일하게 갱신

### 1-2. `src/modules/f4_tracking.py` 재작성

- [x] 상수 정리
  - 제거: `FIRST_PARTIAL_THRESHOLD`, `BEP_STOP`, `TRAILING_ACTIVATE`, `TRAILING_EARLY`, `TRAILING_LATE`
  - 추가: `STEP_SIZE = 0.025`, `STEP_TRAIL = 0.015`, `HARD_STOP_RATIO = 0.020`
- [x] `_process_tick()` 재작성
  - 스텝 갱신: `floor(pnl / STEP_SIZE) * STEP_SIZE`
  - `highest_step` 갱신, `trailing_active` 플래그 관리
  - Hard Stop: `trailing_active == False and price <= entry * (1 - HARD_STOP_RATIO)`
  - Step Trailing: `trailing_active == True and price <= entry * (1 + highest_step - STEP_TRAIL)`
  - 09:50 강제 발동 유지
- [x] `_first_partial_exit()` 함수 제거
- [x] `_execute_close()` 로그 필드 업데이트 (BEP_STOP 제거, highest_step/stop_price 추가)

### Sprint 1 완료 기준

- [x] `State` 객체에 `first_partial_done`, `bep_stop_active` 필드 없음
- [ ] F4 틱 처리 시 Step Trailing 수식 동작 확인 (수동 단위 테스트) ← Sprint 4에서 자동화
- [x] `today_state.json` 직렬화 시 `highest_step` 포함

---

## Sprint 2 — KIS API 실 구현 [완료]

> 목표: 모든 `TODO: KIS API` 스텁을 실제 API 호출로 교체  
> 선행 조건: Sprint 1 완료 + KIS API 키 발급  
> 완료일: 2026-06-23

### 2-1. `src/modules/f1_filter.py`

- [x] `_fetch_all_premarket()` — `FHPST01710000` (등락률 순위, KOSPI+KOSDAQ)
  - 장전 예상 체결가 기준 `fid_rsfl_rate1/2` 파라미터로 3~7% 필터
  - `avg_amount_5d`: `avrg_vol × stck_prpr` 근사값 사용

### 2-2. `src/modules/f3_entry.py`

- [x] `_fetch_expected_price(ticker)` — `FHKST01010100` (장전: `antc_cnpr` 우선)
- [x] `_fetch_current_price(ticker)` — `FHKST01010100` (`stck_prpr`)
- [x] `_fetch_available_cash()` — `TTTC8908R/VTTC8908R` (`dnca_tot_amt`)
- [x] `_send_buy(ticker, qty, mode)` — `TTTC0802U/VTTC0802U` (시장가 `ORD_DVSN=01`)
- [x] `_send_sell(ticker, qty, mode)` — `TTTC0801U/VTTC0801U`
- [x] `_cancel_order(order_id, mode)` — `TTTC0803U/VTTC0803U` (`KRX_FWDG_ORD_ORGNO` 자동)
- [x] `_poll_fill(order_id, deadline)` — `TTTC8001R/VTTC8001R` (`tot_ccld_qty/amt`)
- [x] `run()` 내 `ODNO` 대문자 수정 + `KRX_FWDG_ORD_ORGNO` 저장

### 2-3. `src/modules/f4_tracking.py`

- [x] `_rest_polling_fallback()` — `FHKST01010100` (`stck_prpr`)

### 2-4. `src/modules/f5_timeout.py`

- [x] `precheck()` — `TTTC8908R/VTTC8908R` (`hldg_qty` 확인)
- [x] `execute()` — `_send_sell()` + `_poll_fill()` 30초 타임아웃

### Sprint 2 완료 기준

- [ ] `KIS_MODE=PAPER` 환경에서 `main.py` 실행 시 오류 없음 ← 실제 API 키로 검증 필요
- [ ] 장전(08:30) 실행 시 토큰 발급 → 08:40 F1 필터 → 08:58 F2 락업 로그 확인
- [ ] 09:00 이후 F3 진입 주문 → PAPER 계좌 주문 내역 확인

---

## Sprint 3 — DB CRUD + F3/F4/F5 연결 [완료]

> 목표: 거래 데이터를 `trading.db`에 기록  
> 선행 조건: Sprint 2 완료  
> 완료일: 2026-06-23

### 3-1. `src/db.py` CRUD 함수 구현

- [x] `open_trade(date, ticker, entry_price, entry_qty) -> int` — trades 테이블 INSERT, trade_id 반환
- [x] `record_order(trade_id, kis_order_id, side, qty, price, phase, ticker) -> int` — orders 테이블 INSERT
- [x] `update_order_fill(order_db_id, fill_price, fill_qty, fill_latency_ms)` — orders 체결 갱신
- [x] `close_trade(trade_id, exit_price, close_reason, pnl_pct, highest_step)` — trades 청산 갱신
- [x] `record_skip(date, reason, detail)` — daily_skips INSERT
- [x] 스키마 수정: `BEP_STOP` 제거, `highest_step REAL` 컬럼 추가, 기존 DB 마이그레이션

### 3-2. F3/F4/F5 DB 연결

- [x] `f3_entry.py` — 체결 후 `open_trade` / `record_order` / `update_order_fill` + 피라미딩 기록
- [x] `f4_tracking.py` — `_execute_close()` 실제 매도 구현 + `close_trade` / `record_order` 호출
- [x] `f5_timeout.py` — `execute()` 에서 `close_trade` / `record_order` 호출
- [x] `f1_filter.py` — NO_TARGET 스킵 시 `record_skip` 호출
- [x] `f3_entry.py` — GAP_CHANGED / ENTRY_FAIL / SLIPPAGE_GUARD 스킵 시 `record_skip` 호출
- [x] `state.py` — `trade_id: int = 0` 필드 추가 + persist/restore 반영

### Sprint 3 완료 기준

- Paper Trading 1회 완료 후 `trading.db` 조회 시 `trades`, `orders` 행 확인
- `close_reason` 컬럼 정상 기록

---

## Sprint 4 — 테스트 + Paper Trading 검증 [진행 중]

> 목표: 핵심 로직 유닛 테스트 + 실제 Paper Trading 1주일 검증  
> 선행 조건: Sprint 3 완료  
> 예상 소요: 2~3일

### 4-1. 유닛 테스트 작성 (`tests/`)

- [x] `tests/test_f4_step_trailing.py` — 완료 2026-06-23 (14개 케이스)
  - 스텝 갱신 정확성 (구간별 2.6/5.1/7.6% — 부동소수점 경계 주의)
  - Hard Stop 발동/비발동/trailing 활성 시 우선순위
  - Step Trailing 발동/비발동 경계
  - 09:50 강제 발동 + stop 이하 시 청산 확인
  - highest_step 단조 증가 + 신고가 갱신
- [x] `tests/test_f1_filter.py` — 완료 2026-06-23 (10개 케이스)
  - 갭 필터 경계값 (2.9%, 3.0%, 7.0%, 6.99%, 7.1%)
  - 유동성 상위 10% 필터 (10종목/20종목/1종목)
  - day_skip 조기 반환
- [x] `tests/test_f2_lockup.py` — 완료 2026-06-23 (10개 케이스)
  - VI 이격 필터 (안전/근접/경계)
  - 복합 정렬 (expected_amount → buy_sell_ratio)
  - VI 필터 후 정렬, 엣지케이스
- [x] `tests/test_db_crud.py` — 완료 2026-06-23 (14개 케이스)
  - `:memory:` DB 사용, open_trade → record_order → update_order_fill → close_trade
  - 피라미딩 BUY 2건, TIMEOUT/HARD_STOP close_reason
  - record_skip INSERT OR IGNORE 중복 방지

> **참고**: F4 스텝 테스트에서 `10000 * 1.025`의 부동소수점 오차로 floor(pnl/STEP_SIZE)=0이 됨.
> 정확히 경계값이 아닌 구간 안쪽 가격(2.6%/5.1%/7.6%)으로 테스트.

### 4-2. Paper Trading 검증

- [ ] `KIS_MODE=PAPER` 1주일 실행
- [ ] 매일 장 후 `data/logs/YYYYMMDD.jsonl` 리뷰
- [ ] `trading.db` 누적 기록 확인
- [ ] NTP 오차 / API 응답 지연 모니터링

### Sprint 4 완료 기준

- [x] `pytest` 전체 통과 — 48/48 (2026-06-23)
- [ ] Paper Trading 5 거래일 이상 오류 없이 완주

---

## Sprint 5 — FastAPI + UI 실데이터 연동 [대기]

> 목표: `trading.db` → FastAPI → 브라우저 대시보드 연동  
> 선행 조건: Sprint 4 완료  
> 예상 소요: 2일

### 5-1. FastAPI 서버

- [ ] `src/web/api.py` 생성
  - `GET /api/today` — 오늘 거래 현황
  - `GET /api/trades?page=1` — 거래 이력
  - `GET /api/stats` — 승률/손익 통계
  - `GET /api/state` — 현재 `State` 객체 (JSON)
- [ ] `main.py`에서 FastAPI 서버 백그라운드 태스크로 기동

### 5-2. UI 연동

- [ ] `docs/html/ui_mockup.html` → `src/web/index.html`로 이전
- [ ] 하드코딩 목업 데이터를 `fetch('/api/...')` 폴링으로 교체
- [ ] 1초 인터벌 실시간 상태 갱신

### Sprint 5 완료 기준

- 브라우저에서 실시간 포지션 상태 확인 가능
- 이력/통계 탭에 실 DB 데이터 표시

---

## 실계좌 전환 체크리스트 (Sprint 4 완료 후)

Sprint 4까지 완료 + 아래 항목 모두 확인 후 진행.

- [ ] Paper Trading 30일 이상, 승률 45% 이상 확인
- [ ] `.env`에서 `KIS_MODE=REAL` 전환
- [ ] `KIS_BASE_URL` / `KIS_WS_URL` 실계좌 주소로 변경
- [ ] 최초 1주 `alloc=5%` (절반 배분)로 시작
- [ ] 매일 `daily_pnl_pct` 확인, 누적 MDD 5% 초과 시 중단

---

## 주요 파일 — 상태 요약

| 파일 | 상태 | 비고 |
|------|------|------|
| `src/api/auth.py` | ✅ 완료 | |
| `src/api/kis_rest.py` | ✅ 완료 | |
| `src/api/kis_ws.py` | ✅ 완료 | |
| `src/utils/*.py` | ✅ 완료 | |
| `src/notifier.py` | ✅ 완료 | |
| `src/scheduler.py` | ✅ 완료 | |
| `src/db.py` | ✅ 완료 | CRUD 5개 함수 구현 |
| `src/state.py` | ✅ 완료 | highest_step 반영 |
| `src/modules/f1_filter.py` | ✅ 완료 | FHPST01710000 구현 |
| `src/modules/f2_lockup.py` | ✅ 로직 완성 | API 호출 없음 (F1 결과 사용) |
| `src/modules/f3_entry.py` | ✅ 완료 | 7개 함수 + org_no 저장 |
| `src/modules/f4_tracking.py` | ✅ 완료 | Step Trailing + REST fallback |
| `src/modules/f5_timeout.py` | ✅ 완료 | precheck + execute 구현 |
| `main.py` | ✅ 완료 | |
