# 트랙 B 그림자 가동 설계

> **상태**: 설계 확정 — 미구현. 1단계(봉·지표·차트) → 2단계(신호 엔진·그림자 기록)
> **작성일**: 2026-08-27  
> **갱신일**: 2026-08-27 (차트를 1단계로 끌어올려 2단계 구조로 재편)
> **관련 문서**: [멀티 트랙 전략 병행 운용 설계](2026-08-25-multi-track-strategy-design.md),
> [PRD.md](../../PRD.md), [DB_DESIGN.md](../../DB_DESIGN.md), [CODING_GUIDELINES.md](../../CODING_GUIDELINES.md)

멀티 트랙 스펙(이하 **모(母)스펙**)이 정의한 골격 위에, 트랙 B를 **자본 0으로 실제 가동**시키는
덩어리다. 모스펙 §3.4·§6.2·§7·§8.3을 다루고, §5(예산·불변식)·§6.3~§6.5(승격)·§8.2(트랙 UI)는
다음 덩어리(PILOT)로 미룬다.

**두 단계로 나눈다.**

| | 내용 | 이 단계가 끝나면 |
|---|---|---|
| **1단계 — 눈** | 지문 회전 1회 · 봉/지표 계층 · 봉·지표 차트 | 봉이 쌓이기 시작하고, 09시 급등주의 분봉을 눈으로 본다 |
| **2단계 — 판단** | 신호 엔진 v0 · `shadow_trades` | 실시간 그림자가 돌고, 1단계에 쌓인 봉으로 소급 재생해 대조한다 |

## 1. 목표와 완료 정의

### 1.1 최종 완료 정의 (2단계까지)

> 트랙 B가 **자본 0원으로 매일 스스로 진입·청산 신호를 내고, 그 판단 근거가 재생 가능한
> 형태로 DB에 남는다. 트랙 A의 코드·자본·통계는 하나도 변하지 않는다.**

"분리"를 네 축으로 쪼개면 각각이 검증 가능한 명제가 된다.

| 축 | 분리의 의미 | 확인 방법 |
|---|---|---|
| 코드 | F3/F4/F5 무수정, B는 신규 모듈 | 기존 테스트 스위트 무수정 통과 |
| 데이터 | `shadow_trades` 별도 테이블 | `trades`에 `track='B'` 행 0건 |
| 자본 | 분배 코드를 넣지 않는다 (§10) | A의 주문 수량·금액이 현행과 동일 |
| 통계 | 집계 트랙 스코프 | 모스펙 §6.1에서 완료 |

### 1.2 1단계 완료 정의

> **트랙 B가 판단할 봉과 지표가 매일 쌓이고, 그것이 화면에 보인다.** 신호는 아직 내지 않는다.

### 1.3 왜 차트가 먼저인가

차트를 2단계로 미루면 **근거 없는 v0 숫자**(SMA 20·MACD 12/26/9·하드스탑 2.0%, §16)를 코드에
박고 그 숫자로 한 달치 그림자를 쌓은 뒤에야 눈으로 보게 된다. 모스펙 §8.3의 표현대로 지표
시각화는 장식이 아니라 실험 도구 자체인데, 도구를 나중에 만들 이유가 없다.

**표본 시계는 늦어지지 않는다.** §7의 지표 엔진이 순수 함수이고 v0가 확정 봉에서만 판정하므로,
봉만 쌓여 있으면 규칙을 나중에 정해 **소급 재생**할 수 있다. 표본 시계의 시작은 신호 엔진이
붙는 시점이 아니라 **봉 수집이 시작되는 시점**이다.

부수 효과도 크다. 봉 집계와 확정 봉 정정이 제대로 도는지 눈으로 즉시 보인다. 재생 검증은
코드가 코드를 확인하는 것이라 "봉 자체가 이상하다"를 잡지 못한다.

### 1.4 SHADOW가 답하는 질문

**SHADOW 단계의 판정 대상은 수익이 아니다.** 그림자에는 구조적 낙관 편향이 있고(모스펙 §6.3),
이 봇에서 체결은 가장 어려운 부분이다. 이 덩어리가 답해야 하는 질문은 둘뿐이다.

1. 신호가 실제로 발생하는가
2. 빈도가 분석 가능한가

"수익이 나는가"는 PILOT이 답한다. 그림자 승률로 승격을 논하지 않는다.

## 2. 확정된 전제

| 항목 | 결정 |
|---|---|
| 순서 | **차트가 먼저**(1단계), 신호 엔진이 나중(2단계) — §1.3 |
| 규칙을 담는 법 | **v0 하드코딩 + 파라미터만 `strategy_configs`** |
| 규칙의 숫자 | 1단계 차트를 보고 정한다. 미리 확정하지 않는다 |
| 검증 수단 | **차트 + 기록 + 재생 검증** 셋 다 |
| 자본 | 0. `TRACK_WEIGHT` 분배 코드를 **작성하지 않는다** |
| 확정 봉 정정 | **포함한다** (§6.2) |
| 실주문 | 없다. B는 주문 API를 호출하지 않는다 |

