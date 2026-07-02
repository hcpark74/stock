# DAILY 1 갭업 자동매매 시스템

KOSPI/KOSDAQ 장전 갭업 후보를 자동으로 찾고, 09:00 전후 진입부터 11:00 전량 청산까지 관리하는 Python 자동매매 봇입니다. 한국투자증권(KIS) OpenAPI를 사용하며 `PAPER` 모의투자와 `REAL` 실계좌 전환을 지원합니다.

## 핵심 흐름

```text
F1 09:00~09:10  후보 스캔: 갭/유동성 필터 + 예상체결가 보강
F2 09:10        대상 종목 잠금: 유동성, 예상금액, VI 근접 여부 확인
F3 09:10:10     진입: 갭 재검증, 매수 주문, 미체결 시 짧은 재시도
F4 진입 후       보유 추적: WebSocket/REST 가격 추적, Step Trailing, Hard Stop
F5 11:00        타임아웃 청산: 남은 수량 시장가 전량 청산
```

F2에서 대상 종목이 잠기면 F3는 기본적으로 매수 실행을 시도합니다. 단, 당일 스킵, 대상 없음, 갭 재검증 실패, 가격 조회 불가, 주문가능수량 0, 상태 충돌처럼 명확한 사유가 있으면 `F3_ENTRY_BLOCKED`로 이유를 남기고 진입을 막습니다. F3 진입이 실패하면 주문 취소와 실패 사유를 로그로 남기고 당일 진입을 종료합니다. UI 하단 파이프라인은 현재 포지션 상태뿐 아니라 오늘 로그 기준 진행 단계도 반영하므로, 진입 실패 후 `IDLE`로 돌아가도 F3 실패까지 진행된 것으로 표시됩니다.

## 설치

Python 3.12 기준으로 개발되었습니다.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -r requirements-dev.txt
```

## 환경변수

`.env.example`을 복사해 `.env`를 만들고 실제 값을 입력합니다.

```powershell
copy .env.example .env
```

주요 값:

```env
KIS_MODE=PAPER
KIS_APP_KEY=your_app_key_here
KIS_APP_SECRET=your_app_secret_here
KIS_ACCOUNT_NO=12345678-01
KIS_ACCOUNT_TYPE=01
KIS_BASE_URL=https://openapivts.koreainvestment.com:29443
KIS_WS_URL=ws://ops.koreainvestment.com:31000

TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here

KIS_RATE_INTERVAL_SEC=0.20
F1_EXPECTED_QUOTE_CONCURRENCY=1
F1_MARKET_INTERVAL_SEC=3.0
F2_RETRY_F1_ON_FAIL=1
F2_RETRY_F1_INTERVAL_SEC=30
F2_RETRY_F1_MIN_REMAINING_SEC=2

F3_ENTRY_MAX_ATTEMPTS=2
F3_ENTRY_RETRY_DELAY_SEC=0.5
F3_ENTRY_FIRST_FILL_SEC=12.0
F3_ENTRY_RETRY_FILL_SEC=8.0
F3_ENTRY_RETRY_DEADLINE=09:11:00
F3_PRE_ORDER_QUIET_SEC=1.5
F3_FIRST_ORDER_AT=09:10:20
F3_PYRAMID_AT=09:10:40
F3_PYRAMID_FILL_SEC=10.0
```

`F2_RETRY_F1_ON_FAIL`은 모의투자(`PAPER`) 실험용으로 기본 예시에 활성화되어 있습니다. 실계좌(`REAL`) 코드 기본값은 비활성이지만, `.env`에 `F2_RETRY_F1_ON_FAIL=1`이 남아 있으면 명시적으로 켜지므로 REAL 전환 전에는 `0`으로 바꾸세요. F2에서 후보가 모두 제외되면 09:10 전까지만 F1을 다시 시도하며, 데드라인까지 `F2_RETRY_F1_MIN_REMAINING_SEC`보다 적게 남았거나 `DRY_RUN=1`이면 재시도하지 않습니다. 예약된 F2 시각도 09:10이므로 이 재시도는 주로 09:00 F1 직후 체이닝 경로에서 의미가 있습니다.

실계좌 전환 시에는 `KIS_MODE=REAL`, 실계좌 URL, 실계좌 번호를 모두 확인한 뒤 소액으로 검증하세요.

## 실행

```powershell
.\.venv\Scripts\Activate.ps1
python main.py
```

실행 후:

- 스케줄러가 KST 기준으로 F1~F5 작업을 자동 실행합니다.
- 09:00~09:11 사이에 켜면 catch-up으로 F1/F2/F3를 보완 실행합니다.
- Web UI는 기본 `http://localhost:8080`에서 열립니다.
- Security: `/api/status` and `/api/assets` can expose account asset values. The UI server binds to `127.0.0.1` by default; set `UI_HOST=0.0.0.0` only on a trusted network.
- 로그는 `data/logs/YYYYMMDD.jsonl`에 기록됩니다.
- DB는 `data/db/trading.db`를 사용합니다.

