[개발환경] 데일리 갭 자동매매 시스템 개발환경 설정 가이드

문서 버전: v1.0
작성일: 2026년 6월 23일
대상 OS: Windows 11 (운영 환경) / Windows 11 또는 WSL2 (개발 환경)
연관 문서: PRD.md

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. 시스템 요구사항
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  항목                | 최소           | 권장
  ─────────────────  |---------------|──────────────────
  OS                  | Windows 10 64bit | Windows 11 Pro
  Python              | 3.11           | 3.12
  RAM                 | 4GB            | 8GB
  디스크 여유 공간      | 10GB           | 50GB (2년 로그 기준)
  인터넷               | 유선 100Mbps   | 유선 + LTE 백업
  시스템 시각          | NTP 동기화 필수 | 오차 ±200ms 이내

● Python 3.11 이상 필수 이유:
  asyncio 안정성 개선, tomllib 내장, Self 타입 힌트 지원.
  3.12 권장: 더 낮은 asyncio 오버헤드, 더 명확한 예외 메시지.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
2. 프로젝트 디렉토리 구조
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  stock/                        # 프로젝트 루트
  ├── .env                      # API 키 및 환경변수 (git 제외)
  ├── .env.example              # 환경변수 템플릿 (git 포함)
  ├── .gitignore
  ├── requirements.txt
  ├── requirements-dev.txt      # 개발/테스트 전용
  ├── main.py                   # 진입점 (스케줄러 부트스트랩)
  │
  ├── src/                      # 핵심 소스
  │   ├── __init__.py
  │   ├── state.py              # 전역 State 스키마 및 atomic 조작
  │   ├── scheduler.py          # APScheduler 설정 (F1~F5 등록)
  │   ├── notifier.py           # Telegram 알림 비동기 큐
  │   │
  │   ├── modules/
  │   │   ├── __init__.py
  │   │   ├── f1_filter.py      # F1: 갭/유동성 필터링
  │   │   ├── f2_lockup.py      # F2: 타겟 락업 엔진
  │   │   ├── f3_entry.py       # F3: 진입 주문 모듈
  │   │   ├── f4_tracking.py    # F4: 장중 추적 스탑
  │   │   └── f5_timeout.py     # F5: 11시 타임아웃 청산
  │   │
  │   ├── api/
  │   │   ├── __init__.py
  │   │   ├── kis_rest.py       # KIS REST API 래퍼 (rate limit 포함)
  │   │   ├── kis_ws.py         # KIS WebSocket 클라이언트
  │   │   └── auth.py           # 토큰 발급/갱신/캐시 관리
  │   │
  │   └── utils/
  │       ├── __init__.py
  │       ├── time_sync.py      # NTP 검증
  │       ├── logger.py         # JSON Lines 로거 설정
  │       └── spike_filter.py   # 시세 스파이크 필터
  │
  ├── data/                     # 런타임 데이터 (git 제외)
  │   ├── logs/                 # YYYYMMDD.jsonl
  │   ├── state/                # today_state.json
  │   ├── params/               # history.json
  │   └── auth/                 # token_cache.json
  │
  ├── tests/
  │   ├── __init__.py
  │   ├── test_state.py
  │   ├── test_f1_filter.py
  │   ├── test_f4_tracking.py
  │   └── fixtures/             # 테스트용 mock 시세 데이터
  │
  ├── scripts/
  │   ├── init_dirs.py          # data/ 하위 디렉토리 초기화
  │   ├── watchdog_check.py     # Task Scheduler에서 호출하는 워치독
  │   └── backtest.py           # 백테스트 진입점 (§8 최적화)
  │
  └── docs/
      ├── PRD.md
      └── DEV_ENV.md            # 이 문서

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
3. Python 환경 설정
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

──────────────────────────────────────────────────
3-1. Python 설치 확인
──────────────────────────────────────────────────

  python --version          # 3.11.x 또는 3.12.x 확인
  python -m pip --version   # pip 최신 버전 확인

  Python이 없으면: https://www.python.org/downloads/
  설치 시 "Add python.exe to PATH" 반드시 체크.