확정 봉 정정을 뺄 수 있었지만 넣었다. 봉이 틀린 채로 쌓은 그림자 기록은 나중에 재해석이
불가능해 표본 전체가 버려진다. **표본 수집이 이 덩어리의 목적이므로 데이터 정확성이 먼저다.**

## 3. 지문 격리 — 모스펙 §7.2에서 이탈한다

모스펙 §7.2는 [live.py:55](../../../src/live.py#L55) `_accumulate_minute`를 OHLCV로 확장하라고
했다. 이 스펙은 그렇게 하지 않는다.

`src/live.py`는 [release.py:20](../../../src/release.py#L20) `_STRATEGY_FILES`에 들어 있어
**손댈 때마다 트랙 A의 전략 지문이 돈다.** 지금 A의 표본 상태가 이렇다.

```
baseline-bd1f65eca63c  1건 (08-14)
baseline-ca7a6dd0f5f0  1건 (08-19)
baseline-3401f8c50155  2건 (08-21~24)
baseline-56373a94c7a5  1건 (08-26)
baseline-39cf806f8eac  1건 (08-27)
```

6거래일에 지문 5개. 이미 지문당 1~2건으로 파편화돼 있다. B의 봉 집계기를 `live.py` 안에
두면 **B를 만질 때마다 A의 표본이 리셋되고**, 모스펙 §6.4가 요구하는 20건은 영영 모이지
않는다.

### 3.1 대안

`live.py`에는 **영구적인 훅 한 줄**만 넣는다.

```python
# src/live.py — 이 파일에서 B를 위해 늘어나는 코드의 전부
_tick_listeners: list = []

def register_tick_listener(fn) -> None:
    _tick_listeners.append(fn)

# push_tick() 끝에서
for fn in _tick_listeners:
    try:
        fn(price, ticker, now)
    except Exception:  # noqa: BLE001 — 리스너 실패가 A의 손절 경로를 흔들면 안 된다
        pass
```

OHLCV 집계는 새 모듈 `src/bars.py`가 갖는다. `_accumulate_minute`와 `_minute_history`는
**무변경**이므로 UI 가격흐름도, 모스펙 §8.3의 "`drawPriceFlow`를 건드리지 않는다"는 결정도
함께 지켜진다.

### 3.2 지문 목록을 둘로 나눈다

```
_STRATEGY_FILES   → 트랙 A의 지문. B의 신규 파일을 넣지 않는다
_TRACK_B_FILES    → 트랙 B의 지문. bars/indicators/b_signal/shadow_book/kis_minute_bars
```

근거는 모스펙 §4.7의 선례와 같다 — `src/readiness.py`가 A의 실탄 자격만 판정하므로 지문에서
제외됐듯, B의 파일은 A의 매매 판단에 영향을 주지 않으므로 A의 지문이 아니다.

**결과: 이 덩어리에서 A의 지문은 딱 한 번 돈다** (`live.py` 훅 + `db.py` 테이블 + `main.py`
기동 배선). 이후 B를 아무리 고쳐도 A의 지문은 그대로다.

`main.py`도 `_STRATEGY_FILES`에 있으므로 B의 워커를 띄우는 배선 자체가 지문을 돌린다. 피할
길이 없다 — 모든 진입점이 지문 파일이다. 따라서 **배선은 한 번에 끝나는 모양**으로 넣는다.
`bars.start()` / `bars.stop()` 호출 한 쌍이고, B의 내부가 어떻게 바뀌든 이 두 줄은 변하지
않는다.

## 4. 모듈 경계

지문에 들어가지 않는 신규 파일들이다.

| 파일 | 책임 | 의존 | 순수성 | 단계 |
|---|---|---|---|---|
| `src/bars.py` | 틱 → 1분 OHLCV + 틱파생값 누적, 확정 봉 정정 조율 | `api/kis_minute_bars` | I/O 있음 | 1 |
| `src/indicators.py` | `sma`/`ema`/`macd` | 없음 | **순수** | 1 |
| `src/api/kis_minute_bars.py` | 분봉 API (POC 승격) | `api/kis_rest` | I/O 전담 | 1 |
| `docs/html/assets/bars_chart.js` | 봉·지표 차트 (§9) | 없음 | 드로잉 + 순수 헬퍼 | 1 |
| `src/modules/b_signal.py` | 트랙 B 신호 엔진 v0 | `indicators` | **순수** | 2 |
| `src/modules/shadow_book.py` | `shadow_trades` 기록 | `db` | I/O 있음 | 2 |

차트를 `app.js`에 넣지 않고 **별도 파일**로 뺀다. `app.js`는 이미 1,870줄이고 그 안에
`drawPriceFlow`(약 200줄)가 A의 참조선·마커·VI 밴드와 얽혀 있다. 같은 파일에 캔들과 MACD를
욱여넣으면 모스펙 §8.3이 지키려던 경계가 파일 안에서 흐려진다.

`bars.py`를 `src/modules/`가 아니라 최상위에 두는 이유는
[CODING_GUIDELINES.md](../../CODING_GUIDELINES.md) §2의 "`modules/` 코드는 `api/`를 직접
import하지 않는다" 규칙 때문이다. `live.py`와 같은 층위의 인프라 모듈로 본다.

그 규칙 덕에 **`b_signal.py`는 봉을 받아 신호를 돌려주는 순수 함수**가 된다. 오프라인
재생 검증과 실시간이 같은 코드를 타고, 테스트가 결정적이다.

지문 파일 수정은 셋뿐이고 **전부 1단계에서 한꺼번에** 끝낸다.

| 파일 | 변경 | 재발 여부 |
|---|---|---|
| `src/live.py` | `register_tick_listener` + `push_tick` 팬아웃 | 한 번. 이후 무변경 |
| `src/db.py` | `shadow_trades` CREATE TABLE | 한 번. 이후 무변경 |
| `main.py` | `bars.start()` / `bars.stop()` 호출 한 쌍 | 한 번. 이후 무변경 |

**`shadow_trades` 테이블은 1단계에서 만들고 비워 둔다.** 쓰는 것은 2단계지만, 2단계에
만들면 지문이 한 번 더 돈다. 빈 테이블 하나를 미리 두는 비용이 A의 표본을 한 번 더 쪼개는
비용보다 훨씬 싸다.

`src/api/server.py`는 `_STRATEGY_FILES`에 없다. `/api/bars`(§9.3) 추가는 지문을 돌리지 않는다.

## 5. 데이터 흐름

```
WS 체결 틱 (46필드, 모스펙 §11.1)
  → f4._handle_price_tick                        [무변경]
      ├→ live.push_tick        A: UI·틱버퍼      [팬아웃 훅만 추가]
      │     └→ bars.on_tick    논블로킹 deque     [신규]
      └→ tick_capture.enqueue  durable           [무변경]

  bars 워커 (별도 asyncio task, 봉 마감에만 깨어남)
      → 1분 OHLCV 확정 + 틱파생값 집계                      [1단계]
      → 분봉 API 정정   09:11 이후 · BACKGROUND · 1분 1회    [1단계]
                        A가 HOLDING이고 WS stale이면 스킵 (§6.3)
      → indicators      순수 함수                            [1단계]
      → data/bars/YYYYMMDD_TICKER.json                       [1단계]
           ↕
      → GET /api/bars → 봉·지표 차트 (§9)                    [1단계]

      → b_signal.evaluate()   v0 규칙, 파라미터는 strategy_configs   [2단계]
      → shadow_book.record()  배치 write                             [2단계]
```

## 6. 봉 계층

### 6.1 집계

`bars.on_tick`은 1분 OHLCV와 함께 모스펙 §11.1이 확정한 틱 파생값을 누적한다.

| 값 | idx | 봉 집계 방식 |
|---|---|---|
| OHLCV | 2 / 12 | 시가·고가·저가·종가·거래량 합 |
| 체결강도 `CTTR` | 18 | 봉 구간 마지막 값 |
| 체결구분 `CCLD_DVSN` | 21 | 매수/매도 주도 체결량 분리 합 |
| 최우선 호가 `ASKP1`/`BIDP1` | 10 / 11 | 봉 종료 시점 값 |
| 총 호가잔량 | 38 / 39 | 봉 종료 시점 값 |

**틱 파생값은 분봉 API로 정정되지 않는다.** 정정 대상은 OHLCV뿐이므로 `corrected: false`를
값에 표시한다.

파일 형식은 기존 `data/backtest_bars/YYYYMMDD_TICKER.json`을 그대로 따른다 —
`{date, time, open, high, low, close, volume}`에 틱 파생값과 `confirmed` 플래그를 더한다.
저장 위치는 `data/bars/YYYYMMDD_TICKER.json`이고, 재생 검증의 입력이 된다.

### 6.2 확정 봉 정정

틱 집계 봉과 거래소 공식 분봉은 다를 수 있다. WS 유실 구간과 REST 백업 폴링 구간에서는
틱이 성겨 OHLC가 부정확해진다.

`scripts/kis_minute_bar_poc.py`의 `parse_minute_bars()`(POC:69)를 `src/api/kis_minute_bars.py`로
승격시킨다. 1페이지가 최근 약 30봉을 주므로 **1분 1회 호출로 충분**하다.

정정이 도착하면 집계본을 대체하고 **지표를 재계산한다.** 이미 신호를 낸 봉이 정정으로
뒤집히면 `shadow_trades`에 `correction_flipped` 플래그를 남긴다 — 지우지 않는다. 실시간
판정과 사후 진실이 어긋난 사례 자체가 그림자 검증의 자료다.

### 6.3 유량 가드

분봉 API 호출을 거르는 조건은 둘이다.

1. **09:00~09:11** — `FORBIDDEN_START/END`(POC:45)를 런타임 규약으로 승격. A의 F1 선정
   (09:00)부터 F3 체결 마감(09:11)까지의 창이다. B는 이 구간에 지표가 없으므로 잃는 것이 없다
2. **A가 HOLDING이고 WS가 stale일 때** — A의 F4 REST 백업이 깨어나 PAPER 초당 1건 예산을
   쓰고 있는 상황이다. B의 정정이 A의 손절 추적과 경합하면 안 된다

우선순위는 `REQUEST_PRIORITY_BACKGROUND(40)`으로 주문 경로 뒤에 세운다.

### 6.4 트랙 B 전용 `SpikeFilter` (모스펙 §3.4)

`live.push_tick`은 스파이크 필터 **이전**에 호출된다(필터는 `_process_tick` 내부,
f4_tracking.py:845). 따라서 §3.1의 팬아웃 훅으로 들어오는 틱은 **여과되지 않은 원시 틱**이다.

`bars`는 **자체 `SpikeFilter` 인스턴스**를 갖는다. A의 인스턴스를 공유하면 B가 소비하는
틱까지 A의 필터 내부 상태(직전가·연속 이상치 카운트)에 반영되어 A의 손절 판정이 오염된다.

필터를 통과하지 못한 틱은 봉에 넣지 않되 **버린 건수를 봉에 기록한다.** 이상치가 몰린 봉은
지표를 믿을 수 없다는 신호이고, 그 사실이 사후 분석에 필요하다.

### 6.5 재시작 복구

봉은 인메모리 누적이므로 **장중 재시작하면 그날 봉이 통째로 사라진다.** 지표는 최소 26봉을
요구하므로(모스펙 §7.1) 복구가 없으면 재시작한 날의 B는 09:35가 아니라 재시작 후 26분이
지나서야 깨어난다.

기동 시 분봉 API로 당일 봉을 복원한다. 1페이지가 약 30봉이므로 여러 장을 이어 받는다
(`fetch_all_minute_bars`, POC:254). 복원된 봉은 전부 `confirmed: true`다 — 공식 분봉이기
때문이다.

두 가지 제약이 걸린다.

- **09:00~09:11에 재시작하면 복원을 09:11까지 미룬다** (§6.3의 금지창). 그 구간에는 B가
  쓸 봉이 어차피 26개도 되지 않는다
- **틱 파생값(체결강도·호가·잔량)은 복원되지 않는다.** 분봉 API가 주지 않는다. 복원 구간의
  봉에는 `tick_derived: null`을 남기고, v0 규칙은 이 값들을 쓰지 않으므로 판정에 영향이 없다.
  이 값을 쓰는 규칙을 나중에 만들면 그때 복원 구간을 신호 대상에서 제외해야 한다

트랙 B의 포지션 상태는 모스펙 §3.2의 `today_state.json` `tracks` 섹션에서 그대로 복원된다 —
이 스펙이 새로 만들 것은 없다.

## 7. 지표 엔진

```python
indicators.sma(bars, period)              -> list[float | None]
indicators.ema(bars, period)              -> list[float | None]
indicators.macd(bars, fast, slow, signal) -> list[dict]   # macd, signal, hist
```

상태도 I/O도 없다. 값이 설 수 없는 구간은 `None`을 돌려주고, 호출부가 `None`을 신호 없음으로
읽는다 — 0으로 채우면 MACD 히스토그램의 부호 판정이 개장 직후 거짓 신호를 낸다.

**전일 분봉 시드는 쓰지 않는다.** 09시 최대 상승 종목은 전일 종가 대비 갭이 크고, 전일 EMA를
이어받으면 개장 즉시 MACD가 극단값이 된다(모스펙 §7.1).

## 8. 신호 엔진 v0

규칙의 **모양은 코드에 고정**하고 **숫자만** `strategy_configs`에서 받는다. 여러 규칙을
설정으로 갈아끼우는 DSL은 이 덩어리의 범위가 아니다 — 파서·검증·테스트 비용이 통째로
들어오는데, 지금 필요한 규칙은 하나다.

### 8.1 규칙

**진입** — 아래를 모두 만족하는 **확정 봉**에서 신호. 미확정 봉으로는 판정하지 않는다.

- 봉 시각 ≥ `b_signal_start` (09:35)
- 봉 시각 ≤ `b_entry_deadline` (14:00)
- 종가 > `SMA(b_sma_period)`
- MACD 히스토그램이 직전 봉 음수 → 현재 봉 양수
- 트랙 B가 IDLE

**청산** — 먼저 걸리는 것 하나.

| 조건 | `close_reason` |
|---|---|
| 진입가 대비 −`b_hard_stop_ratio` (2.0%) 이하 | `HARD_STOP` |
| MACD 히스토그램 음전 | `SIGNAL_EXIT` |
| 종가 < `SMA(b_sma_period)` | `INDICATOR_STOP` |
| 15:15 (F5와 동일 시각) | `TIMEOUT` |

네 사유 전부 [db.py:43](../../../src/db.py#L43)의 `close_reason` CHECK에 이미 있다 —
`HARD_STOP`·`TIMEOUT`은 원래 있었고, `SIGNAL_EXIT`·`INDICATOR_STOP`·`TRACK_HALTED`는 모스펙
§4.5에서 미리 넓혀뒀다. `shadow_trades`도 같은 목록을 쓴다.

### 8.2 파라미터

```json
{
  "b_signal_start": "09:35",
  "b_entry_deadline": "14:00",
  "b_sma_period": 20,
  "b_macd_fast": 12, "b_macd_slow": 26, "b_macd_signal": 9,
  "b_hard_stop_ratio": 0.02
}
```

`strategy_configs`에 `kind='EXPLORATORY'`로 넣고 `code_fingerprint`는 `_TRACK_B_FILES`
해시를 쓴다.

### 8.3 가정 체결가 — 신호 봉 종가가 아니다

**가정 진입가는 신호 봉의 다음 확정 봉 시가다.** 신호를 낸 봉의 종가에 체결됐다고 가정하면
"봉이 닫히는 순간을 미리 알고 그 가격에 샀다"가 되어 그림자가 현실보다 유리해진다.

판정 근거로 셋을 함께 남긴다 — 신호 봉 종가, 다음 봉 시가, 신호 시점의 최우선 호가 스프레드.
슬리피지 가정은 **적용하지 않고 원자료만 남긴다.** 기존 `cost_model`의 가정을 사후에 여러
값으로 갈아끼워 재해석할 수 있어야 한다. 백테스트 하네스에서 이미 겪었듯 얕은 표본에서는
체결 가정 0.2% 차이에 결론의 부호가 뒤집힌다 — 가정을 데이터에 굳혀 넣으면 그 재해석이
불가능해진다.

## 9. 봉·지표 차트 (1단계)

모스펙 §8.3을 이 스펙의 1단계로 끌어온다. 규칙을 실험하며 정하는 것이 목표이므로 **지표
시각화는 장식이 아니라 실험 도구 자체다.**

### 9.1 화면 배치

오늘 화면에 세로로 쌓는다.

```
┌─ 기존 틱 가격흐름 (트랙 A) ──────────┐   [무변경]
│  원시 틱 · 최근 20분 · A의 참조선     │
├─ 가격 패널 (트랙 B) ─────────────────┤   [신규]
│  캔들 (1분 OHLCV)                    │
│  + 이동평균선 (설정 가능한 기간 N개)  │
├─ MACD 패널 (트랙 B) ─────────────────┤   [신규]
│  MACD선 · 시그널선 · 히스토그램       │
│  0 기준선                            │
└──────────────────────────────────────┘
```

MACD를 별도 패널로 분리하는 것은 선택이 아니다 — 가격은 수만 원대, MACD는 0 근방에서
진동하므로 같은 축에 그릴 수 없다.

### 9.2 렌더링

차트 라이브러리를 쓰지 않는다. 기존 화면이 이미 Canvas 2D를 직접 쓰고 있고
([app.js:548](../../html/assets/app.js#L548) `resizePriceFlowCanvas`), 캔들은 몸통
`fillRect` + 꼬리 `lineTo`라 신규 코드가 작다. DPR 처리·테마 색상(`themeVal`)·시간축
매핑·그리드는 이미 있는 것을 그대로 쓴다.

라이브러리를 넣으면 이 프로젝트에 없던 프런트 의존성 관리가 통째로 따라온다.

### 9.3 데이터 경로

```
GET /api/bars?track=B&date=YYYYMMDD
  → { bars: [{ts, open, high, low, close, volume, confirmed, tick_derived}...],
      indicators: { sma: [...], macd: [{macd, signal, hist}...] },
      meta: { corrected_count, spike_dropped, restored_range } }
```

**지표는 서버에서 계산한다.** §7에서 지표 엔진을 순수 함수로 격리한 이유가 여기서 살아난다 —
전략 판정과 차트가 **같은 함수**를 타야 "차트는 매수 신호인데 봇은 안 샀다"는 혼란이 없다.
브라우저에서 다시 계산하면 그 보장이 깨진다.

`src/api/server.py`는 `_STRATEGY_FILES`에 없으므로 이 엔드포인트는 A의 지문을 돌리지 않는다.

### 9.4 확정 봉과 미확정 봉을 구분한다

§6.2에서 마지막 봉은 틱 집계본이고 분봉 API 응답이 오면 대체된다. **그 차이가 화면에
보여야** "차트에서 본 값과 지표가 판단한 값이 다른" 상황을 디버깅할 수 있다.

미확정 봉은 흐리게 처리하고, 정정이 도착하면 그대로 반영한다. 정정으로 값이 바뀐 봉은
`meta.corrected_count`로 셈해 화면에 표시한다 — 정정이 잦으면 그날 데이터를 믿을 수 없다는
신호다.

§6.5의 복원 구간(`restored_range`)과 §6.4에서 스파이크로 버린 틱이 많은 봉도 함께 표시한다.

### 9.5 `app.js`를 건드리지 않는다

차트는 `docs/html/assets/bars_chart.js` 신규 파일이고, `index.html`에 `<script>` 한 줄을
더한다. `drawPriceFlow`는 한 줄도 바뀌지 않는다.

**[tests/js/price_flow_checks.js](../../../tests/js/price_flow_checks.js)의 무수정 통과가
이 결정의 회귀 방지선이다.** 이 테스트는 `app.js`에서 함수를 이름으로 추출해 Node에서 실제
실행하므로, `drawPriceFlow` 주변의 순수 헬퍼가 바뀌면 즉시 깨진다. 새 차트의 순수 헬퍼
(스케일 도메인, 봉→좌표 매핑, MACD 패널 도메인)는 같은 방식으로 `tests/js/bars_chart_checks.js`를
새로 만들어 검증한다.

### 9.6 1단계에는 마커가 없다

그림자 진입·청산 마커는 신호가 있어야 찍히므로 2단계다. 1단계 차트는 캔들·이동평균·MACD와
확정/미확정 구분까지다.

### 9.7 봉 수집을 기다리지 않는다

`data/backtest_bars/`에 POC가 받아 둔 실제 분봉이 이미 있다
(`20260727_006340.json` 등, `{date, time, open, high, low, close, volume}` 31봉). 형식이
§6.1과 같으므로 **차트는 첫 실시간 봉이 쌓이기 전에 이 파일들로 그려서 검증할 수 있다.**

## 10. 예산 — 이번엔 구현하지 않는다

모스펙 §5.1의 `TRACK_WEIGHT` 분배 코드를 **작성하지 않는다.**

SHADOW에서 B의 예산은 0이므로 분배 로직의 결과는 "A가 전액"이고, 이는 현행 코드와 정확히
같다. **코드를 넣고 0을 곱하는 것보다 넣지 않는 것이 A 무변화의 더 강한 보장이다.** F3의
예산 경로는 한 줄도 바뀌지 않는다.

같은 이유로 모스펙 §5.2~§5.4(불변식 감사·위반 정책·UNCERTAIN 처리)도 범위 밖이다. 실주문이
없으면 장부 스큐가 생길 경로 자체가 없다. 이것들은 PILOT과 함께 들어간다.

## 11. `shadow_trades`

**테이블은 1단계에서 만들고, 쓰는 것은 2단계다** (§4). 지문을 두 번 돌리지 않기 위해서다.

`trades`에 `execution_mode='SHADOW'`로 섞지 않는다. 필터 누락의 결과가 비대칭이기 때문이다 —
일어나지도 않은 거래가 실손익에 섞이면 조용히 오염되고 발견이 늦다(모스펙 §6.2).

```sql
CREATE TABLE IF NOT EXISTS shadow_trades (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    date                TEXT NOT NULL,
    track               TEXT NOT NULL DEFAULT 'B',
    ticker              TEXT NOT NULL,
    name                TEXT,
    experiment_id       TEXT,
    config_id           TEXT,
    signal_bar_ts       TEXT NOT NULL,     -- 신호를 낸 확정 봉의 시각
    signal_at           TEXT NOT NULL,     -- 그 봉이 확정된 실제 시각
    signal_close        REAL NOT NULL,     -- 신호 봉 종가
    assumed_entry_price REAL,              -- 다음 확정 봉 시가 (§8.3)
    assumed_entry_at    TEXT,
    assumed_exit_price  REAL,
    assumed_exit_at     TEXT,
    close_reason        TEXT CHECK (close_reason IN (
                            'HARD_STOP','SIGNAL_EXIT','INDICATOR_STOP',
                            'TIMEOUT','TRACK_HALTED'
                        )),
    pnl_pct             REAL,
    spread_at_signal    REAL,              -- 최우선 호가 스프레드
    indicators_json     TEXT NOT NULL,     -- 판정에 쓴 지표값 전량
    bars_ref            TEXT NOT NULL,     -- data/bars/... 재생 검증 입력
    correction_flipped  INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    UNIQUE (date, track, signal_bar_ts)
);
```

**`trades`로의 FK를 두지 않는다.** 실자본 손익에 섞일 경로가 구조적으로 존재하지 않아야
한다는 것이 이 테이블의 존재 이유다. 승격 시에도 데이터를 옮기지 않는다.

## 12. 오류 격리

앞선 조사에서 확인한 세 구멍을 여기서 막는다. **"SHADOW니까 안전하다"는 가정이 숨는 곳들이다.**

### 12.1 B는 A의 손절 앞에 서지 않는다

[f4_tracking.py:805](../../../src/modules/f4_tracking.py#L805)의 `live.push_tick`은
`_process_tick`(청산 판정)보다 **앞에서 동기 실행**된다. 바로 아래 `tick_capture.enqueue`는
try/except로 격리돼 있지만 `push_tick`은 무방비다.

`bars.on_tick`은 [tick_capture.py:264](../../../src/modules/tick_capture.py#L264)의 계약을
그대로 복사한다.

```python
def on_tick(...) -> None:
    """논블로킹. 어떤 예외도 전파하지 않는다(주문 경로 격리)."""
    try:
        _queue.append(...)
    except Exception:  # noqa: BLE001
        pass
```

**A의 손절 판정 경로에서 B가 하는 일은 `deque.append` 하나뿐이다.** 봉 확정·정정·지표·신호는
전부 별도 태스크에서 돈다. 지표 재계산을 매 틱이 아니라 봉 마감에만 하는 것도 같은 이유다.

### 12.2 B의 워커가 죽어도 A는 산다

`bars` 워커는 `tick_capture._writer_loop`과 같은 형태의 별도 asyncio 태스크다. 죽으면
`B_WORKER_DEAD`를 CRIT로 남기고 그날 B는 침묵한다. A의 WS 루프·폴링 루프에 전파되지 않는다.

### 12.3 B의 DB 쓰기가 A의 주문을 밀지 않는다

[db.py:16](../../../src/db.py#L16)의 aiosqlite 전역 단일 커넥션은 모든 작업을 커넥션 스레드
큐에 FIFO로 세운다. `shadow_trades` 쓰기가 A의 `open_trade`/`close_trade` 앞에 서면 주문
기록이 밀린다.

**B의 DB 쓰기는 09:11 이후, 봉 마감 단위 배치로만 한다.** A의 진입창에는 쓰지 않는다.

### 12.4 B는 A의 상태를 쓰지 않는다

B가 쓰는 상태는 `state.track('B')`가 반환하는 `TrackState`뿐이다. `state.get()`(트랙 A)에
대한 write는 코드 규약이자 테스트로 고정한다.

## 13. 수용 기준

### 단계별 배분

| 기준 | 1단계 | 2단계 |
|---|---|---|
| ① 기존 스위트 무수정 통과 | ✔ | ✔ |
| ② `trades`에 `track='B'` 0건 | ✔ (자명 — 쓰는 코드가 없다) | ✔ |
| ③ A 무간섭 3측정 | 앞의 둘 | 셋 다 |
| ④ 차트 | ✔ | 마커 추가 |
| ⑤ 재생 검증 | — | ✔ |

1단계에서 세 번째 측정(A의 DB 쓰기 지연)이 빠지는 이유는 **1단계에 B의 DB 쓰기가 없기
때문**이다. 봉은 파일로 떨어지고 `shadow_trades`는 비어 있다.

### 세부 기준

1. **기존 테스트 스위트 무수정 통과.** 고쳐야 한다면 실행 계층을 건드렸다는 뜻이고 전제가
   무너진 것이다 (모스펙 §8.4). **JS 쪽에서는 `price_flow_checks.js`가 이 기준이다** — 이것이
   무수정으로 통과해야 A의 차트가 안전하다 (§9.5)
2. **`trades`에 `track='B'` 행 0건** — 테스트로 강제
3. **A 무간섭 3측정.** 셋 다 **오프라인에서 결정적으로** 잰다 — 프로덕션 계측을 추가하면
   F4를 건드리게 되고, 그 순간 (1)의 "무수정"이 깨진다
   - **주문 수량·금액** — 같은 현금 스냅샷 입력에 F3의 예산 계산 결과가 동일하다. §10에서
     분배 코드를 넣지 않으므로 자명하지만, 나중에 누가 넣는 것을 막는 회귀선으로 고정한다
   - **틱→손절 판정 지연** — 고정 틱 스트림을 `_handle_price_tick`에 재생하고, B 리스너를
     등록한 경우와 안 한 경우의 소요 시간을 비교한다. 리스너가 동기 경로에 더하는 일은
     `deque.append` 하나이므로 유의한 차이가 없어야 한다
   - **A의 DB 쓰기 지연** — B의 `shadow_trades` 쓰기가 09:11 이전에 일어나지 않음을 테스트로
     강제한다. 장중 검증은 로그에서 A의 주문 기록 시각과 B의 배치 쓰기 시각이 겹치지 않음을
     확인하는 것으로 갈음한다
4. **차트가 실제 봉을 그린다** — `data/backtest_bars/`의 실제 분봉으로 캔들·이동평균·MACD
   2패널이 그려지고, 확정/미확정 봉이 구분돼 보인다 (§9.7). 라이브 봉을 기다리지 않는다
5. **재생 검증** — `bars_ref`가 가리키는 봉으로 `indicators`와 `b_signal`을 재실행한 결과가
   `shadow_trades`의 기록과 일치한다

### 13.6 신규 테스트

**1단계**

- 봉 집계 — WS 유실 구간, 다건 프레임(모스펙 §11.2), 종목 교체
- 지표 골든값 — `sma`/`ema`/`macd`의 알려진 입출력, `None` 구간의 경계
- 분봉 정정 — 집계본 대체와 지표 재계산
- 유량 가드 — 09:00~09:11 미호출, A HOLDING + WS stale 시 미호출
- B 전용 `SpikeFilter` — A의 필터 상태가 B의 틱으로 오염되지 않는다 (§6.4)
- 재시작 복구 — 장중 재기동 시 분봉 API로 당일 봉 복원, 금지창 재시작은 09:11까지 지연 (§6.5)
- 워커 격리 — `bars` 워커가 예외로 죽어도 A의 WS 루프가 계속 돈다 (§12.2)
- `tests/js/bars_chart_checks.js` — 차트의 순수 헬퍼(스케일 도메인, 봉→좌표 매핑, MACD 패널
  도메인). `price_flow_checks.js`와 같은 추출·eval 방식 (§9.5)

**2단계**

- `b_signal` 결정성 — 같은 봉 입력에 같은 신호
- `correction_flipped` — 신호를 낸 봉이 정정으로 뒤집힌 기록이 남는다
- 소급 재생 — 1단계에 쌓인 봉으로 재구성한 신호가 실시간 `shadow_trades`와 일치한다

## 14. 범위 밖

- **트랙 UI**(트랙 선택·A/B 비교 뷰, 모스펙 §8.2) — PILOT과 함께. 비교할 실적이 생긴 뒤다
- **예산 분배·불변식 감사** (모스펙 §5) — PILOT과 함께
- **승격 게이트·자동 강등** (모스펙 §6.3~§6.5) — PILOT과 함께
- **실주문** — 정의상 PILOT
- **규칙 DSL** — 규칙이 둘 이상 필요해지면 그때
- **트랙 3개 이상, 다른 종목 트랙** — 모스펙 §9 그대로

## 15. 선행 작업 — 캡처 공백 원인 확인

**2026-08-26의 캡처가 0바이트다.**

```
data/strategy_ticks/20260826/047040.09.jsonl.gz     0 bytes
data/strategy_ticks/20260827/006340.09.jsonl.gz   770 KB (09:26 기준, 증가 중)
```

두 날 모두 `price_path_manifests`에 `trade_id=None`으로 매니페스트가 생성됐다 — 모스펙
§3.3·§3.6의 관측 계층 중립화가 **실장에서 동작함이 확인됐다.** B가 먹을 틱이 실제로 흐른다.

그런데 08-26은 틱이 한 건도 안 들어왔고 `finalize`도 실행되지 않아 `IN_PROGRESS`로 남았다.
같은 코드에서 결과가 갈렸다. **절반의 날이 비면 표본 수집 기간이 두 배가 된다.** 원인부터
확인하고 B를 얹는다.

## 16. 미결 사항

- **v0 규칙의 숫자.** SMA 20·MACD(12,26,9)·하드스탑 2.0%는 관례적 출발점이지 근거 있는
  값이 아니다. **1단계 차트에서 09시 급등주의 분봉을 보고 2단계 착수 전에 정한다** — 이것이
  차트를 먼저 만드는 이유다(§1.3). 정할 때는 `strategy_configs`에 새 `config_id`로 남긴다
- **08-26 공백의 원인** (§15). 확인 결과에 따라 이 스펙의 선행 작업이 늘 수 있다
- **1단계 차트를 며칠 보고 2단계로 넘어갈지.** 미리 정하지 않는다. 규칙이 눈에 잡히면
  넘어간다. 다만 봉은 1단계 첫날부터 쌓이므로 이 기간이 표본 시계를 늦추지 않는다
