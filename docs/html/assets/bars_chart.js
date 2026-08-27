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

function barsVolumeDomain(bars) {
  // 거래량 축은 언제나 0에서 시작한다 — 밑을 잘라내면 막대 길이 비교가 거짓말이 된다.
  let max = 0;
  for (const b of (bars || [])) {
    const v = b ? b.volume : null;
    if (Number.isFinite(v) && v > max) max = v;
  }
  if (!(max > 0)) return {min: 0, max: 1};
  return {min: 0, max: max * (1 + BARS_DOMAIN_PAD)};
}

function barsIndexAtX(x, chartW, padLeft, count) {
  // barsTimeIndex의 역함수. 십자선이 어느 봉 위에 있는지 되찾는 데 쓴다.
  const n = Math.max(1, count || 1);
  const slot = chartW / n;
  if (!(slot > 0)) return 0;
  const i = Math.floor((x - padLeft) / slot);
  return Math.max(0, Math.min(n - 1, i));
}

function barsTimeTicks(bars, chartW, minGapPx) {
  // 라벨이 겹치지 않을 만큼 벌어지는 "정각 분" 간격만 고른다. 봉이 빠진
  // 구간이 있어도 각 봉의 실제 시각으로 판정하므로 어긋나지 않는다.
  const rows = bars || [];
  const n = rows.length;
  if (!n || !(chartW > 0)) return [];
  const slot = chartW / n;
  const gap = minGapPx > 0 ? minGapPx : 56;
  const step = [1, 2, 5, 10, 15, 30, 60].find(m => m * slot >= gap) || 60;
  const ticks = [];
  for (let i = 0; i < n; i++) {
    const t = String((rows[i] && rows[i].time) || '').padStart(6, '0');
    const hh = t.slice(0, 2);
    const mm = Number(t.slice(2, 4));
    if (!/^\d\d$/.test(hh) || !Number.isFinite(mm)) continue;
    if (mm % step !== 0) continue;
    ticks.push({index: i, label: `${hh}:${t.slice(2, 4)}`});
  }
  return ticks;
}

// ── 드로잉 ────────────────────────────────────────────────────────────
// 아래는 캔버스에 의존하므로 Node 하네스가 추출하지 않는다.

const BARS_UP = '#ef5350';
const BARS_DOWN = '#1e88e5';
const BARS_SMA = '#f7a600';
const BARS_SIGNAL = '#9b59b6';
const BARS_AXIS = '#787b86';
const BARS_GRID = 'rgba(120,123,134,.18)';
const BARS_CROSS = 'rgba(120,123,134,.7)';

// 시간 라벨은 맨 아래 패널에만 붙인다 — 세 패널이 같은 x축을 공유하므로 한 번
// 이면 충분하고, 위 두 패널은 아래 여백을 줄여 그림에 더 많은 픽셀을 준다.
const BARS_PAD = {l: 52, r: 14, t: 12, b: 22};
const BARS_PAD_UPPER = {l: 52, r: 14, t: 10, b: 6};

const BARS_PANELS = ['bars-price', 'bars-volume', 'bars-macd'];

let barsPayload = null;   // 마지막으로 받은 /api/bars 응답
let barsHover = null;     // {panel, index, y} — 십자선 위치. 없으면 null.

function barsPadFor(id) {
  return id === 'bars-macd' ? BARS_PAD : BARS_PAD_UPPER;
}

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
  const displayH = Math.max(48, Math.round(Number(canvas.dataset.baseH)));
  const pw = Math.round(displayW * ratio);
  const ph = Math.round(displayH * ratio);
  if (canvas.width !== pw || canvas.height !== ph) { canvas.width = pw; canvas.height = ph; }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  return {ctx, W: displayW, H: displayH};
}

