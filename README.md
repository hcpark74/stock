# DAILY 1 — 갭업 자동매매 시스템

KOSPI/KOSDAQ 장전 갭업 종목을 자동으로 탐색·진입·추적 청산하는 Python 자동매매 봇.  
한국투자증권(KIS) OpenAPI 사용. PAPER(모의투자) / REAL(실계좌) 전환 가능.

---

## 트레이딩 로직

장전 8:40부터 10:00까지 5단계 파이프라인이 순차 실행된다.

```
F1 (08:40)  갭 필터링  →  +3~7% 갭업 + 유동성 상위 10% 종목 선별
F2 (08:58)  종목 잠금  →  1순위 종목 최종 확정
F3 (08:59:40) 진입    →  갭 재검증 → 70% 시장가 진입 → 30% 피라미딩
F4 (09:00~)  추적 관리 →  WebSocket 실시간 틱 수신, Step Trailing / Hard Stop
F5 (10:00)  타임아웃   →  잔여 포지션 전량 청산
```

**청산 조건**

| 방식 | 조건 |
|---|---|
| Step Trailing | +2.5% 스텝 도달 후 -1.5% 하락 시 청산 |
| Hard Stop | Trailing 미활성 구간에서 진입가 -2.0% 이탈 |
| Timeout | 10:00 강제 청산 |

---

## 설치

**Python 3.11+ 필요**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

개발 도구(테스트·린트) 포함 설치:
```powershell
pip install -r requirements-dev.txt
```

---

## 환경변수 설정

`.env` 파일을 편집한다. 기본값은 모의투자(PAPER) 설정.

```env
KIS_MODE=PAPER          # PAPER | REAL

# 모의투자 API 키 (실계좌 전환 시 아래 주석 처리된 4줄과 교체)
KIS_APP_KEY=...
KIS_APP_SECRET=...
KIS_ACCT_NO=...
KIS_BASE_URL=https://openapivts.koreainvestment.com:29443

KIS_ACCT_CD=01

TELEGRAM_BOT_TOKEN=...  # 알림 수신용
TELEGRAM_CHAT_ID=...

NTP_SERVER=time.google.com,time.windows.com,kr.pool.ntp.org  # 쉼표로 구분, 순서대로 폴백
UI_PORT=8000
```

---

## 실행

```powershell
# 가상환경 활성화 (d:\Private\stock 폴더에서 터미널 열면 자동 활성화됨)
.\.venv\Scripts\Activate.ps1

# 봇 시작
python main.py
```

봇이 시작되면:
- 스케줄러가 F1~F5 작업을 KST 기준으로 자동 실행
- **08:40~09:00 사이 기동** 시 F1 missed 감지 → F1→F2→F3 즉시 보완 실행
- **Web UI**: http://localhost:8000 에서 실시간 모니터링
- **로그**: `data/logs/YYYYMMDD.jsonl`
- **DB**: `data/db/trading.db` (SQLite, WAL 모드)

**로그 실시간 확인:**
```powershell
Get-Content data\logs\(Get-Date -Format 'yyyyMMdd').jsonl -Wait
```

**프로세스 종료:**
```powershell
# 터미널에서 Ctrl+C  (권장)

# 또는 PID로 강제 종료
Stop-Process -Id (Get-Content main.pid) -Force
```

---

## Web UI

`http://localhost:8000` — 봇과 같은 asyncio 루프에서 FastAPI로 동작.

| 탭 | 내용 |
|---|---|
| 오늘 | 현재 포지션, 실시간 틱 가격, Arc 진행 게이지, 이벤트 로그 |
| 이력 | 전체 거래 내역 테이블 (SQLite) |
| 통계 | 승률 도넛, 청산 사유별 손익, 월별 누적 손익 |

---

## 프로젝트 구조

```
main.py                 # 진입점 — 스케줄러 + uvicorn 부트스트랩
src/
  live.py               # 모듈 간 공유 라이브 상태 (틱, WS, NTP)
  state.py              # 인메모리 포지션 상태 + today_state.json 영속화
  db.py                 # SQLite (trades / orders / partial_exits)
  notifier.py           # Telegram 알림 워커
  scheduler.py          # APScheduler F1~F5 등록
  api/
    auth.py             # KIS OAuth2 토큰 관리
    kis_rest.py         # KIS REST 클라이언트
    kis_ws.py           # KIS WebSocket 클라이언트
    server.py           # FastAPI Web UI 서버
  modules/
    f1_filter.py        # 갭/유동성 필터
    f2_lockup.py        # 종목 잠금
    f3_entry.py         # 진입 주문 + 피라미딩
    f4_tracking.py      # Step Trailing / Hard Stop
    f5_timeout.py       # 타임아웃 청산
  utils/
    logger.py           # JSONL 로거
    time_sync.py        # NTP 시각 검증
    spike_filter.py     # 틱 스파이크 필터
data/
  logs/                 # YYYYMMDD.jsonl 일별 로그
  state/                # today_state.json (재시작 복구용)
  db/                   # trading.db
  auth/                 # KIS 토큰 캐시
docs/html/
  index.html            # 프로덕션 Web UI
  ui_mockup.html        # 정적 목업 (디자인 참고용)
```

---

## 테스트

```powershell
pytest tests/ -v
```

---

## 주의사항

- **PC를 켜둬야 함** — 봇은 스케줄러 기반으로 장중 실행. 서버 프로세스가 종료되면 거래 없음.
- **PAPER → REAL 전환** — `.env`에서 `KIS_MODE=REAL`로 변경 후 실계좌 API 키 4줄로 교체.
- **NTP 오차** — 500ms 초과 시 CRIT 경고. `NTP_SERVER`에 쉼표로 구분된 서버 목록을 지정하면 순서대로 폴백. 기본값: `time.google.com,time.windows.com,kr.pool.ntp.org`.
- **재시작 복구** — 장중 프로세스가 죽어도 `today_state.json`으로 HOLDING 포지션 자동 복구.
