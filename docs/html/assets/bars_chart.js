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

function barsGapX(rows, index, chartW, padLeft) {
  // 갭 경계의 x. 빠진 분에는 폭이 없으므로(x축이 인덱스 기준) 재개 봉 슬롯의
  // 왼쪽 모서리, 즉 두 캔들 정확히 사이에 선을 놓는다.
  const n = Math.max(1, (rows || []).length);
  const slot = chartW / n;
  const at = Math.max(0, Math.min(n, Number(index) || 0));
  return padLeft + slot * at;
}

function barsWindow(total, view) {
  // 화면에 보이는 구간 [start, end). 증권사 차트처럼 일부만 띄우고 나머지는
  // 스크롤로 간다 — 하루 391봉을 한 화면에 밀어 넣으면 캔들이 2~3px가 된다.
  const n = Math.max(0, Math.floor(total) || 0);
  if (n <= 0) return {start: 0, end: 0, count: 0};
  const wanted = (view && Number.isFinite(view.count)) ? Math.floor(view.count) : n;
  const count = Math.max(1, Math.min(wanted > 0 ? wanted : n, n));
  const rawEnd = (view && Number.isFinite(view.end)) ? Math.floor(view.end) : n;
  const end = Math.max(count, Math.min(rawEnd, n));
  return {start: end - count, end: end, count: count};
}

function barsZoomView(view, total, factor, anchor) {
  // 커서 아래 봉을 제자리에 두고 확대·축소한다. end가 마지막에 닿으면 null로
  // 돌려 "최신 추종" 상태로 되돌린다 — 그래야 30초 갱신이 계속 따라간다.
  const win = barsWindow(total, view);
  if (!win.count) return {count: (view && view.count) || 120, end: null};
  const next = Math.max(20, Math.min(total, Math.round(win.count * factor)));
  const at = Math.max(win.start, Math.min(Math.round(anchor), win.end - 1));
  const ratio = win.count > 1 ? (at - win.start) / (win.count - 1) : 0;
  let end = Math.round(at - ratio * (next - 1)) + next;
  end = Math.max(next, Math.min(end, total));
  return {count: next, end: end >= total ? null : end};
}

function barsSliceView(payload, win) {
  // 창을 먼저 자르고 그 뒤는 전부 기존 함수를 그대로 쓴다. 인덱스 기준
  // 좌표 함수들이 자른 배열을 받으면 손댈 것이 없다.
  const all = (payload && payload.bars) || [];
  const ind = (payload && payload.indicators) || {};
  const cut = arr => (Array.isArray(arr) ? arr : []).slice(win.start, win.end);
  const cutMap = obj => {
    const out = {};
    for (const key of Object.keys(obj || {})) out[key] = cut(obj[key]);
    return out;
  };
  const gaps = (((payload && payload.meta) || {}).gaps || [])
    // start에 정확히 걸린 갭은 버린다 — 왼쪽 끝의 경계선은 가리킬 앞 봉이 없다.
    .filter(g => g && g.index > win.start && g.index < win.end)
    .map(g => ({after: g.after, resume: g.resume, missing: g.missing,
                jump_pct: g.jump_pct, index: g.index - win.start}));
  return {
    rows: all.slice(win.start, win.end),
    ma: cutMap(ind.ma),
    volMa: cutMap(ind.vol_ma),
    macd: cut(ind.macd),
    gaps: gaps,
  };
}

// ── 드로잉 ────────────────────────────────────────────────────────────
// 아래는 캔버스에 의존하므로 Node 하네스가 추출하지 않는다.

const BARS_UP = '#ef5350';
const BARS_DOWN = '#1e88e5';
const BARS_SIGNAL = '#9b59b6';
const BARS_AXIS = '#787b86';
const BARS_GRID = 'rgba(120,123,134,.18)';
const BARS_CROSS = 'rgba(120,123,134,.7)';
const BARS_GAP = '#26a69a';
const BARS_BADGE_TX = '#ffffff';

// 이동평균 3종. 증권사 차트의 관례를 따르되 캔들의 적/청과 겹치지 않는 색을 쓴다.
const BARS_MA = [
  {period: '5', color: '#f7a600'},
  {period: '20', color: '#ab47bc'},
  {period: '60', color: '#78909c'},
];
const BARS_VOL_MA = [
  {period: '5', color: '#f7a600'},
  {period: '20', color: '#ab47bc'},
];