──────────────────────────────────────────────────
3-2. 가상환경 생성 및 활성화
──────────────────────────────────────────────────

  # 프로젝트 루트에서 실행 (PowerShell)
  python -m venv .venv

  # 활성화 (PowerShell)
  .\.venv\Scripts\Activate.ps1

  # 활성화 확인 — 프롬프트 앞에 (.venv) 표시되어야 함
  python --version

  ※ PowerShell 실행 정책 오류 발생 시:
     Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser

──────────────────────────────────────────────────
3-3. 패키지 설치
──────────────────────────────────────────────────

  # 운영 패키지
  pip install -r requirements.txt

  # 개발/테스트 패키지 추가 설치
  pip install -r requirements-dev.txt

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
4. 패키지 목록 (requirements.txt)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

──────────────────────────────────────────────────
requirements.txt (운영)
──────────────────────────────────────────────────

  # HTTP 클라이언트 (비동기, rate limit 레이어 구현 용이)
  httpx==0.27.*

  # WebSocket 클라이언트
  websockets==13.*

  # 비동기 스케줄러 (APScheduler asyncio 백엔드)
  APScheduler==3.10.*

  # 환경변수 관리 (.env 파일 로드)
  python-dotenv==1.0.*

  # NTP 시각 동기화 검증
  ntplib==0.4.*

  # 설정 파일 파싱 (선택 — config.toml 사용 시)
  # tomli==2.0.*   # Python 3.11+ 는 내장 tomllib 사용

──────────────────────────────────────────────────
requirements-dev.txt (개발/테스트 전용)
──────────────────────────────────────────────────

  -r requirements.txt

  # 테스트 프레임워크
  pytest==8.*
  pytest-asyncio==0.23.*

  # HTTP mock (KIS API 테스트용)
  respx==0.21.*

  # WebSocket mock
  pytest-mock==3.14.*

  # 코드 스타일
  ruff==0.4.*

  # 타입 검사
  mypy==1.10.*

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
5. 환경변수 설정 (.env)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

──────────────────────────────────────────────────
.env.example (git에 포함 — 실제 값 없음)
──────────────────────────────────────────────────

  # ── KIS API ──────────────────────────────────────
  KIS_APP_KEY=your_app_key_here
  KIS_APP_SECRET=your_app_secret_here
  KIS_ACCOUNT_NO=your_account_number         # 예: 12345678-01
  KIS_ACCOUNT_TYPE=01                        # 01: 종합, 03: 선물옵션
  KIS_BASE_URL=https://openapi.koreainvestment.com:9443

  # ── 운영 모드 ──────────────────────────────────────
  # REAL: 실계좌 / PAPER: 모의투자 (기본값)
  KIS_MODE=PAPER

  # ── Telegram ──────────────────────────────────────
  TELEGRAM_BOT_TOKEN=your_bot_token_here
  TELEGRAM_CHAT_ID=your_chat_id_here

  # ── 시스템 ────────────────────────────────────────
  NTP_SERVER=pool.ntp.org
  LOG_DIR=data/logs
  STATE_DIR=data/state
  PARAMS_DIR=data/params
  AUTH_DIR=data/auth

──────────────────────────────────────────────────
실제 .env 파일 생성
──────────────────────────────────────────────────

  # .env.example을 복사 후 실제 값으로 채움
  copy .env.example .env
  # 이후 .env 파일을 편집기로 열어 값 입력

  ● .env는 절대 git commit 하지 않음 (.gitignore에 포함).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
6. KIS API 설정
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

──────────────────────────────────────────────────
6-1. KIS Open API 신청 절차
──────────────────────────────────────────────────

  1. 한국투자증권 계좌 개설 (비대면 가능).
  2. KIS Developers 포털 접속: https://apiportal.koreainvestment.com
  3. 앱 등록 → App Key / App Secret 발급.
  4. 모의투자 신청 (선택): 실계좌 전 테스트용.
     모의투자 URL: https://openapivts.koreainvestment.com:29443

