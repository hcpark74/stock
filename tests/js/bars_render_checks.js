// 봉 차트 드로잉 검증 — 순수 함수가 아니라 캔버스에 그리는 경로를 실제로 돌린다.
// bars_chart_checks.js가 계산을 보고, 이 파일이 그리기를 본다.
// 실행: node tests\js\bars_render_checks.js
//
// 캔버스를 기록용 스텁으로 바꿔 끼우고 실제 페이로드(2026-08-27 006340 앞
// 40봉)로 세 패널을 그린다. 확인하는 것:
//   - 예외 없이 끝나고 좌표에 NaN이 하나도 없다
//   - 세 패널이 모두 실제로 그려진다 (조용히 비는 패널이 없다)
//   - globalAlpha와 setLineDash가 원상 복구된다 (미확정 봉·십자선의 뒷정리)
//   - DPR 1.5에서 여러 번 그려도 캔버스 높이가 자라지 않는다 (높이 폭주 회귀)
//   - 십자선이 짚은 봉과 판독 줄의 값이 일치한다
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ASSET = path.join(__dirname, '..', '..', 'docs', 'html', 'assets', 'bars_chart.js');
const FIXTURE = path.join(__dirname, 'fixtures', 'bars_payload.json');
const src = fs.readFileSync(ASSET, 'utf8');
const payload = JSON.parse(fs.readFileSync(FIXTURE, 'utf8'));

let bad = [];
let ops = [];
const drawn = new Set();

function makeCtx(id) {
  const state = {alpha: 1, dash: [], saved: null};
  const guard = (fn, args) => {
    for (const a of args) {
      if (typeof a === 'number' && !Number.isFinite(a)) bad.push(`${id}.${fn}(${args})`);
    }
  };
  const rec = name => (...args) => {
    guard(name, args);
    drawn.add(`${id}.${name}`);
    ops.push({panel: id, op: name, args: args, stroke: ctx.strokeStyle, fill: ctx.fillStyle});
  };
  const ctx = {
    save() { state.saved = {alpha: state.alpha, dash: state.dash}; },
    restore() { if (state.saved) { state.alpha = state.saved.alpha; state.dash = state.saved.dash; } },
    setTransform: rec('setTransform'),
    clearRect: rec('clearRect'),
    beginPath: rec('beginPath'),
    moveTo: rec('moveTo'),
    lineTo: rec('lineTo'),
    stroke: rec('stroke'),
    fillRect: rec('fillRect'),
    fillText(text, x, y) {
      guard('fillText', [x, y]);
      drawn.add(`${id}.fillText`);
      ops.push({panel: id, op: 'fillText', args: [text, x, y], fill: ctx.fillStyle});
    },
    setLineDash(dash) { state.dash = dash || []; },
    // 범례가 라벨 폭을 재서 다음 항목을 배치한다. 폭이 유한하기만 하면 된다.
    measureText(text) { return {width: String(text).length * 6}; },
    _state: state,
  };
  Object.defineProperty(ctx, 'globalAlpha', {
    get: () => state.alpha,
    set: v => { state.alpha = v; },
  });
  return ctx;
}

const SIZES = {
  'bars-price': {w: 1430, h: 240},
  'bars-volume': {w: 1430, h: 64},
  'bars-macd': {w: 1430, h: 120},
};
const els = {};
for (const [id, size] of Object.entries(SIZES)) {
  const ctx = makeCtx(id);
  // clientHeight를 height 속성에 연동한다 — CSS 높이가 없을 때의 브라우저
  // 동작이고, 높이 폭주 회귀 검사를 헛돌지 않게 만드는 조건이다. 가로는
  // CSS width:100%가 잡아 주므로 레이아웃 폭으로 고정한다.
  els[id] = {
    id, dataset: {}, width: 760, height: size.h,
    clientWidth: size.w,
    get clientHeight() { return this.height; },
    getContext: () => ctx, _ctx: ctx,
    addEventListener() {},
    getBoundingClientRect: () => ({left: 0, top: 0, width: size.w, height: size.h}),
  };
}
let subText = '';
let readout = '';
els['bars-sub'] = {set textContent(v) { subText = v; }, get textContent() { return subText; }};
els['bars-readout'] = {
  set textContent(v) { readout = v; }, get textContent() { return readout; },
  set innerHTML(v) { readout = v; }, get innerHTML() { return readout; },
};

// DPR 1.5 — 높이 폭주 버그가 나던 배율(윈도우 150% 확대)에서 검증한다.
global.window = {devicePixelRatio: 1.5, addEventListener() {}};
global.document = {getElementById: id => els[id] || null, addEventListener() {}};
global.setInterval = () => 0;
global.fetch = () => Promise.reject(new Error('render check makes no network calls'));