function barsFrame(id) {
  const canvas = document.getElementById(id);
  if (!canvas) return null;
  const pad = barsPadFor(id);
  const {ctx, W, H} = barsResize(canvas);
  ctx.clearRect(0, 0, W, H);
  return {ctx, W, H, pad, chartW: W - pad.l - pad.r, chartH: H - pad.t - pad.b};
}

function barsGrid(f, rows, ticks, withLabels) {
  const {ctx, W, pad, chartW, chartH} = f;
  ctx.setLineDash([]);
  ctx.lineWidth = 1;
  ctx.strokeStyle = BARS_GRID;
  for (let i = 0; i < 4; i++) {
    const y = pad.t + chartH * i / 3;
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(W - pad.r, y); ctx.stroke();
  }
  ctx.fillStyle = BARS_AXIS;
  ctx.font = '10px Noto Sans KR,sans-serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'top';
  for (const tick of ticks) {
    const x = barsTimeIndex(rows, tick.index, chartW, pad.l);
    ctx.beginPath(); ctx.moveTo(x, pad.t); ctx.lineTo(x, pad.t + chartH); ctx.stroke();
    if (withLabels) ctx.fillText(tick.label, x, pad.t + chartH + 5);
  }
  ctx.textAlign = 'left';
}

function barsAxisLabel(f, text, y) {
  const {ctx, pad} = f;
  ctx.fillStyle = BARS_AXIS;
  ctx.font = '10px Noto Sans KR,sans-serif';
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  ctx.fillText(text, pad.l - 6, y);
  ctx.textAlign = 'left';
}

function barsFmtVolume(value) {
  if (!Number.isFinite(value)) return '—';
  if (value >= 10000) return (value / 10000).toFixed(1) + '만';
  return Math.round(value).toLocaleString();
}

// 십자선. 세로선은 세 패널 모두에, 가로선과 축 라벨은 마우스가 있는 패널에만.
function barsCrosshair(f, id, rows, domain, format) {
  if (!barsHover || !rows.length) return;
  const {ctx, W, pad, chartW, chartH} = f;
  const x = barsTimeIndex(rows, barsHover.index, chartW, pad.l);
  ctx.save();
  ctx.strokeStyle = BARS_CROSS;
  ctx.lineWidth = 1;
  ctx.setLineDash([3, 3]);
  ctx.beginPath(); ctx.moveTo(x, pad.t); ctx.lineTo(x, pad.t + chartH); ctx.stroke();
  if (barsHover.panel === id && domain) {
    const y = Math.max(pad.t, Math.min(pad.t + chartH, barsHover.y));
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(W - pad.r, y); ctx.stroke();
    ctx.setLineDash([]);
    const span = (domain.max - domain.min) || 1;
    const value = domain.min + span * (1 - (y - pad.t) / (chartH || 1));
    barsAxisLabel(f, format(value), y);
  }
  ctx.restore();
}

