'use strict';

// ── 상수 ────────────────────────────────────────────────────────────────
const TICKER_NAMES = {
  '005930':'삼성전자','000660':'SK하이닉스','035420':'NAVER',
  '005380':'현대차','000270':'기아','028260':'삼성물산',
  '035720':'카카오','003550':'LG',
};
const REASON_CLS = {TRAILING:'b-tr',HARD_STOP:'b-hs',TIMEOUT:'b-to',OPEN:'b-op'};
const REASON_LBL = {TRAILING:'TRAILING',HARD_STOP:'HARD_STOP',TIMEOUT:'TIMEOUT',OPEN:'진행 중'};

// ── 유틸 ─────────────────────────────────────────────────────────────────
const $  = id => document.getElementById(id);
const fmt = (n, dec=0) => n==null ? '—' : Number(n).toLocaleString('ko-KR',{minimumFractionDigits:dec,maximumFractionDigits:dec});
const fmtPct = n => n==null ? '—' : (n>=0?'+':'')+Number(n).toFixed(2)+'%';
const fmtM = n => n==null ? '—' : (n>=1e6 ? (n/1e6).toFixed(2)+'M' : fmt(n))+'원';
const fmtWon = n => n==null ? '—' : `${fmt(n)}<span class="u">원</span>`;
const cls = (el, c) => { el.className = el.className.replace(/\b(up|dn|flat)\b/g,''); el.classList.add(c); };
const esc = s => String(s ?? '').replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));

// ── 탭 전환 ──────────────────────────────────────────────────────────────
function go(id, btn) {
  document.querySelectorAll('.sc').forEach(s=>s.classList.remove('on'));
  document.querySelectorAll('.ttab').forEach(b=>b.classList.remove('on'));
  document.querySelectorAll('.menu-item').forEach(b=>b.classList.remove('on'));
  const screen = $('sc-'+id) || $('sc-today');
  screen.classList.add('on');
  if(btn) btn.classList.add('on');
  else {
    const menu = document.querySelector(`.menu-item[onclick*="'${id}'"]`);
    if(menu) menu.classList.add('on');
  }
  if (id==='selection') loadF1();
  if (id==='assets') {
    renderAssets(_lastStatus);
    loadAssets(false);
  }
  if (id==='orders') loadOrders();
  if (id==='history') loadHistory();
  if (id==='stats')   loadStats();
}

// ── 시계 ─────────────────────────────────────────────────────────────────
// ── Arc 게이지 ────────────────────────────────────────────────────────────
let _arcAnims = [];

(function tick(){
  setTimeout(tick, 1000);
  try {
    const now = new Date();
    const kst = new Date(now.getTime()+9*3600*1000);
    const p = n=>String(n).padStart(2,'0');
    $('clk').textContent = p(kst.getUTCHours())+':'+p(kst.getUTCMinutes())+':'+p(kst.getUTCSeconds())+' KST';
    $('btm-now').textContent = p(kst.getUTCHours())+':'+p(kst.getUTCMinutes());
    updateArc(kst);
  } catch(e) {}
})();

function updateArc(kst) {
  const h=kst.getUTCHours(), m=kst.getUTCMinutes(), s=kst.getUTCSeconds();
  const p=n=>String(n).padStart(2,'0');
  $('arc-now').textContent = p(h)+':'+p(m)+' 현재';

  const startMin=8*60+40, endMin=10*60, nowMin=h*60+m+s/60;
  let prog = Math.max(0, Math.min(1, (nowMin-startMin)/(endMin-startMin)));
  const elapsed = Math.max(0, Math.round(nowMin-startMin));
  $('arc-elapsed').textContent = elapsed+'분 경과';

  drawArc(prog);
}

function themeVal(dark, light) {
  return document.documentElement.getAttribute('data-theme')==='light' ? light : dark;
}

function drawArc(prog) {
  const c=$('arc'); if(!c) return;
  const ctx=c.getContext('2d'), cx=140, cy=5, r=130;
  ctx.clearRect(0,0,280,145);

  ctx.beginPath(); ctx.arc(cx,cy,r,Math.PI,0,true);
  ctx.strokeStyle=themeVal('#2a2e39','#c8cbd6'); ctx.lineWidth=10; ctx.lineCap='round'; ctx.stroke();

  if(prog>0){
    ctx.beginPath(); ctx.arc(cx,cy,r,Math.PI,Math.PI*(1-prog),true);
    ctx.strokeStyle='rgba(247,166,0,.08)'; ctx.lineWidth=22; ctx.lineCap='round'; ctx.stroke();

    ctx.beginPath(); ctx.arc(cx,cy,r,Math.PI,Math.PI*(1-prog),true);
    ctx.strokeStyle='#f7a600'; ctx.lineWidth=10; ctx.lineCap='round'; ctx.stroke();
  }

  ['09:00','09:20','09:40'].forEach((lbl,i)=>{
    const p=(i+1)/4, a=Math.PI*(1-p), ri=r-22;
    const x=cx+ri*Math.cos(a), y=cy+ri*Math.sin(a);
    ctx.fillStyle=themeVal('#363a45','#4f5260'); ctx.font='10px Noto Sans KR,sans-serif';
    ctx.textAlign='center'; ctx.textBaseline='middle';
    ctx.fillText(lbl,x,y);
  });

  // 이벤트 점들
  for(const dot of _arcAnims){
    const a=Math.PI*(1-dot.prog), x=cx+r*Math.cos(a), y=cy+r*Math.sin(a);
    ctx.beginPath(); ctx.arc(x,y,5,0,Math.PI*2);
    ctx.fillStyle=dot.color; ctx.fill();
    if(dot.label){
      ctx.fillStyle=dot.color; ctx.font='10px Noto Sans KR,sans-serif';
      ctx.textAlign='center'; ctx.textBaseline='top';
      ctx.fillText(dot.label,x,y+9);
    }
  }

  if(prog>0 && prog<1){
    const ca=Math.PI*(1-prog), cx2=cx+r*Math.cos(ca), cy2=cy+r*Math.sin(ca);
    ctx.beginPath(); ctx.arc(cx2,cy2,9,0,Math.PI*2);
    ctx.fillStyle='rgba(247,166,0,.15)'; ctx.fill();
    ctx.beginPath(); ctx.arc(cx2,cy2,4.5,0,Math.PI*2);
    ctx.fillStyle='#f7a600'; ctx.fill();
  }
}

// ── Status 업데이트 ───────────────────────────────────────────────────────
let _lastStatus = null;
let _lastAssets = null;
let _priceFlow = [];
let _priceFlowTicker = null;