──────────────────────────────────────────────────
6-2. 모의투자(PAPER) vs 실계좌(REAL) 환경 분리
──────────────────────────────────────────────────

  구분          | Base URL                                        | KIS_MODE
  ─────────────|─────────────────────────────────────────────── |─────────
  모의투자       | https://openapivts.koreainvestment.com:29443   | PAPER
  실계좌         | https://openapi.koreainvestment.com:9443       | REAL

  ● KIS_MODE=PAPER 상태에서는 실제 주문이 발생하지 않음.
  ● 개발 및 테스트는 반드시 PAPER 모드에서 진행.
  ● REAL 전환 전 아래 항목 최종 확인:
    □ .env의 KIS_MODE=REAL로 변경
    □ KIS_BASE_URL을 실계좌 URL로 변경
    □ 계좌번호(KIS_ACCOUNT_NO) 실계좌 번호로 변경
    □ 잔고 확인 (소액 테스트 권장)

──────────────────────────────────────────────────
6-3. WebSocket 접속 정보
──────────────────────────────────────────────────

  모의투자 WS: ws://ops.koreainvestment.com:31000
  실계좌  WS: ws://ops.koreainvestment.com:21000

  구독 TR: H0STCNT0 (주식 체결 실시간 조회)
  승인 방식: sendMessage로 구독 요청 시 Access Token 포함.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
7. Telegram Bot 설정
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  1. Telegram에서 @BotFather 검색 → /newbot 명령 실행.
  2. Bot 이름 및 username 입력 → API Token 발급.
  3. 발급된 토큰을 TELEGRAM_BOT_TOKEN에 저장.

  4. Chat ID 확인 방법:
     a. 발급된 Bot과 1:1 채팅 시작 (아무 메시지나 전송).
     b. 브라우저에서 아래 URL 접속:
        https://api.telegram.org/bot{TOKEN}/getUpdates
     c. 응답 JSON에서 "chat" → "id" 값 확인.
     d. 해당 값을 TELEGRAM_CHAT_ID에 저장.

  5. 테스트 알림 발송 확인 (PowerShell):
     $TOKEN = $env:TELEGRAM_BOT_TOKEN
     $CHAT  = $env:TELEGRAM_CHAT_ID
     Invoke-RestMethod -Uri "https://api.telegram.org/bot$TOKEN/sendMessage" `
       -Method POST `
       -Body @{ chat_id=$CHAT; text="[TEST] 알림 채널 연결 확인" }

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
8. NTP 시간 동기화 설정 (Windows)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  PRD §4 요건: 시스템 클럭 오차 ±200ms 이내. 500ms 초과 시 CRIT 알림.

──────────────────────────────────────────────────
8-1. Windows Time Service 설정 (관리자 PowerShell)
──────────────────────────────────────────────────

  # NTP 서버 설정 (pool.ntp.org 권장)
  w32tm /config /manualpeerlist:"pool.ntp.org,0x9" /syncfromflags:manual /reliable:YES /update

  # Windows Time 서비스 재시작
  Restart-Service w32tm

  # 즉시 동기화
  w32tm /resync /force

  # 동기화 상태 확인
  w32tm /query /status

  "Leap Indicator: 0 (no warning)" 및 "Stratum: 3" 이하 확인.

──────────────────────────────────────────────────
8-2. 애플리케이션 레벨 NTP 검증 (시작 시 자동 실행)
──────────────────────────────────────────────────

  # src/utils/time_sync.py 에서 다음 로직 구현
  # ntplib를 사용하여 시스템 시각 오차를 측정하고
  # 허용 범위(±200ms) 초과 시 CRIT 알림 발송.

  허용 기준:
    오차 <= 200ms  → 정상 (INFO 로그)
    200ms < 오차 <= 500ms → WARN 로그
    오차 > 500ms  → CRIT 알림 + 운영자 확인 요청

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
9. 데이터 디렉토리 초기화
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  # scripts/init_dirs.py 실행 (최초 1회)
  python scripts/init_dirs.py

  위 스크립트는 아래 디렉토리를 생성한다:
    data/logs/
    data/state/
    data/params/
    data/auth/

  ● data/ 전체는 .gitignore에 추가 (API 토큰, 포지션 상태 등 민감 정보 포함).
  ● data/params/history.json 은 최초 빈 배열 [] 로 초기화.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
10. .gitignore 설정
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  .venv/
  .env
  __pycache__/
  *.pyc
  *.pyo
  .mypy_cache/
  .ruff_cache/
  .pytest_cache/
  data/
  *.log

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
11. 프로세스 워치독 설정 (Windows Task Scheduler)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  PRD §6-7 요건: 프로세스 사망 시 1분 이내 자동 재시작.

