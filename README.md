# DAILY 1 갭업 자동매매 시스템

KOSPI/KOSDAQ 장전 갭업 후보를 자동으로 찾고, 09:00 전후 진입부터 11:00 전량 청산까지 사람 개입 없이 관리하는 Python 자동매매 봇입니다. 한국투자증권(KIS) OpenAPI를 사용하며, `PAPER` 모의투자와 `REAL` 실계좌 전환을 지원합니다.

## 목차

1. [이 프로젝트는 무엇인가요?](#1-이-프로젝트는-무엇인가요) — 비개발자를 위한 소개
2. [하루 매매 흐름 (F1~F5)](#2-하루-매매-흐름-f1f5) — 봇이 매일 아침 하는 일
3. [꼭 알아야 할 주의사항](#3-꼭-알아야-할-주의사항) — 위험 고지 · REAL 전환 체크리스트 · 장애 대응 절차
4. [시작하기](#4-시작하기) — 설치 · 환경변수 · 실행
5. [안전 테스트 모드 (DRY_RUN)](#5-안전-테스트-모드-dry_run)
6. [화면 안내 (Web UI)](#6-화면-안내-web-ui)
7. [Telegram 알림](#7-telegram-알림)
8. [개발자 안내](#8-개발자-안내) — 프로젝트 구조 · Web UI 내부 동작 · 테스트

---

## 1. 이 프로젝트는 무엇인가요?

**갭업(Gap-Up)** 이란 어떤 종목이 전날 종가보다 눈에 띄게 높은 가격으로 장을 시작하는 현상을 말합니다. 이 시스템은 "갭업으로 시작한 종목은 장 초반에 추가 상승 흐름이 이어지는 경우가 많다"는 전략을 자동화한 것입니다.

핵심 방향성은 다음 세 가지입니다.

- **하루 1회, 짧은 승부**: 매일 장 시작 직후(09:00~09:11)에 조건에 맞는 종목 하나에 진입하고, 늦어도 11:00에는 전량 매도하고 하루를 마칩니다. 밤새 보유하는 위험(오버나이트 리스크)이 없습니다.
- **사람 개입 최소화**: 종목 선정, 매수, 손절/익절 추적, 최종 청산까지 전 과정이 자동입니다. 운영자는 Web UI와 Telegram 알림으로 상태를 지켜보기만 하면 됩니다.
- **안전 우선**: 실계좌(`REAL`)에 연결하기 전에 모의투자(`PAPER`)와 완전 오프라인 테스트(`DRY_RUN`)로 충분히 검증할 수 있도록 3단계 실행 모드를 제공합니다. 조건이 애매하면 매수하지 않고 그날은 쉬는 것이 기본 동작입니다.

## 2. 하루 매매 흐름 (F1~F5)

봇의 하루는 F1부터 F5까지 5단계로 나뉩니다. 각 단계는 정해진 시각에 자동으로 실행됩니다.

```text
F1 09:00~09:10  후보 스캔: 갭/유동성 필터 + 예상체결가 보강
F2 09:10        대상 종목 잠금: 유동성, 예상금액, VI 근접 여부 확인
F3 09:10:10     진입: 갭 재검증, 매수 주문, 미체결 시 짧은 재시도
F4 진입 후       보유 추적: WebSocket/REST 가격 추적, Step Trailing, Hard Stop
F5 11:00        타임아웃 청산: 남은 수량 시장가 전량 청산
```

쉽게 풀면 이렇습니다.

| 단계 | 하는 일 | 비유 |
|---|---|---|
| F1 | 갭업한 종목들을 훑어보고 후보 목록을 만듭니다 | 서류 심사 |
| F2 | 후보 중 조건을 모두 통과한 종목 하나를 확정합니다 | 최종 면접 |
| F3 | 확정 종목을 실제로 매수합니다 | 계약 |
| F4 | 보유 중 가격을 계속 지켜보며 이익은 지키고 손실은 자릅니다 | 관리 |
| F5 | 11시가 되면 남은 수량을 무조건 전부 팝니다 | 마감 |

### 진입이 막히는 경우

F2에서 대상 종목이 잠기면 F3는 기본적으로 매수 실행을 시도합니다. 단, 당일 스킵, 대상 없음, 갭 재검증 실패, 가격 조회 불가, 주문가능수량 0, 상태 충돌처럼 명확한 사유가 있으면 `F3_ENTRY_BLOCKED`로 이유를 남기고 진입을 막습니다. F3 진입이 실패하면 주문 취소와 실패 사유를 로그로 남기고 당일 진입을 종료합니다. UI 하단 파이프라인은 현재 포지션 상태뿐 아니라 오늘 로그 기준 진행 단계도 반영하므로, 진입 실패 후 `IDLE`로 돌아가도 F3 실패까지 진행된 것으로 표시됩니다.

### 늦게 켜도 이어서 실행 (catch-up)

09:00 이후 F1이 끝나면 F2/F3는 예약 시각만 기다리지 않고 즉시 체인 실행됩니다. 프로세스를 09:00~09:11 사이에 다시 켜는 catch-up 경로도 같은 규칙을 사용하며, F3 예정 시각이 이미 지난 경우에는 `force=True`로 시간 대기 없이 진입 절차를 이어갑니다.

## 3. 꼭 알아야 할 주의사항

### 3.1 금융 및 시스템 장애 위험 고지

이 시스템은 실제 돈으로 주식을 사고파는 프로그램입니다. 사용 전에 아래 위험을 반드시 이해해야 하며, 모든 투자 결과의 책임은 운영자 본인에게 있습니다.

- **원금 손실 위험**: 자동매매는 수익을 보장하지 않습니다. 갭업 종목은 시장에서 변동성이 가장 큰 종목군입니다. Hard Stop(-2.0%)과 Step Trailing(-1.5%)이 기본 방어선이지만, 급락·하한가·VI(변동성완화장치) 발동·시장가 슬리피지 상황에서는 실제 손실이 설정값보다 커질 수 있습니다.
- **거래 비용**: 화면에 표시되는 손익에는 증권사 수수료와 증권거래세 등 거래 비용이 반영되지 않으므로, 실제 계좌 손익은 표시값보다 나쁠 수 있습니다. 이 전략은 매일 매수·매도를 반복하므로 거래 비용이 누적됩니다.
- **손절은 이 프로그램이 켜져 있을 때만 동작합니다**: Hard Stop과 Trailing Stop은 증권사 서버에 예약된 주문이 아니라, 이 봇이 가격을 지켜보다가 직접 매도 주문을 내는 방식입니다. **PC 꺼짐, 절전 모드, 네트워크 단절, 프로세스 종료 중에는 어떤 손절도 실행되지 않고 포지션이 방치됩니다.**
- **외부 장애 위험**: KIS API 점검·지연·응답 스펙 변경, 시세 수신 지연 등으로 주문이 실패하거나 의도와 다른 가격에 체결될 수 있습니다.
- **소프트웨어 결함 위험**: 이 프로그램은 버그가 있을 수 있으며, 예상치 못한 주문을 낼 가능성을 배제할 수 없습니다.
- **운영 전제 조건**: 위 위험 때문에, 장중(09:00~11:00)에는 ① Telegram 긴급(CRIT) 알림을 즉시 받을 수 있는 상태, ② 증권사 MTS/HTS로 직접 수동 청산할 수 있는 준비가 항상 되어 있어야 합니다.

### 3.2 REAL(실계좌) 전환 체크리스트

PAPER 모의투자에서 충분히 검증한 뒤, 아래 항목을 위에서부터 순서대로 확인하세요.

코드 수준의 미해결 위험과 승인 기준은 [실전투자 전환 점검표](docs/REAL_TRADING_CHECKLIST.md)를 함께 확인하세요. 문서의 `REAL-BLOCKER`가 하나라도 남아 있으면 실계좌로 전환하지 마세요.

| # | 확인 항목 | 기준 |
|---|---|---|
| 1 | PAPER에서 진입~청산 전체 사이클(F1~F5) 검증 | 통계 메뉴에서 여러 거래일의 승률·청산 사유 확인 |
| 2 | `DRY_RUN=0` | 시뮬레이션 모드 해제 |
| 3 | `KIS_MODE=REAL` | 실행 모드 전환 |
| 4 | `KIS_BASE_URL=https://openapi.koreainvestment.com:9443`<br>`KIS_WS_URL=ws://ops.koreainvestment.com:21000` | 실계좌 API 주소로 교체 (`.env.example` 주석 참조) |
| 5 | 실전용 `KIS_APP_KEY`/`KIS_APP_SECRET` | 실전 키는 모의투자 키와 별도 발급 |
| 6 | `KIS_ACCOUNT_NO` 실계좌 번호 | 계좌번호·상품코드 재확인 |
| 7 | `F2_RETRY_F1_ON_FAIL=0` | PAPER 실험용 재시도 비활성화 |
| 8 | `F3_ALLOC_RATIO` 하향 검토 | 기본 0.95(주문가능 현금의 95%) — 초기에는 소액 계좌 또는 낮은 비율로 시작 |
| 9 | Telegram 알림 실제 수신 테스트 | 봇 기동 시 알림이 휴대폰에 도착하는지 확인 |
| 10 | 시간 동기화 상태 확인 | 오늘 화면의 NTP 표시가 정상인지, `TIME_SYNC_WARN` 로그가 없는지 |
| 11 | 자산 메뉴에서 예수금·주문가능금액 확인 | 의도한 투입 금액과 일치하는지 |
| 12 | 첫 1~2주는 장중 직접 모니터링 | 09:00~11:00 화면과 알림을 실시간 확인 |

참고: KIS 모의투자는 장전 예상체결가, KOSDAQ 랭킹 조회 등 일부 응답이 실계좌와 다르므로, REAL 전환 직후 며칠은 F1 후보 선정 결과가 PAPER와 달라질 수 있습니다.

### 3.3 청산 실패·통신 장애 대응 절차

Telegram 알림은 `제목 → 상황 → 조치 → 세부 → 코드` 형식으로 옵니다. 긴급(CRIT) 알림의 "조치" 항목을 우선 따르되, 대표 상황별 절차는 다음과 같습니다.

| 상황 (알림 코드) | 시스템이 하는 일 | 운영자가 할 일 |
|---|---|---|
| **11시 청산 실패** (`TIMEOUT_ORDER_FAILED`) | 시장가 매도 후 30초간 전량 체결을 확인, 최대 3회 시도. 재시도 전에는 ① 직전 주문의 체결/미체결 상태를 조회해 미체결 잔량은 취소를 확정하고 ② 실제 잔고를 재조회해 검증된 잔량만 다시 주문(중복 매도 방지). 여러 번 나눠 체결되면 평균 청산가로 손익을 기록. 전량 청산을 확인하지 못하면 긴급 알림 발송 | **즉시** MTS/HTS에 접속해 잔여 수량을 수동 시장가 매도. 이후 계좌와 봇 상태 확인 후 재시작 |
| **청산 완료·체결가 미확인** (`TIMEOUT_CLOSE_UNVERIFIED`) | 잔고가 0으로 확인되어 매도는 완료된 것으로 보이나 체결가 조회에 실패한 경우. 거래를 임의 가격으로 기록하지 않고 알림 발송 | 증권사 주문/체결 내역에서 실제 체결가를 확인하고 거래 기록과 대조 |
| **잔고 없음** (`TIMEOUT_NO_HOLDINGS`) | 10:59:50 잔고 확인에서 실제 보유가 0이면 매도 주문을 내지 않고 알림 발송 (상태 파일과 계좌 불일치 의심) | 계좌 보유/체결 내역과 봇 상태 파일을 대조하고 원인 확인 |
| **손절 매도 주문 오류** (`F4_SELL_ERROR`) | 매도 주문 실패를 긴급 알림으로 통지 | 즉시 MTS/HTS에서 수동 청산 |
| **전일 포지션 잔류 의심** (`STALE_POSITION_DETECTED`) | 전일 상태가 정상 종료(`CLOSED`/`IDLE`)면 알림 없이 파일을 폐기하고 당일 거래 진행 (`STALE_STATE_DISCARDED` 로그만 기록). 그 외(보유/진입 중이거나 상태값 누락·손상)면 긴급 알림과 함께 **당일 자동 진입을 차단**하고, 증거 사본을 `today_state.stale_<날짜>.json`으로 격리한 뒤 나머지 기동은 계속 | 계좌 보유 수량과 미체결 주문을 확인. 문제가 없으면 `data/state/today_state.json` 삭제 후 재시작하면 차단이 해제됨. 잔고가 실제로 남아 있으면 수동 정리 먼저 |
| **WebSocket 시세 끊김** | 2초 이상 무응답이면 REST 1초 폴링으로 자동 전환 (운영자 개입 불필요) | 오늘 화면의 WS 연결 표시만 확인 |
| **장중 프로세스 중단** | 재시작하면 `today_state.json`으로 상태 복구. 09:00~09:11 사이면 F1/F2/F3 catch-up 실행 | 가능한 한 빨리 재시작하고, 보유 중이었다면 재시작 후 가격 추적·스탑이 정상 동작하는지 확인 |
| **PC/네트워크 완전 장애 (장중 보유 중)** | 아무것도 못 함 — 손절 미실행 상태 | 복구가 오래 걸릴 것 같으면 MTS로 직접 청산 판단. "봇이 다시 붙을 때까지 스탑이 없다"를 전제로 결정 |
| **시간 동기화 실패** (`TIME_SYNC_ERROR`) | NTP 서버 조회 실패를 알림 | 시스템 시계가 크게 어긋나지 않았는지 확인 (스케줄 시각이 밀릴 수 있음) |
| **잔고 조회 실패** (`BALANCE_QUERY_FAILED`) | 진입 직전 잔고 조회가 오류(호출 제한 등)를 반환하면 1초 간격으로 최대 3회 재시도. 끝내 실패하면 현금 0으로 오인하지 않고 진입을 차단하며 긴급 알림 발송 (실제 잔고 부족 `INSUFFICIENT_BALANCE`와 구분됨) | KIS API 상태와 계좌를 확인. 일시 장애면 다음 거래일 자동 정상화 |

### 3.4 일반 주의사항

- 장중에는 PC와 프로세스가 계속 실행 중이어야 합니다.
- `.env`(계좌 비밀정보가 담긴 파일)는 절대 커밋하지 않습니다.
- 실계좌(REAL) 모드에서는 기동 시 KIS 휴장일 조회로 개장 여부를 확인해 휴장일에는 당일 작업을 건너뜁니다(모의투자 미지원 TR).

## 4. 시작하기

### 설치

Python 3.12 기준으로 개발되었습니다.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -r requirements-dev.txt
```

### 환경변수

`.env.example`을 복사해 `.env`를 만들고 실제 값을 입력합니다.

```powershell
copy .env.example .env
```

주요 값 (각 줄 위의 `#` 주석이 해당 설정의 의미입니다):

```env
# ── 기본 연결 정보 ─────────────────────────────
# 실행 모드: PAPER=모의투자, REAL=실계좌
KIS_MODE=PAPER
# 한국투자증권 OpenAPI에서 발급받은 앱 키/시크릿
KIS_APP_KEY=your_app_key_here
KIS_APP_SECRET=your_app_secret_here
# 계좌번호와 상품코드(보통 01)
# Account env priority: KIS_ACCT_NO/KIS_ACCT_CD override KIS_ACCOUNT_NO/KIS_ACCOUNT_TYPE.
# Use one pair only in normal operation; KIS_ACCOUNT_* is the documented default.
# Empty KIS_ACCT_* values do not fall back; they are treated as invalid configuration.
KIS_ACCOUNT_NO=12345678-01
KIS_ACCOUNT_TYPE=01
# KIS_ACCT_NO=12345678-01
# KIS_ACCT_CD=01
# API 서버 주소 (아래 예시는 모의투자용 주소)
KIS_BASE_URL=https://openapivts.koreainvestment.com:29443
KIS_WS_URL=ws://ops.koreainvestment.com:31000

# ── Telegram 알림 ─────────────────────────────
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here

# ── API 호출 속도/재시도 ───────────────────────
# API 호출 사이 최소 간격(초) — 증권사 호출 횟수 제한 준수용
# 미설정 시 KIS_MODE 기본값: REAL=0.20 (초당 18건 제한) / PAPER=1.1 (초당 1건 제한)
# KIS_RATE_INTERVAL_SEC=0.20
# 일시적 통신 오류 시 재시도 횟수와 대기 시간(초, 시작~최대)
KIS_MAX_TRANSIENT_RETRIES=2
KIS_TRANSIENT_RETRY_BASE_SEC=1.0
KIS_TRANSIENT_RETRY_MAX_SEC=8.0

# ── F1: 후보 종목 선정 조건 ────────────────────
# 갭(전일 종가 대비 시초가 상승률) 허용 범위: 3% 이상만 후보,
# 3~8%가 선호 구간, 10% 이상은 과열로 제외
F1_GAP_MIN=0.030
F1_GAP_CORE_MAX=0.080
F1_GAP_HARD_MAX=0.100
# 예상 거래대금 최소 기준(원). 기본 1억 — 저유동성 미체결 방지(2026-07-20). 0이면 제한 없음
F1_MIN_EXPECTED_AMOUNT=100000000
# 갭이 큰(선호 구간 초과) 종목은 예상 거래대금 50억 원 이상일 때만 허용
F1_HIGH_GAP_MIN_EXPECTED_AMOUNT=5000000000
# VI(변동성완화장치) 발동 가격까지의 여유: 1% 미만이면 위험으로 배점 제외,
# 3% 이상이면 안전으로 간주
F1_MIN_VI_GAP=0.010
F1_SAFE_VI_GAP=0.030
# 유동성(거래대금) 상위 10% 종목만 후보로 사용, 최소 10개는 확보
F1_SELECTION_TOP_PCT=0.10
F1_MIN_CANDIDATES=10
# 예상체결가 조회 동시 요청 수 / KOSPI·KOSDAQ 시장 간 조회 간격(초)
F1_EXPECTED_QUOTE_CONCURRENCY=1
F1_MARKET_INTERVAL_SEC=3.0

# PAPER 전용 Fast Path 관측 프로브(매매 판단/주문에는 영향 없음)
PAPER_FAST_PROBE=0
PAPER_FAST_PROBE_DIR=data/paper_fast_probe
PAPER_FAST_PROBE_OPEN_OFFSET_MS=300
PAPER_FAST_PROBE_OPEN_MAX_LATENESS_MS=2500
PAPER_FAST_PROBE_OPEN_TIMEOUT_SEC=2.5

# ── F2: 후보 전멸 시 F1 재시도 ─────────────────
# 1이면 F2에서 후보가 모두 탈락했을 때 F1 스캔을 다시 시도 (PAPER 실험용)
F2_RETRY_F1_ON_FAIL=1
# 재시도 간격(초) / 데드라인까지 이 시간(초)보다 적게 남으면 재시도 생략
F2_RETRY_F1_INTERVAL_SEC=5
F2_RETRY_F1_MIN_REMAINING_SEC=2

# ── F3: 매수 주문 실행 ─────────────────────────
# 진입 직전 VI 발동 확인 — 발동 중이면 해당 후보 차단(1=활성, 2026-07-20 미체결 인시던트)
F3_VI_CHECK_ENABLED=1
# 매수 주문 최대 시도 횟수와 재시도 전 대기(초)
F3_ENTRY_MAX_ATTEMPTS=2
F3_ENTRY_RETRY_DELAY_SEC=0.5
# 첫 주문/재시도 주문의 체결 대기 시간(초)
F3_ENTRY_FIRST_FILL_SEC=12.0
F3_ENTRY_RETRY_FILL_SEC=8.0
# 이 시각을 지나면 재시도를 포기하고 당일 진입 종료
F3_ENTRY_RETRY_DEADLINE=09:11:00
# 주문 직전 가격 급변이 없는지 확인하는 대기 시간(초)
F3_PRE_ORDER_QUIET_SEC=1.5
# 1이면 최종 1호 매도호가를 재조회한 뒤 가격 상한 지정가로만 진입
F3_LIMIT_BUY_ENABLED=1
# 재검증 기준가 대비 허용 슬리피지(0.005 = 0.5%)
F3_MAX_ENTRY_SLIPPAGE_RATIO=0.005
# 최종 호가를 주문 전송에 사용할 수 있는 최대 나이(ms)
# PAPER는 1.1초 API 호출 간격을 통과하도록 1500ms 권장(코드 기본값도 1500ms).
# REAL의 코드 기본값은 500ms이며, 공용 .env 예시는 PAPER 기준이다.
F3_FINAL_QUOTE_MAX_AGE_MS=1500
# 상한 지정가 체결 대기 시간(초). 이후 잔량은 취소하고 체결분만 보유
F3_LIMIT_FILL_TIMEOUT_SEC=2.0
# 추가 매수(피라미딩) 실행 시각과 체결 대기 시간(초)
F3_PYRAMID_AT=09:10:40
F3_PYRAMID_FILL_SEC=10.0
```

PAPER Fast Path 관측 범위와 다음 거래일 판정 기준은
[`docs/PAPER_FAST_PATH_PROBE.md`](docs/PAPER_FAST_PATH_PROBE.md)를 참고하세요.

`F2_RETRY_F1_ON_FAIL`은 모의투자(`PAPER`) 실험용으로 기본 예시에 활성화되어 있습니다. 실계좌(`REAL`) 코드 기본값은 비활성이지만, `.env`에 `F2_RETRY_F1_ON_FAIL=1`이 남아 있으면 명시적으로 켜지므로 REAL 전환 전에는 `0`으로 바꾸세요. F2에서 후보가 모두 제외되면 09:10 전까지만 F1을 다시 시도하며, 데드라인까지 `F2_RETRY_F1_MIN_REMAINING_SEC`보다 적게 남았거나 `DRY_RUN=1`이면 재시도하지 않습니다. 예약된 F2 시각도 09:10이므로 이 재시도는 주로 09:00 F1 직후 체이닝 경로에서 의미가 있습니다.

`F3_LIMIT_BUY_ENABLED`의 코드 기본값은 활성(`1`)입니다. `F3_FINAL_QUOTE_MAX_AGE_MS`를 직접 설정하지 않으면 PAPER는 1500ms, REAL은 500ms를 사용합니다. PAPER에서 1~1499ms를 지정하면 1.1초 REST 호출 간격과 충돌하지 않도록 유효값을 1500ms로 올리며, `0`은 신선도 가드 비활성 의미로 그대로 유지합니다. 공용 `.env` 예시는 PAPER 기준이므로 REAL 전환 시 더 엄격한 500ms를 원하면 명시적으로 변경하세요. `antc_cnpr` 우선순위는 아직 변경하지 않았고, `F3_RECHECK_QUOTE_FIELDS` 로그로 `antc_cnpr`·`stck_prpr`의 실제 응답을 먼저 관측하는 단계입니다.

### 실행

```powershell
.\.venv\Scripts\Activate.ps1
python main.py
```

실행 후:

- 스케줄러가 KST 기준으로 F1~F5 작업을 자동 실행합니다.
- 09:00~09:11 사이에 켜면 catch-up으로 F1/F2/F3를 보완 실행하며, F1 결과가 나오면 F2/F3를 즉시 이어서 실행합니다.
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

## 5. 안전 테스트 모드 (DRY_RUN)

외부 KIS 인증/API/주문/WebSocket 없이 F1~F4 흐름을 확인하는 안전한 테스트 모드입니다. 실제 계좌나 네트워크에 전혀 연결하지 않으므로, 처음 설치했거나 코드를 수정했을 때 가장 먼저 사용하는 모드입니다.

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

## 6. 화면 안내 (Web UI)

봇을 실행하면 웹 브라우저로 볼 수 있는 관리 화면이 함께 켜집니다(기본 주소 `http://localhost:8080`). 운영자는 이 화면에서 오늘의 진행 상황, 계좌 상태, 거래 기록을 확인합니다.

| 화면 | 내용 |
|---|---|
| 오늘 | 현재 상태, F1/F2/F3 선정 요약, 보유 후 가격흐름, 가격/손익, 하단 진행 파이프라인, 이벤트 로그 |
| 우선 선정 | F1 스냅샷 후보 목록과 통과 가능성 우선 정렬 |
| 자산 | 계좌 현재 스냅샷: 총평가금액, 예수금, 보유종목, 주식평가금액, 평가손익, 주문가능금액 |
| 주문 | 주문/체결 처리 이력: 주문번호, 종목, 매수/매도, 수량, 가격, 체결 상태, 주문 단계 |
| 이력 | 과거 거래 기록 |
| 통계 | 승률, 평균 손익, 청산 사유별 성과 |

**자산 메뉴와 주문 메뉴의 차이**: 자산 메뉴는 증권사 계좌에서 조회한 "지금 계좌에 무엇이 얼마나 있는가"를 보여주고, 주문 메뉴는 봇이 실행한 "매수/매도 행위와 그 처리 결과"를 보여줍니다. `주문가능금액`은 계좌 정보이므로 자산 메뉴가 기준이며, 주문 메뉴에서는 참고용으로만 표시합니다.

**오늘 화면**은 운영 상태 확인에 집중합니다. 후보 전체 리스트는 우선 선정 메뉴에서 확인하고, 오늘 화면에는 최종 후보 요약과 보유 후 가격흐름만 표시합니다. 가격흐름 차트는 증권사 차트처럼 "지금"이 오른쪽 끝인 최근 20분 슬라이딩 창으로 그려지며(눈금 1분, 시간 라벨 간격은 화면 너비에 따라 자동 조정), 새로고침해도 보유 중 수신된 가격 흐름이 사라지지 않고 복원됩니다. 청산 후에도 진입부터 청산까지 전체 구간이 유지되고, 매수(▲ 빨강)/매도(▼ 파랑) 체결 시점이 차트 위에 표시되어 하루 매매를 복기할 수 있습니다.

**하단 파이프라인**은 오늘 F1~F5 중 어느 단계까지 진행됐는지 보여줍니다. 예를 들어 오늘 F3에서 매수 실패가 발생하면, 봇이 대기 상태로 돌아간 뒤에도 "F3에서 실패했다"는 표시가 그대로 유지되어 하루의 결과를 놓치지 않고 확인할 수 있습니다.

## 7. Telegram 알림

Telegram 알림은 내부 로그 코드가 아니라 운영자가 바로 이해할 수 있는 형식으로 전송됩니다.

```text
긴급: 전일 포지션 잔류 의심
상황: 이전 거래일 상태 파일에 포지션 정보가 남아 있습니다.
조치: 계좌 보유 수량과 미체결 주문을 확인하고, 필요하면 수동 정리 후 재시작하세요.
세부: date=20260630
코드: STALE_POSITION_DETECTED
```

형식은 `제목 -> 상황 -> 조치 -> 세부 -> 코드` 순서입니다.

## 8. 개발자 안내

### 프로젝트 구조

```text
main.py
src/
  api/
    auth.py             # KIS OAuth2 토큰 관리
    kis_rest.py         # KIS REST 클라이언트 + 전역 rate limit
    kis_ws.py           # KIS WebSocket 클라이언트
    server.py           # FastAPI Web UI API
    status_logic.py     # 상태/F1 표시용 순수 헬퍼 (API·테스트 공용)
  modules/
    f1_filter.py        # F1 후보 스캔, 예상체결가 보강, 스냅샷 저장
    f1_selector.py      # F1 후보 점수화·선정 기준 (갭/거래대금/VI 가중치)
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
    number.py           # KIS 응답 숫자 문자열 파싱
    spike_filter.py
    time_sync.py
api_tests/              # KIS API 수동 점검 스크립트 (인증/잔고/주문 스모크)
scripts/                # 운영 보조 스크립트 (디렉터리 초기화, PAPER 청산, watchdog)
docs/
  PRD.md
  DEV_ENV.md
  CODING_GUIDELINES.md
  UI_DESIGN.md
  DB_DESIGN.md
  TABLE_DESIGN.md
  SPRINT.md
  html/                 # Web UI 정적 파일
tests/
```

### Web UI 내부 동작

- FastAPI 서버가 봇과 같은 이벤트 루프에서 실행됩니다.
- 주문/체결 내역은 `/api/orders`를 조회해 표시합니다. `/api/stream`의 로그 이벤트가 들어오면 즉시 재조회하고, 5초 폴링을 백업으로 사용합니다.
- 자산 조회 성공 결과는 `asset_snapshots` DB 테이블에도 저장해 감사/장애 분석용 이력으로 남깁니다.
- 보유 후 가격흐름 차트는 서버의 최근 tick 버퍼(최대 5,000건)를 원천으로 원시 tick 선을 그립니다. 보유 중에는 `[max(진입시각, 지금-20분) ~ 지금]` 슬라이딩 창, 청산(CLOSED) 후에는 진입~마지막 관측 tick/체결 구간을 고정해 표시합니다. 눈금 간격은 창 크기에 따라 1/2/5/10분, 시간 라벨 간격은 겹치지 않도록 화면 너비 기준으로 1·2·5·10·15·30분 중 자동 선택됩니다. 캔버스 폭은 CSS `width:100%`를 따릅니다.
- tick 버퍼가 20분을 못 담는 활발한 종목은 첫 tick 이전 구간을 분 단위 이력(`minute_price_history`)으로 채웁니다. 그리기는 150ms 코얼레싱 + 시간 버킷 min/max 다운샘플링으로 tick 폭주 시에도 부하를 제한합니다.
- 매수/매도 마커는 `/api/status`의 `trade_marks`(당일 체결 이력이 있는 주문 — `FILLED`/`PARTIAL_FILL`, 부분체결 후 취소된 주문 포함: `order_type`, `order_phase`, `fill_price`, `filled_at`)를 사용합니다. tick 버퍼는 인메모리라 재시작 시 사라지지만, 마커는 DB(orders)에서 오고 당일 청산(CLOSED) 상태는 재시작 시 상태 파일에서 복원되므로 재시작 후에도 표시됩니다.
- 청산 시 `state.set_closed`는 tick 이력을 지우지 않습니다. 09:10 이전 조기·수동
  진입에서 청산되면 주문 로직 없이 `F4_POST_CLOSE_OBSERVE_UNTIL`(기본 09:10)까지
  가격 관측만 이어가며, 이력은 다음 거래일 시작(`_clear_for_trading_day`) 때 정리됩니다.
- 하단 파이프라인은 `/api/status`의 `pipeline_stage`, `pipeline_failed`를 사용합니다. F3 미체결 실패 후 상태가 `IDLE`로 돌아가도 F3 실패 단계가 유지됩니다.

### 테스트

현재 기준 검증 명령:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_kis_rest.py tests\test_f1_filter.py tests\test_f2_lockup.py tests\test_f3_entry.py tests\test_f4_step_trailing.py tests\test_api_server.py tests\test_notifier.py -q -p no:cacheprovider
.\.venv\Scripts\python.exe -m ruff check src\notifier.py tests\test_notifier.py
```

최근 catch-up/token/UI 변경 검증:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_main_schedule_flow.py tests\test_kis_rest.py -q -p no:cacheprovider
.\.venv\Scripts\python.exe -m ruff check main.py src\api\kis_rest.py src\utils\logger.py tests\test_kis_rest.py tests\test_main_schedule_flow.py
node --check docs\html\assets\app.js
node tests\js\price_flow_checks.js
```

`tests/js/price_flow_checks.js`는 가격흐름 차트의 순수 로직(슬라이딩 창 계산, 매수/매도 마커 파싱·종목 필터, 다운샘플링)을 `app.js`에서 추출해 실행 검증합니다.