function drawBarsPricePanel(payload, ticks) {
  const f = barsFrame('bars-price');
  if (!f) return;
  const {ctx, pad, chartW, chartH} = f;
  const rows = payload.bars || [];
  const smaSeries = (payload.indicators && payload.indicators.sma) || [];
  const domain = barsPriceDomain(rows, smaSeries);
  barsGrid(f, rows, ticks, false);

  const width = barsCandleWidth(rows.length, chartW);
  rows.forEach((bar, i) => {
    const x = barsTimeIndex(rows, i, chartW, pad.l);
    const up = bar.close >= bar.open;
    // 미확정 봉은 흐리게 — 분봉 API 정정이 오면 값이 바뀐다는 표시다.
    ctx.globalAlpha = bar.confirmed ? 1 : 0.4;
    ctx.strokeStyle = up ? BARS_UP : BARS_DOWN;
    ctx.fillStyle = up ? BARS_UP : BARS_DOWN;
    ctx.beginPath();
    ctx.moveTo(x, barsYAt(bar.high, domain, pad.t, chartH));
    ctx.lineTo(x, barsYAt(bar.low, domain, pad.t, chartH));
    ctx.stroke();
    const yOpen = barsYAt(bar.open, domain, pad.t, chartH);
    const yClose = barsYAt(bar.close, domain, pad.t, chartH);
    ctx.fillRect(x - width / 2, Math.min(yOpen, yClose), width, Math.max(1, Math.abs(yClose - yOpen)));
    ctx.globalAlpha = 1;
  });

  ctx.strokeStyle = BARS_SMA;
  ctx.beginPath();
  let started = false;
  smaSeries.forEach((value, i) => {
    if (!Number.isFinite(value)) { started = false; return; }
    const x = barsTimeIndex(rows, i, chartW, pad.l);
    const y = barsYAt(value, domain, pad.t, chartH);
    if (started) ctx.lineTo(x, y); else { ctx.moveTo(x, y); started = true; }
  });
  ctx.stroke();

  // 그리드 4선 전부에 값을 붙인다 — 두 개만 있으면 중간을 눈으로 재야 한다.
  for (let i = 0; i < 4; i++) {
    const y = pad.t + chartH * i / 3;
    const value = domain.max - (domain.max - domain.min) * i / 3;
    barsAxisLabel(f, Math.round(value).toLocaleString(), y);
  }
  barsCrosshair(f, 'bars-price', rows, domain, v => Math.round(v).toLocaleString());
}

function drawBarsVolumePanel(payload, ticks) {
  const f = barsFrame('bars-volume');
  if (!f) return;
  const {ctx, pad, chartW, chartH} = f;
  const rows = payload.bars || [];
  const domain = barsVolumeDomain(rows);
  barsGrid(f, rows, ticks, false);

  const width = barsCandleWidth(rows.length, chartW);
  const base = pad.t + chartH;
  rows.forEach((bar, i) => {
    if (!Number.isFinite(bar.volume)) return;
    const x = barsTimeIndex(rows, i, chartW, pad.l);
    const y = barsYAt(bar.volume, domain, pad.t, chartH);
    ctx.globalAlpha = bar.confirmed ? 1 : 0.4;
    ctx.fillStyle = bar.close >= bar.open ? BARS_UP : BARS_DOWN;
    ctx.fillRect(x - width / 2, y, width, Math.max(1, base - y));
    ctx.globalAlpha = 1;
  });

  barsAxisLabel(f, barsFmtVolume(domain.max), pad.t);
  barsCrosshair(f, 'bars-volume', rows, domain, barsFmtVolume);
}

function drawBarsMacdPanel(payload, ticks) {
  const f = barsFrame('bars-macd');
  if (!f) return;
  const {ctx, W, pad, chartW, chartH} = f;
  const rows = payload.bars || [];
  const macdRows = (payload.indicators && payload.indicators.macd) || [];
  const domain = barsMacdDomain(macdRows);
  barsGrid(f, rows, ticks, true);

  const zeroY = barsYAt(0, domain, pad.t, chartH);
  ctx.strokeStyle = 'rgba(120,123,134,.45)';
  ctx.beginPath(); ctx.moveTo(pad.l, zeroY); ctx.lineTo(W - pad.r, zeroY); ctx.stroke();

  const width = barsCandleWidth(macdRows.length, chartW);
  macdRows.forEach((row, i) => {
    if (!row || !Number.isFinite(row.hist)) return;
    const x = barsTimeIndex(rows, i, chartW, pad.l);
    const y = barsYAt(row.hist, domain, pad.t, chartH);
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
      const x = barsTimeIndex(rows, i, chartW, pad.l);
      const y = barsYAt(value, domain, pad.t, chartH);
      if (started) ctx.lineTo(x, y); else { ctx.moveTo(x, y); started = true; }
    });
    ctx.stroke();
  }

  barsAxisLabel(f, domain.max.toFixed(1), pad.t);
  barsAxisLabel(f, domain.min.toFixed(1), pad.t + chartH);
  barsCrosshair(f, 'bars-macd', rows, domain, v => v.toFixed(1));
}

