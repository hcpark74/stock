# '개선' 메뉴 — 파라미터 진단 화면 설계

날짜: 2026-07-15
상태: 확정 (사용자 승인)

## 목적

통계 메뉴(성과 확인)와 별도로, **전략 파라미터를 언제·무엇을·어느 방향으로 조정해야 하는지** 데이터 근거와 함께 보여주는 '개선' 메뉴를 추가한다. 지표를 나열하는 화면이 아니라, 파라미터별 진단 카드가 "현재값 → 근거 수치 → 판정 → 조정 방향"을 한 덩어리로 제시한다.

기존 통계 화면의 '개선 힌트' 섹션은 판별력이 낮아(손절 -2.0% 설정 시 `max_loss <= -2` 경고 영구 점등, 최저 청산사유 무조건 표시 등) 제거하고, 이 화면의 판정 뱃지가 그 역할을 대체한다.

## 대상 파라미터 (현재값)

| 모듈 | 파라미터 | 현재값 |
|---|---|---|
| F1 | GAP_MIN / GAP_CORE_MAX | 3.0% / 8.0% |
| F3 | GAP_MAX_ORDER / GAP_MAX_FILL (버퍼) | 6.5% / 7.0% (0.5%p) |
| F4 | STEP_SIZE | +2.5% |
| F4 | STEP_TRAIL | −1.5% |
| F4 | HARD_STOP_RATIO | −2.0% |
| F4 | 강제 트레일링 시각 | 10:50 |
| F5 | 타임아웃 청산 시각 | 11:00 |

## 화면 구성

사이드바에 '개선' 메뉴 신설(통계와 설정 사이). 화면 ID `sc-improve`, 진입 시 `loadImprove()` 호출. 상단 탭(오늘/이력/통계)에는 추가하지 않는다(선정·자산·주문과 동일하게 사이드바 전용).

위에서 아래로:

1. **전략 종합 카드** 1장 + **파라미터 카드** 6장 (그리드)
2. **거래별 고점 반납 표** — 날짜 / 종목 / MFE / 최종손익 / 반납폭 / 청산사유
3. **슬리피지 상세 표** — 주문 phase별 평균·최대 슬리피지, 평균 체결지연(ms)
4. **스킵·보유시간 요약** — 스킵 사유별 건수, 청산사유별 평균 보유시간(분)

### 카드 공통 형식

```
┌──────────────────────────────────────────────┐
│ STEP_SIZE  현재 +2.5%              [🔴 조정 검토] │
│ 스텝1 도달 2건(22%) · 근접 이탈 4건               │
│ → 고점 +1.5~2.4%까지 갔다가 손실로 끝난 거래가     │
│   스텝1 도달보다 많습니다. 간격 2.0% 축소를 검토.   │
└──────────────────────────────────────────────┘
```

- 뱃지 4단계: 🟢 양호 / 🟡 관찰 / 🔴 조정 검토 / ⚪ 표본 부족
- ⚪일 때는 "판정까지 N건 더 필요"를 표시해 성급한 조정을 막는다.
- 판정 기준값(0.3%p, 50% 등)은 가이드 문구에 그대로 노출해 판정 근거를 추적 가능하게 한다.
- 카드의 "현재값"은 API가 echo하는 실제 파라미터 값을 사용한다(하드코딩 금지).

## 지표 정의

- **MFE**(고점 수익률) = `(high_price / entry_price − 1) × 100`. 두 값이 모두 있는 거래만 계산.
- **반납폭(giveback)** = `MFE − pnl_pct` (%p).
- **근접 이탈(near miss)** = 스텝1 미도달(`highest_step < STEP_SIZE`) AND `MFE ≥ 1.5%` AND `pnl_pct ≤ 0`인 거래.
- **슬리피지**: FILLED 주문 중 order_price·fill_price가 모두 있는 건만. 불리하면 양수로 부호 통일 —
  매수(`FIRST_BUY`,`PYRAMID_BUY`): `(fill − order) / order × 100`,
  매도(`CLOSE_SELL`,`TIMEOUT_SELL`,`SLIPPAGE_SELL`): `(order − fill) / order × 100`.
