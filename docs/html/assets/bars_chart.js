// 트랙 B 봉·지표 차트.
// app.js의 drawPriceFlow(트랙 A 틱 차트)는 한 줄도 건드리지 않는다 — 요구사항이
// 다르다(원시 틱 vs 1분 봉, 20분 창 vs 60분+, 1패널 vs 2패널).
// 아래 bars* 함수들은 순수 함수이며 tests/js/bars_chart_checks.js가 Node에서
// 이름으로 추출해 실행한다. 외부 의존을 넣지 말 것.

const BARS_MIN_CANDLE_PX = 1;
const BARS_DOMAIN_PAD = 0.02;

function barsPriceDomain(bars, smaSeries) {
  let min = Infinity;
  let max = -Infinity;
  for (const b of (bars || [])) {
    if (Number.isFinite(b.low)) min = Math.min(min, b.low);
    if (Number.isFinite(b.high)) max = Math.max(max, b.high);
  }
  for (const v of (smaSeries || [])) {
    if (Number.isFinite(v)) { min = Math.min(min, v); max = Math.max(max, v); }
  }
  if (!Number.isFinite(min) || !Number.isFinite(max)) return {min: 0, max: 1};
  if (max === min) { const pad = Math.abs(max) * BARS_DOMAIN_PAD || 1; return {min: min - pad, max: max + pad}; }
  const pad = (max - min) * BARS_DOMAIN_PAD;
  return {min: min - pad, max: max + pad};
}

function barsMacdDomain(macdRows) {
  // 0을 반드시 담는다 — 0 기준선이 화면 밖으로 나가면 히스토그램 부호를 못 읽는다.
  let min = 0;
  let max = 0;
  for (const r of (macdRows || [])) {
    for (const key of ['macd', 'signal', 'hist']) {
      const v = r ? r[key] : null;
      if (Number.isFinite(v)) { min = Math.min(min, v); max = Math.max(max, v); }
    }
  }
  if (max === min) return {min: min - 1, max: max + 1};
  const pad = (max - min) * BARS_DOMAIN_PAD;
  return {min: min - pad, max: max + pad};
}

function barsCandleWidth(count, chartW) {
  const n = Math.max(1, count || 1);
  const slot = chartW / n;
  return Math.max(BARS_MIN_CANDLE_PX, slot * 0.7);
}

function barsTimeIndex(bars, i, chartW, padLeft) {
  const n = Math.max(1, (bars || []).length);
  const slot = chartW / n;
  return padLeft + slot * (i + 0.5);
}

function barsYAt(value, domain, padTop, chartH) {
  const span = (domain.max - domain.min) || 1;
  const ratio = (value - domain.min) / span;
  return padTop + chartH * (1 - ratio);
}