──────────────────────────────────────────────────
11-1. 작업 스케줄러 등록 (관리자 PowerShell)
──────────────────────────────────────────────────

  $Action = New-ScheduledTaskAction `
    -Execute "C:\path\to\.venv\Scripts\python.exe" `
    -Argument "C:\path\to\stock\scripts\watchdog_check.py" `
    -WorkingDirectory "C:\path\to\stock"

  $Trigger = New-ScheduledTaskTrigger `
    -RepetitionInterval (New-TimeSpan -Minutes 1) `
    -Once `
    -At (Get-Date)

  $Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 1) `
    -RestartCount 0

  Register-ScheduledTask `
    -TaskName "StockBot_Watchdog" `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -RunLevel Highest

  ※ C:\path\to\ 부분은 실제 절대 경로로 교체.

──────────────────────────────────────────────────
11-2. watchdog_check.py 동작 명세
──────────────────────────────────────────────────

  1. main.py 프로세스 실행 여부 확인 (psutil 사용 또는 PID 파일 방식).
  2. 프로세스 없음 → main.py 재시작.
  3. 장 종료 시간(10:01 이후) 에는 재시작 하지 않음.
  4. 재시작 시 Telegram 알림: PROCESS_RESTART_DETECTED.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
12. 실행 방법
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

──────────────────────────────────────────────────
12-1. 정상 실행 (운영)
──────────────────────────────────────────────────

  # 가상환경 활성화
  .\.venv\Scripts\Activate.ps1

  # 실행
  python main.py

  ● main.py는 08:30에 KIS 토큰 갱신 및 NTP 검증을 수행하고,
    이후 APScheduler에 F1~F5를 등록한 뒤 이벤트 루프를 유지한다.
  ● 11:00 청산 완료 후 다음 날 08:30까지 대기 상태로 유지된다 (종료 안 함).

──────────────────────────────────────────────────
12-2. 개발/테스트 실행
──────────────────────────────────────────────────

  # 단위 테스트
  pytest tests/ -v

  # 특정 모듈만 테스트
  pytest tests/test_f4_tracking.py -v

  # 전체 커버리지
  pytest tests/ --cov=src --cov-report=term-missing

──────────────────────────────────────────────────
12-3. 코드 품질 검사
──────────────────────────────────────────────────

  # 린트 + 자동 수정
  ruff check src/ --fix

  # 타입 검사
  mypy src/

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
13. 개발 환경 체크리스트
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  최초 환경 설정 완료 여부를 아래 순서로 확인한다.

  □ Python 3.11+ 설치 및 PATH 등록 확인
  □ 가상환경 생성 및 활성화 확인
  □ requirements.txt 패키지 설치 완료
  □ .env 파일 생성 및 KIS API Key 입력 완료
  □ KIS_MODE=PAPER 확인 (실계좌 전환 전)
  □ data/ 디렉토리 초기화 완료 (init_dirs.py 실행)
  □ NTP 동기화 상태 확인 (w32tm /query /status)
  □ Telegram Bot 알림 테스트 발송 확인
  □ PAPER 모드 단순 주문 API 호출 테스트 성공
  □ WebSocket 체결 데이터 수신 테스트 성공
  □ pytest 전체 통과 확인
  □ Task Scheduler 워치독 등록 완료

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
14. 알려진 제약 및 주의사항
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

● KIS API 모의투자 제한:
  - 모의투자 환경은 장전 예상 체결가(F1 데이터 소스)가 실계좌 대비
    일부 제한될 수 있음. 실계좌 전환 전 데이터 품질 확인 필요.
  - WebSocket 체결 데이터 지연이 실계좌보다 클 수 있음.

● Windows 절전 모드:
  운영 PC의 절전/화면 보호기를 반드시 비활성화.
  절전 진입 시 스케줄러 타이밍 오차 발생 가능.
  설정 경로: 전원 관리 → 절전 모드 → "안 함"

● KIS API 점검 시간:
  평일 05:00~07:00 (API 정기 점검 가능).
  08:30 토큰 갱신 로직이 이 시간대 이후 실행되므로 일반적으로 무관.
  그러나 점검 연장 시 08:30 토큰 갱신 실패 가능 → CRIT 알림으로 감지.

● 방화벽:
  KIS REST API (포트 9443) 및 WebSocket (포트 21000/31000) 아웃바운드 허용 필요.
  회사 네트워크 사용 시 방화벽 예외 등록 확인.
---

## 15. 2026-07-01 운영 변수 및 DRY_RUN 업데이트

### 추가 환경변수

아래 값은 `.env.example`에 반영되어 있으며, 실제 운영 시 `.env`에서 조정한다.

```env
DRY_RUN=0
DRY_RUN_TICKER=005930
DRY_RUN_PREV_CLOSE=10000
DRY_RUN_EXPECTED_PRICE=10300
DRY_RUN_EXPECTED_QTY=500000
DRY_RUN_ENTRY_PRICE=10300
DRY_RUN_ENTRY_QTY=10
DRY_RUN_STEP_DELAY=0.2
DRY_RUN_LOG_DIR=data/dry_run/logs
DRY_RUN_STATE_DIR=data/dry_run/state
DRY_RUN_DB_DIR=data/dry_run/db

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

