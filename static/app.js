// LSR Trading Dashboard - Fixed

// Helpers
const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);
const api = async (p, timeoutMs = 10000) => {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const r = await fetch(p, { signal: controller.signal });
    if (!r.ok) return null;
    return await r.json();
  } catch {
    return null;
  } finally {
    clearTimeout(timeoutId);
  }
};
const fmt = (n) => n == null ? '—' : Number(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtInt = (n) => n == null ? '—' : Number(n).toLocaleString('en-US');

// State (expose to window for debugging)
window.chart = null;
window.candleSeries = null;
window.ema20Series = null;
window.ema50Series = null;
window.ema200Series = null;
let chart = null;
let candleSeries = null;
let ema20Series = null;
let ema50Series = null;
let ema200Series = null;
let activeSymbol = 'ES';
let activeTf = '5m';
const SYMBOLS = ['ES', 'NQ', 'CL', 'GC', 'SI'];
let ws = null;
let wsReconnectTimer = null;
let wsReconnectAttempts = 0;
let chartInitRetries = 0;
let fastDataInFlight = false;
let pendingFastRefresh = false;
let fastRefreshTimer = null;
let lastFastRefreshAt = 0;
let slowDataInFlight = false;
let pendingSlowRefresh = false;
let slowRefreshTimer = null;
let lastSlowRefreshAt = 0;
let latestLevels = [];
let latestQuotes = {};
let chartRequestToken = 0;
const MIN_FAST_REFRESH_INTERVAL_MS = 400;
const MIN_SLOW_REFRESH_INTERVAL_MS = 3000;

// Initialize on DOM ready
document.addEventListener('DOMContentLoaded', () => {
  console.log('DOM loaded, initializing dashboard...');
  initChart();
  loadFastData();
  loadSlowData();
  loadSwingPoints();
  initWebSocket();
  // Fallback polling cadence for steady updates when WS stalls.
  setInterval(() => scheduleFastRefresh(150), 3000);
  setInterval(() => scheduleSlowRefresh(500), 15000);
});

// Chart initialization
function initChart() {
  const container = $('#chart-container');
  if (!container) {
    console.error('Chart container not found');
    return;
  }
  
  if (typeof LightweightCharts === 'undefined') {
    console.error('LightweightCharts library not loaded');
    if (chartInitRetries < 20) {
      chartInitRetries += 1;
      setTimeout(initChart, 500); // Retry
    }
    return;
  }
  chartInitRetries = 0;

  console.log('Creating chart...');
  chart = window.chart = LightweightCharts.createChart(container, {
    width: container.clientWidth,
    height: 450,
    layout: {
      background: { color: '#0d1117' },
      textColor: '#8b949e',
      fontSize: 11,
      fontFamily: "'JetBrains Mono', monospace"
    },
    grid: {
      vertLines: { color: '#1c2128' },
      horzLines: { color: '#1c2128' }
    },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    rightPriceScale: { borderColor: '#30363d' },
    timeScale: { borderColor: '#30363d', timeVisible: true, secondsVisible: false }
  });

  candleSeries = window.candleSeries = chart.addCandlestickSeries({
    upColor: '#3fb950',
    downColor: '#f85149',
    borderUpColor: '#3fb950',
    borderDownColor: '#f85149',
    wickUpColor: '#3fb950',
    wickDownColor: '#f85149'
  });

  ema20Series = window.ema20Series = chart.addLineSeries({ color: '#d29922', lineWidth: 1, priceLineVisible: false });
  ema50Series = window.ema50Series = chart.addLineSeries({ color: '#58a6ff', lineWidth: 1, priceLineVisible: false });
  ema200Series = window.ema200Series = chart.addLineSeries({ color: '#8b949e', lineWidth: 1, priceLineVisible: false });

  // Resize observer
  new ResizeObserver(() => {
    if (chart) chart.applyOptions({ width: container.clientWidth });
  }).observe(container);

  // Symbol buttons
  const btnDiv = $('#chart-btns');
  SYMBOLS.forEach(sym => {
    const btn = document.createElement('button');
    btn.className = 'chart-btn' + (sym === activeSymbol ? ' active' : '');
    btn.textContent = sym;
    btn.onclick = () => {
      activeSymbol = sym;
      $$('.chart-btn').forEach(b => b.classList.toggle('active', b.textContent === sym));
      $$('.inst').forEach(i => i.classList.toggle('selected', i.dataset.symbol === activeSymbol));
      loadChart();
      loadSwingPoints();
    };
    btnDiv.appendChild(btn);
  });

  // Timeframe buttons
  $$('.tf-btn').forEach(btn => {
    btn.onclick = () => {
      activeTf = btn.dataset.tf;
      $$('.tf-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      loadChart();
    };
  });

  console.log('Chart initialized successfully');
  loadChart();
}

// Load chart data
async function loadChart() {
  if (!chart || !candleSeries) return;
  const requestToken = ++chartRequestToken;
  const symbol = activeSymbol;
  const tf = activeTf;
  const hours = activeTf === '1h' ? 72 : activeTf === '15m' ? 48 : 24;
  const data = await api(`/api/candles?symbol=${symbol}&tf=${tf}&hours=${hours}`);

  if (requestToken !== chartRequestToken) return;
  if (!data || !data.candles) {
    console.error('No chart data received');
    return;
  }

  console.log(`Loaded ${data.candles.length} candles for ${symbol} ${tf}`);
  candleSeries.setData(data.candles);
  ema20Series.setData(data.ema20 || []);
  ema50Series.setData(data.ema50 || []);
  ema200Series.setData(data.ema200 || []);
  
  chart.timeScale().fitContent();
}

function updateStatus(status) {
  if (!status) return;
  const badge = $('#badge');
  if (!badge) return;
  const state = status.status || 'offline';
  badge.textContent = state.toUpperCase();
  if (state === 'running') {
    badge.className = 'badge badge-running';
  } else if (state === 'degraded') {
    badge.className = 'badge badge-unknown';
  } else {
    badge.className = 'badge badge-offline';
  }
}

function updateAccount(account) {
  if (!account) return;
  $('#balance').textContent = '$' + fmtInt(account.balance || 0);
  $('#realized').textContent = fmt(account.realizedPnl || 0);
  $('#realized').className = 'stat-val ' + (account.realizedPnl > 0 ? 'pos' : account.realizedPnl < 0 ? 'neg' : '');
  $('#unrealized').textContent = fmt(account.unrealizedPnl || 0);
  $('#unrealized').className = 'stat-val ' + (account.unrealizedPnl > 0 ? 'pos' : account.unrealizedPnl < 0 ? 'neg' : '');
  $('#updated').textContent = new Date().toLocaleTimeString();
}

function normalizeQuotes(quotesPayload) {
  if (!quotesPayload) return {};
  if (quotesPayload.quotes && Array.isArray(quotesPayload.quotes)) {
    return quotesPayload.quotes.reduce((acc, row) => {
      if (row && row.symbol) acc[row.symbol] = row;
      return acc;
    }, {});
  }
  if (Array.isArray(quotesPayload)) {
    return quotesPayload.reduce((acc, row) => {
      if (row && row.symbol) acc[row.symbol] = row;
      return acc;
    }, {});
  }
  return quotesPayload;
}

function renderInstruments() {
  const container = $('#instruments');
  if (!container) return;
  const source = latestLevels.length > 0
    ? latestLevels
    : SYMBOLS.map(sym => ({ symbol: sym }));
  container.innerHTML = source.map(inst => {
    const quote = latestQuotes[inst.symbol] || {};
    const price = quote.last ?? inst.last ?? null;
    return `
      <div class="inst ${inst.symbol === activeSymbol ? 'selected' : ''}" data-symbol="${inst.symbol}">
        <div class="inst-sym">${inst.symbol}</div>
        <div class="inst-price">${fmt(price)}</div>
        <div class="inst-row"><span class="inst-label">Bid</span><span class="inst-val">${fmt(quote.bid ?? inst.bid)}</span></div>
        <div class="inst-row"><span class="inst-label">Ask</span><span class="inst-val">${fmt(quote.ask ?? inst.ask)}</span></div>
        <div class="inst-row"><span class="inst-label pdh">PDH</span><span class="inst-val">${fmt(inst.pdh)}</span></div>
        <div class="inst-row"><span class="inst-label pdl">PDL</span><span class="inst-val">${fmt(inst.pdl)}</span></div>
        <div class="inst-row"><span class="inst-label pdc">PDC</span><span class="inst-val">${fmt(inst.pdc)}</span></div>
      </div>
    `;
  }).join('');

  $$('.inst').forEach(el => {
    el.onclick = () => {
      activeSymbol = el.dataset.symbol;
      $$('.inst').forEach(i => i.classList.toggle('selected', i.dataset.symbol === activeSymbol));
      $$('.chart-btn').forEach(b => b.classList.toggle('active', b.textContent === activeSymbol));
      loadChart();
      loadSwingPoints();
    };
  });
}

function updatePositions(positions) {
  if (positions && positions.positions && positions.positions.length > 0) {
    $('#posCount').textContent = positions.positions.length;
    $('#positions').innerHTML = positions.positions.map(p => `
      <div class="pos-row">
        <div class="pos-info">
          <span class="pos-sym">${p.symbol}</span>
          <span class="pos-dir pos-${(p.direction || '').toLowerCase()}">${(p.direction || '').toUpperCase()}</span>
          <div class="pos-detail">
            Entry ${fmt(p.avgPrice)} · SL ${fmt(p.sl)} · TP ${fmt(p.tp)}
          </div>
        </div>
        <div class="pos-pnl ${p.unrealizedPnl > 0 ? 'pos' : p.unrealizedPnl < 0 ? 'neg' : ''}">${fmt(p.unrealizedPnl)}</div>
      </div>
    `).join('');
  } else {
    $('#posCount').textContent = '0';
    $('#positions').innerHTML = '<div style="color:var(--dim);padding:12px 0;">Flat — no open positions</div>';
  }
}

function updateTrades(trades) {
  if (!trades) return;
  const summary = trades.summary || {};
  $('#summary').innerHTML = `
    <span class="summary-stat"><span class="summary-dot" style="background:#3fb950"></span>${summary.wins || 0}W</span>
    <span class="summary-stat"><span class="summary-dot" style="background:#f85149"></span>${summary.losses || 0}L</span>
    <span class="summary-stat">|</span>
    <span class="summary-stat">${(trades.trades || []).filter(t => t.status === 'open').length} open</span>
  `;

  $('#trades').innerHTML = (trades.trades || []).map(t => `
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

function updateSignals(signals) {
  if (!signals || !signals.signals) return;
  const signalsEl = $('#signals');
  if (!signalsEl) return;
  signalsEl.innerHTML = signals.signals.map(s => `<div class="sig">${s}</div>`).join('');
  signalsEl.scrollTop = signalsEl.scrollHeight;
}

function updateSwingPoints(data) {
  const el = $('#swingPoints');
  if (!el) return;
  const rows = data?.swingPoints || data?.points || [];
  if (!rows.length) {
    el.innerHTML = '<div style="color:var(--dim)">No swing points</div>';
    return;
  }
  const recent = rows.slice(-8).reverse();
  el.innerHTML = recent.map(sp => {
    const label = sp.label || sp.type || sp.kind || 'SWING';
    const price = sp.price ?? sp.value ?? null;
    const ts = sp.time || sp.ts || '';
    return `<div class="sig">${label} ${fmt(price)} <span style="color:var(--dim)">${ts}</span></div>`;
  }).join('');
}

async function loadFastData() {
  if (fastDataInFlight) {
    pendingFastRefresh = true;
    return;
  }
  fastDataInFlight = true;
  try {
    console.log('[loadFastData] Starting data fetch...');
    const [status, account, positions, quotes] = await Promise.all([
      api('/api/status'),
      api('/api/account'),
      api('/api/positions'),
      api('/api/quotes')
    ]);
    updateStatus(status);
    updateAccount(account);
    updatePositions(positions);
    if (quotes) {
      latestQuotes = normalizeQuotes(quotes);
      renderInstruments();
    }
    console.log('[loadFastData] UI update complete');
  } catch (error) {
    console.error('[loadFastData] Error:', error);
  } finally {
    fastDataInFlight = false;
    if (pendingFastRefresh) {
      pendingFastRefresh = false;
      scheduleFastRefresh(250);
    }
  }
}

async function loadSlowData() {
  if (slowDataInFlight) {
    pendingSlowRefresh = true;
    return;
  }
  slowDataInFlight = true;
  try {
    const [levels, trades, signals] = await Promise.all([
      api('/api/levels', 12000),
      api('/api/trades', 12000),
      api('/api/signals', 12000),
    ]);
    if (levels && levels.instruments) {
      latestLevels = levels.instruments;
      renderInstruments();
    }
    updateTrades(trades);
    updateSignals(signals);
    await loadSwingPoints();
  } catch (error) {
    console.error('[loadSlowData] Error:', error);
  } finally {
    slowDataInFlight = false;
    if (pendingSlowRefresh) {
      pendingSlowRefresh = false;
      scheduleSlowRefresh(750);
    }
  }
}

async function loadSwingPoints() {
  const data = await api(`/api/swing-points?symbol=${activeSymbol}`, 8000);
  updateSwingPoints(data);
}

function scheduleFastRefresh(delayMs = 150) {
  if (fastRefreshTimer) return;
  const elapsed = Date.now() - lastFastRefreshAt;
  const effectiveDelay = elapsed >= MIN_FAST_REFRESH_INTERVAL_MS
    ? delayMs
    : Math.max(delayMs, MIN_FAST_REFRESH_INTERVAL_MS - elapsed);
  fastRefreshTimer = setTimeout(() => {
    fastRefreshTimer = null;
    lastFastRefreshAt = Date.now();
    loadFastData();
  }, effectiveDelay);
}

function scheduleSlowRefresh(delayMs = 500) {
  if (slowRefreshTimer) return;
  const elapsed = Date.now() - lastSlowRefreshAt;
  const effectiveDelay = elapsed >= MIN_SLOW_REFRESH_INTERVAL_MS
    ? delayMs
    : Math.max(delayMs, MIN_SLOW_REFRESH_INTERVAL_MS - elapsed);
  slowRefreshTimer = setTimeout(() => {
    slowRefreshTimer = null;
    lastSlowRefreshAt = Date.now();
    loadSlowData();
  }, effectiveDelay);
}

// WebSocket for real-time updates
function initWebSocket() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = `${protocol}//${window.location.host}/api/ws`;
  
  console.log('Connecting to WebSocket:', wsUrl);
  ws = new WebSocket(wsUrl);
  
  ws.onopen = () => {
    console.log('✓ WebSocket connected');
    wsReconnectAttempts = 0;
    if (wsReconnectTimer) {
      clearTimeout(wsReconnectTimer);
      wsReconnectTimer = null;
    }
  };
  
  ws.onmessage = (event) => {
    try {
      if (typeof event.data === 'string' && event.data.length > 500000) {
        console.warn('WebSocket message too large; dropped');
        return;
      }
      const update = JSON.parse(event.data);
      console.log('WebSocket update:', update.type);

      if (update.type === 'quote') {
        if (update.symbol) {
          latestQuotes[update.symbol] = { ...latestQuotes[update.symbol], ...update };
          renderInstruments();
        }
        scheduleFastRefresh(75);
      } else if (update.type === 'position' || update.type === 'order') {
        scheduleFastRefresh(150);
      } else {
        scheduleSlowRefresh(300);
      }
    } catch (e) {
      console.error('WebSocket message error:', e);
    }
  };
  
  ws.onerror = (error) => {
    console.error('WebSocket error:', error);
  };
  
  ws.onclose = () => {
    const baseDelay = Math.min(30000, 1000 * (2 ** wsReconnectAttempts));
    const jitter = Math.floor(Math.random() * 500);
    const delay = baseDelay + jitter;
    wsReconnectAttempts = Math.min(wsReconnectAttempts + 1, 5);
    console.log(`WebSocket disconnected, reconnecting in ${Math.round(delay / 1000)}s...`);
    wsReconnectTimer = setTimeout(initWebSocket, delay);
  };
}

console.log('Dashboard script loaded');