vm.runInThisContext(src, {filename: ASSET});

let failures = 0;
const check = (label, cond) => {
  if (!cond) { console.error(`FAIL ${label}`); failures++; }
  else console.log(`ok   ${label}`);
};

const rows = payload.bars;
check('fixture carries bars', rows.length === 60);
check('fixture carries a defined sma tail', Number.isFinite(payload.indicators.sma[59]));
check('fixture carries a defined macd tail', Number.isFinite(payload.indicators.macd[59].macd));

drawBarsChart(payload);
check('no non-finite coordinate on the first draw', bad.length === 0);
if (bad.length) console.error('  ', bad.slice(0, 5));

for (const id of Object.keys(SIZES)) {
  check(`${id} drew its bars`, drawn.has(`${id}.fillRect`));
  check(`${id} drew its grid`, drawn.has(`${id}.stroke`));
  check(`${id} left globalAlpha at 1`, els[id]._ctx._state.alpha === 1);
  check(`${id} left no line dash set`, els[id]._ctx._state.dash.length === 0);
}
check('time labels go on the bottom panel', drawn.has('bars-macd.fillText'));
check('the summary line reports the bar count', subText.includes(`${rows.length}봉 중`));
check('the summary line reports how many are on screen', /\d+봉 표시/.test(subText));
check('nothing is read out until the mouse is over the chart', readout === '');

// 높이 폭주 회귀: 같은 페이로드를 세 번 그려도 백킹 스토어가 자라면 안 된다.
const height = els['bars-price'].height;
drawBarsChart(payload);
drawBarsChart(payload);
check('canvas height holds across redraws', els['bars-price'].height === height);
check('backing height is the display height times DPR', height === Math.round(240 * 1.5));

// 십자선 — x를 되돌려 짚은 봉이 판독 줄과 같은 봉인지 본다.
const target = 27;
const pad = barsPadFor('bars-price');
const chartW = SIZES['bars-price'].w - pad.l - pad.r;
const x = barsTimeIndex(rows, target, chartW, pad.l);
barsHover = {panel: 'bars-price', index: barsIndexAtX(x, chartW, pad.l, rows.length), y: 120};
bad = [];
drawBarsChart(payload);
check('the crosshair lands on the bar its x maps to', barsHover.index === target);
check('no non-finite coordinate with the crosshair up', bad.length === 0);

const bar = rows[target];
const hhmm = `${bar.time.slice(0, 2)}:${bar.time.slice(2, 4)}`;
check('the readout names the hovered minute', readout.includes(hhmm));
check('the readout carries that bar high and close',
  readout.includes(Math.round(bar.high).toLocaleString()) &&
  readout.includes(Math.round(bar.close).toLocaleString()));
check('the readout says so when the bar is unconfirmed',
  bar.confirmed ? !readout.includes('미확정') : readout.includes('미확정'));

// 빈 페이로드 — 장 시작 전 화면에서 매 30초마다 지나는 경로다.
bad = [];
barsHover = null;
drawBarsChart({bars: [], indicators: {sma: [], macd: []}, meta: {bar_count: 0}});
check('an empty payload neither throws nor emits NaN', bad.length === 0);
check('an empty payload shows the waiting message', subText === '봉 수집 대기');

// 갭 표시 — 봉이 빠진 자리에 경계선이 서고 요약 줄이 그것을 말한다.
{
  const gapped = {
    bars: rows.slice(0, 3).concat(rows.slice(6)),
    indicators: {
      sma: payload.indicators.sma.slice(0, 3).concat(payload.indicators.sma.slice(6)),
      macd: payload.indicators.macd.slice(0, 3).concat(payload.indicators.macd.slice(6)),
    },
    ticker: payload.ticker,
    meta: Object.assign({}, payload.meta, {
      bar_count: 37,
      gaps: [{after: rows[2].time, resume: rows[6].time, index: 3, missing: 3, jump_pct: -2.66}],
    }),
  };
  bad = [];
  barsHover = null;
  const before = drawn.size;
  drawBarsChart(gapped);
  check('a gapped series draws without a non-finite coordinate', bad.length === 0);
  check('the summary line reports the gap', subText.includes('갭 1회(3분)'));
  check('the price panel labels the missing minutes', drawn.has('bars-price.fillText'));
  check('drawing still happened', drawn.size >= before);
  check('the gap mark leaves no line dash set',
    els['bars-price']._ctx._state.dash.length === 0);

  // 갭이 없으면 요약 줄에 갭 문구가 없다
  drawBarsChart(payload);
  check('a continuous series says nothing about gaps', !subText.includes('갭'));
}

