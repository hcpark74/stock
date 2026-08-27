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

// ── 드로잉 ────────────────────────────────────────────────────────────
// 아래는 캔버스에 의존하므로 Node 하네스가 추출하지 않는다.

const BARS_UP = '#ef5350';
const BARS_DOWN = '#1e88e5';
const BARS_SMA = '#f7a600';
const BARS_SIGNAL = '#9b59b6';
const BARS_PAD = {l: 52, r: 14, t: 12, b: 22};

function barsResize(canvas) {
  const ratio = window.devicePixelRatio || 1;
  // 표시 높이는 최초 1회만 재고 그 뒤로는 붙잡아 둔 값을 쓴다. CSS가 높이를
  // 잡아 주지 못하면(예: app.css가 낡은 채로 캐시된 경우) canvas의 height
  // 속성이 곧 레이아웃 높이가 되고, 아래에서 거기에 DPR을 곱해 되쓰는 순간
  // 갱신할 때마다 캔버스가 커지는 폭주가 된다. 가로는 CSS width:100%가 잡는다.
  if (!canvas.dataset.baseH) {
    canvas.dataset.baseH = String(canvas.clientHeight || canvas.height || 80);
  }
  const displayW = Math.max(320, Math.round(canvas.clientWidth || canvas.width));
  const displayH = Math.max(80, Math.round(Number(canvas.dataset.baseH)));
  const pw = Math.round(displayW * ratio);
  const ph = Math.round(displayH * ratio);
  if (canvas.width !== pw || canvas.height !== ph) { canvas.width = pw; canvas.height = ph; }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  return {ctx, W: displayW, H: displayH};
}

function barsGrid(ctx, W, H, chartH) {
  ctx.strokeStyle = 'rgba(120,123,134,.18)';
  ctx.lineWidth = 1;
  for (let i = 0; i < 4; i++) {
    const y = BARS_PAD.t + chartH * i / 3;
    ctx.beginPath(); ctx.moveTo(BARS_PAD.l, y); ctx.lineTo(W - BARS_PAD.r, y); ctx.stroke();
  }
}

function drawBarsPricePanel(payload) {
  const canvas = document.getElementById('bars-price');
  if (!canvas) return;
  const {ctx, W, H} = barsResize(canvas);
  ctx.clearRect(0, 0, W, H);
  const chartW = W - BARS_PAD.l - BARS_PAD.r;
  const chartH = H - BARS_PAD.t - BARS_PAD.b;
  const rows = payload.bars || [];
  const smaSeries = (payload.indicators && payload.indicators.sma) || [];
  const domain = barsPriceDomain(rows, smaSeries);
  barsGrid(ctx, W, H, chartH);

  const width = barsCandleWidth(rows.length, chartW);
  rows.forEach((bar, i) => {
    const x = barsTimeIndex(rows, i, chartW, BARS_PAD.l);
    const up = bar.close >= bar.open;
    // 미확정 봉은 흐리게 — 분봉 API 정정이 오면 값이 바뀐다는 표시다.
    ctx.globalAlpha = bar.confirmed ? 1 : 0.4;
    ctx.strokeStyle = up ? BARS_UP : BARS_DOWN;
    ctx.fillStyle = up ? BARS_UP : BARS_DOWN;
    ctx.beginPath();
    ctx.moveTo(x, barsYAt(bar.high, domain, BARS_PAD.t, chartH));
    ctx.lineTo(x, barsYAt(bar.low, domain, BARS_PAD.t, chartH));
    ctx.stroke();
    const yOpen = barsYAt(bar.open, domain, BARS_PAD.t, chartH);
    const yClose = barsYAt(bar.close, domain, BARS_PAD.t, chartH);
    ctx.fillRect(x - width / 2, Math.min(yOpen, yClose), width, Math.max(1, Math.abs(yClose - yOpen)));
    ctx.globalAlpha = 1;
  });

  ctx.strokeStyle = BARS_SMA;
  ctx.beginPath();
  let started = false;
  smaSeries.forEach((value, i) => {
    if (!Number.isFinite(value)) { started = false; return; }
    const x = barsTimeIndex(rows, i, chartW, BARS_PAD.l);
    const y = barsYAt(value, domain, BARS_PAD.t, chartH);
    if (started) ctx.lineTo(x, y); else { ctx.moveTo(x, y); started = true; }
  });
  ctx.stroke();

  ctx.fillStyle = '#787b86';
  ctx.font = '10px Noto Sans KR,sans-serif';
  ctx.textAlign = 'right';
  ctx.fillText(Math.round(domain.max).toLocaleString(), BARS_PAD.l - 6, BARS_PAD.t + 8);
  ctx.fillText(Math.round(domain.min).toLocaleString(), BARS_PAD.l - 6, BARS_PAD.t + chartH);
}