// 가격축은 오른쪽이다 — HTS의 가장 큰 시각적 서명이고, 현재가 태그가 붙는 자리다.
// 시간 라벨은 맨 아래 패널에만 둔다. 세 패널이 x축을 공유하므로 한 번이면 된다.
const BARS_PAD = {l: 8, r: 62, t: 12, b: 22};
const BARS_PAD_UPPER = {l: 8, r: 62, t: 10, b: 6};

const BARS_PANELS = ['bars-price', 'bars-volume', 'bars-macd'];
const BARS_MIN_VIEW = 20;
const BARS_DEFAULT_VIEW = 120;

let barsPayload = null;   // 마지막으로 받은 /api/bars 응답 (전체 구간)
let barsHover = null;     // {panel, index, y} — index는 화면 구간 기준
let barsView = {count: BARS_DEFAULT_VIEW, end: null};  // end null = 최신 추종
let barsDrag = null;      // {x, end} — 드래그 시작점

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

// 축 라벨은 오른쪽 눈금자에 붙는다.
function barsAxisLabel(f, text, y) {
  const {ctx, W, pad} = f;
  ctx.fillStyle = BARS_AXIS;
  ctx.font = '10px Noto Sans KR,sans-serif';
  ctx.textAlign = 'left';
  ctx.textBaseline = 'middle';
  ctx.fillText(text, W - pad.r + 6, y);
}

// 현재가·십자선 값처럼 "지금 이 값"을 눈금자 위에 덮어 쓰는 배지.
function barsAxisBadge(f, text, y, color) {
  const {ctx, W, pad} = f;
  ctx.font = '10px Noto Sans KR,sans-serif';
  ctx.textAlign = 'left';
  ctx.textBaseline = 'middle';
  const top = Math.round(y) - 8;
  ctx.fillStyle = color;
  ctx.fillRect(W - pad.r + 1, top, pad.r - 3, 16);
  ctx.fillStyle = BARS_BADGE_TX;
  ctx.fillText(text, W - pad.r + 5, top + 8);
}

function barsTimeBadge(f, text, x) {
  const {ctx, pad, chartH} = f;
  ctx.font = '10px Noto Sans KR,sans-serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'top';
  const w = 36;
  ctx.fillStyle = 'rgba(120,123,134,.9)';
  ctx.fillRect(x - w / 2, pad.t + chartH + 2, w, 14);
  ctx.fillStyle = BARS_BADGE_TX;
  ctx.fillText(text, x, pad.t + chartH + 4);
  ctx.textAlign = 'left';
}

function barsFmtVolume(value) {
  if (!Number.isFinite(value)) return '—';
  if (value >= 10000) return (value / 10000).toFixed(1) + '만';
  return Math.round(value).toLocaleString();
}

// 값이 정의된 구간만 이어 그린다. None 구간에서 선을 끊는 것이 요점이다.
function barsLine(f, rows, series, color, width) {
  const {ctx, pad, chartW, chartH} = f;
  if (!series || !series.length) return;
  ctx.strokeStyle = color;
  ctx.lineWidth = width || 1;
  ctx.beginPath();
  let started = false;
  for (let i = 0; i < series.length; i++) {
    const value = series[i];
    if (!Number.isFinite(value)) { started = false; continue; }
    const x = barsTimeIndex(rows, i, chartW, pad.l);
    const y = barsYAt(value, f.domain, pad.t, chartH);
    if (started) ctx.lineTo(x, y); else { ctx.moveTo(x, y); started = true; }
  }
  ctx.stroke();
  ctx.lineWidth = 1;
}

function barsLegend(f, entries) {
  const {ctx, pad} = f;
  ctx.font = '10px Noto Sans KR,sans-serif';
  ctx.textAlign = 'left';
  ctx.textBaseline = 'top';
  let x = pad.l + 4;
  for (const entry of entries) {
    ctx.fillStyle = entry.color;
    ctx.fillText(entry.label, x, pad.t + 2);
    x += ctx.measureText(entry.label).width + 8;
  }
}

// 봉이 빠진 자리. 09:00~09:11은 분봉 정정이 금지돼 있어 거래소가 메워 주기
// 전까지 계열에 구멍이 남고, 그 창이 하필 B가 판단하는 구간이다.
function barsGapMarks(f, rows, gaps, withLabel) {
  if (!gaps || !gaps.length) return;
  const {ctx, pad, chartW, chartH} = f;
  ctx.save();
  ctx.strokeStyle = BARS_GAP;
  ctx.fillStyle = BARS_GAP;
  ctx.lineWidth = 1;
  ctx.setLineDash([2, 3]);
  ctx.font = '10px Noto Sans KR,sans-serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'top';
  for (const gap of gaps) {
    const x = barsGapX(rows, gap.index, chartW, pad.l);
    ctx.beginPath();
    ctx.moveTo(x, pad.t);
    ctx.lineTo(x, pad.t + chartH);
    ctx.stroke();
    if (withLabel) ctx.fillText(`${gap.missing}분 없음`, x, pad.t + 2);
  }
  ctx.restore();
}

