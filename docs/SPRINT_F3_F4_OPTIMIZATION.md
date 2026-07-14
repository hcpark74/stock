# F3/F4 속도 및 복구력 개선 스프린트

> **작성일**: 2026-07-08  
> **상태**: 구현 및 검증 완료 (2026-07-14 전체 테스트 320건 통과)<br>
> **기준 문서**: [SPRINT.md](SPRINT.md) 종료 후 후속 스프린트  
> **목표**: 09:00 직후 F3 진입 경로 지연을 줄이고, HOLDING 중 재시작 시 F4 trailing 상태를 안정적으로 복구한다.
> **후속 운영 안정화**: 2026-07-14 가격흐름/F5 변경은 [SPRINT.md](SPRINT.md)의 후속 작업 절을 참조한다.

---

## 배경

최근 Paper Trading 검증과 리뷰를 통해 F3 중복 거래 방지, 후보 재시도 루프, F4 REST 백업 감시, WebSocket 재연결, 매도 체결 poll 안정화가 보강되었다.

다음 병목은 두 가지다.

- F3 진입 직전 후보 재검증이 후보별 순차 quote 조회 구조라 09:00 직후 진입 속도에 직접 영향을 준다.
- F4 trailing 상태는 메모리에서는 갱신되지만 장중 tick마다 state/DB에 저장되지 않아, 보유 중 재시작 시 high_price/highest_step/trailing_active 복구가 약해질 수 있다.

---

## Sprint A. F3 진입 경로 지연 최적화

### 목표

F3에서 최종 후보를 고르는 시간을 줄이되, 주문 직전 갭 재검증과 매수가능수량 확인의 안전장치는 유지한다.

### 현재 문제

- `_pick_final_entry_candidate()`가 후보를 순차로 조회한다.
- 앞 후보의 quote retry 및 sleep이 뒤 후보 조회를 막는다.
- KIS REST rate limiter 때문에 실제 호출 간격은 지켜지지만, 후보별 retry 대기 때문에 전체 탐색 시간이 길어진다.
- 후보 주문 실패 후 다음 후보를 고를 때 남은 후보들을 다시 재검증할 수 있어 재시도 경로가 느려질 수 있다.

### 작업 항목

- [x] 후보별 quote 재검증을 `asyncio.gather` 기반 병렬 태스크로 변경
- [x] KIS rate limiter가 호출 간격을 계속 보장하는지 테스트 확인 (`tests/test_kis_rest.py`)
- [x] API 응답의 `prev_close=0`인 경우 retry 전에 F1 snapshot 기반 fallback을 우선 적용
- [x] 재검증 결과를 valid 후보 리스트로 보관하고, 주문 실패 시 다음 valid 후보로 즉시 전환
- [x] 다음 후보 주문 직전 해당 후보 1개만 freshness check 수행
- [x] `F3_RECHECK_BATCH_TIMEOUT_SEC` 옵션 검토 및 선택 적용
- [x] 속도 우선 모드에서도 `GAP_CHANGED`, `GAP_RECHECK_UNAVAILABLE`, `BUYABLE_QTY_ZERO` 차단 로그가 유지되는지 확인

### 완료 기준

- [x] 후보 3개 기준 F3 최종 후보 선택 시간이 순차 구조 대비 단축된다.
- [x] 후보 quote retry가 발생해도 다른 후보 조회가 불필요하게 막히지 않는다.
- [x] 주문 실패 후 다음 후보 전환 시 전체 재검증을 반복하지 않는다.
- [x] 기존 F3 테스트와 신규 지연 최적화 테스트가 통과한다.
- [x] Paper Trading 로그에서 F3 단계별 소요 시간이 확인 가능하다.

---


### Sprint A 구현 메모

- `F3_RECHECK_BATCH_TIMEOUT_SEC=0`이면 기존처럼 모든 후보 재검증을 기다린다.
- `F3_RECHECK_BATCH_TIMEOUT_SEC>0`이면 제한 시간 안에 완료된 후보만 평가하고, 지연 후보는 취소 후 `F3_RECHECK_BATCH_TIMEOUT`으로 기록한다.
- 주문 실패 후 다음 후보 전환 시 전체 재검증을 반복하지 않고, 캐시된 다음 후보 1개만 freshness check 한다.
---

## Sprint B. HOLDING 중 trailing 상태 주기 저장

### 목표

장중 재시작이 발생해도 F4가 기존 trailing 기준을 잃지 않도록 `high_price`, `highest_step`, `trailing_active`를 주기적으로 저장한다.

### 현재 문제