function applyStatus(d) {
  if(d.assets) _lastAssets = d.assets;
  if(_lastAssets && !d.assets) d.assets = _lastAssets;
  _lastStatus = d;

  // 탑바 심볼
  $('tb-sym').textContent = d.ticker || '—';
  $('tb-sname').textContent = d.ticker ? (TICKER_NAMES[d.ticker]||d.ticker) : '대기중';

  // 모드 뱃지
  const isReal = d.mode!=='PAPER';
  $('tb-mode').textContent = d.mode||'PAPER';
  $('tb-mode').style.color = isReal ? 'var(--dn)' : 'var(--br)';

  // KIS 상태 (WS 연결이 되거나 mode가 있으면 OK로 간주)
  const kisOk = d.ws_connected || d.position_status!=='IDLE';
  setLiStatus('kis', kisOk?'ok':'off');

  // WS 상태
  setLiStatus('ws', d.ws_connected?'ok':'off');

  // NTP 상태
  const ntpCls = {OK:'ok',WARN:'warn',CRIT:'err',ERROR:'err'}[d.ntp_level]||'off';
  setLiStatus('ntp', ntpCls);
  const ntpTxt = d.ntp_offset_ms>0 ? `NTP +${Math.round(d.ntp_offset_ms)}ms` : 'NTP';
  $('si-ntp').querySelector('span:last-child').textContent = ntpTxt;
  $('ntp-lbl').textContent = ntpTxt;

  // 상태 스트립
  const badge = $('st-badge');
  badge.textContent = {IDLE:'대기중',ENTERING:'진입중',HOLDING:'보유중',CLOSED:'청산됨'}[d.position_status]||d.position_status;
  badge.className = 'st-badge '+(d.position_status||'IDLE');
  $('st-tk').textContent  = d.ticker||'';
  $('st-name').textContent = d.ticker ? (TICKER_NAMES[d.ticker]||'') : '';

  // PnL 필
  const pills = $('tv-pills');
  if(d.pnl_pct!=null){
    const up = d.pnl_pct>=0;
    pills.innerHTML = `<span class="tv-pnl ${up?'up':'dn'}">${fmtPct(d.pnl_pct)}</span>`;
  } else {
    pills.innerHTML = '';
  }

  // 포지션 그리드
  $('pv-entry').innerHTML = d.entry_price ? fmt(d.entry_price)+'<span class="u">원</span>' : '—';
  const curEl = $('pv-cur');
  curEl.innerHTML = d.current_price ? fmt(d.current_price)+'<span class="u">원</span>' : '—';
  curEl.className = 'pval'+(d.current_price&&d.entry_price ? (d.current_price>d.entry_price?' pup':' pdn') : '');
  const pnlEl = $('pv-pnl');
  pnlEl.textContent = fmtPct(d.pnl_pct);
  pnlEl.className = 'pval'+(d.pnl_pct==null?'':d.pnl_pct>=0?' pup':' pdn');
  $('pv-high').innerHTML = d.high_price ? fmt(d.high_price)+'<span class="u">원</span>' : '—';
  $('pv-qty').innerHTML  = d.remaining_qty!=null ? fmt(d.remaining_qty)+'<span class="u">주</span>' : '—';
  const amt = d.entry_price&&d.entry_qty ? d.entry_price*d.entry_qty : null;
  $('pv-amount').innerHTML = amt ? fmtM(amt) : '—';
  updatePriceFlow(d);

  // 플래그
  const trail = d.trailing_active;
  const hasStep = d.highest_step>0;
  updateFlag('fl-trail', trail, trail?'Step Trailing 활성':'Step Trailing', '#22d3ee');
  updateFlag('fl-step', hasStep, hasStep?`최고 스텝 +${(d.highest_step*100).toFixed(1)}%`:'최고 스텝', 'var(--br)');
  const stopPrice = d.trail_stop || d.hard_stop;
  const stopLbl = d.trailing_active&&d.trail_stop ? `Trail Stop ${fmt(d.trail_stop)}원` : (d.hard_stop?`Hard Stop ${fmt(d.hard_stop)}원`:'Hard Stop —');
  $('fl-stop-lbl').textContent = stopLbl;
  $('fl-stop').querySelector('.dot').className = 'dot'+(stopPrice?' off':'  off');

  // 바텀 파이프라인 상태 반영
  updatePipeline(d.position_status, d);
  renderAssetSummary(d);
  renderAssets(d);
}

function setLiStatus(id, cls) {
  const li = $('li-'+id);
  li.className = 'li '+cls;
  const dot = $('dot-'+id);
  dot.className = 'dot'+(cls==='ok'?'':cls==='warn'?' warn':cls==='err'?' err':' off');
}

function updateFlag(id, on, label, dotColor) {
  const el=$(id);
  el.className='flag'+(on?' on':'');
  const dot=el.querySelector('.dot');
  dot.style.background = on ? dotColor : 'var(--s2)';
  dot.className='dot'+(on?'':' off');
  el.querySelector('span:last-child') || (el.childNodes[1]&&(el.childNodes[1].textContent=label));
  el.lastChild.textContent=label;
}

function updatePipeline(status, pipeline) {
  const stages=['F1 스캔','F2 잠금','F3 진입','F4 Step Trailing','F5 타임아웃'];
  const activeIdx = Number.isInteger(pipeline?.pipeline_stage)
    ? pipeline.pipeline_stage
    : ({IDLE:0,ENTERING:2,HOLDING:3,CLOSED:4}[status]??0);
  const failed = pipeline?.pipeline_failed === true;
  const segs = stages.map((s,i)=>{
    const c = failed && i===activeIdx ? 'b-fail' : i<activeIdx ? 'b-done' : i===activeIdx ? 'b-active' : 'b-dim';
    return `<span class="${c}">${s}</span>`;
  });
  $('btm-pipeline').innerHTML = segs.join('<span class="b-arr">›</span>');
}

// ── F1 스캔 패널 ─────────────────────────────────────────────────────────
let _lastF1 = null;

const F1_STATUS_LABEL = {
  IDLE:'대기',
  RUNNING:'진행중',
  RETRYING:'재시도',
  DONE:'완료',
  NO_TARGET:'대상없음',
  FAILED:'오류',
};

function pctFromRatio(v) {
  return v==null ? null : Number(v) * 100;
}

function f1StepClass(status, idx) {
  if(status==='FAILED') return idx===0 ? 'fail' : '';
  if(status==='NO_TARGET') return idx < 3 ? 'done' : idx===3 ? 'fail' : '';
  if(status==='DONE') return 'done';
  if(status==='RUNNING' || status==='RETRYING') return idx < 2 ? 'done' : idx===2 ? 'active' : '';
  return idx===0 ? 'active' : '';
}