// 십자선. 세로선은 세 패널 모두에, 가로선과 값 배지는 마우스가 있는 패널에만.
function barsCrosshair(f, id, rows, format) {
  if (!barsHover || !rows.length) return;
  const {ctx, W, pad, chartW, chartH} = f;
  const x = barsTimeIndex(rows, barsHover.index, chartW, pad.l);
  ctx.save();
  ctx.strokeStyle = BARS_CROSS;
  ctx.lineWidth = 1;
  ctx.setLineDash([3, 3]);
  ctx.beginPath(); ctx.moveTo(x, pad.t); ctx.lineTo(x, pad.t + chartH); ctx.stroke();
  if (barsHover.panel === id && f.domain) {
    const y = Math.max(pad.t, Math.min(pad.t + chartH, barsHover.y));
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(W - pad.r, y); ctx.stroke();
    ctx.setLineDash([]);
    const span = (f.domain.max - f.domain.min) || 1;
    const value = f.domain.min + span * (1 - (y - pad.t) / (chartH || 1));
    barsAxisBadge(f, format(value), y, 'rgba(120,123,134,.95)');
  }
  ctx.restore();
  // 시간 배지는 x축을 가진 맨 아래 패널에만
  if (id === 'bars-macd') {
    const bar = rows[barsHover.index];
    const t = String((bar && bar.time) || '').padStart(6, '0');
    barsTimeBadge(f, `${t.slice(0, 2)}:${t.slice(2, 4)}`, x);
  }
}

function drawBarsPricePanel(view, ticks) {
  const f = barsFrame('bars-price');
  if (!f) return;
  const {ctx, pad, chartW, chartH} = f;
  const rows = view.rows;
  const maSeries = BARS_MA.map(m => view.ma[m.period] || []);
  f.domain = barsPriceDomain(rows, [].concat.apply([], maSeries));
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
    ctx.moveTo(x, barsYAt(bar.high, f.domain, pad.t, chartH));
    ctx.lineTo(x, barsYAt(bar.low, f.domain, pad.t, chartH));
    ctx.stroke();
    const yOpen = barsYAt(bar.open, f.domain, pad.t, chartH);
    const yClose = barsYAt(bar.close, f.domain, pad.t, chartH);
    ctx.fillRect(x - width / 2, Math.min(yOpen, yClose), width, Math.max(1, Math.abs(yClose - yOpen)));
    ctx.globalAlpha = 1;
  });

  BARS_MA.forEach((m, i) => barsLine(f, rows, maSeries[i], m.color, 1));

  // 그리드 4선 전부에 값을 붙인다 — 두 개만 있으면 중간을 눈으로 재야 한다.
  for (let i = 0; i < 4; i++) {
    const y = pad.t + chartH * i / 3;
    const value = f.domain.max - (f.domain.max - f.domain.min) * i / 3;
    barsAxisLabel(f, Math.round(value).toLocaleString(), y);
  }

  // 현재가 태그 — 마지막 봉의 종가를 눈금자 위에 덮어 쓴다.
  const last = rows[rows.length - 1];
  if (last && Number.isFinite(last.close)) {
    const y = barsYAt(last.close, f.domain, pad.t, chartH);
    ctx.save();
    ctx.strokeStyle = last.close >= last.open ? BARS_UP : BARS_DOWN;
    ctx.setLineDash([4, 3]);
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(f.W - pad.r, y); ctx.stroke();
    ctx.restore();
    barsAxisBadge(f, Math.round(last.close).toLocaleString(), y,
      last.close >= last.open ? BARS_UP : BARS_DOWN);
  }

  barsLegend(f, BARS_MA.map(m => ({label: 'MA' + m.period, color: m.color})));
  barsGapMarks(f, rows, view.gaps, true);
  barsCrosshair(f, 'bars-price', rows, v => Math.round(v).toLocaleString());
}

