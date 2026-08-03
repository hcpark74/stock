// 세션 스케줄/아크 순수 로직 + 서빙 기본값 회귀 검증 — 의존성 없이 실행한다.
// 실행: node tests\js\schedule_checks.js
const fs = require('fs');
const path = require('path');

const appPath = path.join(__dirname, '..', '..', 'docs', 'html', 'assets', 'app.js');
const htmlPath = path.join(__dirname, '..', '..', 'docs', 'html', 'index.html');
const src = fs.readFileSync(appPath, 'utf8');
const html = fs.readFileSync(htmlPath, 'utf8');

function extract(name) {
  const start = src.indexOf('function ' + name + '(');
  if (start < 0) throw new Error(name + ' not found');
  let depth = 0;
  const open = src.indexOf('{', start);
  for (let j = open; j < src.length; j++) {
    if (src[j] === '{') depth++;
    else if (src[j] === '}') { depth--; if (depth === 0) return src.slice(start, j + 1); }
  }
  throw new Error(name + ' unbalanced');
}

let failures = 0;
const check = (label, cond) => {
  console.log((cond ? 'PASS' : 'FAIL') + '  ' + label);
  if (!cond) failures++;
};

// 1) parseClockMin
eval(extract('parseClockMin'));
check('parseClockMin 15:15 -> 915', parseClockMin('15:15') === 915);
check('parseClockMin 15:14:50 -> 914', parseClockMin('15:14:50') === 914);
check('parseClockMin 08:40 -> 520', parseClockMin('08:40') === 520);
check('parseClockMin bad -> null', parseClockMin('abc') === null);
check('parseClockMin empty -> null', parseClockMin('') === null);
check('parseClockMin undefined -> null', parseClockMin(undefined) === null);

// 2) sessionArcProgress
eval(extract('sessionArcProgress'));
const START = 8 * 60 + 40, END = 15 * 60 + 15;
check('progress: before start -> 0', sessionArcProgress(START, END, START - 10) === 0);
check('progress: at start -> 0', sessionArcProgress(START, END, START) === 0);
check('progress: at end -> 1', sessionArcProgress(START, END, END) === 1);
check('progress: after end -> 1', sessionArcProgress(START, END, END + 100) === 1);
check('progress: midpoint ~0.5', (function () {
  const p = sessionArcProgress(START, END, (START + END) / 2);
  return p > 0.49 && p < 0.51;
})());
check('progress: end<=start guard -> 0 (no NaN)', sessionArcProgress(600, 600, 700) === 0);

// 3) sessionArcTicks
eval(extract('sessionArcTicks'));
const ticks = sessionArcTicks(START, END);
check('ticks: length 3', Array.isArray(ticks) && ticks.length === 3);
check('ticks: HH:MM format', ticks.every(t => /^\d{2}:\d{2}$/.test(t)));
const tmin = ticks.map(parseClockMin);
check('ticks: ascending', tmin.every((v, i) => i === 0 || v > tmin[i - 1]));
check('ticks: inside span', tmin.every(v => v > START && v < END));
check('ticks: not the old fixed 09:00/09:20/09:40',
  !(ticks[0] === '09:00' && ticks[1] === '09:20' && ticks[2] === '09:40'));

// 4) index.html served-default regression
const arcRow = html.slice(html.indexOf('arc-strip'), html.indexOf('state-strip'));
check('index: arc row has no 11:00', !arcRow.includes('11:00'));
check('index: arc row has 15:15', arcRow.includes('15:15'));
check('index: id="arc-end" present', html.includes('id="arc-end"'));
check('index: id="btm-end" present', html.includes('id="btm-end"'));
check('index: btm-time end 15:15', (function () {
  const btm = html.slice(html.indexOf('id="btm-time"'), html.indexOf('id="btm-time"') + 400);
  return btm.includes('15:15') && !btm.includes('11:00');
})());

// 5) F5 stage wording regression
check('index: no "F5 timeout" wording', !html.includes('F5 타임아웃'));
check('index: has "F5 close" wording', html.includes('F5 마감 청산'));
check('app.js: no "F5 timeout" stage wording', !src.includes('F5 타임아웃'));
check('app.js: has "F5 close" wording', src.includes('F5 마감 청산'));
check('app.js: TIMEOUT reason enum preserved', src.includes("TIMEOUT:'시간 청산'"));
check('app.js: TIMEOUT_CLOSE event label preserved', src.includes('TIMEOUT_CLOSE:'));
check('index: history TIMEOUT filter preserved', html.includes('data-h-value="TIMEOUT"'));

// 6) app.js cache-buster bumped
const scriptTag = html.slice(html.indexOf('app.js?v='), html.indexOf('app.js?v=') + 60);
check('index: app.js cache-buster is not old version',
  html.includes('app.js?v=') && !scriptTag.includes('20260717-market-closed'));

// 7) 매도 후 추적 종료 제어
check('index: post-close tracking stop button present', html.includes('id="tracking-stop-btn"'));
check('app.js: post-close tracking stop handler present', src.includes('function stopPostCloseTracking()'));
check('app.js: tracking stop uses POST endpoint',
  src.includes("fetch('/api/tracking/stop', {method:'POST'})"));
check('app.js: tracking stop button is CLOSED-only',
  src.includes("const closed = d.position_status === 'CLOSED'"));

// 8) 가격흐름 세로 스케일 토글
check('index: 전체 스케일 버튼 present', html.includes('id="scale-full"'));
check('index: 트레일링 스케일 버튼 present', html.includes('id="scale-trailing"'));
check('index: 스케일 data 속성 present', html.includes('data-scale="trailing"'));
check('index: 스케일 버튼 aria-pressed present', html.includes('aria-pressed'));
check('app.js: priceFlowYDomain 헬퍼 present', src.includes('function priceFlowYDomain('));
check('app.js: setPriceFlowScale 핸들러 present', src.includes('function setPriceFlowScale('));
check('app.js: 스케일 localStorage 키 present', src.includes('PRICE_FLOW_SCALE_KEY'));
check('app.js: guarded localStorage getter present', src.includes('function lsGet('));
check('app.js: guarded localStorage setter present', src.includes('function lsSet('));
check('app.js: 플롯 클리핑 present', src.includes('ctx.clip()'));
check('index: app.js cache-buster bumped off post-close-stop', (function () {
  const tag = html.slice(html.indexOf('app.js?v='), html.indexOf('app.js?v=') + 60);
  return !tag.includes('20260803-post-close-stop');
})());

console.log(failures === 0 ? '\nALL CHECKS PASSED' : '\n' + failures + ' CHECK(S) FAILED');
process.exit(failures ? 1 : 0);
