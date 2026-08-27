// 봉·지표 차트 순수 로직 검증 — bars_chart.js에서 함수를 추출해 실제 실행한다.
// 실행: node tests\js\bars_chart_checks.js
const fs = require('fs');
const path = require('path');
const src = fs.readFileSync(
  path.join(__dirname, '..', '..', 'docs', 'html', 'assets', 'bars_chart.js'), 'utf8');

function extract(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`${name} not found`);
  let depth = 0;
  const open = src.indexOf('{', start);
  for (let j = open; j < src.length; j++) {
    if (src[j] === '{') depth++;
    else if (src[j] === '}') { depth--; if (depth === 0) return src.slice(start, j + 1); }
  }
  throw new Error(`${name} unbalanced`);
}

const BARS_MIN_CANDLE_PX = 1;
const BARS_DOMAIN_PAD = 0.02;

eval(extract('barsPriceDomain'));
eval(extract('barsMacdDomain'));
eval(extract('barsTimeIndex'));
eval(extract('barsCandleWidth'));
eval(extract('barsYAt'));

let failures = 0;
const check = (label, cond) => {
  if (!cond) { console.error(`FAIL ${label}`); failures++; }
  else console.log(`ok   ${label}`);
};
const near = (a, b, eps = 1e-6) => Math.abs(a - b) < eps;

const BARS = [
  {high: 110, low: 90, open: 95, close: 105},
  {high: 130, low: 100, open: 105, close: 125},
  {high: 120, low: 80, open: 125, close: 85},
];

// 가격 도메인은 고가·저가를 모두 담고 이동평균선도 담는다
{
  const d = barsPriceDomain(BARS, [null, null, 150]);
  check('price domain covers every low', d.min <= 80);
  check('price domain covers the sma above every high', d.max >= 150);
}

// 이동평균이 전부 null이어도 도메인이 성립한다
{
  const d = barsPriceDomain(BARS, [null, null, null]);
  check('null sma does not poison the domain', d.min <= 80 && d.max >= 130);
}

// 봉이 없으면 도메인이 무너지지 않는다
{
  const d = barsPriceDomain([], []);
  check('empty bars give a finite domain', Number.isFinite(d.min) && Number.isFinite(d.max));
  check('empty domain is not inverted', d.max > d.min);
}

// 모든 값이 같아도 도메인이 0폭이 되지 않는다 (0으로 나누기 방지)
{
  const flat = [{high: 100, low: 100, open: 100, close: 100}];
  const d = barsPriceDomain(flat, [100]);
  check('flat bars get a padded domain', d.max > d.min);
}

// MACD 도메인은 0을 반드시 포함한다 — 0 기준선이 화면 밖으로 나가면 안 된다
{
  const d = barsMacdDomain([{macd: 5, signal: 4, hist: 1}, {macd: 8, signal: 6, hist: 2}]);
  check('macd domain includes zero', d.min <= 0 && d.max >= 8);
}
{
  const d = barsMacdDomain([{macd: -5, signal: -4, hist: -1}]);
  check('negative macd domain still includes zero', d.min <= -5 && d.max >= 0);
}
{
  const d = barsMacdDomain([{macd: null, signal: null, hist: null}]);
  check('all-null macd gives a finite domain', Number.isFinite(d.min) && Number.isFinite(d.max));
}

// x 좌표는 봉 중심이고 좌에서 우로 단조 증가한다
{
  const xs = [0, 1, 2].map(i => barsTimeIndex(BARS, i, 300, 40));
  check('x is monotonically increasing', xs[0] < xs[1] && xs[1] < xs[2]);
  check('first bar sits inside the chart area', xs[0] > 40);
  check('last bar stays inside the chart area', xs[2] < 340);
  check('empty bars do not divide by zero', Number.isFinite(barsTimeIndex([], 0, 300, 40)));
}

// 캔들 폭은 양수이고 봉이 많아질수록 좁아진다
{
  const wide = barsCandleWidth(10, 300);
  const narrow = barsCandleWidth(200, 300);
  check('candle width is positive', wide > 0 && narrow > 0);
  check('more bars means narrower candles', narrow < wide);
  check('candle width never collapses to zero', barsCandleWidth(5000, 300) >= 1);
  check('zero bars still give a positive candle width', Number.isFinite(barsCandleWidth(0, 300)) && barsCandleWidth(0, 300) >= 1);
}

// y 매핑은 뒤집혀 있다 — 큰 값이 위(작은 y)
{
  const domain = {min: 0, max: 100};
  check('max maps to the top', near(barsYAt(100, domain, 10, 200), 10));
  check('min maps to the bottom', near(barsYAt(0, domain, 10, 200), 210));
  check('mid maps to the middle', near(barsYAt(50, domain, 10, 200), 110));
}

// 0폭 도메인이 들어와도 NaN을 내지 않는다
{
  const y = barsYAt(5, {min: 5, max: 5}, 10, 200);
  check('zero-width domain does not produce NaN', Number.isFinite(y));
}

if (failures) { console.error(`\n${failures} check(s) failed`); process.exit(1); }
console.log('\nall bars_chart checks passed');