function drawBarsVolumePanel(view, ticks) {
  const f = barsFrame('bars-volume');
  if (!f) return;
  const {ctx, pad, chartW, chartH} = f;
  const rows = view.rows;
  f.domain = barsVolumeDomain(rows);
  barsGrid(f, rows, ticks, false);

  const width = barsCandleWidth(rows.length, chartW);
  const base = pad.t + chartH;
  rows.forEach((bar, i) => {
    if (!Number.isFinite(bar.volume)) return;
    const x = barsTimeIndex(rows, i, chartW, pad.l);
    const y = barsYAt(bar.volume, f.domain, pad.t, chartH);
    ctx.globalAlpha = bar.confirmed ? 1 : 0.4;
    ctx.fillStyle = bar.close >= bar.open ? BARS_UP : BARS_DOWN;
    ctx.fillRect(x - width / 2, y, width, Math.max(1, base - y));
    ctx.globalAlpha = 1;
  });

  for (const m of BARS_VOL_MA) barsLine(f, rows, view.volMa[m.period], m.color, 1);

  barsAxisLabel(f, barsFmtVolume(f.domain.max), pad.t);
  barsGapMarks(f, rows, view.gaps, false);
  barsCrosshair(f, 'bars-volume', rows, barsFmtVolume);
}

function drawBarsMacdPanel(view, ticks) {
  const f = barsFrame('bars-macd');
  if (!f) return;
  const {ctx, W, pad, chartW, chartH} = f;
  const rows = view.rows;
  const macdRows = view.macd;
  f.domain = barsMacdDomain(macdRows);
  barsGrid(f, rows, ticks, true);

  const zeroY = barsYAt(0, f.domain, pad.t, chartH);
  ctx.strokeStyle = 'rgba(120,123,134,.45)';
  ctx.beginPath(); ctx.moveTo(pad.l, zeroY); ctx.lineTo(W - pad.r, zeroY); ctx.stroke();

  const width = barsCandleWidth(macdRows.length, chartW);
  macdRows.forEach((row, i) => {
    if (!row || !Number.isFinite(row.hist)) return;
    const x = barsTimeIndex(rows, i, chartW, pad.l);
    const y = barsYAt(row.hist, f.domain, pad.t, chartH);
    ctx.fillStyle = row.hist >= 0 ? BARS_UP : BARS_DOWN;
    ctx.fillRect(x - width / 2, Math.min(y, zeroY), width, Math.max(1, Math.abs(zeroY - y)));
  });

  barsLine(f, rows, macdRows.map(r => (r ? r.macd : null)), BARS_MA[0].color, 1);
  barsLine(f, rows, macdRows.map(r => (r ? r.signal : null)), BARS_SIGNAL, 1);

  barsAxisLabel(f, f.domain.max.toFixed(1), pad.t);
  barsAxisLabel(f, f.domain.min.toFixed(1), pad.t + chartH);
  barsGapMarks(f, rows, view.gaps, false);
  barsCrosshair(f, 'bars-macd', rows, v => v.toFixed(1));
}

function barsReadout(view) {
  const out = document.getElementById('bars-readout');
  if (!out) return;
  const rows = view.rows;
  if (!barsHover || !rows[barsHover.index]) { out.textContent = ''; return; }
  const i = barsHover.index;
  const bar = rows[i];
  const md = view.macd[i] || {};
  const t = String(bar.time || '').padStart(6, '0');
  const change = bar.open ? ((bar.close - bar.open) / bar.open) * 100 : 0;
  const n = v => (Number.isFinite(v) ? Math.round(v).toLocaleString() : '—');
  const f1 = v => (Number.isFinite(v) ? v.toFixed(1) : '—');
  const ma = BARS_MA
    .map(m => `<span class="bars-ma${m.period}">${m.period} ${n((view.ma[m.period] || [])[i])}</span>`)
    .join(' ');
  out.innerHTML =
    '<b>' + t.slice(0, 2) + ':' + t.slice(2, 4) + '</b>' +
    ' 시 ' + n(bar.open) + ' 고 ' + n(bar.high) +
    ' 저 ' + n(bar.low) + ' 종 ' + n(bar.close) +
    ' <span class="' + (change >= 0 ? 'bars-up' : 'bars-dn') + '">' +
    (change >= 0 ? '+' : '') + change.toFixed(2) + '%</span>' +
    ' · 거래량 ' + barsFmtVolume(bar.volume) +
    ' · MA ' + ma +
    ' · MACD ' + f1(md.macd) + '/' + f1(md.signal) +
    (bar.confirmed ? '' : ' · 미확정');
}