- **손절 체결 편차** = `평균 손절 체결 pnl_pct + HARD_STOP(2.0)` 의 절대 초과분 (%p).
- **빠른 손절** = HARD_STOP 청산 중 진입~청산 10분 이내인 거래.
- **연속 손실 스트릭**: CLOSED 거래를 date 순으로 정렬해 `pnl_pct ≤ 0` 연속 구간. 현재 진행 중 스트릭과 역대 최대를 모두 계산.
- **기대값** = `승률 × 평균수익 + (1 − 승률) × 평균손실` (평균손실은 음수).

## 카드별 판정 규칙

판정 로직·기준값은 프론트(app.js)에 둔다(기존 힌트 패턴과 일치). 우선순위: 🔴 조건 중 하나라도 참이면 🔴, 아니면 🟡 조건, 아니면 🟢. 표본 가드 미달 시 무조건 ⚪.

| 카드 | 표본 가드 | 판정 |
|---|---|---|
| 전략 종합 | total < 10 → ⚪ | 🔴 기대값 < 0 (total ≥ 20) → "전략 자체 재검토" · 🔴 현재 연속손실 ≥ 3 → "일시 중단 검토" · 🟡 손익비 < 1 (total ≥ 10) · 그 외 🟢 |
| HARD_STOP | 손절 n < 3 → ⚪ | 🔴 체결 편차 > 0.3%p → "지정가 손절 또는 폭 조정 검토" · 🔴 손절 비중 > 50% (total ≥ 10) → "진입 품질 우선 점검" · 🟡 빠른 손절 비중 ≥ 50% → "시초 변동성 구간, 진입 지연 검토" |
| STEP_SIZE | total < 5 → ⚪ | 🔴 근접 이탈 ≥ 3건 AND 근접 이탈 > 스텝1 도달 건수 → "간격 2.0% 축소 검토" · 🟡 근접 이탈 ≥ 2건 · 🟢 스텝1 도달률 ≥ 40% |
| STEP_TRAIL | 트레일링 청산 n < 5 → ⚪ | 🔴 평균 반납폭 > 2.0%p → "폭 축소 검토" · 🟡 평균 반납폭 > 1.5%p |
| 슬리피지 버퍼 | 매수 슬리피지 n < 3 → ⚪ | 🔴 SLIPPAGE_GUARD ≥ 2건 OR 매수 슬리피지 최대 > 0.5%p → "GAP_MAX_ORDER 하향 또는 버퍼 확대 검토" · 🟡 매수 슬리피지 평균 > 0.25%p |
| F5 타임아웃 | TIMEOUT n < 5 → ⚪ | 🔴 평균 손익 < 0 → "청산 시각 단축 검토" · 🟡 평균 MFE ≥ 1.5% → "강제 트레일링(10:50) 앞당김 검토" |
| F1 갭 범위 | 스킵+거래일 < 10 → ⚪ | 🟡 스킵일 > 거래일 → "후보 부족, 범위 확대는 신중히". 진입 갭 미저장으로 정밀 판정 불가 — 카드에 "진입 갭 기록 시 판정 가능" 안내 고정 표시. 🔴 없음 |

## API: `GET /api/improve`

집계는 서버, 판정은 프론트. 기존 `/api/stats` 패턴을 따라 예외 시 빈 기본 구조를 반환하고 `API_IMPROVE_FAILED`로 로깅.