// 증권사 차트 관례 — 오른쪽 가격축, 이동평균 3종, 현재가 태그, 거래량 이동평균
{
  bad = [];
  ops = [];
  barsHover = null;
  barsView = {count: 120, end: null};
  drawBarsChart(payload);
  check('the redesigned chart draws without a non-finite coordinate', bad.length === 0);
  if (bad.length) console.error('  ', bad.slice(0, 5));

  const priceOps = ops.filter(o => o.panel === 'bars-price');
  const padR = BARS_PAD.r;
  const W = SIZES['bars-price'].w;

  // 가격축이 오른쪽이다 — 축 라벨의 x가 눈금자 안에 있어야 한다.
  const axisText = priceOps.filter(o => o.op === 'fillText' && o.args[1] >= W - padR);
  check('price labels are drawn on the right-hand ruler', axisText.length >= 4);
  check('nothing is labelled in a left-hand gutter any more',
    priceOps.filter(o => o.op === 'fillText' && o.args[1] < BARS_PAD.l).length === 0);

  // 이동평균 3종이 각자의 색으로 그려진다.
  // 기대값을 먼저 못 박는다 — BARS_MA를 그냥 순회하면 상수에서 항목을 지웠을 때
  // 검사도 같이 줄어들어 아무것도 못 잡는다.
  check('three moving averages are configured',
    BARS_MA.map(m => m.period).join(',') === '5,20,60');
  for (const ma of BARS_MA) {
    check(`MA${ma.period} is drawn`,
      priceOps.some(o => o.op === 'stroke' && o.stroke === ma.color));
  }
  check('the legend names every moving average',
    BARS_MA.every(ma => priceOps.some(
      o => o.op === 'fillText' && o.args[0] === 'MA' + ma.period && o.fill === ma.color)));

  // 현재가 태그가 눈금자 위에 덮인다.
  const last = rows[rows.length - 1];
  const badge = priceOps.filter(
    o => o.op === 'fillText' && o.args[0] === Math.round(last.close).toLocaleString());
  check('the last close is tagged on the price ruler',
    badge.length >= 1 && badge.some(o => o.args[1] >= W - padR));

  // 거래량 이동평균
  const volOps = ops.filter(o => o.panel === 'bars-volume');
  check('two volume moving averages are configured',
    BARS_VOL_MA.map(m => m.period).join(',') === '5,20');
  for (const ma of BARS_VOL_MA) {
    check(`volume MA${ma.period} is drawn`,
      volOps.some(o => o.op === 'stroke' && o.stroke === ma.color));
  }
}

// 표시 창 — 창을 좁히면 그만큼만 그린다
{
  const total = rows.length;
  barsView = {count: 20, end: null};
  ops = [];
  bad = [];
  drawBarsChart(payload);
  const candles = ops.filter(o => o.panel === 'bars-price' && o.op === 'fillRect');
  // 캔들 몸통 20개 + 현재가 배지 1개
  check('a narrowed window draws only the visible candles',
    candles.length === 21);
  check('a narrowed window still draws cleanly', bad.length === 0);
  check('the summary says how many of the total are shown',
    subText.includes(`${total}봉 중 20봉 표시`));

  // 창을 왼쪽으로 밀면 다른 봉이 보인다
  barsView = {count: 20, end: 20};
  ops = [];
  drawBarsChart(payload);
  const labels = ops
    .filter(o => o.panel === 'bars-macd' && o.op === 'fillText')
    .map(o => o.args[0]);
  check('panning left shows earlier minutes',
    labels.some(l => /^09:0/.test(String(l))));
  barsView = {count: BARS_DEFAULT_VIEW, end: null};
}

// 십자선이 맨 아래 패널에 시간 배지를 붙인다
{
  const win = barsWindow(rows.length, barsView);
  barsHover = {panel: 'bars-price', index: 10, y: 100};
  ops = [];
  bad = [];
  drawBarsChart(payload);
  const bar = rows[win.start + 10];
  const hhmm = `${bar.time.slice(0, 2)}:${bar.time.slice(2, 4)}`;
  check('the crosshair time badge sits on the bottom panel',
    ops.some(o => o.panel === 'bars-macd' && o.op === 'fillText' && o.args[0] === hhmm));
  check('the time badge is not repeated on the upper panels',
    !ops.some(o => o.panel === 'bars-price' && o.op === 'fillText' && o.args[0] === hhmm));
  check('the crosshair draws cleanly', bad.length === 0);
  barsHover = null;
}

if (failures) { console.error(`\n${failures} check(s) failed`); process.exit(1); }
console.log('\nall bars_render checks passed');