function renderF1(d) {
  _lastF1 = d;
  const status = d.status || 'IDLE';
  const state = $('f1-state');
  state.textContent = F1_STATUS_LABEL[status] || status;
  state.className = 'f1-state ' + status;

  const updated = d.updated_at ? d.updated_at.substring(11,19) : '—';
  const snapshot = d.snapshot_name ? `스냅샷 ${d.snapshot_name}` : '스냅샷 없음';
  const attempt = d.last_event?.attempt ? ` · ${d.last_event.attempt}회 시도` : '';
  $('f1-meta').textContent = `${snapshot} · ${updated}${attempt}`;

  const steps = [
    ['Ranking 조회', d.raw_count ?? 0],
    ['예상체결 보강', d.expected_valid ?? 0],
    ['Gap 3~7%', d.gap_pass ?? 0],
    ['유동성 정렬', d.liquidity_pass ?? 0],
    ['후보 확정', d.selected?.ticker || '—'],
  ];
  $('f1-steps').innerHTML = steps.map((s,i)=>`
    <div class="f1-step ${f1StepClass(status, i)}">
      <div class="f1-step-top"><span class="f1-dot"></span>${esc(s[0])}</div>
      <div class="f1-step-val">${esc(s[1])}</div>
    </div>
  `).join('');

  const selected = d.selected;
  $('f1-summary').innerHTML = `
    <div><div class="f1-k">최종 후보</div><div class="f1-v ${selected?'up':''}">${selected ? esc(`${selected.ticker} ${selected.name||''}`) : '—'}</div></div>
    <div><div class="f1-k">최종 갭</div><div class="f1-v ${selected?'up':''}">${selected ? fmtPct(pctFromRatio(selected.gap_pct)) : '—'}</div></div>
    <div><div class="f1-k">예상체결 대금</div><div class="f1-v">${selected ? (Number(selected.expected_amount||0)/1e8).toFixed(1)+'억' : '—'}</div></div>
    <div><div class="f1-k">구간 보정</div><div class="f1-v br">${esc(`CORE ${d.core_gap||0} · HIGH ${d.high_gap_allowed||0}`)}</div></div>
  `;

  const note = $('f1-today-note');
  if(note) {
    const count = d.candidates?.length || 0;
    note.textContent = count ? `후보 ${count}개 전체 목록은 선정 메뉴에서 확인` : '후보 전체 목록은 선정 메뉴에서 확인';
  }
  renderSelection(d);
}

function updatePriceFlow(d) {
  const ticker = d?.ticker || null;
  if(ticker !== _priceFlowTicker) {
    _priceFlowTicker = ticker;
    _priceFlow = [];
  }
  if(Array.isArray(d?.tick_history) && d.tick_history.length) {
    _priceFlow = d.tick_history
      .map(row => ({ts: Date.parse(row.ts) || Date.now(), price: Number(row.price || 0)}))
      .filter(row => row.price > 0)
      .slice(-120);
  }
  const price = Number(d?.current_price || 0);
  if(price > 0 && d?.position_status === 'HOLDING') {
    const last = _priceFlow[_priceFlow.length - 1];
    if(!last || last.price !== price) {
      _priceFlow.push({ts: Date.now(), price});
      if(_priceFlow.length > 120) _priceFlow.shift();
    }
  }
  drawPriceFlow(d);
}

function drawPriceFlow(d) {
  const c = $('price-flow');
  if(!c) return;
  const ctx = c.getContext('2d');
  const W = c.width, H = c.height;
  ctx.clearRect(0, 0, W, H);
  const pad = {l:46,r:14,t:14,b:24};
  const chartW = W - pad.l - pad.r;
  const chartH = H - pad.t - pad.b;
  const points = _priceFlow;
  const refs = [d?.entry_price, d?.high_price, d?.trail_stop, d?.hard_stop, d?.current_price]
    .map(Number).filter(v => v > 0);
  const values = points.map(p => p.price).concat(refs);
  const sub = $('flow-sub');
  if(!values.length || d?.position_status !== 'HOLDING') {
    const emptyText = d?.position_status === 'CLOSED' ? '포지션 청산됨' : '보유 포지션 없음';
    if(sub) sub.textContent = emptyText;
    ctx.fillStyle = themeVal('#787b86', '#4f5260');
    ctx.font = '12px Noto Sans KR,sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText(emptyText, W / 2, H / 2);
    return;
  }
  if(sub) sub.textContent = `${points.length} ticks · 현재 ${fmt(d.current_price)}원`;
  let min = Math.min(...values), max = Math.max(...values);
  if(min === max) { min *= .998; max *= 1.002; }
  const span = max - min;
  min -= span * .12; max += span * .12;
  const xAt = i => pad.l + (points.length <= 1 ? chartW : chartW * i / (points.length - 1));
  const yAt = v => pad.t + (max - v) / (max - min) * chartH;

  ctx.strokeStyle = themeVal('rgba(120,123,134,.18)', 'rgba(79,82,96,.18)');
  ctx.lineWidth = 1;
  for(let i=0;i<4;i++){
    const y = pad.t + chartH * i / 3;
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(W - pad.r, y); ctx.stroke();
  }

  const drawRef = (value, color, label) => {
    if(!value) return;
    const y = yAt(Number(value));
    ctx.strokeStyle = color; ctx.setLineDash([4,4]); ctx.beginPath();
    ctx.moveTo(pad.l, y); ctx.lineTo(W - pad.r, y); ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = color; ctx.font = '10px Noto Sans KR,sans-serif'; ctx.textAlign = 'left';
    ctx.fillText(label, pad.l + 4, y - 4);
  };
  drawRef(d.entry_price, '#f7a600', '진입');
  drawRef(d.trail_stop || d.hard_stop, '#ef5350', d.trail_stop ? 'Trail Stop' : 'Hard Stop');
  drawRef(d.high_price, '#7b9ef9', '최고');

  if(points.length > 1) {
    ctx.strokeStyle = Number(d.current_price) >= Number(d.entry_price || d.current_price) ? '#26a69a' : '#ef5350';
    ctx.lineWidth = 2;
    ctx.beginPath();
    points.forEach((p,i) => {
      const x = xAt(i), y = yAt(p.price);
      if(i === 0) ctx.moveTo(x,y); else ctx.lineTo(x,y);
    });
    ctx.stroke();
  }
  const last = points[points.length - 1];
  if(last) {
    ctx.fillStyle = Number(last.price) >= Number(d.entry_price || last.price) ? '#26a69a' : '#ef5350';
    ctx.beginPath(); ctx.arc(xAt(points.length - 1), yAt(last.price), 4, 0, Math.PI * 2); ctx.fill();
  }
  ctx.fillStyle = themeVal('#787b86', '#4f5260');
  ctx.font = '10px Noto Sans KR,sans-serif';
  ctx.textAlign = 'right';
  ctx.fillText(fmt(max), pad.l - 6, pad.t + 4);
  ctx.fillText(fmt(min), pad.l - 6, pad.t + chartH);
}

