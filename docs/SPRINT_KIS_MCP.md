# KIS MCP 개발 도구 도입 스프린트

> **상태**: 완료 — Phase 1~6 및 완료 조건 7/7 검증
> **작성일**: 2026-07-21
> **정책 기준**: [PRD.md §5-5](PRD.md)
> **적용 범위**: 개발 환경의 API 명세 확인, 사고 분석, 과거 데이터 검증

## 1. 목표

KIS MCP를 자동매매 실행 경로와 분리된 개발 보조 도구로 도입한다. 다음 세 가지 사용 사례를
안전하게 검증하는 것이 목표다.

1. TR ID, 요청 파라미터, 응답 필드와 오류 코드의 공식 명세 확인
2. 장애 발생 시 계좌 잔고·미체결 주문을 조회해 봇 로그와 대조
3. 장 종료 후 과거 시세를 이용한 전략 파라미터 검증

## 2. 제외 범위와 안전 불변조건

- 봇 런타임에서 MCP를 호출하거나 의존하지 않는다.
- MCP를 주문, 정정, 취소 또는 런타임 장애 우회 수단으로 사용하지 않는다.
- `src/api/kis_rest.py`의 호출 제한, 토큰 갱신, 재시도, `send_guard`를 우회하지 않는다.
- 09:00~09:11에는 KIS MCP를 호출하지 않는다.
- 계좌·시세 실행 PoC는 PAPER만 사용한다. Code Assistant에는 KIS 자격 증명을 전달하지 않는다.
- 실계좌 접근이 꼭 필요하면 조회 전용·최소 권한 여부를 먼저 확인하고 별도 승인 후 사용한다.
- MCP를 제거하거나 사용할 수 없어도 자동매매와 운영 UI는 정상 작동해야 한다.

## 3. 작업 체크리스트

### Phase 1 — 공식 소스와 기능 확인

- [x] 한국투자증권 공식 저장소·문서에서 MCP 서버의 배포 주체와 최신 설치 방법 확인
- [x] Code Assistant와 Trading 기능의 제공 도구 목록 및 권한 범위 확인
- [x] 주문·정정·취소 도구를 비활성화하거나 노출하지 않는 구성이 가능한지 확인
- [x] 호출 제한이 기존 Open API 앱 키 쿼터와 공유되는지 확인
- [x] 확인한 버전, 공식 URL, 실행 명령과 확인일을 `DEV_ENV.md`에 기록

> 공식 문서로 검증되기 전에는 설치 명령과 기능 지원 여부를 확정 사항으로 기록하지 않는다.

**확인 결과 (2026-07-21, 5/5; 2026-07-22 범위 판정 확정)**

- Code Assistant는 공식 NPM 패키지 기준 `0.1.1`이며 API 검색·샘플코드 조회 전용이다.
- Trading MCP `0.1.0`은 잔고·주문내역뿐 아니라 주문·정정·취소 기능도 제공한다.
- Trading MCP 서버는 모든 상품군 도구를 무조건 등록하며, 공식 문서와 현재 소스에는 주문 도구만
  끄는 allowlist/denylist 설정이 없다. “비활성화 가능 여부 확인”의 결론은 **불가능**이다.
- Code Assistant는 KIS Open API를 호출하거나 앱 키를 받지 않으므로 기존 봇 쿼터를 소비하지 않는다.
- Trading MCP는 사용자 앱 키로 KIS Open API를 직접 호출하고 공식 호출 제한을 적용받는다. 같은
  자격 증명을 사용하면 기존 봇 쿼터와 공유하는 것으로 판정한다. 앱 키·계좌·사용자 중 정확한 내부
  산정 단위는 공개 자료에서 확인되지 않았지만, Trading MCP를 미도입하고 공유 시 충돌로 처리하는
  보수적 운영 규칙이 확정되어 안전 판정의 미완료 사유로 남기지 않는다.
- 결정: Phase 2는 Code Assistant만 대상으로 진행하고 Trading MCP 설치는 보류한다.

### Phase 2 — 격리된 개발 환경 구성

- [x] PAPER 앱 키 준비 불필요 확인 — Code Assistant에는 KIS 자격 증명을 전달하지 않음
- [x] 비밀값 미사용 — 프로젝트 MCP 설정에 환경 변수와 자격 증명 없음
- [x] 프로젝트 전용 Codex 설정에 Code Assistant `0.1.1`만 등록
- [x] API 검색·공식 소스 읽기 도구 9개만 `enabled_tools` allowlist로 허용
- [x] 설정 파일과 검증 출력에 앱 시크릿·토큰·계좌번호 원문이 없음을 확인