```json
{
  "params": {
    "step_size_pct": 2.5, "step_trail_pct": 1.5, "hard_stop_pct": 2.0,
    "gap_max_order_pct": 6.5, "gap_max_fill_pct": 7.0,
    "f1_gap_min_pct": 3.0, "f1_gap_core_max_pct": 8.0,
    "timeout_time": "11:00", "force_trailing_time": "10:50"
  },
  "overall": {
    "total": 0, "wins": 0, "win_rate": 0, "avg_win": 0, "avg_loss": 0,
    "payoff_ratio": 0, "expectancy": 0,
    "cur_loss_streak": 0, "max_loss_streak": 0
  },
  "hard_stop": {
    "n": 0, "share_pct": 0, "avg_fill_pnl": 0, "avg_slip_pp": 0,
    "fast_stop_n": 0, "avg_min_to_stop": 0
  },
  "step": {"step1_n": 0, "step1_rate": 0, "near_miss_n": 0},
  "trailing": {"n": 0, "avg_giveback_pp": 0, "avg_pnl": 0},
  "slippage": {
    "buy": {"n": 0, "avg_pp": 0, "max_pp": 0, "avg_latency_ms": 0},
    "sell": {"n": 0, "avg_pp": 0, "max_pp": 0, "avg_latency_ms": 0},
    "by_phase": {"FIRST_BUY": {"n": 0, "avg_pp": 0, "max_pp": 0, "avg_latency_ms": 0}},
    "guard_n": 0
  },
  "timeout_exit": {"n": 0, "avg_pnl": 0, "avg_mfe": 0},
  "candidates": {"skips": {"NO_TARGET": 0}, "skip_days": 0, "trade_days": 0},
  "mfe_rows": [
    {"date": "", "ticker": "", "name": "", "mfe_pct": 0, "pnl_pct": 0,
     "giveback_pp": 0, "close_reason": ""}
  ],
  "hold_time": {"HARD_STOP": {"n": 0, "avg_min": 0}}
}
```

- `params`는 f1_selector / f3_entry / f4_tracking 모듈 상수에서 읽어 echo(% 단위 변환).
- `mfe_rows`는 CLOSED 거래 전체를 date 내림차순으로. MFE 계산 불가(고점/진입가 누락) 거래는 mfe/giveback을 null로.
- `guard_n`은 `close_reason='SLIPPAGE_GUARD'` 거래 수.
- `candidates.trade_days`는 CLOSED 거래가 있는 날짜 수, `skip_days`는 daily_skips 행 수.

## 프론트 변경

- [docs/html/index.html] 사이드바 메뉴 '개선' 추가, `sc-improve` 화면 마크업(카드 그리드 + 상세 표 3종).
- [docs/html/assets/app.js] `go()`에 `if (id==='improve') loadImprove();` 추가. `loadImprove()` → fetch `/api/improve` → `renderImproveCards()` / `renderMfeTable()` / `renderSlippageTable()` / `renderSkipHold()`.
- 판정 기준값은 app.js 상단에 상수 객체(`IMPROVE_RULES`)로 모아 정의.
- **통계 화면에서 제거**: '개선 힌트' 섹션 마크업(`stats-hints`), `renderStatsHints()` 및 호출부. `sampleNote()`(표본 안내줄)는 통계 화면에 유지하고, 개선 화면 상단에도 동일하게 표시.
- CSS: 기존 factor-cell / hint-item 스타일 계열을 따르는 카드 스타일 추가(뱃지 색: 양호 `#26a69a`, 관찰 `#f7a600`, 조정 검토 `#ef5350`, 표본 부족 `#787b86`).

## 테스트

[tests/test_api_server.py]에 추가:

1. 빈 DB → 기본 구조(모든 키 존재, n=0) 반환.
2. 시드 거래 → MFE·반납폭·근접 이탈 건수 계산 검증.
3. 매수/매도 슬리피지 부호 방향(불리=양수) 각각 검증.
4. 연속 손실 스트릭(현재/최대) 검증.
5. 보유시간(분) 및 빠른 손절 카운트 검증.
6. daily_skips 집계 검증.

## 범위 밖 (후속 과제)

- 진입 시점 갭 크기 저장(trades 컬럼 추가) — F1 갭 범위 카드의 정밀 판정에 필요.
- 종목 선정 점수/순위 저장 — "진입 후보 품질" 판정에 필요.
- 파라미터 화면 내 직접 수정 기능(현재는 진단만).