function renderSelection(d) {
  if(!$('sel-tbody')) return;
  const status = d?.status || 'IDLE';
  $('sel-status').textContent = F1_STATUS_LABEL[status] || status;
  $('sel-status').className = 'sc2-val ' + (status === 'DONE' ? 'pup' : status === 'NO_TARGET' || status === 'FAILED' ? 'pdn' : '');
  $('sel-raw').textContent = d?.raw_count ?? 0;
  $('sel-expected').textContent = d?.expected_valid ?? 0;
  $('sel-selected').textContent = d?.selected?.ticker || '—';

  const rows = d?.candidates || [];
  const processRows = d?.selection_process || [];
  if(!rows.length) {
    $('sel-tbody').innerHTML = '<tr><td colspan="10" class="empty">F1 후보 스냅샷 없음</td></tr>';
    return;
  }
  const stepByKey = Object.fromEntries(processRows.map(step => [step.key, step]));
  const picked = key => new Set((stepByKey[key]?.tickers || []).map(String));
  const selectedTickers = picked('f1');
  const lockedTickers = picked('f2');
  const finalTickers = picked('f3');
  const badgeHtml = ticker => {
    const key = String(ticker || '');
    const badges = [];
    if(selectedTickers.has(key)) badges.push('<span class="badge b-op">선정</span>');
    if(lockedTickers.has(key)) badges.push('<span class="badge b-tr">잠금</span>');
    if(finalTickers.has(key)) badges.push('<span class="badge b-hs">최종</span>');
    return badges.length ? `<div class="sel-badges">${badges.join('')}</div>` : '—';
  };
  const candidateHtml = rows.map(c=>{
    const isFinal = finalTickers.has(String(c.ticker || ''));
    const verdict = c.verdict || '';
    const pass = c.gap_allowed === true || verdict === '통과' || verdict === '고갭통과';
    const band = c.gap_band || ((c.gap_source || '').startsWith('expected') ? '예상체결' : '등락률');
    const amount = c.expected_amount || c.avg_amount_5d || null;
    const avgAmount = c.avg_amount_5d || null;
    return `<tr class="${isFinal ? 'sel-final-row' : ''}">
      <td class="sel-state-cell">${badgeHtml(c.ticker)}</td>
      <td>${esc(c.ticker)}</td>
      <td>${esc(c.name || TICKER_NAMES[c.ticker] || '')}</td>
      <td class="${pass?'pup':''}">${fmtPct(pctFromRatio(c.gap_pct))}</td>
      <td>${esc(band)}</td>
      <td>${fmtPct(pctFromRatio(c.ranking_gap_pct))}</td>
      <td>${c.expected_api_gap_pct==null ? '—' : fmtPct(pctFromRatio(c.expected_api_gap_pct))}</td>
      <td>${amount ? (Number(amount)/1e8).toFixed(1)+'억' : '—'}</td>
      <td>${avgAmount ? (Number(avgAmount)/1e8).toFixed(1)+'억' : '—'}</td>
      <td><span class="badge ${pass?'b-tr':'b-to'}">${esc(verdict || (pass?'통과':'제외'))}</span></td>
    </tr>`;
  }).join('');
  $('sel-tbody').innerHTML = candidateHtml;
}

function positionAssetValues(d) {
  if(!d) return {stockValue:null, pnlAmount:null, total:null, cash:null, buyable:null, holdings:0};
  const assets = d.assets || _lastAssets || {};
  const qty = Number(d.remaining_qty || 0);
  const cur = Number(d.current_price || 0);
  const entry = Number(d.entry_price || 0);
  const stockValue = assets.stock_value != null ? Number(assets.stock_value) : (qty > 0 && cur > 0 ? qty * cur : null);
  const pnlAmount = assets.pnl_amount != null ? Number(assets.pnl_amount) : (qty > 0 && cur > 0 && entry > 0 ? (cur - entry) * qty : null);
  return {
    stockValue,
    pnlAmount,
    total: assets.total_asset != null ? Number(assets.total_asset) : stockValue,
    cash: assets.cash != null ? Number(assets.cash) : null,
    buyable: assets.buyable_cash != null ? Number(assets.buyable_cash) : null,
    buyableSource: assets.buyable_cash_source || null,
    holdings: assets.holdings_count != null ? Number(assets.holdings_count) : (qty > 0 && d.ticker ? 1 : 0),
  };
}

function buyableSourceLabel(source) {
  if(source === 'ord_psbl_cash') return '주문가능 현금';
  if(source === 'dnca_tot_amt') return '예수금 기준';
  return '출처 대기';
}

function assetSnapshotLabel(assets, freshLabel='자산 메뉴에서 상세') {
  if(!assets) return freshLabel;
  if(assets.snapshot_source === 'DB') {
    const t = shortTime(assets.captured_at);
    return t === '—' ? '마지막 저장 스냅샷' : `마지막 저장 ${t}`;
  }
  if(assets.snapshot_source === 'KIS') {
    const t = shortTime(assets.captured_at);
    return t === '—' ? freshLabel : `KIS 조회 ${t}`;
  }
  return freshLabel;
}

function renderAssetSummary(d) {
  const v = positionAssetValues(d);
  const assets = d?.assets || _lastAssets || null;
  $('as-total').innerHTML = v.total == null ? '—' : fmtWon(v.total);
  $('as-cash').innerHTML = v.cash == null ? '—' : fmtWon(v.cash);
  $('as-holdings').textContent = `${v.holdings}종목`;
  const pnl = $('as-pnl');
  pnl.innerHTML = v.pnlAmount == null ? '—' : fmtWon(v.pnlAmount);
  pnl.className = 'asset-v ' + (v.pnlAmount == null ? '' : v.pnlAmount >= 0 ? 'up' : 'dn');
  $('as-buyable').innerHTML = v.buyable == null ? '—' : fmtWon(v.buyable);
  if($('as-source')) $('as-source').textContent = assetSnapshotLabel(assets);
  $('order-buyable').textContent = v.buyable == null ? '—' : fmtM(v.buyable);
}

