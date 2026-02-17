// ============================================================
// LSR Trading Dashboard — Real-time Frontend
// ============================================================

// --- Utilities ---
const $ = s => document.querySelector(s);
const $$ = s => document.querySelectorAll(s);
const fmt = n => n == null ? '—' : Number(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtInt = n => n == null ? '—' : Number(n).toLocaleString('en-US');

async function api(path, timeoutMs = 10000) {
  const ctrl = new AbortController();
  const tid = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const r = await fetch(path, { signal: ctrl.signal });
    return r.ok ? await r.json() : null;
  } catch { return null; }
  finally { clearTimeout(tid); }
}

// --- Config ---
const SYMBOLS = ['ES', 'NQ', 'CL', 'GC', 'SI'];
const FAST_POLL_MS = 3000;
const SLOW_POLL_MS = 15000;
const CHART_POLL_MS = 10000;

// --- State ---
let activeSymbol = 'ES';
let activeTf = '5m';

// Chart
let chart = null;
let candleSeries = null;
let ema20Series = null;
let ema50Series = null;
let ema200Series = null;
let chartReqId = 0;
let lastCandle = null;

// Data
let quotes = {};
let levels = [];

// WebSocket
let ws = null;
let wsTimer = null;
let wsAttempts = 0;
let wsMessageCount = 0;

// ======================== ENTRY POINT ========================

document.addEventListener('DOMContentLoaded', () => {
  initChart();
  connectWS();

  // Initial full data load
  pollFast();
  pollSlow();

  // Periodic REST reconciliation
  setInterval(pollFast, FAST_POLL_MS);
  setInterval(pollSlow, SLOW_POLL_MS);
  setInterval(loadChart, CHART_POLL_MS);
});

// ======================== CHART ========================

function initChart() {
  const el = $('#chart-container');
  if (!el) return;
  if (typeof LightweightCharts === 'undefined') {
    setTimeout(initChart, 500);
    return;
  }

  chart = window.chart = LightweightCharts.createChart(el, {
    width: el.clientWidth,
    height: 450,
    layout: {
      background: { color: '#0d1117' },
      textColor: '#8b949e',
      fontSize: 11,
      fontFamily: "'JetBrains Mono', monospace",
    },
    grid: {
      vertLines: { color: '#1c2128' },
      horzLines: { color: '#1c2128' },
    },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    rightPriceScale: { borderColor: '#30363d' },
    timeScale: { borderColor: '#30363d', timeVisible: true, secondsVisible: false },
  });

  candleSeries = chart.addCandlestickSeries({
    upColor: '#3fb950', downColor: '#f85149',
    borderUpColor: '#3fb950', borderDownColor: '#f85149',
    wickUpColor: '#3fb950', wickDownColor: '#f85149',
  });
  ema20Series = chart.addLineSeries({ color: '#d29922', lineWidth: 1, priceLineVisible: false });
  ema50Series = chart.addLineSeries({ color: '#58a6ff', lineWidth: 1, priceLineVisible: false });
  ema200Series = chart.addLineSeries({ color: '#8b949e', lineWidth: 1, priceLineVisible: false });

  new ResizeObserver(() => chart?.applyOptions({ width: el.clientWidth })).observe(el);

  // Symbol buttons
  const btns = $('#chart-btns');
  SYMBOLS.forEach(sym => {
    const b = document.createElement('button');
    b.className = 'chart-btn' + (sym === activeSymbol ? ' active' : '');
    b.textContent = sym;
    b.onclick = () => switchSymbol(sym);
    btns.appendChild(b);
  });

  // Timeframe buttons
  $$('.tf-btn').forEach(b => {
    b.onclick = () => {
      activeTf = b.dataset.tf;
      $$('.tf-btn').forEach(x => x.classList.remove('active'));
      b.classList.add('active');
      loadChart();
    };
  });

  loadChart();
}

async function loadChart() {
  if (!chart || !candleSeries) return;
  const id = ++chartReqId;
  const sym = activeSymbol;
  const tf = activeTf;
  const hours = tf === '1h' ? 72 : tf === '15m' ? 48 : 24;
  const data = await api(`/api/candles?symbol=${sym}&tf=${tf}&hours=${hours}`);

  // Discard if user switched symbol/tf while we were loading
  if (id !== chartReqId) return;
  if (!data?.candles?.length) return;

  candleSeries.setData(data.candles);
  ema20Series.setData(data.ema20 || []);
  ema50Series.setData(data.ema50 || []);
  ema200Series.setData(data.ema200 || []);
  chart.timeScale().fitContent();

  // Store last candle for live-tick updates between full refreshes
  lastCandle = { ...data.candles[data.candles.length - 1] };
}

