# 테이블 설계 요약

> 상세 컬럼과 SQL 정의의 원천은 `docs/DB_DESIGN.md`입니다.  
> 이 문서는 테이블의 책임, 관계, 화면별 데이터 원천만 빠르게 확인하기 위한 지도입니다.
> 최종 수정: 2026-07-14

## 테이블 책임

| 테이블 | 책임 | 주 사용처 |
|---|---|---|
| `trades` | 하루 거래 1건의 진입부터 청산까지의 생명주기 | 이력, 통계 |
| `orders` | KIS 주문 요청, 체결, 취소, 실패 결과 | 주문 |
| `partial_exits` | F4 1차 익절 상세 | 이력, 통계 |
| `daily_skips` | 당일 거래 스킵 또는 진입 중단 사유 | 이력, 진단 |
| `asset_snapshots` | KIS 잔고 조회 성공 결과의 감사/장애 분석 이력 | 자산 |

## 관계

```text
trades 1 ── N orders
trades 1 ── N partial_exits
orders 1 ── N partial_exits

daily_skips      # date 기준 업무 연결, FK 없음
asset_snapshots  # 조회 시각 기준 누적 이력, FK 없음
```

`daily_skips`는 `trades.date`와 같은 거래일 개념을 공유하지만 거래가 생성되지 않은 날도 기록해야 하므로 FK를 두지 않습니다.

`asset_snapshots`는 특정 거래의 하위 데이터가 아니라 KIS 잔고 조회 시점별 계좌 상태입니다. 주문 판단 근거를 사후 확인하기 위한 이력이라 거래 테이블과 직접 연결하지 않습니다.

## 화면별 원천

| 화면 | 주 원천 | 보조 원천 |
|---|---|---|
| 오늘 | `/api/status`, JSONL 로그, 원시 tick/분 단위 메모리 버퍼 | `trade_marks`, `asset_snapshots` fallback |
| 자산 | KIS 잔고 조회 `/api/assets` | 마지막 `asset_snapshots` |
| 주문 | `orders` + `trades` JOIN | JSONL 로그 이벤트 |
| 이력 | `trades` | `orders`, `partial_exits` |
| 통계 | `trades` | `partial_exits` |

## 저장 정책

| 데이터 | 저장 방식 |
|---|---|
| 주문/체결 | 발생 즉시 `orders` 저장 |
| 거래 생명주기 | 진입 시 `trades` 생성, 청산 시 갱신 |
| 1차 익절 | 발생 시 `partial_exits` 저장 |
| 거래 스킵 | 당일 1행을 `daily_skips` 저장 |
| 자산 조회 | KIS 조회 성공 시마다 `asset_snapshots` 저장 |
| 진행 단계/진단 이벤트 | DB가 아니라 JSONL 로그 기준 |
| 원시 tick | 메모리 deque 최대 5,000개, 프로세스 재시작 또는 거래일 변경 시 초기화 |
| 분 단위 가격 | 메모리 deque 최대 180분, 같은 분의 마지막 가격과 tick 수 유지 |

## 오늘 가격흐름 조합

- `tick_history`: 진입 이후의 최근 원시 tick. 최근 구간의 세밀한 선과 SSE 증분 갱신에 사용합니다.
- `minute_price_history`: 원시 tick 버퍼가 20분을 담지 못할 때 앞 구간을 보완합니다.
- `trade_marks`: 당일 `orders`의 체결 데이터를 종목별·체결시각 오름차순으로 조회해 매수/매도 마커와 가격 fallback에 사용합니다.
- 위 세 데이터는 운영 화면용 조회 모델이며 SQLite에 새로운 시세 테이블을 만들지 않습니다.

## 유지보수 규칙

- 컬럼 추가/제약 변경은 `src/db.py`와 `docs/DB_DESIGN.md`를 먼저 갱신합니다.
- 이 문서에는 컬럼 표를 중복 작성하지 않습니다.
- `asset_snapshots.raw_json`에는 계좌 상태 값이 포함될 수 있으므로 외부 공유 대상에서 제외합니다.