`F2_RETRY_F1_ON_FAIL`은 모의투자(`PAPER`) 실험용으로 env 예시에 활성화되어 있다. 실계좌(`REAL`) 코드 기본값은 비활성이지만, `.env`에 `F2_RETRY_F1_ON_FAIL=1`이 남아 있으면 명시적으로 켜지므로 REAL 전환 전에는 `0`으로 변경한다. F2 실패 후 재시도는 F1 deadline인 09:10 전까지만 수행하며, 데드라인까지 `F2_RETRY_F1_MIN_REMAINING_SEC`보다 적게 남았거나 `DRY_RUN=1`이면 재시도하지 않는다. 예약 F2 fallback 경로보다는 09:00 F1 직후 체이닝 경로에서 주로 의미가 있다.

### DRY_RUN 실행 목적

- 실계좌/모의계좌 주문 없이 F1 -> F4 흐름을 확인한다.
- DRY_RUN 실행 시 로그, 상태, DB는 `data/dry_run/*` 경로를 사용한다.
- 외부 KIS 인증, 주문, WebSocket을 건너뛰므로 안전한 회귀 테스트에 사용한다.

### KIS rate limit 운영 기준

- REST 호출은 `KIS_RATE_INTERVAL_SEC` 기준으로 전역 직렬화한다.
- F1 예상체결가 보강은 `F1_EXPECTED_QUOTE_CONCURRENCY`로 동시 작업 수를 제한한다.
- F1 KOSPI/KOSDAQ 랭킹 조회 사이에는 `F1_MARKET_INTERVAL_SEC` 간격을 둔다.
- F3 매수 주문 직전에는 `F3_PRE_ORDER_QUIET_SEC`만큼 대기해 직전 조회 호출과 주문 호출이 붙지 않게 한다.
- KOSDAQ 랭킹 조회가 KIS 응답 코드 `OPSQ2001` 등으로 실패할 수 있으므로, F1 로그의 `market`, `rt_cd`, `msg_cd`를 함께 확인한다.

### 테스트 명령

현재 검증 기준은 가상환경 파이썬을 명시해서 실행한다.

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_kis_rest.py tests\test_f1_filter.py tests\test_f2_lockup.py tests\test_f3_entry.py tests\test_f4_step_trailing.py tests\test_api_server.py tests\test_notifier.py -q -p no:cacheprovider
.\.venv\Scripts\python.exe -m ruff check src\notifier.py tests\test_notifier.py
```

### Telegram 알림 확인

- 알림 메시지는 `제목 -> 상황 -> 조치 -> 세부 -> 코드` 순서로 표시된다.
- `STALE_POSITION_DETECTED`처럼 조치가 필요한 이벤트는 이벤트 코드보다 사람이 읽는 제목을 우선한다.
- Telegram 전송에는 Markdown `parse_mode`를 사용하지 않는다. 이벤트 코드의 `_` 문자나 한글 문구가 파싱 오류를 만들지 않게 하기 위함이다.