function tickChart(price) {
  if (!candleSeries || !lastCandle || price == null) return;
  lastCandle.high = Math.max(lastCandle.high ?? price, price);
  lastCandle.low = Math.min(lastCandle.low ?? price, price);
  lastCandle.close = price;
  candleSeries.update(lastCandle);
}

function switchSymbol(sym) {
  if (sym === activeSymbol) return;
  activeSymbol = sym;
  $$('.inst').forEach(i => i.classList.toggle('selected', i.dataset.symbol === sym));
  $$('.chart-btn').forEach(b => b.classList.toggle('active', b.textContent === sym));
  lastCandle = null;
  loadChart();
  loadSwingPoints();
}

// ======================== DATA LOADING ========================

async function pollFast() {
  try {
    const [status, account, positions, quotesData] = await Promise.all([
      api('/api/status'),
      api('/api/account'),
      api('/api/positions'),
      api('/api/quotes'),
    ]);
    renderStatus(status);
    renderAccount(account);
    renderPositions(positions);
    if (quotesData?.quotes && typeof quotesData.quotes === 'object') {
      Object.assign(quotes, quotesData.quotes);
      updateQuotePrices();
    }
  } catch (e) { console.error('pollFast:', e); }
}

async function pollSlow() {
  try {
    const [levelsData, trades, signals] = await Promise.all([
      api('/api/levels', 12000),
      api('/api/trades', 12000),
      api('/api/signals', 12000),
    ]);
    if (levelsData?.instruments) {
      levels = levelsData.instruments;
      rebuildInstruments();
    }
    renderTrades(trades);
    renderSignals(signals);
    loadSwingPoints();
  } catch (e) { console.error('pollSlow:', e); }
}

async function loadSwingPoints() {
  const data = await api(`/api/swing-points?symbol=${activeSymbol}`, 8000);
  renderSwingPoints(data);
}

// ======================== RENDER FUNCTIONS ========================

function renderStatus(d) {
  if (!d) return;
  const el = $('#badge');
  if (!el) return;
  const s = d.status || 'offline';
  el.textContent = s.toUpperCase();
  el.className = 'badge ' + (s === 'running' ? 'badge-running' : s === 'degraded' ? 'badge-unknown' : 'badge-offline');
}

function renderAccount(d) {
  if (!d || d.error) return;
  const set = (id, val) => { const e = $(id); if (e) e.textContent = val; };
  const cls = (id, c) => { const e = $(id); if (e) e.className = 'stat-val ' + c; };
  set('#balance', '$' + fmtInt(d.balance || 0));
  set('#realized', fmt(d.realizedPnl || 0));
  cls('#realized', d.realizedPnl > 0 ? 'pos' : d.realizedPnl < 0 ? 'neg' : '');
  set('#unrealized', fmt(d.unrealizedPnl || 0));
  cls('#unrealized', d.unrealizedPnl > 0 ? 'pos' : d.unrealizedPnl < 0 ? 'neg' : '');
  set('#updated', new Date().toLocaleTimeString());
}

function rebuildInstruments() {
  const el = $('#instruments');
  if (!el) return;
  const src = levels.length > 0 ? levels : SYMBOLS.map(s => ({ symbol: s }));
  el.innerHTML = src.map(inst => {
    const q = quotes[inst.symbol] || {};
    return `
      <div class="inst ${inst.symbol === activeSymbol ? 'selected' : ''}" data-symbol="${inst.symbol}">
        <div class="inst-sym">${inst.symbol}</div>
        <div class="inst-price" data-q="last">${fmt(q.last ?? inst.last)}</div>
        <div class="inst-row"><span class="inst-label">Bid</span><span class="inst-val" data-q="bid">${fmt(q.bid ?? inst.bid)}</span></div>
        <div class="inst-row"><span class="inst-label">Ask</span><span class="inst-val" data-q="ask">${fmt(q.ask ?? inst.ask)}</span></div>
        <div class="inst-row"><span class="inst-label pdh">PDH</span><span class="inst-val">${fmt(inst.pdh)}</span></div>
        <div class="inst-row"><span class="inst-label pdl">PDL</span><span class="inst-val">${fmt(inst.pdl)}</span></div>
        <div class="inst-row"><span class="inst-label pdc">PDC</span><span class="inst-val">${fmt(inst.pdc)}</span></div>
      </div>`;
  }).join('');
  $$('.inst').forEach(card => { card.onclick = () => switchSymbol(card.dataset.symbol); });
}

