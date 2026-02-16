// LSR Trading Dashboard - Fixed

// Helpers
const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);
const api = (p) => fetch(p).then(r => r.ok ? r.json() : null).catch(() => null);
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

// Initialize on DOM ready
document.addEventListener('DOMContentLoaded', () => {
  console.log('DOM loaded, initializing dashboard...');
  initChart();
  loadData();
  initWebSocket();
  // Fallback polling if WebSocket fails
  setInterval(loadData, 30000); // Refresh every 30s as backup
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
    setTimeout(initChart, 500); // Retry
    return;
  }

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
      loadChart();
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
  
  const hours = activeTf === '1h' ? 72 : activeTf === '15m' ? 48 : 24;
  const data = await api(`/api/candles?symbol=${activeSymbol}&tf=${activeTf}&hours=${hours}`);
  
  if (!data || !data.candles) {
    console.error('No chart data received');
    return;
  }

  console.log(`Loaded ${data.candles.length} candles for ${activeSymbol} ${activeTf}`);
  candleSeries.setData(data.candles);
  ema20Series.setData(data.ema20 || []);
  ema50Series.setData(data.ema50 || []);
  ema200Series.setData(data.ema200 || []);
  
  chart.timeScale().fitContent();
}

// Load all dashboard data
async function loadData() {
  const [status, account, levels, positions, trades, signals] = await Promise.all([
    api('/api/status'),
    api('/api/account'),
    api('/api/levels'),
    api('/api/positions'),
    api('/api/trades'),
    api('/api/signals')
  ]);

  // Update status
  if (status) {
    const badge = $('#badge');
    badge.textContent = status.status === 'running' ? 'RUNNING' : 'OFFLINE';
    badge.className = 'badge ' + (status.status === 'running' ? 'badge-running' : 'badge-offline');
  }

  // Update account
  if (account) {
    $('#balance').textContent = '$' + fmtInt(account.balance || 0);
    $('#realized').textContent = fmt(account.realizedPnl || 0);
    $('#realized').className = 'stat-val ' + (account.realizedPnl > 0 ? 'pos' : account.realizedPnl < 0 ? 'neg' : '');
    $('#unrealized').textContent = fmt(account.unrealizedPnl || 0);
    $('#unrealized').className = 'stat-val ' + (account.unrealizedPnl > 0 ? 'pos' : account.unrealizedPnl < 0 ? 'neg' : '');
    $('#updated').textContent = new Date().toLocaleTimeString();
  }

  // Update instruments
  if (levels && levels.instruments) {
    const container = $('#instruments');
    container.innerHTML = levels.instruments.map(inst => `
      <div class="inst ${inst.symbol === activeSymbol ? 'selected' : ''}" data-symbol="${inst.symbol}">
        <div class="inst-sym">${inst.symbol}</div>
        <div class="inst-price">${fmt(inst.last || 0)}</div>
        <div class="inst-row"><span class="inst-label">P&L</span><span class="inst-val">—</span></div>
        <div class="inst-row"><span class="inst-label pdh">PDH</span><span class="inst-val">${fmt(inst.pdh)}</span></div>
        <div class="inst-row"><span class="inst-label pdl">PDL</span><span class="inst-val">${fmt(inst.pdl)}</span></div>
        <div class="inst-row"><span class="inst-label pdc">PDC</span><span class="inst-val">${fmt(inst.pdc)}</span></div>
      </div>
    `).join('');
    
    // Add click handlers
    $$('.inst').forEach(el => {
      el.onclick = () => {
        activeSymbol = el.dataset.symbol;
        $$('.inst').forEach(i => i.classList.toggle('selected', i.dataset.symbol === activeSymbol));
        $$('.chart-btn').forEach(b => b.classList.toggle('active', b.textContent === activeSymbol));
        loadChart();
      };
    });
  }

  // Update positions
  if (positions && positions.positions && positions.positions.length > 0) {
    $('#posCount').textContent = positions.positions.length;
    $('#positions').innerHTML = positions.positions.map(p => `
      <div class="pos-row">
        <div class="pos-info">
          <span class="pos-sym">${p.symbol}</span>
          <span class="pos-dir pos-${p.direction.toLowerCase()}">${p.direction.toUpperCase()}</span>
          <div class="pos-detail">
            Entry ${fmt(p.avgPrice)} · SL ${fmt(p.sl)} · TP ${fmt(p.tp)}
          </div>
        </div>
        <div class="pos-pnl ${p.unrealizedPnl > 0 ? 'pos' : 'neg'}">${fmt(p.unrealizedPnl)}</div>
      </div>
    `).join('');
  } else {
    $('#posCount').textContent = '0';
    $('#positions').innerHTML = '<div style="color:var(--dim);padding:12px 0;">Flat — no open positions</div>';
  }

  // Update trades
  if (trades) {
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
          <span class="trade-side">${t.side}</span>
          <span class="trade-meta">${fmt(t.price)} · ${t.rr}R · ${t.session} · ${t.time}</span>
        </div>
        <div>
          <span class="trade-status st-${t.status}">${t.status.toUpperCase()}</span>
          <span class="trade-pnl ${t.pnl > 0 ? 'pos' : t.pnl < 0 ? 'neg' : ''}">${fmt(t.pnl)}</span>
        </div>
      </div>
    `).join('');
  }

  // Update activity
  if (signals && signals.signals) {
    $('#signals').innerHTML = signals.signals.map(s => `<div class="sig">${s}</div>`).join('');
    const feed = $('#signals');
    if (feed) feed.scrollTop = feed.scrollHeight;
  }
}

// WebSocket for real-time updates
function initWebSocket() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = `${protocol}//${window.location.host}/api/ws`;
  
  console.log('Connecting to WebSocket:', wsUrl);
  ws = new WebSocket(wsUrl);
  
  ws.onopen = () => {
    console.log('✓ WebSocket connected');
    if (wsReconnectTimer) {
      clearTimeout(wsReconnectTimer);
      wsReconnectTimer = null;
    }
  };
  
  ws.onmessage = (event) => {
    try {
      const update = JSON.parse(event.data);
      console.log('WebSocket update:', update.type);
      
      // Trigger data refresh on any update
      if (update.type === 'quote' || update.type === 'position' || update.type === 'order') {
        loadData();
      }
    } catch (e) {
      console.error('WebSocket message error:', e);
    }
  };
  
  ws.onerror = (error) => {
    console.error('WebSocket error:', error);
  };
  
  ws.onclose = () => {
    console.log('WebSocket disconnected, reconnecting in 5s...');
    wsReconnectTimer = setTimeout(initWebSocket, 5000);
  };
}

console.log('Dashboard script loaded');
