# PAPER Fast Path 관측 프로브

## 목적

PAPER의 1.1초 REST 호출 제한에서 F1의 약 60회 단건 예상체결 조회를
장전 순위 2회와 멀티시세 2회로 대체할 수 있는지 실서버 응답으로 검증한다.

이 단계는 관측 전용이다. 기존 F1/F2/F3 후보 선정, 상태 파일, 주문 수량,
주문 전송 경로를 변경하지 않는다. `KIS_MODE=PAPER`, `DRY_RUN!=1`,
`PAPER_FAST_PROBE=1`을 모두 만족할 때만 실행된다.

## 실행 시각과 호출

- 08:59:45: KOSPI/KOSDAQ 등락률 순위 API를 각각 호출한다.
- 각 순위 응답의 최대 30종목을 시장별 멀티시세 API로 조회한다.
- 장전 응답만으로 관측 대상 3종목을 선정해 기록한다.
- 09:00:00.300: 기존 F1이 시작되기 직전에 대상 3종목의 멀티시세를
  한 번 더 조회한다.
- 09:00:02.800보다 늦게 시작하면 개장 관측은 생략하고 기존 F1을 즉시
  계속한다.

호출 엔드포인트:

- 등락률 순위: `FHPST01710000`
- 관심종목 멀티시세: `FHKST11300006`

## 설정

```dotenv
PAPER_FAST_PROBE=1
PAPER_FAST_PROBE_DIR=data/paper_fast_probe
PAPER_FAST_PROBE_OPEN_OFFSET_MS=300
PAPER_FAST_PROBE_OPEN_MAX_LATENESS_MS=2500
PAPER_FAST_PROBE_OPEN_TIMEOUT_SEC=2.5
```

공용 예시는 안전하게 비활성(`0`)이고, 현재 PAPER `.env`에서만 활성화한다.
REAL 또는 DRY_RUN에서는 값이 `1`이어도 코드 레벨에서 실행되지 않는다.

## 산출물

원시 응답과 타이밍은 다음 파일에 JSON Lines 형식으로 저장한다.

```text
data/paper_fast_probe/YYYYMMDD.jsonl
```

파일에는 공개 시세 응답의 `rt_cd`, `msg_cd`, `msg1`, `output`과 요청
파라미터만 저장한다. 인증 헤더, 앱 키, 토큰, 계좌번호는 저장하지 않는다.

주요 이벤트:

- `PAPER_FAST_PROBE_RANKING`: 시장별 장전 순위 원시 응답
- `PAPER_FAST_PROBE_MULTI`: 시장별 장전 멀티시세 원시 응답
- `PAPER_FAST_PROBE_PREOPEN_DONE`: 요청/응답 종목 대응 및 관측 대상 3종목
- `PAPER_FAST_PROBE_PREOPEN_SKIPPED`: 09:00 이후 장전 관측 미스파이어 생략
- `PAPER_FAST_PROBE_OPEN_MULTI`: 개장 직후 멀티시세 원시 응답
- `PAPER_FAST_PROBE_OPEN_DONE`: 실제 시작 지연과 유효 매도1호가 개수
- `PAPER_FAST_PROBE_OPEN_SKIPPED`: 지각 또는 장전 대상 부재로 생략

각 API 이벤트의 `timing`에는 전체 소요 시간, 예상 레이트리미터 대기,
이를 뺀 추정 네트워크 처리 시간이 기록된다.

## 다음 거래일 판정 항목

1. 08:59대 등락률 순위가 비어 있지 않고 `fid_rsfl_rate1/2` 필터가
   예상체결 등락률에 맞게 작동하는가.
2. 시장별 멀티시세의 요청 종목과 반환 종목이 일치하는가.
3. 장전 `intr_antc_vol`, `intr_antc_cntg_prdy_ctrt`, `inter2_prpr`,
   `inter2_prdy_clpr`가 실제 의미 있는 값을 갖는가.
4. `mrkt_trtm_cls_name`, `hour_cls_code`가 장전/개장 상태를 구분하며,
   VI 또는 거래정지 판단에 사용할 수 있는가.
5. 09:00:00.300 조회에서 `inter2_askp`가 유효한가. 비어 있다면
   1회 재조회 필요성과 적정 지연 시간을 결정한다.
6. 장전 선정 상위 3종목과 기존 F1 결과의 순위·누락 종목 차이가
   허용 가능한가.
7. 추가 5회 호출이 PAPER 제한 오류 없이 끝나고 기존 F1/F2/F3가
   그대로 실행되는가.

이 항목을 만족한 뒤에만 실제 Fast Path 후보 선정과 주문 경로를 별도
변경으로 구현한다.

## 2026-07-27 장중 수동 확인

PAPER 서버에서 멀티시세 엔드포인트 접수와 일괄 반환은 확인했다.
KOSPI 30종목 요청은 30종목, KOSDAQ 28종목 요청은 28종목이 반환됐다.
응답에는 `inter2_askp`, `inter2_bidp`, `inter2_prdy_clpr`, `inter2_prpr`,
`intr_antc_vol`, `intr_antc_cntg_prdy_ctrt`, `mrkt_trtm_cls_name`,
`hour_cls_code`가 포함됐다.

다만 이 확인은 개장 후 수행되어 예상체결 관련 필드가 0이었다. 따라서
장전 순위의 의미, 장전 예상체결 필드 값, 상태 필드의 VI 의미는 아직
검증되지 않았으며 위 관측 프로브가 이를 확인한다.