function renderAssets(d) {
  if(!$('asset-tbody')) return;
  const v = positionAssetValues(d);
  $('asset-total').textContent = v.total == null ? '—' : fmt(v.total);
  $('asset-cash').textContent = v.cash == null ? '—' : fmt(v.cash);
  $('asset-buyable').textContent = v.buyable == null ? '—' : fmt(v.buyable);
  const assets = d?.assets || _lastAssets || null;
  $('asset-buyable-source').textContent = `${buyableSourceLabel(v.buyableSource)} · ${assetSnapshotLabel(assets, 'KIS 현재 조회')}`;
  $('asset-stock').textContent = v.stockValue == null ? '—' : fmtM(v.stockValue);
  $('asset-pnl').textContent = v.pnlAmount == null ? '—' : fmt(v.pnlAmount);
  $('asset-pnl').className = 'sc2-val ' + (v.pnlAmount == null ? '' : v.pnlAmount >= 0 ? 'pup' : 'pdn');

  const rows = [];
  rows.push(`<tr><td>예수금</td><td>${v.cash == null ? '—' : fmt(v.cash)}</td><td>—</td><td><span class="badge ${v.cash == null ? 'b-to' : 'b-op'}">${v.cash == null ? 'API 대기' : '연동'}</span></td></tr>`);
  rows.push(`<tr><td>주문가능금액</td><td>${v.buyable == null ? '—' : fmt(v.buyable)}</td><td>${esc(buyableSourceLabel(v.buyableSource))}</td><td><span class="badge ${v.buyable == null ? 'b-to' : 'b-op'}">${v.buyable == null ? 'API 대기' : '주문가능'}</span></td></tr>`);
  if(v.holdings && d?.ticker) {
    rows.push(`<tr><td>${esc(d.ticker)} ${esc(TICKER_NAMES[d.ticker] || '')}</td><td>${fmt(v.stockValue)}</td><td>—</td><td><span class="badge b-tr">보유중</span></td></tr>`);
    rows.push(`<tr><td>평가손익</td><td class="${v.pnlAmount >= 0 ? 'pup' : 'pdn'}">${fmt(v.pnlAmount)}</td><td>${fmtPct(d.pnl_pct)}</td><td><span class="badge ${v.pnlAmount >= 0 ? 'b-tr' : 'b-hs'}">${v.pnlAmount >= 0 ? '수익' : '손실'}</span></td></tr>`);
  } else if(v.holdings) {
    rows.push(`<tr><td>보유종목</td><td>${fmt(v.holdings)}종목</td><td>—</td><td><span class="badge b-tr">보유중</span></td></tr>`);
  } else {
    rows.push('<tr><td>보유종목</td><td>0</td><td>0%</td><td><span class="badge b-to">대기중</span></td></tr>');
  }
  $('asset-tbody').innerHTML = rows.join('');
  $('set-mode').textContent = d?.mode || 'PAPER';
}

function shortTime(s) {
  if(!s) return '—';
  const m = String(s).match(/T(\d{2}:\d{2}:\d{2})/);
  return m ? m[1] : String(s).slice(0, 8);
}

function orderStatusBadge(status) {
  const s = String(status || 'PENDING');
  if(s === 'FILLED') return '<span class="badge b-tr">체결</span>';
  if(s === 'PARTIAL_FILL') return '<span class="badge b-op">부분체결</span>';
  if(s === 'CANCELLED') return '<span class="badge b-to">취소</span>';
  if(s === 'FAILED') return '<span class="badge b-hs">실패</span>';
  return '<span class="badge b-op">대기</span>';
}

function orderSideLabel(order) {
  const phase = String(order.order_phase || '');
  if(phase === 'CANCEL') return '취소';
  if(phase === 'PYRAMID_BUY') return '피라미딩';
  if(phase.includes('SELL')) return '매도';
  if(order.order_type === 'SELL') return '매도';
  return '매수';
}