function barsReadout(payload) {
  const out = document.getElementById('bars-readout');
  if (!out) return;
  const rows = payload.bars || [];
  if (!barsHover || !rows[barsHover.index]) { out.textContent = ''; return; }
  const bar = rows[barsHover.index];
  const ind = payload.indicators || {};
  const sma = (ind.sma || [])[barsHover.index];
  const md = (ind.macd || [])[barsHover.index] || {};
  const t = String(bar.time || '').padStart(6, '0');
  const change = bar.open ? ((bar.close - bar.open) / bar.open) * 100 : 0;
  const n = v => (Number.isFinite(v) ? Math.round(v).toLocaleString() : '—');
  const f1 = v => (Number.isFinite(v) ? v.toFixed(1) : '—');
  out.innerHTML =
    '<b>' + t.slice(0, 2) + ':' + t.slice(2, 4) + '</b>' +
    ' 시 ' + n(bar.open) + ' 고 ' + n(bar.high) +
    ' 저 ' + n(bar.low) + ' 종 ' + n(bar.close) +
    ' <span class="' + (change >= 0 ? 'bars-up' : 'bars-dn') + '">' +
    (change >= 0 ? '+' : '') + change.toFixed(2) + '%</span>' +
    ' · 거래량 ' + barsFmtVolume(bar.volume) +
    ' · SMA ' + n(sma) +
    ' · MACD ' + f1(md.macd) + '/' + f1(md.signal) +
    (bar.confirmed ? '' : ' · 미확정');
}

function drawBarsChart(payload) {
  barsPayload = payload;
  const rows = payload.bars || [];
  // 시간 눈금은 세 패널이 공유한다. 맨 아래 패널의 폭으로 한 번만 고른다.
  const probe = document.getElementById('bars-macd');
  const chartW = probe
    ? Math.max(80, (probe.clientWidth || probe.width) - BARS_PAD.l - BARS_PAD.r)
    : 600;
  const ticks = barsTimeTicks(rows, chartW, 56);

  drawBarsPricePanel(payload, ticks);
  drawBarsVolumePanel(payload, ticks);
  drawBarsMacdPanel(payload, ticks);
  barsReadout(payload);

  const sub = document.getElementById('bars-sub');
  if (!sub) return;
  const meta = payload.meta || {};
  if (!meta.bar_count) { sub.textContent = '봉 수집 대기'; return; }
  const unconfirmed = meta.bar_count - (meta.confirmed_count || 0);
  sub.textContent =
    (payload.ticker || '-') + ' · ' + meta.bar_count + '봉 (미확정 ' + unconfirmed + ')' +
    ' · 스파이크 ' + (meta.spike_dropped || 0) + ' · 출처 ' + (meta.source || '-');
}

function barsRedraw() {
  if (barsPayload) drawBarsChart(barsPayload);
}

function barsBindHover() {
  for (const id of BARS_PANELS) {
    const canvas = document.getElementById(id);
    if (!canvas) continue;
    canvas.addEventListener('mousemove', event => {
      const rows = (barsPayload && barsPayload.bars) || [];
      if (!rows.length) return;
      const rect = canvas.getBoundingClientRect();
      const pad = barsPadFor(id);
      const chartW = rect.width - pad.l - pad.r;
      const index = barsIndexAtX(event.clientX - rect.left, chartW, pad.l, rows.length);
      barsHover = {panel: id, index: index, y: event.clientY - rect.top};
      barsRedraw();
    });
    canvas.addEventListener('mouseleave', () => {
      barsHover = null;
      barsRedraw();
    });
  }
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
  barsBindHover();
  refreshBarsChart();
  setInterval(refreshBarsChart, 30000);
  window.addEventListener('resize', () => refreshBarsChart());
});