function updateQuotePrices() {
  for (const sym of Object.keys(quotes)) {
    const card = $(`.inst[data-symbol="${sym}"]`);
    if (!card) continue;
    const q = quotes[sym];
    const le = card.querySelector('[data-q="last"]');
    const be = card.querySelector('[data-q="bid"]');
    const ae = card.querySelector('[data-q="ask"]');
    if (le && q.last != null) le.textContent = fmt(q.last);
    if (be && q.bid != null) be.textContent = fmt(q.bid);
    if (ae && q.ask != null) ae.textContent = fmt(q.ask);
  }
}

function renderPositions(d) {
  const el = $('#positions');
  const countEl = $('#posCount');
  if (!el) return;
  const list = d?.positions || [];
  if (countEl) countEl.textContent = list.length;
  if (!list.length) {
    el.innerHTML = '<div style="color:var(--dim);padding:12px 0;">Flat — no open positions</div>';
    return;
  }
  el.innerHTML = list.map(p => `
    <div class="pos-row">
      <div class="pos-info">
        <span class="pos-sym">${p.symbol}</span>
        <span class="pos-dir pos-${(p.direction || '').toLowerCase()}">${(p.direction || '').toUpperCase()}</span>
        <div class="pos-detail">Entry ${fmt(p.avgPrice)} · SL ${fmt(p.sl)} · TP ${fmt(p.tp)}</div>
      </div>
      <div class="pos-pnl ${p.unrealizedPnl > 0 ? 'pos' : p.unrealizedPnl < 0 ? 'neg' : ''}">${fmt(p.unrealizedPnl)}</div>
    </div>
  `).join('');
}

function renderTrades(d) {
  if (!d) return;
  const s = d.summary || {};
  const sumEl = $('#summary');
  if (sumEl) {
    sumEl.innerHTML = `
      <span class="summary-stat"><span class="summary-dot" style="background:#3fb950"></span>${s.wins || 0}W</span>
      <span class="summary-stat"><span class="summary-dot" style="background:#f85149"></span>${s.losses || 0}L</span>
      <span class="summary-stat">|</span>
      <span class="summary-stat">${(d.trades || []).filter(t => t.status === 'open').length} open</span>
    `;
  }
  const el = $('#trades');
  if (!el) return;
  el.innerHTML = (d.trades || []).map(t => `
    <div class="trade-row">
      <div class="trade-left">
        <span class="trade-sym">${t.symbol}</span>
        <span class="trade-side">${t.side || '—'}</span>
        <span class="trade-meta">${fmt(t.price)} · ${t.rr ?? '—'}R · ${t.session || '—'} · ${t.time || '—'}</span>
      </div>
      <div>
        <span class="trade-status st-${t.status}">${(t.status || 'pending').toUpperCase()}</span>
        <span class="trade-pnl ${t.pnl > 0 ? 'pos' : t.pnl < 0 ? 'neg' : ''}">${fmt(t.pnl)}</span>
      </div>
    </div>
  `).join('');
}

function renderSignals(d) {
  if (!d?.signals) return;
  const el = $('#signals');
  if (!el) return;
  el.innerHTML = d.signals.map(s => `<div class="sig">${s}</div>`).join('');
  el.scrollTop = el.scrollHeight;
}

function renderSwingPoints(d) {
  const el = $('#swingPoints');
  if (!el) return;
  // Adapt to multiple possible response shapes from bridge
  const rows = d?.swingPoints || d?.points || d?.swings || d?.SwingPoints || (Array.isArray(d) ? d : []);
  if (!rows.length) {
    el.innerHTML = '<div style="color:var(--dim)">No swing points</div>';
    return;
  }
  el.innerHTML = rows.slice(-10).reverse().map(sp => {
    const label = sp.label || sp.type || sp.kind || sp.Type ||
      (sp.direction === 'High' || sp.Direction === 'High' ? 'SH' :
       sp.direction === 'Low' || sp.Direction === 'Low' ? 'SL' : 'SP');
    const price = sp.price ?? sp.value ?? sp.Price ?? sp.Value ?? null;
    const ts = sp.time || sp.ts || sp.Time || sp.Timestamp || '';
    return `<div class="sig">${label} ${fmt(price)} <span style="color:var(--dim)">${ts}</span></div>`;
  }).join('');
}