function drawBarsChart(payload) {
  barsPayload = payload;
  const total = (payload.bars || []).length;
  const win = barsWindow(total, barsView);
  const view = barsSliceView(payload, win);

  const probe = document.getElementById('bars-macd');
  const chartW = probe
    ? Math.max(80, (probe.clientWidth || probe.width) - BARS_PAD.l - BARS_PAD.r)
    : 600;
  const ticks = barsTimeTicks(view.rows, chartW, 56);

  drawBarsPricePanel(view, ticks);
  drawBarsVolumePanel(view, ticks);
  drawBarsMacdPanel(view, ticks);
  barsReadout(view);

  const sub = document.getElementById('bars-sub');
  if (!sub) return;
  const meta = payload.meta || {};
  if (!meta.bar_count) { sub.textContent = '봉 수집 대기'; return; }
  const unconfirmed = meta.bar_count - (meta.confirmed_count || 0);
  const gapList = meta.gaps || [];
  const missing = gapList.reduce((sum, g) => sum + (g.missing || 0), 0);
  sub.textContent =
    (payload.ticker || '-') + ' · ' + meta.bar_count + '봉 중 ' + win.count + '봉 표시' +
    ' (미확정 ' + unconfirmed + ')' +
    ' · 출처 ' + (meta.source || '-') +
    (gapList.length ? ' · 갭 ' + gapList.length + '회(' + missing + '분)' : '');
}

function barsRedraw() {
  if (barsPayload) drawBarsChart(barsPayload);
}

// 화면 구간 인덱스 → 전체 배열 인덱스
function barsAbsoluteIndex(id, clientX, canvas) {
  const total = ((barsPayload && barsPayload.bars) || []).length;
  const win = barsWindow(total, barsView);
  if (!win.count) return 0;
  const rect = canvas.getBoundingClientRect();
  const pad = barsPadFor(id);
  const chartW = rect.width - pad.l - pad.r;
  return win.start + barsIndexAtX(clientX - rect.left, chartW, pad.l, win.count);
}

function barsBindHover() {
  for (const id of BARS_PANELS) {
    const canvas = document.getElementById(id);
    if (!canvas) continue;

    canvas.addEventListener('mousemove', event => {
      const total = ((barsPayload && barsPayload.bars) || []).length;
      if (!total) return;
      const win = barsWindow(total, barsView);
      if (barsDrag) {
        // 드래그 스크롤. 이동한 픽셀을 봉 개수로 환산해 창을 민다.
        const rect = canvas.getBoundingClientRect();
        const pad = barsPadFor(id);
        const slot = (rect.width - pad.l - pad.r) / Math.max(1, win.count);
        const moved = Math.round((barsDrag.x - event.clientX) / (slot || 1));
        const end = Math.max(win.count, Math.min(barsDrag.end + moved, total));
        barsView = {count: win.count, end: end >= total ? null : end};
      }
      const absolute = barsAbsoluteIndex(id, event.clientX, canvas);
      const rect = canvas.getBoundingClientRect();
      const fresh = barsWindow(total, barsView);
      barsHover = {
        panel: id,
        index: Math.max(0, Math.min(absolute - fresh.start, fresh.count - 1)),
        y: event.clientY - rect.top,
      };
      barsRedraw();
    });

    canvas.addEventListener('mouseleave', () => {
      barsHover = null;
      barsDrag = null;
      barsRedraw();
    });

    canvas.addEventListener('mousedown', event => {
      const total = ((barsPayload && barsPayload.bars) || []).length;
      const win = barsWindow(total, barsView);
      barsDrag = {x: event.clientX, end: win.end};
      event.preventDefault();
    });

    canvas.addEventListener('mouseup', () => { barsDrag = null; });

    canvas.addEventListener('wheel', event => {
      const total = ((barsPayload && barsPayload.bars) || []).length;
      if (!total) return;
      event.preventDefault();
      const anchor = barsAbsoluteIndex(id, event.clientX, canvas);
      barsView = barsZoomView(barsView, total, event.deltaY > 0 ? 1.2 : 1 / 1.2, anchor);
      barsRedraw();
    }, {passive: false});

    // 더블클릭이면 기본 창으로, 최신 끝에 다시 붙는다.
    canvas.addEventListener('dblclick', () => {
      barsView = {count: BARS_DEFAULT_VIEW, end: null};
      barsRedraw();
    });
  }
  window.addEventListener('mouseup', () => { barsDrag = null; });
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