function renderOrders(rows) {
  const orders = Array.isArray(rows) ? rows : [];
  if($('order-total')) $('order-total').textContent = fmt(orders.length);
  if($('order-filled')) $('order-filled').textContent = fmt(orders.filter(o=>o.status === 'FILLED').length);
  if($('order-pending')) $('order-pending').textContent = fmt(orders.filter(o=>o.status === 'PENDING' || o.status === 'PARTIAL_FILL').length);
  if($('order-closed')) $('order-closed').textContent = fmt(orders.filter(o=>o.status === 'CANCELLED' || o.status === 'FAILED').length);

  const body = $('orders-tbody');
  if(!body) return;
  if(!orders.length) {
    body.innerHTML = '<tr><td colspan="10" class="empty">오늘 주문 내역 없음</td></tr>';
    return;
  }
  body.innerHTML = orders.map(o => {
    const tickerName = TICKER_NAMES[o.ticker] || '';
    return `<tr>
      <td>${esc(o.kis_order_id || (o.id ? `DB#${o.id}` : '—'))}</td>
      <td>${esc(shortTime(o.ordered_at))}</td>
      <td>${esc(o.ticker || '—')} ${esc(tickerName)}</td>
      <td>${esc(orderSideLabel(o))}</td>
      <td>${fmt(o.order_qty)}</td>
      <td>${o.order_price == null ? '—' : fmt(o.order_price)}</td>
      <td>${o.fill_qty == null ? '—' : fmt(o.fill_qty)}</td>
      <td>${orderStatusBadge(o.status)}</td>
      <td>${esc(o.order_phase || '—')}</td>
      <td>${esc(o.error_msg || o.error_code || '—')}</td>
    </tr>`;
  }).join('');
}

// ── 이벤트 로그 렌더 ─────────────────────────────────────────────────────
const LOG_EVENT_MAP = {
  TICK:{n:'틱 수신',cls:''},
  DAILY_STATE_RESET:{n:'새 거래일 상태 초기화(Daily State Reset)',cls:''},
  WS_CONNECTED:{n:'웹소켓 연결(WebSocket Connected)',cls:''},
  WS_DISCONNECTED:{n:'웹소켓 연결 끊김(WebSocket Disconnected)',cls:'lv-WARN'},
  TOKEN_REFRESHED:{n:'KIS 토큰 갱신(Token Refreshed)',cls:''},
  TOKEN_LOADED_FROM_CACHE:{n:'KIS 토큰 캐시 로드(Token Loaded From Cache)',cls:''},
  TIME_SYNC_WARN:{n:'시각 오차 경고(Time Sync Warning)',cls:'lv-WARN'},
  TIME_SYNC_OK:{n:'시각 동기화 정상(Time Sync OK)',cls:''},
  TIME_SYNC_ERROR:{n:'시각 동기화 실패(Time Sync Error)',cls:'lv-CRIT'},
  TIME_SYNC_FALLBACK:{n:'시각 동기화 서버 재시도(Time Sync Fallback)',cls:'lv-WARN'},
  F1_DONE:{n:'F1 필터 완료(F1 Done)',cls:''},
  F1_API_ERROR:{n:'F1 API 오류(F1 API Error)',cls:'lv-WARN'},
  F1_FETCH_DONE:{n:'F1 API 조회 완료(F1 Fetch Done)',cls:''},
  F1_FILTER_EMPTY:{n:'F1 필터 결과 없음(F1 Filter Empty)',cls:''},
  F1_RETRY_WAIT:{n:'F1 재시도 대기(F1 Retry Wait)',cls:'lv-WARN'},
  F1_EXPECTED_COMPARE:{n:'F1 예상체결 비교(F1 Expected Compare)',cls:''},
  F1_SNAPSHOT_SAVED:{n:'F1 후보 스냅샷 저장(F1 Snapshot Saved)',cls:''},
  F1_EXPECTED_QUOTE_ERROR:{n:'F1 예상가 조회 오류(F1 Expected Quote Error)',cls:'lv-WARN'},
  NO_TARGET:{n:'대상 종목 없음(No Target)',cls:''},
  F2_SKIPPED:{n:'F2 종목 잠금 생략(F2 Skipped)',cls:'lv-WARN'},
  TARGET_LOCKED:{n:'대상 종목 잠금(Target Locked)',cls:''},
  F3_SKIPPED:{n:'F3 진입 생략(F3 Skipped)',cls:'lv-WARN'},
  F3_RECHECK:{n:'F3 진입 전 재검증(F3 Recheck)',cls:''},
  F3_ENTRY_BLOCKED:{n:'F3 진입 차단(F3 Entry Blocked)',cls:'lv-WARN'},
  ENTRY_PRE_ORDER_WAIT:{n:'진입 주문 전 대기(Entry Pre-order Wait)',cls:''},
  ENTRY_ORDER_SENT:{n:'진입 주문 전송(Entry Order Sent)',cls:''},
  ENTRY_RETRY_START:{n:'진입 재시도 시작(Entry Retry Start)',cls:'lv-WARN'},
  ENTRY_RETRY_SKIPPED:{n:'진입 재시도 생략(Entry Retry Skipped)',cls:'lv-WARN'},
  ENTRY_FILL_POLL_TIMEOUT:{n:'진입 체결조회 시간초과(Entry Fill Poll Timeout)',cls:'lv-WARN'},
  ENTRY_CANCEL_SENT:{n:'진입 주문 취소 전송(Entry Cancel Sent)',cls:'lv-WARN'},
  ENTRY_EXECUTED:{n:'진입 체결(Entry Executed)',cls:''},
  ENTRY_FAIL:{n:'진입 실패(Entry Failed)',cls:'lv-WARN'},
  GAP_CHANGED:{n:'진입 전 갭 변동(Gap Changed)',cls:'lv-WARN'},
  SLIPPAGE_GUARD:{n:'슬리피지 가드 발동(Slippage Guard)',cls:'lv-WARN'},
  PYRAMID_EXECUTED:{n:'피라미딩 체결(Pyramid Executed)',cls:''},
  PYRAMID_SKIPPED:{n:'피라미딩 생략(Pyramid Skipped)',cls:''},
  PYRAMID_TIMEOUT:{n:'피라미딩 체결 시간 초과(Pyramid Timeout)',cls:'lv-WARN'},
  TRAILING_STOP:{n:'트레일링 스탑 청산(Trailing Stop)',cls:''},
  HARD_STOP:{n:'하드 스탑 청산(Hard Stop)',cls:'lv-CRIT'},
  TIMEOUT_CLOSE:{n:'타임아웃 청산(Timeout Close)',cls:''},
  TIMEOUT_RETRY:{n:'타임아웃 청산 재시도(Timeout Retry)',cls:'lv-WARN'},
  TIMEOUT_ORDER_FAILED:{n:'타임아웃 청산 주문 실패(Timeout Order Failed)',cls:'lv-CRIT'},
  PROCESS_RESTART_DETECTED:{n:'프로세스 재시작 감지(Process Restart Detected)',cls:'lv-WARN'},
  ORDER_SMOKE_BUY_FILLED:{n:'주문 테스트 매수 체결(Order Smoke Buy Filled)',cls:''},
  ORDER_SMOKE_SELL_FILLED:{n:'주문 테스트 매도 체결(Order Smoke Sell Filled)',cls:''},
};

function renderLogs(logs) {
  const scroll = $('ev-scroll');
  if(!logs.length){ scroll.innerHTML='<div class="empty">이벤트 없음</div>'; return; }
  scroll.innerHTML = logs.map((l,i)=>{
    const info = LOG_EVENT_MAP[l.event]||{n:l.event,cls:''};
    const eventName = l.event_label || info.n;
    const t = l.ts ? l.ts.substring(11,19) : '';
    const detail = buildLogDetail(l);
    const lvCls = l.level==='CRIT'?'lv-CRIT':l.level==='WARN'?'lv-WARN':'lv-INFO';
    const cur = i===0 ? '<span class="ev-cur">▌</span>' : '';
    return `<div class="ev ${info.cls||lvCls}">
      <div class="ev-t">${t}</div>
      <div><div class="ev-n">${eventName}${cur}</div><div class="ev-d">${detail}</div></div>
    </div>`;
  }).join('');
}

function buildLogDetail(l) {
  const parts=[];
  if(l.ticker) parts.push(l.ticker);
  if(l.reason) parts.push(`사유 ${l.reason}`);
  if(l.order_id) parts.push(`주문 ${l.order_id}`);
  if(l.offset_ms!=null) parts.push(`+${l.offset_ms}ms ${l.level}`);
  if(l.ntp_server) parts.push(l.ntp_server);
  if(l.order_price) parts.push(`주문가 ${fmt(l.order_price)}원`);
  if(l.order_qty) parts.push(`주문 ${fmt(l.order_qty)}주`);
  if(l.sleep_sec!=null) parts.push(`대기 ${fmt(l.sleep_sec,1)}초`);
  if(l.entry_attempt!=null && l.max_attempts!=null) parts.push(`시도 ${fmt(l.entry_attempt)}/${fmt(l.max_attempts)}`);
  if(l.entry_price) parts.push(`진입 ${fmt(l.entry_price)}원`);
  if(l.exit_price)  parts.push(`청산 ${fmt(l.exit_price)}원`);
  if(l.pnl_pct!=null) parts.push(`P&L ${fmtPct(l.pnl_pct)}`);
  if(l.fill_qty)    parts.push(`${fmt(l.fill_qty)}주`);
  if(l.gap_pct)     parts.push(`갭 ${fmtPct(l.gap_pct)}`);
  if(l.cash!=null) parts.push(`현금 ${fmt(l.cash)}`);
  if(l.poll_attempts!=null) parts.push(`조회 ${fmt(l.poll_attempts)}회`);
  if(l.poll_last_output_count!=null) parts.push(`체결조회 ${fmt(l.poll_last_output_count)}건`);
  if(l.poll_last_matched===true) parts.push('주문매칭');
  if(l.poll_last_ccld_qty!=null && l.poll_last_ccld_qty>0) parts.push(`체결 ${fmt(l.poll_last_ccld_qty)}주`);
  if(l.rt_cd!=null) parts.push(`rt ${l.rt_cd}`);
  if(l.msg_cd) parts.push(l.msg_cd);
  if(l.poll_last_msg_cd) parts.push(`last ${l.poll_last_msg_cd}`);
  if(l.raw_count!=null) parts.push(`raw ${fmt(l.raw_count)}`);
  if(l.ranking_pass!=null) parts.push(`ranking ${fmt(l.ranking_pass)}`);
  if(l.expected_pass!=null) parts.push(`expected ${fmt(l.expected_pass)}`);
  if(l.final_pass!=null) parts.push(`final ${fmt(l.final_pass)}`);
  if(l.expected_valid!=null) parts.push(`보강 ${fmt(l.expected_valid)}`);
  if(l.mismatch_count!=null) parts.push(`불일치 ${fmt(l.mismatch_count)}`);
  if(l.count!=null && l.path) parts.push(`${fmt(l.count)}건 저장`);
  if(l.error)       parts.push(l.error.substring(0,40));
  if(l.poll_last_error) parts.push(l.poll_last_error.substring(0,40));
  if(l.msg1)        parts.push(String(l.msg1).substring(0,40));
  if(l.poll_last_msg1) parts.push(String(l.poll_last_msg1).substring(0,40));
  if(l.message)     parts.push(l.message.substring(0,60));
  if(l.token_prefix) parts.push(l.token_prefix);
  return parts.join(' · ')||l.event;
}

// ── 이력 렌더 ────────────────────────────────────────────────────────────
function renderHistory(trades, stats) {
  if(stats){
    $('h-total').textContent = stats.total||0;
    $('h-wr').textContent    = (stats.win_rate||0)+'%';
    $('h-wr').className      = 'sc2-val '+(stats.win_rate>=50?'pup':'pdn');
    $('h-avg').textContent   = fmtPct(stats.avg_pnl);
    $('h-avg').className     = 'sc2-val '+(stats.avg_pnl>=0?'pup':'pdn');
    $('h-maxloss').textContent = fmtPct(stats.max_loss);
  }

  const tbody=$('h-tbody');
  if(!trades.length){ tbody.innerHTML='<tr><td colspan="8" class="empty">거래 없음</td></tr>'; return; }
  tbody.innerHTML = trades.map(t=>{
    const reason = t.status==='OPEN'?'OPEN':(t.close_reason||'—');
    const rc = REASON_CLS[reason]||'b-to';
    const rl = REASON_LBL[reason]||reason;
    const pnlCls = t.pnl_pct==null?'':(t.pnl_pct>=0?'pup':'pdn');
    const name = TICKER_NAMES[t.ticker]||'';
    return `<tr>
      <td>${t.date||'—'}</td>
      <td>${t.ticker||'—'} <span style="color:var(--mu);font-size:11px">${name}</span></td>
      <td>${t.entry_price?fmt(t.entry_price):'—'}</td>
      <td>${t.exit_price?fmt(t.exit_price):'<span style="color:var(--mu)">—</span>'}</td>
      <td class="${pnlCls}">${fmtPct(t.pnl_pct)}</td>
      <td class="${t.highest_step?'pup':''}">${t.highest_step?'✓':'<span style="color:var(--mu)">—</span>'}</td>
      <td class="${t.pyramided?'pup':''}">${t.pyramided?'✓':'<span style="color:var(--mu)">—</span>'}</td>
      <td><span class="badge ${rc}">${rl}</span></td>
    </tr>`;
  }).join('');
}

// ── Stats 렌더 ───────────────────────────────────────────────────────────
let _statsData = null;

function renderStats(s) {
  _statsData = s;
  $('d-pct').textContent = (s.win_rate||0)+'%';
  $('d-lbl').textContent = `${s.wins}승 ${s.losses}패`;
  $('d-wins').textContent = `승 (${s.wins})`;
  $('d-losses').textContent = `패 (${s.losses})`;

  // 월별 그리드
  const grid=$('monthly-grid');
  if(!s.monthly||!s.monthly.length){ grid.innerHTML='<div class="empty">데이터 없음</div>'; }
  else {
    grid.innerHTML = s.monthly.map(m=>{
      const yy=m.ym.substring(0,4), mm=m.ym.substring(4,6);
      const pc = m.sum_pnl>=0?'pup':'pdn';
      return `<div class="mcell"><div class="mname">${yy}.${mm}</div><div class="mpnl ${pc}">${fmtPct(m.sum_pnl)}</div><div class="mtr">${m.n}거래</div></div>`;
    }).join('');
  }

  drawDonut(s.wins, s.losses);
  drawBar(s.by_reason);
}

function drawDonut(wins, losses) {
  const c=$('donut'); if(!c) return;
  const ctx=c.getContext('2d'), cx=96, cy=96, r=72, inner=48;
  ctx.clearRect(0,0,192,192);
  const total=wins+losses||1, winA=wins/total*2*Math.PI;
  ctx.beginPath(); ctx.moveTo(cx,cy);
  ctx.arc(cx,cy,r,-Math.PI/2,-Math.PI/2+winA);
  ctx.closePath(); ctx.fillStyle='#26a69a'; ctx.fill();
  ctx.beginPath(); ctx.moveTo(cx,cy);
  ctx.arc(cx,cy,r,-Math.PI/2+winA,-Math.PI/2+2*Math.PI);
  ctx.closePath(); ctx.fillStyle='#2a2e39'; ctx.fill();
  ctx.beginPath(); ctx.arc(cx,cy,inner,0,2*Math.PI);
  ctx.fillStyle=themeVal('#1e222d','#eaecf2'); ctx.fill();
}

function drawBar(byReason) {
  const c=$('bar'); if(!c) return;
  const ctx=c.getContext('2d');
  ctx.clearRect(0,0,420,192);
  const data=[
    {lbl:'TRAILING', val:byReason.TRAILING?.avg_pnl||0, color:'#26a69a', n:byReason.TRAILING?.n||0},
    {lbl:'TIMEOUT',  val:byReason.TIMEOUT?.avg_pnl||0,  color:'#787b86', n:byReason.TIMEOUT?.n||0},
    {lbl:'HARD_STOP',val:byReason.HARD_STOP?.avg_pnl||0,color:'#ef5350', n:byReason.HARD_STOP?.n||0},
  ];
  const pad={t:24,r:20,b:48,l:48};
  const W=420,H=192,cW=W-pad.l-pad.r,cH=H-pad.t-pad.b;
  const allVals=data.map(d=>Math.abs(d.val)).filter(v=>v>0);
  const maxA=allVals.length ? Math.max(...allVals)*1.3 : 2.5;
  const zY=pad.t+cH*(maxA/(2*maxA));

  ctx.beginPath(); ctx.moveTo(pad.l,zY); ctx.lineTo(W-pad.r,zY);
  ctx.strokeStyle=themeVal('#363a45','#c8cbd6'); ctx.lineWidth=1; ctx.stroke();

  const slot=cW/data.length, bW=slot*0.38;
  data.forEach((d,i)=>{
    const x=pad.l+i*slot+(slot-bW)/2;
    const bH=Math.abs(d.val)/(2*maxA)*cH;
    const y=d.val>=0?zY-bH:zY;
    ctx.fillStyle=d.color+'18'; ctx.fillRect(x,y,bW,bH);
    ctx.fillStyle=d.color; ctx.fillRect(x,d.val>=0?zY-3:zY,bW,3);
    ctx.font='bold 13px Noto Sans KR,sans-serif'; ctx.textAlign='center';
    ctx.fillStyle=d.color;
    ctx.fillText((d.val>=0?'+':'')+d.val.toFixed(2)+'%',x+bW/2,d.val>=0?y-8:y+bH+13);
    ctx.font='10px Noto Sans KR,sans-serif'; ctx.fillStyle=themeVal('#787b86','#4f5260');
    ctx.fillText(d.lbl,x+bW/2,H-pad.b+13);
    ctx.fillText('('+d.n+'건)',x+bW/2,H-pad.b+25);
  });
  ctx.textAlign='right'; ctx.font='10px Noto Sans KR,sans-serif'; ctx.fillStyle=themeVal('#787b86','#4f5260');
  [-2,-1,0,1,2].forEach(v=>{
    const yp=pad.t+(maxA-v)/(2*maxA)*cH;
    ctx.fillText((v>0?'+':'')+v+'%',pad.l-6,yp+3);
    ctx.beginPath(); ctx.moveTo(pad.l,yp); ctx.lineTo(pad.l+cW,yp);
    ctx.strokeStyle='#363a4540'; ctx.lineWidth=1; ctx.stroke();
  });
}

// ── API 호출 ─────────────────────────────────────────────────────────────
async function loadStatus() {
  try {
    const r = await fetch('/api/status');
    if(!r.ok) return;
    applyStatus(await r.json());
  } catch(e){}
}

async function loadAssets(refresh) {
  const btn = $('asset-refresh');
  const meta = $('asset-updated');
  const setAssetMeta = (text, cls='idle', title='') => {
    if(!meta) return;
    meta.textContent = text;
    meta.className = 'status-chip ' + cls;
    meta.title = title || text;
  };
  const assetErrorLabel = err => {
    const msg = String(err?.message || '').trim();
    const code = msg.match(/\bmsg_cd=([^ ]+)/)?.[1] || String(err?.code || '').trim();
    const msg1 = msg.match(/\bmsg1=(.+)$/)?.[1];
    if(code && msg1) return `${code}: ${msg1}`;
    if(code) return code;
    const missing = msg.match(/missing field ([A-Za-z0-9_]+)/);
    if(missing) return `응답 필드 누락: ${missing[1]}`;
    if(msg.includes('missing output2')) return '잔고 요약 누락';
    if(msg.includes('output1 is not a list')) return '보유종목 응답 형식 오류';
    if(msg.includes('output1 item is not an object')) return '보유종목 항목 형식 오류';
    const invalid = msg.match(/invalid field ([A-Za-z0-9_]+)/);
    if(invalid) return `응답 숫자 오류: ${invalid[1]}`;
    return msg || '원인 미상';
  };
  try {
    if(refresh && btn) {
      btn.disabled = true;
      btn.textContent = '…';
      setAssetMeta('KIS 잔고 조회중', 'warn');
    }
    let r = await fetch('/api/assets' + (refresh ? '?refresh=1' : ''));
    let d = null;
    let missingAssetApi = false;
    if(r.status === 404) {
      missingAssetApi = true;
      r = await fetch('/api/status');
      if(!r.ok) throw new Error('asset api missing');
      d = await r.json();
      setAssetMeta('자산 API 미반영', 'warn');
    } else {
      if(!r.ok) throw new Error('asset api failed');
      d = await r.json();
    }
    if(d.assets) {
      _lastAssets = d.assets;
      _lastStatus = _lastStatus || {};
      _lastStatus.assets = d.assets;
      renderAssetSummary(_lastStatus);
      renderAssets(_lastStatus);
      if(!missingAssetApi) {
        const metaText = d.assets.snapshot_source === 'DB'
          ? assetSnapshotLabel(d.assets)
          : '최근 조회 ' + new Date().toLocaleTimeString('ko-KR', {hour12:false});
        setAssetMeta(metaText, d.assets.snapshot_source === 'DB' ? 'warn' : 'ok');
      }
    } else if(meta) {
      if(refresh && d.error) {
        const reason = assetErrorLabel(d.error);
        setAssetMeta('KIS 잔고 실패: ' + reason, 'warn', d.error.message || reason);
      } else {
        setAssetMeta(refresh ? 'KIS 잔고 응답 없음' : '자산 조회 대기', refresh ? 'warn' : 'idle');
      }
    }
  } catch(e) {
    setAssetMeta('자산 API 호출 실패', 'err');
  } finally {
    if(btn) {
      btn.disabled = false;
      btn.textContent = '↻';
    }
  }
}

function refreshAssets() {
  loadAssets(true);
}

async function loadLogs() {
  try {
    const r = await fetch('/api/logs?n=60');
    if(!r.ok) return;
    renderLogs(await r.json());
  } catch(e){}
}

async function loadF1() {
  try {
    const r = await fetch('/api/f1');
    if(!r.ok) return;
    renderF1(await r.json());
  } catch(e){}
}

async function loadOrders() {
  try {
    const r = await fetch('/api/orders');
    if(!r.ok) return;
    renderOrders(await r.json());
  } catch(e){}
}

async function loadHistory() {
  try {
    const [hr, sr] = await Promise.all([fetch('/api/history'), fetch('/api/stats')]);
    const trades = hr.ok ? await hr.json() : [];
    const stats  = sr.ok ? await sr.json() : null;
    renderHistory(trades, stats);
  } catch(e){}
}

async function loadStats() {
  try {
    const r = await fetch('/api/stats');
    if(!r.ok) return;
    renderStats(await r.json());
  } catch(e){}
}

// ── SSE 구독 ─────────────────────────────────────────────────────────────
function connectSSE() {
  const es = new EventSource('/api/stream');
  es.onmessage = e => {
    try {
      const d = JSON.parse(e.data);
      if(d.type==='tick') {
        if(_lastStatus) {
          _lastStatus.current_price = d.price;
          if(_lastStatus.entry_price)
            _lastStatus.pnl_pct = +((d.price/_lastStatus.entry_price-1)*100).toFixed(2);
          applyStatus(_lastStatus);
        }
      } else if(d.type==='status') {
        loadStatus();
      } else if(d.type==='log') {
        loadLogs();
        loadF1();
        loadOrders();
      }
    } catch(err){}
  };
  es.onerror = () => { es.close(); setTimeout(connectSSE, 5000); };
}

// ── 테마 ─────────────────────────────────────────────────────────────────
function toggleTheme() {
  const html = document.documentElement;
  const isLight = html.getAttribute('data-theme') === 'light';
  const next = isLight ? 'dark' : 'light';
  html.setAttribute('data-theme', next === 'dark' ? '' : 'light');
  $('theme-btn').textContent = next === 'light' ? '☀' : '🌙';
  localStorage.setItem('theme', next);
  // canvas는 CSS 변수를 읽지 못하므로 재렌더
  drawArc(0);
  drawPriceFlow(_lastStatus);
  if (_statsData) renderStats(_statsData);
}

(function initTheme() {
  const saved = localStorage.getItem('theme') || 'dark';
  if (saved === 'light') {
    document.documentElement.setAttribute('data-theme', 'light');
    document.addEventListener('DOMContentLoaded', () => {
      const btn = $('theme-btn');
      if (btn) btn.textContent = '🌙';
    });
  }
})();

// ── 초기 로드 ────────────────────────────────────────────────────────────
loadStatus();
loadF1();
loadLogs();
loadOrders();
connectSSE();

// 폴링 백업 (SSE가 오래된 이벤트를 놓칠 경우 대비)
setInterval(loadStatus, 3000);
setInterval(loadF1, 5000);
setInterval(loadLogs, 10000);
setInterval(loadOrders, 5000);