- `state.persist()`는 진입 직후와 청산 시점에는 호출된다.
- `_process_tick()`에서 `high_price`, `highest_step`, `trailing_active`가 갱신되어도 즉시 저장하지 않는다.
- `trades.high_price`, `trades.highest_step` 컬럼은 존재하지만 장중 진행 상태 저장에는 거의 사용되지 않는다.

### 작업 항목

- [x] `F4_STATE_PERSIST_INTERVAL_SEC` 추가
- [x] `_process_tick()`에서 trailing 관련 상태가 바뀌면 state 저장 예약
- [x] 매 tick 파일 쓰기를 피하기 위한 throttle 적용
- [x] step 상승 또는 trailing 활성화 시 즉시 저장 경로 추가
- [x] `db.update_trade_progress(trade_id, high_price, highest_step)` 추가
- [x] DB 진행 상태 저장에도 throttle 적용
- [x] 재시작 복구 시 `today_state.json` 우선, 필요하면 OPEN trade DB row로 보조 복구

### 완료 기준

- [x] HOLDING 중 `today_state.json`에 최신 `high_price`, `highest_step`, `trailing_active`가 주기적으로 반영된다.
- [x] step 상승 직후 재시작해도 이전 trailing 기준이 복구된다.
- [x] DB의 OPEN trade row에도 장중 진행 상태가 남는다.
- [x] F4 기존 trailing/hard stop 테스트와 신규 persistence 테스트가 통과한다.

---


### Sprint B 구현 메모

- F4 장중 state 파일/DB 진행 상태 저장은 실제 보유 거래(`trade_id` 존재)에서만 동작한다.
- `high_price`만 바뀌는 일반 tick은 `F4_STATE_PERSIST_INTERVAL_SEC`로 throttle 한다.
- `highest_step` 상승 또는 `trailing_active` 활성화는 재시작 복구 기준이 바뀌는 순간이므로 state 파일과 DB에 즉시 저장한다.
- 재시작 복구는 `today_state.json`의 HOLDING 상태를 우선한다. state 파일이 없거나 HOLDING이 아니면 당일 `OPEN` trade row를 보조 복구 자료로 사용한다.
---

## 추가 보강 메모

- F4 `_process_tick()`은 청산 판정을 state/DB 진행상태 저장보다 먼저 수행한다. 강제 trailing 활성화 tick에서 stop 조건이 동시에 만족되어도 매도 주문 앞에 persist await가 끼지 않는다.
- F3 병렬 quote recheck는 후보별 예외를 격리한다. 한 후보의 quote 조회 예외는 `F3_RECHECK_QUOTE_ERROR`와 `GAP_RECHECK_UNAVAILABLE`로 처리하고 다른 후보 평가는 계속한다.
- F3 가용 현금 조회 예외는 `BALANCE_QUERY_ERROR`로 기록하고 현금 0원으로 안전 차단한다.
- F3 최종 후보 재검증 배치는 `F3_RECHECK_BATCH_TIMING` 로그로 요청 후보 수, 완료 후보 수, elapsed_ms를 남긴다.

---
## 권장 구현 순서

1. F3 후보 quote 병렬 재검증
2. F3 snapshot fallback 및 후보 재시도 캐시
3. F4 state 파일 주기 저장
4. F4 DB progress 저장
5. 장중 재시작 복구 보강

---

## 리스크와 운영 메모

- F3 병렬화는 API 호출을 무제한 늘리는 방식이 아니어야 한다. 기존 `kis_rest` rate limiter를 계속 통과해야 한다.
- F3 batch timeout을 너무 짧게 두면 더 좋은 후보가 늦게 도착했을 때 놓칠 수 있다.
- F4 state 저장을 매 tick마다 수행하면 파일 I/O가 과해질 수 있으므로 throttle이 필요하다.
- DB progress 저장은 운영 복구에는 유리하지만, tick 빈도에 따라 쓰기 부하가 생길 수 있다.

---

## 검증 계획

- [x] `tests/test_f3_entry.py` 후보 병렬 재검증 테스트 추가
- [x] `tests/test_f3_entry.py` recheck 후보별 예외 격리 테스트 추가
- [x] `tests/test_f3_entry.py` 주문 실패 후 캐시된 다음 후보 즉시 전환 테스트 추가
- [x] `tests/test_f4_step_trailing.py` high_price/highest_step 변경 시 state persist 테스트 추가
- [x] `tests/test_f4_step_trailing.py` 청산 tick에서 persist가 매도 판단보다 앞서지 않는 테스트 추가
- [x] `tests/test_db_crud.py` `update_trade_progress` 테스트 추가
- [x] Windows ACL 환경에서는 `--basetemp=.pytest_tmp_*` 옵션으로 전체 테스트 실행 (2026-07-14, 320 passed)