로그 실시간 확인:

```powershell
Get-Content data\logs\(Get-Date -Format 'yyyyMMdd').jsonl -Wait
```

종료:

```powershell
# 권장: 실행 터미널에서 Ctrl+C

# 필요 시 PID 파일 기반 종료
Stop-Process -Id (Get-Content main.pid) -Force
```

## DRY_RUN

외부 KIS 인증/API/주문/WebSocket 없이 F1~F4 흐름을 확인하는 안전한 테스트 모드입니다.

```env
DRY_RUN=1
DRY_RUN_TICKER=005930
DRY_RUN_PREV_CLOSE=10000
DRY_RUN_EXPECTED_PRICE=10300
DRY_RUN_EXPECTED_QTY=500000
DRY_RUN_ENTRY_PRICE=10300
DRY_RUN_ENTRY_QTY=10
```

DRY_RUN 데이터는 운영 데이터와 분리됩니다.

```text
data/dry_run/logs
data/dry_run/state
data/dry_run/db
```

## Web UI

FastAPI 서버가 봇과 같은 이벤트 루프에서 실행됩니다.

| 화면 | 내용 |
|---|---|
| 오늘 | 현재 상태, F1 후보, 가격/손익, 하단 진행 파이프라인, 이벤트 로그 |
| 우선 선정 | F1 스냅샷 후보 목록과 통과 가능성 우선 정렬 |
| 이력 | SQLite 거래 이력 |
| 통계 | 승률, 평균 손익, 청산 사유별 성과 |

하단 파이프라인은 `/api/status`의 `pipeline_stage`, `pipeline_failed`를 사용합니다. 예를 들어 오늘 F3에서 미체결 실패가 발생하면 상태가 다시 `IDLE`이어도 F3 실패 단계가 유지됩니다.

## Telegram 알림

Telegram 알림은 내부 로그 코드가 아니라 운영자가 바로 이해할 수 있는 형식으로 전송됩니다.

```text
긴급: 전일 포지션 잔류 의심
상황: 이전 거래일 상태 파일에 포지션 정보가 남아 있습니다.
조치: 계좌 보유 수량과 미체결 주문을 확인하고, 필요하면 수동 정리 후 재시작하세요.
세부: date=20260630
코드: STALE_POSITION_DETECTED
```

형식은 `제목 -> 상황 -> 조치 -> 세부 -> 코드` 순서입니다.

## 프로젝트 구조

```text
main.py
src/
  api/
    auth.py             # KIS OAuth2 토큰 관리
    kis_rest.py         # KIS REST 클라이언트 + 전역 rate limit
    kis_ws.py           # KIS WebSocket 클라이언트
    server.py           # FastAPI Web UI API
  modules/
    f1_filter.py        # F1 후보 스캔, 예상체결가 보강, 스냅샷 저장
    f2_lockup.py        # F2 대상 종목 잠금
    f3_entry.py         # F3 진입 주문, 재시도, 실패 로그
    f4_tracking.py      # F4 Step Trailing / Hard Stop
    f5_timeout.py       # F5 11시 청산
  db.py                 # SQLite CRUD
  live.py               # UI/WS 공유 라이브 상태
  notifier.py           # Telegram 알림 큐와 문구 포매터
  scheduler.py          # APScheduler 등록
  state.py              # 인메모리 상태 + today_state.json 복구
  utils/
    logger.py           # JSONL 이벤트 로그
    spike_filter.py
    time_sync.py
docs/
  PRD.md
  DEV_ENV.md
  CODING_GUIDELINES.md
  UI_DESIGN.md
  DB_DESIGN.md
  SPRINT.md
  html/                 # Web UI 정적 파일
tests/
```

## 테스트

현재 기준 검증 명령:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_kis_rest.py tests\test_f1_filter.py tests\test_f2_lockup.py tests\test_f3_entry.py tests\test_f4_step_trailing.py tests\test_api_server.py tests\test_notifier.py -q -p no:cacheprovider
.\.venv\Scripts\python.exe -m ruff check src\notifier.py tests\test_notifier.py
```

## 주의사항

- 장중에는 PC와 프로세스가 계속 실행 중이어야 합니다.
- `.env`는 커밋하지 않습니다.
- `REAL` 전환 전에는 PAPER에서 주문/체결/취소 흐름을 충분히 검증하세요.
- KIS 모의투자는 장전 예상체결가, KOSDAQ 랭킹 조회 등 일부 응답이 실계좌와 다를 수 있습니다.
- `STALE_POSITION_DETECTED`가 오면 실제 계좌 보유 수량과 미체결 주문을 먼저 확인하세요.