// ======================== WEBSOCKET ========================

function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const url = `${proto}//${location.host}/api/ws`;
  try { ws = new WebSocket(url); } catch { scheduleReconnect(); return; }

  ws.onopen = () => {
    console.log('WS connected');
    wsAttempts = 0;
    wsMessageCount = 0;
    if (wsTimer) { clearTimeout(wsTimer); wsTimer = null; }
  };
  ws.onmessage = onWsMessage;
  ws.onerror = () => {};
  ws.onclose = () => scheduleReconnect();
}

function onWsMessage(ev) {
  let msg;
  try { msg = JSON.parse(ev.data); } catch { return; }
  wsMessageCount++;

  // Log first 5 messages so we can diagnose bridge format in console
  if (wsMessageCount <= 5) {
    console.log('WS msg #' + wsMessageCount + ':', JSON.stringify(msg).slice(0, 400));
  }

  const type = (msg.type || msg.Type || msg.event || msg.Event || msg.messageType || '').toString().toLowerCase();
  const rawSym = msg.symbol || msg.Symbol || msg.instrument || msg.Instrument || '';
  const sym = normalizeSym(rawSym);

  // Route by type
  if (type === 'quote' || type === 'tick' || type === 'marketdata' || type === 'last' || type === 'bid' || type === 'ask') {
    handleTick(sym, msg);
  } else if (type === 'position' || type === 'positionupdate') {
    pollFast();
  } else if (type === 'order' || type === 'orderupdate' || type === 'execution' || type === 'fill') {
    pollFast();
  } else if (sym && hasPrice(msg)) {
    // Unknown type but has price data — treat as tick
    handleTick(sym, msg);
  } else {
    if (wsMessageCount <= 20) {
      console.log('WS unhandled:', type || '(no type)', sym || '(no sym)', msg);
    }
  }
}

function normalizeSym(raw) {
  if (!raw) return '';
  // Strip contract suffixes: "ES 03-26" -> "ES", "NQ 06-26" -> "NQ"
  return raw.split(/[\s]/)[0].replace(/\d{2}-\d{2}$/, '').toUpperCase();
}

function hasPrice(msg) {
  return msg.last != null || msg.Last != null || msg.bid != null || msg.Bid != null ||
    msg.price != null || msg.Price != null;
}

function handleTick(sym, msg) {
  if (!sym) return;

  const q = quotes[sym] || {};
  // Handle both camelCase and PascalCase field names
  for (const [lo, hi] of [['last', 'Last'], ['bid', 'Bid'], ['ask', 'Ask'], ['volume', 'Volume']]) {
    if (msg[lo] != null) q[lo] = msg[lo];
    else if (msg[hi] != null) q[lo] = msg[hi];
  }
  if (msg.price != null) q.last = msg.price;
  if (msg.Price != null) q.last = msg.Price;
  quotes[sym] = q;

  // Targeted DOM update — no innerHTML rebuild
  const card = $(`.inst[data-symbol="${sym}"]`);
  if (card) {
    const le = card.querySelector('[data-q="last"]');
    const be = card.querySelector('[data-q="bid"]');
    const ae = card.querySelector('[data-q="ask"]');
    if (le && q.last != null) le.textContent = fmt(q.last);
    if (be && q.bid != null) be.textContent = fmt(q.bid);
    if (ae && q.ask != null) ae.textContent = fmt(q.ask);
  }

  // Live chart update for active symbol
  if (sym === activeSymbol && q.last != null) {
    tickChart(q.last);
  }
}

function scheduleReconnect() {
  if (wsTimer) return;
  const delay = Math.min(30000, 1000 * (2 ** wsAttempts)) + Math.random() * 500;
  wsAttempts = Math.min(wsAttempts + 1, 5);
  console.log(`WS reconnecting in ${Math.round(delay / 1000)}s...`);
  wsTimer = setTimeout(() => { wsTimer = null; connectWS(); }, delay);
}