**연결 검증 결과 (2026-07-21)**

- 설정 위치: `.codex/config.toml`(신뢰된 이 프로젝트에서만 로드)
- 실행 패키지: `@koreainvestment/kis-code-assistant-mcp@0.1.1`
- 실제 MCP 호출: `search_domestic_stock_api`
- 검색 결과: `inquire_balance`, `inquire_balance_rlz_pl`
- Trading MCP와 주문·정정·취소 도구는 등록하지 않음
- 기존 Codex 세션은 설정을 다시 읽도록 재시작하거나 새 세션을 열어야 함

### Phase 3 — Code Assistant 검증

- [x] 현재 사용 중인 잔고조회 TR의 요청 파라미터와 응답 필드 대조
- [x] `ord_psbl_cash`, `prvs_rcdl_excc_amt` 등 현금 관련 필드 의미 확인
- [x] `EGW00215`, HTTP 429 및 대표 주문 거부 코드 조회 절차 확인
- [x] 신규 TR 검토 시 코드 변경 전 공식 명세를 확인하는 작업 흐름 문서화

**검증 결과 (2026-07-21)**

- Code Assistant에서 `inquire_balance`, `inquire_psbl_order`, `order_cash`를 검색하고 공식 GitHub의
  메인·검증 샘플을 읽어 대조했다.
- 잔고조회 `[v1_국내주식-006]` 경로와 TR ID는 현재 코드와 일치한다.
  - 경로: `/uapi/domestic-stock/v1/trading/inquire-balance`
  - REAL: `TTTC8434R`, PAPER: `VTTC8434R`
- `balance_inquiry_params()`의 11개 키는 공식 샘플과 일치한다. 현재 `PRCS_DVSN=01`은
  “전일매매미포함”을 뜻하며 공식 검증 샘플의 `00`(전일매매포함)과 목적에 따라 선택 가능한 값이다.
- 현재 코드가 읽는 보유종목 `output1` 필드(`pdno`, `prdt_name`, `hldg_qty`, `ord_psbl_qty`, `prpr`,
  `pchs_avg_pric`, `pchs_amt`, `evlu_amt`, `evlu_pfls_amt`, `evlu_pfls_rt`)와 요약 `output2` 필드
  (`dnca_tot_amt`, `prvs_rcdl_excc_amt`, `scts_evlu_amt`, `tot_evlu_amt`, `evlu_pfls_smtl_amt`)는 공식
  샘플의 필드 매핑에 존재한다.
- `ord_psbl_cash`는 공식 잔고조회 `output2` 매핑에 없고, 별도 매수가능조회
  `[v1_국내주식-007]`의 `output`에서 “주문가능현금”으로 정의된다. 현재 코드는 잔고 요약값을 1차
  예산으로만 사용하고 주문 직전에 `TTTC8908R`/`VTTC8908R`의 `nrcvb_buy_amt`와
  `nrcvb_buy_qty`로 미수 없는 실제 매수 상한을 다시 적용한다.
- `prvs_rcdl_excc_amt`의 공식 샘플 명칭은 “가수도정산금액”이다. 주문가능현금이나 확정 D+2 예수금과
  동일한 값으로 간주하지 않는다. 코드·UI의 기존 “D+2 정산금” 표기는 공식 명칭으로 정정했다.
- 공식 저장소가 명시한 초당 거래건수 초과 코드는 `EGW00201`이다. `EGW00215`는 Code Assistant 검색과
  읽은 공식 샘플에서 의미를 확인할 수 없었으므로 과거 관측/환경별 코드일 가능성만 기록하고 확정
  매핑하지 않는다. HTTP 429는 HTTP 상태이고 `EGW...`는 응답 본문의 `msg_cd`이므로 별도로 기록한다.
- 공식 `order_cash` 샘플은 실패 시 공통 `printError`를 호출할 뿐 주문 거부 코드 표를 제공하지 않는다.
  프로젝트의 `40100000`(모의투자 영업일 아님) 처리도 관측 기반 호환 규칙으로 유지하며, 대표 거부
  코드는 실제 응답의 `rt_cd`, `msg_cd`, `msg1`을 함께 보존한 뒤 공식 포털/챗봇에 확인한다.

검증한 공식 샘플:

- [주식잔고조회](https://github.com/koreainvestment/open-trading-api/tree/main/examples_llm/domestic_stock/inquire_balance)
- [매수가능조회](https://github.com/koreainvestment/open-trading-api/tree/main/examples_llm/domestic_stock/inquire_psbl_order)
- [주식주문(현금)](https://github.com/koreainvestment/open-trading-api/tree/main/examples_llm/domestic_stock/order_cash)

**신규 TR·오류 코드 검토 절차**

1. Code Assistant의 해당 상품군 `search_*_api`에서 API 이름과 `function_name`을 검색한다.
2. 검색 결과의 `url_main`과 `url_chk`를 `read_source_code`로 읽고, 경로·REAL/PAPER TR ID·필수값·
   허용값·응답 컨테이너와 필드명을 기록한다. 소스의 작성일과 검토일도 함께 남긴다.
3. 코드 변경 전에 호출부와 표로 대조한다. 불일치는 “코드 오류”, “목적에 따른 허용값 차이”,
   “공식 샘플에 없는 확장 필드”로 구분하며, 확장 필드는 필수값으로 만들지 않는다.
4. 현금/수량처럼 주문 안전성에 영향을 주는 값은 의미가 비슷한 필드로 대체하지 않는다. 잔고 요약은
   표시·1차 예산으로만 쓰고, 주문 직전 해당 종목·주문유형의 매수/매도가능조회로 상한을 재검증한다.
5. 오류가 발생하면 추가 호출을 멈추고 HTTP 상태, `rt_cd`, `msg_cd`, `msg1`, 경로, TR ID,
   PAPER/REAL, 발생 시각을 기록한다. 계좌번호·토큰·앱 시크릿은 마스킹한다.
6. Code Assistant는 API/샘플 검색 도구이지 오류 코드 사전이 아니다. 공식 샘플에서 뜻을 확인하지
   못한 코드는 KIS Developers 포털·공식 챗봇/문의에서 확인하고, 확인 전에는 문자열 의미를 추정해
   재시도·주문 분기를 추가하지 않는다.
7. 확인된 명세를 문서와 회귀 테스트에 반영한 뒤 관련 단위 테스트를 통과시킨다. 주문 샘플은 읽기만
   하며 Code Assistant 또는 MCP에서 실제 주문을 실행하지 않는다.

### Phase 4 — 조회 전용 사고 분석 검증

- [x] 장 종료 후 PAPER 계좌 잔고 조회 성공 확인
- [x] 미체결 주문 조회 결과를 봇 DB·JSONL 로그와 대조
- [x] `STALE_POSITION_DETECTED` 발생 시 확인 순서와 기록 양식 작성
- [x] 조회 실패, 권한 부족, 응답 필드 누락 시 중단 기준 확인
- [x] `EGW00201`, 미확인 호출 제한 코드 또는 HTTP 429 발생 즉시 추가 호출을 멈추는 절차 확인

**재현 결과 (2026-07-21 16:27 KST)**

- `scripts/kis_phase4_readonly_audit.py`로 PAPER 잔고와 당일 미체결 GET만 각 1회 호출했다.
- 잔고조회 성공, 공식 필수 요약 필드 존재, 보유 0종목을 확인했다.
- KIS 미체결 0건을 당일 DB 주문 2건(pending 0건), JSONL 이벤트 215건과 대조했다.
- KIS→DB 누락 0건, KIS→JSONL 누락 0건, DB pending→KIS 원장 부재 0건으로 `MATCH` 판정했다.
- 계좌번호·종목·주문번호·금액과 원문 응답은 출력하거나 문서에 저장하지 않았다.
- 조사 모드의 `stop_on_rate_limit=True`는 HTTP 429와 `EGW00201`에서 자동 재시도 없이 반환하며,
  회귀 테스트로 1회 호출 후 중단을 확인했다.
- [사고 분석 런북](KIS_INCIDENT_AUDIT.md)에 `STALE_POSITION_DETECTED` 확인 순서, 판정표, 즉시 중단
  조건과 조사 기록 양식을 작성했다.

Code Assistant는 계좌를 조회할 수 없고 Trading MCP는 주문 도구를 안전하게 분리할 수 없어 계속
보류한다. 따라서 실제 원장 대조는 봇과 동일한 `kis_rest`의 읽기 전용 GET 경로로 수행했다. 이는
사고 분석 절차 검증이며 Trading MCP 도입 승인으로 해석하지 않는다.

**재검증 (2026-07-22 15:40 KST)**

- PAPER 잔고·당일 미체결 GET을 각 1회 재실행했다.
- 보유 0종목, KIS 미체결 0건, DB 당일 주문 2건(pending 0건), JSONL 이벤트 127건을 확인했다.
- 세 원천 간 누락과 stale pending이 모두 0건으로 다시 `MATCH` 판정했다.

### Phase 5 — 과거 데이터 활용 가능성 검증

- [x] F1 갭 3~8% 코어, 8~10% 조건부 구간을 재현할 데이터 범위 확인
- [x] 거래일·수정주가·장전 기준가 등 백테스트에 필요한 데이터 정의 확인
- [x] 소규모 표본으로 데이터 누락과 시간대 정합성 검증
- [x] 기존 개선 메뉴 진단 결과와 비교하는 읽기 전용 PoC 수행

**재현 결과 (2026-07-22 15:46 KST)**

- 공식 `inquire-daily-itemchartprice`의 영업일·시가·종가로 현재 F1 갭 구간을 재현할 수 있음을
  확인했다. PAPER 일봉 GET 3표본에서 F1 저장 갭과 공식 시가 갭의 평균 절대 차이는 0.002%p였다.
- 공식 `inquire-asking-price-exp-ccn`에는 날짜 파라미터가 없어 과거 예상체결가·예상체결량을
  역조회할 수 없다. 8~10% 조건부 통과의 예상체결대금·VI와 전체 F1 순위는 당시 로컬 스냅샷이
  필요하다.
- 전체 20파일에서 시각 범위 밖 5, 주말 2, 확인된 휴장 1, 적격 중 동일 날짜 중복 3파일을 제외해
  대표 9거래일·537행을 선정했다. JSON 오류·필수 필드 누락·종목 중복·갭 산식 불일치는 모두 0건이다.
- 대표 표본은 코어 86건, 조건부 16건이며 조건부 통과는 1건이다. 레코드 자체에는 타임존과
  파라미터 버전이 없어 관측 메타데이터 개선이 필요하다.
- 기존 Improve는 전략 거래 11건의 사후 성과를, 이 PoC는 진입 전 후보 537행의 분포를 다룬다.
  상호 보완 관계이며 현재 표본만으로 F1 임계값을 조정하지 않는다.
- 재현 도구: `scripts/kis_phase5_historical_poc.py`
- 상세 정의와 결과: [과거 데이터 읽기 전용 PoC](KIS_HISTORICAL_POC.md)

### Phase 6 — 운영 안전성 확인

- [x] 09:00~09:11 호출 금지 규칙을 개발자 운영 문서에 반영
- [x] 그 밖의 장중 호출은 봇 중지 또는 별도 PAPER 키 사용 시에만 허용하도록 명시
- [x] MCP 호출 시각, 목적, 사용 계정 구분, 조회 항목을 조사 기록에 남김
- [x] MCP 프로세스 중단·설정 제거 후 봇 단위 테스트 및 시작 경로 정상 확인
- [x] 자격 증명 폐기·교체와 MCP 등록 해제 절차 검증

**재현 결과 (2026-07-22 KST)**

- `.codex/config.toml`을 임시 제거하고 현재 Code Assistant stdio 프로세스 트리 1개를 정확한
  명령행·PID로 종료했다. 잔존 프로세스 0개, 새 `codex mcp list` 등록 0개를 확인했다.
- 설정 부재 정적 감사 `PASS`, 런타임의 MCP 참조 0건, `main.main` 시작 모듈 로드 정상, 전체 테스트
  `440 passed`를 확인했다. 외부 KIS 호출과 주문 호출은 0회였다.
- 검증 후 개발용 설정은 원본 SHA-256과 동일하게 복원했고 MCP 프로세스는 중지 상태로 유지했다.
- 현재 프로젝트 로컬 등록은 `codex mcp remove`의 제거 대상이 아니므로
  `.codex/config.toml`을 제거해야 한다. 사용자 전역 등록일 때만 CLI remove를 사용한다.
- Code Assistant에는 KIS 자격 증명이 없고 Trading MCP는 설치하지 않아 이번에 폐기할 MCP 전용 키는
  없었다. 향후 별도 PAPER 키 또는 공유 키가 발견될 때의 폐기·교체·봇 중지 순서를 문서화했다.
- 재현 도구: `scripts/kis_phase6_safety_audit.py`

## 4. 완료 조건

다음 조건을 모두 만족해야 도입 완료로 표시한다.

- [x] 공식 KIS 자료로 서버 출처, 설치법, 지원 기능과 권한 범위를 검증했다.
- [x] Code Assistant에는 자동매매 앱 키를 전달하지 않았고, 계좌 조회 PoC는 PAPER에서만 검증했다.
- [x] 등록된 Code Assistant 도구는 검색·소스 읽기 전용이며 Trading MCP는 등록하지 않았다.
- [x] 09:00~09:11 호출 금지와 레이트 리밋 사고 대응 절차가 문서화되어 있다.
- [x] API 명세 조회와 조회 전용 사고 분석 시나리오가 각각 1회 이상 재현되었다.
- [x] MCP가 없어도 봇 시작 모듈과 전체 테스트에 영향이 없음을 확인했다.
- [x] 설치·사용·중지·자격 증명 폐기 절차가 `DEV_ENV.md`에 정리되어 있다.

**최종 확인 (2026-07-22 15:56 KST, 7/7)**

| 완료 조건 | 확인 근거 | 판정 |
|---|---|---|
| 공식 출처·설치·기능·권한 | KIS 공식 포털·GitHub, Code Assistant `0.1.1`, Trading MCP 권한 비교 | PASS |
| 계정·자격 증명 분리 | Code Assistant 자격 증명 0개, 계좌·시세 PoC `KIS_MODE=PAPER` 강제 | PASS |
| 주문 기능 차단 | 검색·소스 읽기 9개만 등록, Trading MCP 미등록 | PASS |
| 금지 시간·레이트 리밋 대응 | 09:00~09:11 차단, HTTP 429·`EGW00201` 즉시 중단 테스트 | PASS |
| 명세·사고 분석 재현 | 공식 TR 명세 대조, PAPER 잔고·미체결·DB·JSONL `MATCH` | PASS |
| MCP 없는 런타임 | 설정 제거·프로세스 잔존 0, `START_PATH_IMPORT_OK`, `440 passed` | PASS |
| 전체 운영 절차 | `DEV_ENV.md` 설치·사용·중지·등록 해제·자격 증명 폐기 절차 | PASS |

따라서 이 스프린트의 **Code Assistant 개발 보조 도구 도입은 완료**로 판정한다. Trading MCP는
조회 전용 도구 분리가 지원되지 않아 계속 제외한다. Trading MCP의 정확한 호출 제한 산정 단위는
미공개 제한사항으로 기록하되 현재 도입 범위에는 영향을 주지 않는다.

## 5. 산출물

- [x] `docs/DEV_ENV.md`: 검증된 설치 및 비활성화 절차
- [x] `docs/PRD.md`: 허용·금지 범위와 운영 정책
- [x] `docs/KIS_INCIDENT_AUDIT.md`: 조회 전용 사고 분석 순서, 중단 기준, 기록 양식과 재현 결과
- [x] `docs/KIS_HISTORICAL_POC.md`: 과거 시세 범위, 백테스트 데이터 정의, 누락·시간대 검증과 Improve 비교
- [x] `scripts/kis_phase6_safety_audit.py`: 설정·런타임 독립성·Git 제외·운영 문서 정적 감사
- [x] 사고 분석 기록 예시: 조회 시각, 계정 구분, API 결과와 봇 로그 대조표
- [x] 과거 데이터 PoC 결과: 데이터 범위, 누락률, F1 구간별 표본 수

산출물 확인 결과: **7/7 완료**.

## 6. 철회 기준

다음 중 하나라도 해소되지 않으면 도입을 중단하고 MCP 등록과 자격 증명을 제거한다.

- 주문 계열 권한을 안전하게 통제할 수 없음
- 자동매매 앱 키 또는 호출 쿼터와 분리할 수 없음
- 비밀값이나 계좌 정보가 로그에 노출됨
- 봇 런타임 안정성 또는 09:00~09:11 진입 파이프라인에 영향을 줌
- 공식 출처나 유지보수 주체를 확인할 수 없음

**현재 철회 기준 판정 (2026-07-22)**

| 철회 기준 | 현재 통제·증거 | 상태 |
|---|---|---|
| 주문 권한 통제 불가 | Code Assistant 검색·소스 읽기 9개만 등록, Trading MCP 미등록 | 해소 |
| 앱 키·쿼터 미분리 | Code Assistant 자격 증명·KIS 호출 0개, 실행 PoC는 PAPER 강제 | 해소 |
| 비밀값·계좌정보 노출 | 설정 비밀 표식 0건, 감사 출력 집계·마스킹, `.codex/` Git 제외 | 해소 |
| 런타임·진입 구간 영향 | 런타임 MCP 참조 0건, 설정 제거 상태 `440 passed`, 09:00~09:11 금지 | 해소 |
| 공식 출처 불명 | KIS 공식 포털·GitHub와 패키지 버전 확인 | 해소 |

활성 철회 기준은 **0/5**다. 향후 Trading MCP 등록, MCP용 KIS 자격 증명 추가, 비밀값 노출,
런타임 의존성 추가 또는 금지 시간 호출이 발견되면 즉시 도입 상태를 철회하고 등록·자격 증명 제거
절차를 수행한다.