function drawBarsMacdPanel(payload) {
  const canvas = document.getElementById('bars-macd');
  if (!canvas) return;
  const {ctx, W, H} = barsResize(canvas);
  ctx.clearRect(0, 0, W, H);
  const chartW = W - BARS_PAD.l - BARS_PAD.r;
  const chartH = H - BARS_PAD.t - BARS_PAD.b;
  const rows = payload.bars || [];
  const macdRows = (payload.indicators && payload.indicators.macd) || [];
  const domain = barsMacdDomain(macdRows);
  barsGrid(ctx, W, H, chartH);

  const zeroY = barsYAt(0, domain, BARS_PAD.t, chartH);
  ctx.strokeStyle = 'rgba(120,123,134,.45)';
  ctx.beginPath(); ctx.moveTo(BARS_PAD.l, zeroY); ctx.lineTo(W - BARS_PAD.r, zeroY); ctx.stroke();

  const width = barsCandleWidth(macdRows.length, chartW);
  macdRows.forEach((row, i) => {
    if (!row || !Number.isFinite(row.hist)) return;
    const x = barsTimeIndex(rows, i, chartW, BARS_PAD.l);
    const y = barsYAt(row.hist, domain, BARS_PAD.t, chartH);
    ctx.fillStyle = row.hist >= 0 ? BARS_UP : BARS_DOWN;
    ctx.fillRect(x - width / 2, Math.min(y, zeroY), width, Math.max(1, Math.abs(zeroY - y)));
  });

  for (const [key, color] of [['macd', BARS_SMA], ['signal', BARS_SIGNAL]]) {
    ctx.strokeStyle = color;
    ctx.beginPath();
    let started = false;
    macdRows.forEach((row, i) => {
      const value = row ? row[key] : null;
      if (!Number.isFinite(value)) { started = false; return; }
      const x = barsTimeIndex(rows, i, chartW, BARS_PAD.l);
      const y = barsYAt(value, domain, BARS_PAD.t, chartH);
      if (started) ctx.lineTo(x, y); else { ctx.moveTo(x, y); started = true; }
    });
    ctx.stroke();
  }
}

function drawBarsChart(payload) {
  drawBarsPricePanel(payload);
  drawBarsMacdPanel(payload);
  const sub = document.getElementById('bars-sub');
  if (!sub) return;
  const meta = payload.meta || {};
  if (!meta.bar_count) { sub.textContent = '봉 수집 대기'; return; }
  const unconfirmed = meta.bar_count - (meta.confirmed_count || 0);
  sub.textContent =
    `${payload.ticker || '-'} · ${meta.bar_count}봉 (미확정 ${unconfirmed})` +
    ` · 스파이크 ${meta.spike_dropped || 0} · 출처 ${meta.source || '-'}`;
}

async function refreshBarsChart() {
  try {
    const res = await fetch('/api/bars?track=B');
    if (!res.ok) return;
    drawBarsChart(await res.json());
  } catch (e) {
    // 차트 갱신 실패가 나머지 화면을 흔들면 안 된다
  }
}

document.addEventListener('DOMContentLoaded', () => {
  refreshBarsChart();
  setInterval(refreshBarsChart, 30000);
  window.addEventListener('resize', () => refreshBarsChart());
});
