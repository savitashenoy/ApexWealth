
// ──────────────────────────────────────────────────────────────────────────────
// CONFIG
// ──────────────────────────────────────────────────────────────────────────────
const API = '/api';
let USER = null;
let TICKERS = [];
let holdingsData = [];
let analysisTicker = null;
let analysisPeriod = '1mo';
let dashBarChart, dashAllocationChart;
let allocIndustryChart, allocStockChart, allocBarChart, allocPnlChart;
let analysisPriceChart, analysisVolChart, analysisRetChart, scoreRadarChart, scoreContribChart;
let marketIndexChart, marketIndicesData = [], selectedMarketIndex = 'nifty50', selectedMarketPeriod = '1d';
let activeAnalysisTab = 'technicals';
let perfCumulativeChart;
let watchlistPortfolioItem = null;
let portfolioAlerts = [];
let portfolioAlertDismissedIds = new Set();
let watchlistGroups = ['Default'];
let activeWatchlistGroup = 'Default';
let watchlistGroupOwnerId = null;
let currentWatchlistItems = [];
let marketAutoRefreshTimer = null;
let activeRefreshUserId = null;
let lastUpdateSource = '';

function formatISTTimestamp(d = new Date()) {
  try {
    return new Intl.DateTimeFormat('en-IN', {
      timeZone: 'Asia/Kolkata',
      day: '2-digit', month: 'short', year: 'numeric',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
      hour12: true
    }).format(d);
  } catch(e) {
    return d.toLocaleString();
  }
}

function setLastUpdated(source = '') {
  lastUpdateSource = source || lastUpdateSource || 'Data';
  const el = document.getElementById('last-update-display');
  if (el) el.textContent = `Last update: ${formatISTTimestamp()}${lastUpdateSource ? ' · ' + lastUpdateSource : ''}`;
}

async function fetchJsonSafe(url, options = {}) {
  const r = await fetch(url, options);
  const contentType = (r.headers.get('content-type') || '').toLowerCase();
  const raw = await r.text();
  let d = {};
  if (raw && contentType.includes('application/json')) {
    try { d = JSON.parse(raw); } catch(e) { d = {}; }
  } else if (raw) {
    // Vercel returns HTML for routing/serverless errors. Do not expose JSON parser errors to users.
    const compact = raw.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
    d = {error: compact ? compact.slice(0, 180) : `Request failed (${r.status})`};
  }
  if (!r.ok) throw new Error((d && d.error) || `Request failed (${r.status})`);
  return d;
}


// ──────────────────────────────────────────────────────────────────────────────
// INIT
// ──────────────────────────────────────────────────────────────────────────────
function applyTheme(theme) {
  const isLight = theme !== 'dark';
  document.body.classList.toggle('light', isLight);
  document.body.classList.toggle('dark', !isLight);
  localStorage.setItem('apex_theme', isLight ? 'light' : 'dark');
  const icon = document.getElementById('theme-icon');
  const label = document.getElementById('theme-label');
  if (icon) icon.textContent = isLight ? '☀️' : '🌙';
  if (label) label.textContent = isLight ? 'Light Mode' : 'Dark Mode';
}

function toggleTheme() {
  const isLight = document.body.classList.contains('light');
  applyTheme(isLight ? 'dark' : 'light');
  refreshVisibleCharts();
}

function refreshVisibleCharts() {
  const activePage = document.querySelector('.page.active')?.id?.replace('page-', '');
  if (activePage === 'dashboard' && holdingsData.length) renderDashCharts(holdingsData);
  if (activePage === 'portfolio') {
    if (document.getElementById('tab-performance')?.classList.contains('active')) loadTrades();
  }
  if (activePage === 'analysis' && analysisTicker) {
    loadAnalysis();
    if (activeAnalysisTab === 'score' && snapshotScoreCache) renderScore(snapshotScoreCache);
  }
  if (activePage === 'markets') renderMarketIndexChart();
}

function destroyChartSafe(chartName) {
  try { const c = eval(chartName); if (c) { c.destroy(); eval(`${chartName} = null`); } } catch(e) {}
}

function clearAppState() {
  holdingsData = [];
  analysisTicker = '';
  selectedTicker = null;
  selectedWLTicker = null;
  selectedAnalysisTicker = null;
  snapshotScoreCache = null;
  watchlistGroups = ['Default'];
  activeWatchlistGroup = 'Default';
  watchlistGroupOwnerId = USER?.user_id || null;
  currentWatchlistItems = [];
  renderWatchlistGroupOptions();
  setWatchlistLoadingProgress(0, '', false);
  ['dashBarChart','dashAllocationChart','allocIndustryChart','allocStockChart','allocBarChart','allocPnlChart','perfCumulativeChart','analysisPriceChart','analysisVolChart','analysisRetChart','scoreRadarChart','scoreContribChart','marketIndexChart'].forEach(destroyChartSafe);
  const ids = {
    'dc-holdings':'—','dc-gainers':'—','dc-losers':'—','dc-top-gainer':'—','dc-top-loser':'—','dc-avg-chg':'—',
    'dc-top-gainer-name':'—','dc-top-loser-name':'—','pc-invested':'₹0','pc-curr':'₹0','pc-pnl':'₹0','pc-return':'0%','pc-count':'0'
  };
  Object.entries(ids).forEach(([id,val]) => { const el=document.getElementById(id); if(el) el.textContent=val; });
  const tableClears = {
    'holdings-tbody':'<tr><td colspan="11"><div class="empty-state"><i class="fa fa-briefcase"></i><p>No holdings yet. Add your first position above.</p></div></td></tr>',
    'watchlist-tbody':'<tr><td colspan="8"><div class="empty-state"><i class="fa fa-star"></i><p>Your watchlist is empty.</p></div></td></tr>',
    'watchlist-heatmap-tbody':'<tr><td colspan="6"><div class="empty-state"><i class="fa fa-fire"></i><p>No watchlist data for heat map.</p></div></td></tr>',
    'trades-tbody':'<tr><td colspan="8"><div class="empty-state"><i class="fa fa-history"></i><p>No completed trades yet</p></div></td></tr>'
  };
  Object.entries(tableClears).forEach(([id,html]) => { const el=document.getElementById(id); if(el) el.innerHTML=html; });
  const dashEmpty = document.getElementById('dashboard-empty-state');
  const dashCharts = document.getElementById('dashboard-charts-wrap');
  if (dashEmpty) dashEmpty.classList.remove('show');
  if (dashCharts) dashCharts.classList.remove('is-hidden');
  const lastUpdateEl = document.getElementById('last-update-display');
  if (lastUpdateEl) lastUpdateEl.textContent = 'Last update: —';
}

function isMarketHoursIST(now = new Date()) {
  const parts = new Intl.DateTimeFormat('en-GB', { timeZone: 'Asia/Kolkata', weekday:'short', hour:'2-digit', minute:'2-digit', hour12:false }).formatToParts(now);
  const get = t => parts.find(p => p.type === t)?.value;
  const day = get('weekday');
  if (day === 'Sat' || day === 'Sun') return false;
  const mins = Number(get('hour')) * 60 + Number(get('minute'));
  return mins >= (9 * 60 + 15) && mins <= (15 * 60 + 30);
}

function startMarketAutoRefresh() {
  stopMarketAutoRefresh();
  activeRefreshUserId = USER?.user_id || null;
  marketAutoRefreshTimer = setInterval(async () => {
    if (!USER || USER.user_id !== activeRefreshUserId || !isMarketHoursIST()) return;
    const activePage = document.querySelector('.page.active')?.id?.replace('page-', '');
    try {
      if (activePage === 'portfolio') await loadPortfolio();
      else if (activePage === 'watchlist') await loadWatchlist();
      else if (activePage === 'dashboard') await loadDashboard();
      else {
        // warm the data without changing the current screen
        await Promise.allSettled([
          fetch(`${API}/holdings/${USER.user_id}`, {cache:'no-store'}),
          fetch(`${API}/watchlist/${encodeURIComponent(USER.user_id)}?group=${encodeURIComponent(getSelectedWatchlistGroup())}`, {cache:'no-store'})
        ]);
      }
    } catch(e) {}
  }, 5 * 60 * 1000);
}

function stopMarketAutoRefresh() {
  if (marketAutoRefreshTimer) clearInterval(marketAutoRefreshTimer);
  marketAutoRefreshTimer = null;
  activeRefreshUserId = null;
}

window.onload = async () => {
  applyTheme(localStorage.getItem('apex_theme') || 'light');
  // Load tickers
  try {
    const r = await fetch('/static/tickers.json');
    TICKERS = await r.json();
  } catch(e) { TICKERS = []; }

  // Set today's date on add form
  document.getElementById('add-date').value = new Date().toISOString().split('T')[0];

  // Session restore
  const saved = sessionStorage.getItem('apex_user');
  if (saved) {
    USER = JSON.parse(saved);
    setTimeout(() => startApp(), 1400);
  } else {
    setTimeout(() => showAuth(), 1400);
  }
};

function showAuth() {
  document.getElementById('splash').style.display = 'none';
  document.getElementById('auth-page').style.display = 'flex';
}

async function startApp() {
  clearAppState();
  document.getElementById('splash').style.display = 'none';
  document.getElementById('auth-page').style.display = 'none';
  document.getElementById('app').style.display = 'flex';
  document.getElementById('nav-avatar').textContent = USER.email[0].toUpperCase();
  document.getElementById('nav-email-display').textContent = USER.email;
  await loadPage('dashboard');
  loadMarketTicker();
  setLastUpdated('Login');
  startMarketAutoRefresh();
}

// ──────────────────────────────────────────────────────────────────────────────
// AUTH
// ──────────────────────────────────────────────────────────────────────────────
function switchAuthTab(tab) {
  document.querySelectorAll('.auth-tab').forEach((t,i) => {
    t.classList.remove('active');
    if (['login','signup','change-password'][i] === tab) t.classList.add('active');
  });
  document.querySelectorAll('.auth-form').forEach(f => f.classList.remove('active'));
  const map = {'login':'login-form','signup':'signup-form','change-password':'change-pw-form'};
  document.getElementById(map[tab]).classList.add('active');
}

async function doLogin(e) {
  e.preventDefault();
  const email = document.getElementById('login-email').value;
  const password = document.getElementById('login-password').value;
  document.getElementById('login-error').textContent = '';
  const btn = document.getElementById('login-btn');
  btn.innerHTML = '<div class="spinner"></div>';
  try {
    const d = await fetchJsonSafe(`${API}/login`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,password})});
    USER = {user_id: d.user_id, email: d.email};
    sessionStorage.setItem('apex_user', JSON.stringify(USER));
    startApp();
  } catch(err) {
    document.getElementById('login-error').textContent = err.message;
    btn.textContent = 'Sign In';
  }
}

async function doSignup(e) {
  e.preventDefault();
  const email = document.getElementById('signup-email').value;
  const password = document.getElementById('signup-password').value;
  const confirm = document.getElementById('signup-confirm').value;
  document.getElementById('signup-error').textContent = '';
  if (password !== confirm) { document.getElementById('signup-error').textContent = 'Passwords do not match'; return; }
  try {
    const d = await fetchJsonSafe(`${API}/signup`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,password})});
    USER = {user_id: d.user_id, email: d.email};
    sessionStorage.setItem('apex_user', JSON.stringify(USER));
    startApp();
  } catch(err) {
    document.getElementById('signup-error').textContent = err.message;
  }
}

async function doChangePassword(e) {
  e.preventDefault();
  const email = document.getElementById('cp-email').value;
  const old_password = document.getElementById('cp-old').value;
  const new_password = document.getElementById('cp-new').value;
  document.getElementById('cp-error').textContent = '';
  document.getElementById('cp-success').textContent = '';
  try {
    const d = await fetchJsonSafe(`${API}/change-password`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,old_password,new_password})});
    document.getElementById('cp-success').textContent = '✓ Password updated successfully';
    setTimeout(() => switchAuthTab('login'), 2000);
  } catch(err) {
    document.getElementById('cp-error').textContent = err.message;
  }
}

function doLogout() {
  stopMarketAutoRefresh();
  sessionStorage.removeItem('apex_user');
  clearAppState();
  USER = null;
  watchlistGroups = ['Default'];
  activeWatchlistGroup = 'Default';
  watchlistGroupOwnerId = null;
  currentWatchlistItems = [];
  renderWatchlistGroupOptions();
  document.getElementById('app').style.display = 'none';
  document.getElementById('auth-page').style.display = 'flex';
  switchAuthTab('login');
}

// ──────────────────────────────────────────────────────────────────────────────
// NAVIGATION
// ──────────────────────────────────────────────────────────────────────────────
async function showPage(name) {
  document.querySelectorAll('.nav-page-btn').forEach((b,i) => {
    b.classList.remove('active');
    if (['dashboard','portfolio','watchlist','analysis','markets','screener'][i] === name) b.classList.add('active');
  });
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.getElementById(`page-${name}`).classList.add('active');
  await loadPage(name);
}

async function loadPage(name) {
  if (name === 'dashboard') await loadDashboard();
  else if (name === 'portfolio') await loadPortfolio();
  else if (name === 'watchlist') await loadWatchlist();
  else if (name === 'markets') await loadMarkets();
  else if (name === 'screener') await screenerInit();
  else if (name === 'analysis') { if (analysisTicker) await loadAnalysisSequence(); }
}

// ──────────────────────────────────────────────────────────────────────────────
// MARKET TICKER
// ──────────────────────────────────────────────────────────────────────────────
async function loadMarketTicker() {
  try {
    const r = await fetch(`${API}/market/indices`);
    const indices = await r.json();
    const ticker = document.getElementById('market-ticker');
    ticker.innerHTML = indices.map(idx => `
      <div class="ticker-item">
        <span class="ticker-sym">${idx.name}</span>
        <span class="ticker-val">${fmt(idx.value)}</span>
        <span class="ticker-chg ${idx.chg_pct >= 0 ? 'up' : 'dn'}">${idx.chg_pct >= 0 ? '▲' : '▼'} ${Math.abs(idx.chg_pct)}%</span>
      </div>
    `).join('');
    setLastUpdated('Market ticker');
  } catch(e) {}
}

// ──────────────────────────────────────────────────────────────────────────────
// DASHBOARD
// ──────────────────────────────────────────────────────────────────────────────
async function loadDashboard() {
  const uid = USER?.user_id;
  if (!uid) return;
  const holdings = await fetchJsonSafe(`${API}/holdings/${uid}`, {cache:'no-store'});
  if (!USER || USER.user_id !== uid) return;
  holdingsData = holdings;

  const n = holdings.length;
  const gainers = holdings.filter(h => h.day_chg_pct > 0).length;
  const losers = holdings.filter(h => h.day_chg_pct < 0).length;
  const topGainer = [...holdings].sort((a,b) => b.day_chg_pct - a.day_chg_pct)[0];
  const topLoser = [...holdings].sort((a,b) => a.day_chg_pct - b.day_chg_pct)[0];
  const avgChg = n ? (holdings.reduce((sum,h) => sum + Number(h.day_chg_pct || 0), 0) / n).toFixed(2) : '0.00';

  document.getElementById('dc-holdings').textContent = n;
  document.getElementById('dc-gainers').textContent = gainers;
  document.getElementById('dc-losers').textContent = losers;
  document.getElementById('dc-avg-chg').textContent = `${Number(avgChg) >= 0 ? '+' : ''}${avgChg}%`;
  document.getElementById('dc-avg-chg').className = `card-value ${Number(avgChg) >= 0 ? 'green' : 'red'}`;

  document.getElementById('dc-top-gainer').textContent = topGainer ? `+${topGainer.day_chg_pct}%` : '—';
  document.getElementById('dc-top-gainer-name').textContent = topGainer ? topGainer.symbol : '—';
  document.getElementById('dc-top-loser').textContent = topLoser ? `${topLoser.day_chg_pct}%` : '—';
  document.getElementById('dc-top-loser-name').textContent = topLoser ? topLoser.symbol : '—';

  const empty = document.getElementById('dashboard-empty-state');
  const charts = document.getElementById('dashboard-charts-wrap');
  if (!n) {
    if (empty) empty.classList.add('show');
    if (charts) charts.classList.add('is-hidden');
    ['dashBarChart','dashAllocationChart','allocIndustryChart','allocPnlChart'].forEach(destroyChartSafe);
    setLastUpdated('Dashboard');
    return;
  }
  if (empty) empty.classList.remove('show');
  if (charts) charts.classList.remove('is-hidden');
  renderDashCharts(holdings);
  setLastUpdated('Dashboard');
}

function renderDashCharts(holdings) {
  const labels = holdings.map(h => h.symbol);
  const invested = holdings.map(h => Number(h.invested || (h.buy_price * h.qty) || 0));
  const currVals = holdings.map(h => Number(h.curr_value || 0));

  // Bar chart - invested vs current
  const ctx1 = document.getElementById('dash-bar-chart')?.getContext('2d');
  if (ctx1) {
    if (dashBarChart) dashBarChart.destroy();
    dashBarChart = new Chart(ctx1, {
      type: 'bar',
      data: {
        labels,
        datasets: [
          {label:'Invested',data:invested,backgroundColor:'rgba(80,140,255,0.5)',borderColor:'#508cff',borderWidth:1},
          {label:'Current Value',data:currVals,backgroundColor:'rgba(0,200,150,0.5)',borderColor:'#00c896',borderWidth:1}
        ]
      },
      options: chartOpts('₹')
    });
  }

  // Portfolio allocation by percentage of invested value
  const totalInvested = invested.reduce((a,b) => a + b, 0);
  const allocPct = invested.map(v => totalInvested ? Number(((v / totalInvested) * 100).toFixed(2)) : 0);
  const ctx2 = document.getElementById('dash-allocation-chart')?.getContext('2d');
  const allocTheme = currentThemeColors();
  if (ctx2) {
    if (dashAllocationChart) dashAllocationChart.destroy();
    dashAllocationChart = new Chart(ctx2, {
      type: 'doughnut',
      data: {
        labels,
        datasets: [{
          data: allocPct,
          rawValues: invested,
          backgroundColor: COLORS,
          borderColor: allocTheme.donutBorder,
          borderWidth: 3,
          hoverOffset: 6
        }]
      },
      options: dashboardAllocationDoughnutOpts(totalInvested)
    });
  }

  // By Industry chart moved from Portfolio → Asset Allocation to Dashboard
  const industryMap = {};
  holdings.forEach(h => {
    const key = h.industry || h.sector || 'Unknown';
    industryMap[key] = (industryMap[key] || 0) + Number(h.curr_value || 0);
  });
  const industryCtx = document.getElementById('alloc-industry-chart')?.getContext('2d');
  if (industryCtx) {
    if (allocIndustryChart) allocIndustryChart.destroy();
    allocIndustryChart = new Chart(industryCtx, {
      type: 'doughnut',
      data: { labels: Object.keys(industryMap), datasets: [{ data: Object.values(industryMap), backgroundColor: COLORS, borderColor: currentThemeColors().donutBorder, borderWidth: 2 }] },
      options: doughnutOpts()
    });
  }

  // P&L by Stock chart moved from Portfolio → Asset Allocation to Dashboard
  const pnlCtx = document.getElementById('alloc-pnl-chart')?.getContext('2d');
  const pnls = holdings.map(h => Number(h.pnl || 0));
  if (pnlCtx) {
    if (allocPnlChart) allocPnlChart.destroy();
    allocPnlChart = new Chart(pnlCtx, {
      type: 'bar',
      data: { labels, datasets: [{
        label: 'P&L (₹)', data: pnls,
        backgroundColor: pnls.map(v => v >= 0 ? 'rgba(0,200,150,0.6)' : 'rgba(255,77,109,0.6)'),
        borderColor: pnls.map(v => v >= 0 ? '#00c896' : '#ff4d6d'),
        borderWidth: 1
      }] },
      options: chartOpts('₹')
    });
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// PORTFOLIO
// ──────────────────────────────────────────────────────────────────────────────
async function loadPortfolio() {
  const uid = USER?.user_id;
  if (!uid) return;
  const holdings = await fetchJsonSafe(`${API}/holdings/${uid}`, {cache:'no-store'});
  if (!USER || USER.user_id !== uid) return;
  holdingsData = holdings;
  renderHoldingsTable(holdings);
  renderPortfolioCards(holdings);
  await loadPortfolioAlerts();
  checkPortfolioAlerts(holdings);
  await loadTrades();
  setLastUpdated('Portfolio');
}

function renderPortfolioCards(holdings) {
  const totalInvested = holdings.reduce((s,h) => s+h.invested, 0);
  const totalCurr = holdings.reduce((s,h) => s+h.curr_value, 0);
  const pnl = totalCurr - totalInvested;
  const ret = totalInvested ? ((pnl/totalInvested)*100).toFixed(2) : 0;

  document.getElementById('pc-invested').textContent = `₹${fmtNum(totalInvested)}`;
  document.getElementById('pc-curr').textContent = `₹${fmtNum(totalCurr)}`;
  document.getElementById('pc-pnl').textContent = `${pnl >= 0 ? '+' : ''}₹${fmtNum(pnl)}`;
  document.getElementById('pc-pnl').className = `card-value td-mono ${pnl >= 0 ? 'green' : 'red'}`;
  document.getElementById('pc-return').textContent = `${ret >= 0 ? '+' : ''}${ret}%`;
  document.getElementById('pc-return').className = `card-value ${ret >= 0 ? 'green' : 'red'}`;
  document.getElementById('pc-count').textContent = holdings.length;
}

function renderHoldingsTable(holdings) {
  const tbody = document.getElementById('holdings-tbody');
  if (!holdings.length) {
    tbody.innerHTML = `<tr><td colspan="11"><div class="empty-state"><i class="fa fa-briefcase"></i><p>No holdings yet. Add your first position above.</p></div></td></tr>`;
    return;
  }
  tbody.innerHTML = holdings.map(h => `
    <tr>
      <td class="td-mono">${h.date}</td>
      <td class="td-symbol">${h.symbol}</td>
      <td class="td-mono">₹${h.buy_price}</td>
      <td class="td-mono">${h.qty}</td>
      <td class="td-mono">₹${h.ltp}</td>
      <td class="td-mono">₹${fmtNum(h.invested)}</td>
      <td class="td-mono">₹${fmtNum(h.curr_value)}</td>
      <td class="td-mono ${h.pnl >= 0 ? 'td-green' : 'td-red'}">${h.pnl >= 0 ? '+' : ''}₹${fmtNum(h.pnl)}</td>
      <td><span class="badge ${h.pnl_pct >= 0 ? 'badge-green' : 'badge-red'}">${h.pnl_pct >= 0 ? '▲' : '▼'} ${Math.abs(h.pnl_pct)}%</span></td>
      <td><span class="badge ${h.day_chg_pct >= 0 ? 'badge-green' : 'badge-red'}">${h.day_chg_pct >= 0 ? '+' : ''}${h.day_chg_pct}%</span></td>
      <td>
        <div class="action-icons">
          <button class="action-btn edit" title="Set Alert" onclick="openAlertModal('${h.id}','${h.symbol}')"><i class="fa fa-bell"></i></button>
          <button class="action-btn edit" title="Edit" onclick="openEditModal('${h.id}','${h.symbol}','${h.buy_price}','${h.qty}','${h.date}')"><i class="fa fa-pen"></i></button>
          <button class="action-btn sell" title="Sell" onclick="openSellModal('${h.id}','${h.symbol}','${h.ltp}','${h.qty}')"><i class="fa fa-dollar-sign"></i></button>
          <button class="action-btn del" title="Remove" onclick="deleteHolding('${encodeURIComponent(h.id)}', event)"><i class="fa fa-trash"></i></button>
        </div>
      </td>
    </tr>
  `).join('');
}


function compareAlertValue(actual, op, threshold) {
  const a = Number(actual), t = Number(threshold);
  if (!Number.isFinite(a) || !Number.isFinite(t)) return false;
  if (op === '>') return a > t;
  if (op === '>=') return a >= t;
  if (op === '=') return Math.abs(a - t) < 0.0001;
  if (op === '<') return a < t;
  if (op === '<=') return a <= t;
  return false;
}

function alertColumnLabel(col) {
  return col === 'ltp' ? 'LTP' : col === 'pnl_pct' ? 'P&L (%)' : 'Day Chng %';
}

async function loadPortfolioAlerts() {
  if (!USER) { portfolioAlerts = []; return; }
  try {
    const data = await fetchJsonSafe(`${API}/portfolio-alerts/${encodeURIComponent(USER.user_id)}`, {cache:'no-store'});
    portfolioAlerts = Array.isArray(data) ? data : [];
  } catch(e) {
    portfolioAlerts = [];
  }
  renderPortfolioAlertsTable();
}

function fmtDateTime(v) {
  if (!v) return '—';
  try {
    const d = new Date(v);
    if (Number.isNaN(d.getTime())) return escapeHtml(v);
    return new Intl.DateTimeFormat('en-IN', {timeZone:'Asia/Kolkata', day:'2-digit', month:'short', year:'numeric', hour:'2-digit', minute:'2-digit', second:'2-digit'}).format(d);
  } catch(e) { return escapeHtml(v); }
}

function alertConditionText(alert) {
  return `${alert.symbol || ''} — ${alertColumnLabel(alert.column_name || alert.column)} ${alert.condition_op || alert.condition || '>'} ${alert.threshold}`;
}

function renderPortfolioAlertsTable() {
  const tbody = document.getElementById('portfolio-alerts-tbody');
  if (!tbody) return;
  if (!portfolioAlerts.length) {
    tbody.innerHTML = `<tr><td colspan="5"><div class="empty-state"><i class="fa fa-bell"></i><p>No alerts created yet</p></div></td></tr>`;
    return;
  }
  tbody.innerHTML = portfolioAlerts.map(a => {
    const triggered = !!a.triggered_at;
    return `<tr>
      <td><strong>${escapeHtml(alertConditionText(a))}</strong></td>
      <td><span class="alert-status ${triggered ? 'triggered' : 'active'}">${triggered ? 'Triggered' : 'Active'}</span></td>
      <td>${fmtDateTime(a.triggered_at)}</td>
      <td>${fmtDateTime(a.created_at)}</td>
      <td><div class="action-icons">
        <button class="action-btn edit" title="Edit Alert" onclick="editPortfolioAlert('${encodeURIComponent(a.id)}')"><i class="fa fa-pen"></i></button>
        <button class="action-btn del" title="Delete Alert" onclick="deletePortfolioAlert('${encodeURIComponent(a.id)}')"><i class="fa fa-trash"></i></button>
      </div></td>
    </tr>`;
  }).join('');
}

async function markPortfolioAlertTriggered(alert) {
  if (!USER || !alert?.id) return;
  // Mark locally immediately so the same triggered alert does not notify again
  // while switching panels or during repeated portfolio refreshes.
  if (!alert.triggered_at) alert.triggered_at = new Date().toISOString();
  try {
    const res = await fetchJsonSafe(`${API}/portfolio-alerts/${encodeURIComponent(USER.user_id)}/${encodeURIComponent(alert.id)}/triggered`, {method:'POST', cache:'no-store'});
    alert.triggered_at = res.triggered_at || alert.triggered_at;
    renderPortfolioAlertsTable();
  } catch(e) { renderPortfolioAlertsTable(); }
}

function dismissTriggeredAlert(id) {
  portfolioAlertDismissedIds.add(String(id));
  checkPortfolioAlerts(holdingsData || []);
}
function clearTriggeredAlertNotifications() {
  (portfolioAlerts || []).forEach(a => portfolioAlertDismissedIds.add(String(a.id)));
  const panel = document.getElementById('portfolio-alert-hit-panel');
  if (panel) { panel.classList.remove('show'); panel.innerHTML = ''; }
}

function checkPortfolioAlerts(holdings) {
  const panel = document.getElementById('portfolio-alert-hit-panel');
  if (!panel) return;
  const hits = [];
  for (const alert of portfolioAlerts || []) {
    // Only Active alerts are allowed to notify. Once an alert has triggered_at,
    // keep it visible in the Alerts tab as Triggered but do not show notifications again.
    if (alert.triggered_at) continue;
    if (alert.active === false) continue;
    const h = (holdings || []).find(x => String(x.id) === String(alert.holding_id) || String(x.symbol).toUpperCase() === String(alert.symbol).toUpperCase());
    if (!h) continue;
    const col = alert.column_name || alert.column || 'ltp';
    const actual = h[col];
    if (compareAlertValue(actual, alert.condition_op || alert.condition || '>', alert.threshold)) {
      markPortfolioAlertTriggered(alert);
      if (!portfolioAlertDismissedIds.has(String(alert.id))) {
        hits.push({id: alert.id, text: `${h.symbol}: ${alertColumnLabel(col)} ${alert.condition_op || alert.condition || '>'} ${alert.threshold} — current ${actual}`});
      }
    }
  }
  if (!hits.length) {
    panel.classList.remove('show');
    panel.innerHTML = '';
    return;
  }
  panel.classList.add('show');
  panel.innerHTML = `<div class="alert-hit-head"><strong><i class="fa fa-bell"></i> Portfolio alerts triggered</strong><button class="alert-hit-close" title="Clear notifications" onclick="clearTriggeredAlertNotifications()"><i class="fa fa-times"></i></button></div><ul>${hits.map(h => `<li><div class="alert-hit-row"><span>${escapeHtml(h.text)}</span><button class="alert-dismiss-btn" onclick="dismissTriggeredAlert('${escapeHtml(h.id)}')">Delete notification</button></div></li>`).join('')}</ul>`;
}

function openAlertModal(holdingId, symbol) {
  document.getElementById('alert-id').value = '';
  document.getElementById('alert-holding-id').value = holdingId;
  document.getElementById('alert-symbol').value = symbol;
  document.getElementById('alert-column').value = 'ltp';
  document.getElementById('alert-condition').value = '>';
  document.getElementById('alert-value').value = '';
  document.getElementById('alert-error').textContent = '';
  document.getElementById('alert-modal').classList.add('open');
}
function closeAlertModal() { document.getElementById('alert-modal').classList.remove('open'); }

function editPortfolioAlert(encodedId) {
  const id = decodeURIComponent(encodedId);
  const a = (portfolioAlerts || []).find(x => String(x.id) === String(id));
  if (!a) return;
  document.getElementById('alert-id').value = a.id;
  document.getElementById('alert-holding-id').value = a.holding_id || '';
  document.getElementById('alert-symbol').value = a.symbol || '';
  document.getElementById('alert-column').value = a.column_name || a.column || 'ltp';
  document.getElementById('alert-condition').value = a.condition_op || a.condition || '>';
  document.getElementById('alert-value').value = a.threshold ?? '';
  document.getElementById('alert-error').textContent = '';
  document.getElementById('alert-modal').classList.add('open');
}

async function deletePortfolioAlert(encodedId) {
  const id = decodeURIComponent(encodedId);
  if (!confirm('Delete this alert?')) return;
  try {
    await fetchJsonSafe(`${API}/portfolio-alerts/${encodeURIComponent(USER.user_id)}/${encodeURIComponent(id)}`, {method:'DELETE', cache:'no-store'});
    portfolioAlertDismissedIds.delete(String(id));
    await loadPortfolioAlerts();
    checkPortfolioAlerts(holdingsData || []);
    setLastUpdated('Portfolio alert deleted');
  } catch(e) { alert(e.message || 'Could not delete alert.'); }
}

async function savePortfolioAlert() {
  const err = document.getElementById('alert-error');
  err.textContent = '';
  const alertId = document.getElementById('alert-id').value;
  const holdingId = document.getElementById('alert-holding-id').value;
  const symbol = document.getElementById('alert-symbol').value;
  const column = document.getElementById('alert-column').value;
  const condition = document.getElementById('alert-condition').value;
  const value = parseFloat(document.getElementById('alert-value').value);
  if (!Number.isFinite(value)) { err.textContent = 'Enter a valid alert value.'; return; }
  try {
    const url = alertId ? `${API}/portfolio-alerts/${encodeURIComponent(USER.user_id)}/${encodeURIComponent(alertId)}` : `${API}/portfolio-alerts/${encodeURIComponent(USER.user_id)}`;
    await fetchJsonSafe(url, {
      method: alertId ? 'PUT' : 'POST', headers:{'Content-Type':'application/json'}, cache:'no-store',
      body: JSON.stringify({holding_id: holdingId, symbol, column_name: column, condition_op: condition, threshold: value})
    });
    if (alertId) portfolioAlertDismissedIds.delete(String(alertId));
    closeAlertModal();
    await loadPortfolio();
    setLastUpdated(alertId ? 'Portfolio alert updated' : 'Portfolio alert saved');
  } catch(e) {
    err.textContent = e.message || 'Could not save alert.';
  }
}

// AUTOCOMPLETE
let selectedTicker = null;
let selectedWLTicker = null;
let selectedAnalysisTicker = null;

function tickerSearch(val) {
  const list = document.getElementById('ac-list');
  if (!val || val.length < 1) { list.style.display = 'none'; return; }
  const matches = TICKERS.filter(t =>
    t.symbol.toLowerCase().includes(val.toLowerCase()) ||
    t.name.toLowerCase().includes(val.toLowerCase())
  ).slice(0, 12);
  if (!matches.length) { list.style.display = 'none'; return; }
  list.innerHTML = matches.map(t => `
    <div class="ac-item" onmousedown="event.preventDefault();selectTicker('${t.symbol}','${t.name.replace(/'/g,"\\'")}','${t.industry || t.sector || ''}')">
      <div class="ac-symbol">${t.symbol}</div>
      <div class="ac-name">${t.name}</div>
    </div>
  `).join('');
  list.style.display = 'block';
}

function selectTicker(symbol, name, industry) {
  document.getElementById('add-ticker').value = `${symbol} — ${name}`;
  document.getElementById('ac-list').style.display = 'none';
  selectedTicker = {symbol, name, industry};
}

function closeAC()        { document.getElementById('ac-list').style.display = 'none'; }

async function addHolding() {
  const date = document.getElementById('add-date').value;
  const price = parseFloat(document.getElementById('add-price').value);
  const qty = parseFloat(document.getElementById('add-qty').value);
  const errEl = document.getElementById('add-error');
  errEl.textContent = '';

  if (!selectedTicker) { errEl.textContent = 'Select a ticker from the dropdown'; return; }
  if (!price || price <= 0) { errEl.textContent = 'Enter a valid buy price'; return; }
  if (!qty || qty <= 0) { errEl.textContent = 'Enter a valid quantity'; return; }

  const r = await fetch(`${API}/holdings/${USER.user_id}`, {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({symbol: selectedTicker.symbol, name: selectedTicker.name,
      buy_price: price, qty, date, industry: selectedTicker.industry})
  });
  if (r.ok) {
    document.getElementById('add-ticker').value = '';
    document.getElementById('add-price').value = '';
    document.getElementById('add-qty').value = '';
    document.getElementById('add-date').value = new Date().toISOString().split('T')[0];
    selectedTicker = null;
    await loadPortfolio();
    setLastUpdated('Holding added');
    await loadDashboard();
  } else {
    const d = await r.json();
    errEl.textContent = d.error || 'Error adding holding';
  }
}

async function deleteHolding(id, ev) {
  if (ev) { ev.preventDefault(); ev.stopPropagation(); }
  const holdingId = decodeURIComponent(id || '');
  if (!USER || !holdingId) return;
  if (!confirm('Remove this holding?')) return;
  try {
    await fetchJsonSafe(`${API}/holdings/${encodeURIComponent(USER.user_id)}/${encodeURIComponent(holdingId)}`, {method:'DELETE', cache:'no-store'});
  } catch(err) {
    // Some serverless/proxy setups are stricter with DELETE. Use POST fallback.
    await fetchJsonSafe(`${API}/holdings/${encodeURIComponent(USER.user_id)}/${encodeURIComponent(holdingId)}/delete`, {method:'POST', cache:'no-store'});
  }
  await loadPortfolio();
  await loadDashboard();
  setLastUpdated('Holding removed');
}

// SELL MODAL
let sellHoldingId = null;
let sellMaxQty = 0;
function openSellModal(id, symbol, ltp, qty) {
  sellHoldingId = id;
  sellMaxQty = parseFloat(qty) || 0;
  document.getElementById('sell-symbol').value = symbol;
  document.getElementById('sell-price').value = ltp;
  document.getElementById('sell-qty').value = sellMaxQty;
  document.getElementById('sell-qty').max = sellMaxQty;
  document.getElementById('sell-qty-hint').textContent = `Available quantity: ${sellMaxQty}. Keep full quantity for complete exit or reduce for partial sell.`;
  document.getElementById('sell-error').textContent = '';
  document.getElementById('sell-modal').classList.add('open');
}
function closeSellModal() { document.getElementById('sell-modal').classList.remove('open'); }
async function confirmSell() {
  const sell_price = parseFloat(document.getElementById('sell-price').value);
  const sell_qty = parseFloat(document.getElementById('sell-qty').value);
  if (!sell_price || sell_price <= 0) { document.getElementById('sell-error').textContent = 'Enter a valid sell price'; return; }
  if (!sell_qty || sell_qty <= 0) { document.getElementById('sell-error').textContent = 'Enter a valid sell quantity'; return; }
  if (sellMaxQty && sell_qty > sellMaxQty) { document.getElementById('sell-error').textContent = `Sell quantity cannot exceed available quantity (${sellMaxQty})`; return; }
  const btn = document.querySelector('#sell-modal .btn-primary');
  const origText = btn.textContent;
  btn.innerHTML = '<div class="spinner" style="margin:0;border-top-color:#000"></div>';
  btn.disabled = true;
  document.getElementById('sell-error').textContent = '';
  try {
    const r = await fetch(`${API}/sell/${USER.user_id}/${sellHoldingId}`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({sell_price, qty: sell_qty})
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'Sell failed');
    closeSellModal();
    await loadPortfolio();
  } catch(err) {
    document.getElementById('sell-error').textContent = err.message || 'Network error — try again';
  } finally {
    btn.innerHTML = origText;
    btn.disabled = false;
  }
}

// EDIT MODAL
let editHoldingId = null;
function openEditModal(id, symbol, price, qty, date) {
  editHoldingId = id;
  document.getElementById('edit-holding-id').value = id;
  document.getElementById('edit-symbol').value = symbol;
  document.getElementById('edit-price').value = price;
  document.getElementById('edit-qty').value = qty;
  document.getElementById('edit-date').value = date;
  document.getElementById('edit-error').textContent = '';
  document.getElementById('edit-modal').classList.add('open');
}
function closeEditModal() { document.getElementById('edit-modal').classList.remove('open'); }
async function confirmEdit() {
  const price = parseFloat(document.getElementById('edit-price').value);
  const qty = parseFloat(document.getElementById('edit-qty').value);
  const date = document.getElementById('edit-date').value;
  if (!price || !qty) { document.getElementById('edit-error').textContent = 'Fill all fields'; return; }
  const r = await fetch(`${API}/holdings/${USER.user_id}/${editHoldingId}`, {
    method:'PUT', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({buy_price: price, qty, date})
  });
  if (r.ok) {
    closeEditModal();
    await loadPortfolio();
  }
}

// PORTFOLIO TABS
function switchPortTab(tab) {
  const order = ['holdings','alerts','performance'];
  document.querySelectorAll('#page-portfolio .tabs-bar .tab-btn').forEach((b,i) => {
    b.classList.remove('active');
    if (order[i] === tab) b.classList.add('active');
  });
  document.querySelectorAll('#page-portfolio .tab-content').forEach(t => t.classList.remove('active'));
  document.getElementById(`tab-${tab}`).classList.add('active');
  if (tab === 'performance') loadTrades();
  if (tab === 'alerts') { loadPortfolioAlerts(); }
}

function renderAllocationCharts() {
  if (!holdingsData.length) return;
  const h = holdingsData;

  // Industry donut
  const sMap = {};
  h.forEach(x => { const s = x.industry || x.sector || 'Unknown'; sMap[s] = (sMap[s]||0) + x.curr_value; });
  const ctx1 = document.getElementById('alloc-industry-chart')?.getContext('2d');
  if (ctx1) {
    if (allocIndustryChart) allocIndustryChart.destroy();
    allocIndustryChart = new Chart(ctx1, {type:'doughnut', data:{labels:Object.keys(sMap),datasets:[{data:Object.values(sMap),backgroundColor:COLORS,borderColor:currentThemeColors().donutBorder,borderWidth:2}]}, options:doughnutOpts()});
  }

  // Stock donut
  const ctx2 = document.getElementById('alloc-stock-chart')?.getContext('2d');
  if (ctx2) { if (allocStockChart) allocStockChart.destroy();
  allocStockChart = new Chart(ctx2, {type:'doughnut', data:{labels:h.map(x=>x.symbol),datasets:[{data:h.map(x=>x.curr_value),backgroundColor:COLORS,borderColor:currentThemeColors().donutBorder,borderWidth:2}]}, options:doughnutOpts()}); }

  // Bar invested vs curr
  const ctx3 = document.getElementById('alloc-bar-chart')?.getContext('2d');
  if (ctx3) { if (allocBarChart) allocBarChart.destroy();
  allocBarChart = new Chart(ctx3, {type:'bar',data:{labels:h.map(x=>x.symbol),datasets:[
    {label:'Invested',data:h.map(x=>x.invested),backgroundColor:'rgba(80,140,255,0.5)',borderColor:'#508cff',borderWidth:1},
    {label:'Current Value',data:h.map(x=>x.curr_value),backgroundColor:'rgba(0,200,150,0.5)',borderColor:'#00c896',borderWidth:1}
  ]},options:chartOpts('₹')}); }

  // P&L bar
  const pnls = h.map(x => x.pnl);
  const ctx4 = document.getElementById('alloc-pnl-chart')?.getContext('2d');
  if (ctx4) { if (allocPnlChart) allocPnlChart.destroy();
  allocPnlChart = new Chart(ctx4, {type:'bar',data:{labels:h.map(x=>x.symbol),datasets:[{
    label:'P&L (₹)', data:pnls,
    backgroundColor:pnls.map(v => v>=0?'rgba(0,200,150,0.6)':'rgba(255,77,109,0.6)'),
    borderColor:pnls.map(v => v>=0?'#00c896':'#ff4d6d'),borderWidth:1
  }]},options:chartOpts('₹')}); }
}

async function loadTrades() {
  const r = await fetch(`${API}/trades/${USER.user_id}`);
  const trades = await r.json();
  const tbody = document.getElementById('trades-tbody');

  if (!trades.length) {
    tbody.innerHTML = `<tr><td colspan="8"><div class="empty-state"><i class="fa fa-history"></i><p>No completed trades yet. Sell a holding to record a trade.</p></div></td></tr>`;
    document.getElementById('perf-total-trades').textContent = 0;
    document.getElementById('perf-realised').textContent = '₹0';
    document.getElementById('perf-win-rate').textContent = '0%';
    document.getElementById('perf-best').textContent = '₹0';
    return;
  }

  const totalPnl = trades.reduce((s,t) => s+t.pnl, 0);
  const wins = trades.filter(t => t.pnl > 0).length;
  const best = Math.max(...trades.map(t => t.pnl));

  document.getElementById('perf-total-trades').textContent = trades.length;
  document.getElementById('perf-realised').textContent = `${totalPnl >= 0 ? '+' : ''}₹${fmtNum(totalPnl)}`;
  document.getElementById('perf-realised').className = `card-value td-mono ${totalPnl >= 0 ? 'green' : 'red'}`;
  document.getElementById('perf-win-rate').textContent = `${((wins/trades.length)*100).toFixed(0)}%`;
  document.getElementById('perf-best').textContent = `₹${fmtNum(best)}`;

  tbody.innerHTML = trades.map(t => `
    <tr>
      <td class="td-symbol">${t.symbol}</td>
      <td class="td-mono">${t.qty}</td>
      <td class="td-mono">₹${t.buy_price}</td>
      <td class="td-mono">₹${t.sell_price}</td>
      <td class="td-mono">${t.buy_date}</td>
      <td class="td-mono">${t.sell_date}</td>
      <td class="td-mono ${t.pnl >= 0 ? 'td-green' : 'td-red'}">${t.pnl >= 0 ? '+' : ''}₹${fmtNum(t.pnl)}</td>
      <td><span class="badge ${t.pnl_pct >= 0 ? 'badge-green' : 'badge-red'}">${t.pnl_pct >= 0 ? '▲' : '▼'} ${Math.abs(t.pnl_pct)}%</span></td>
    </tr>
  `).join('');

  // Cumulative P&L chart
  let cum = 0;
  const cumData = trades.map(t => { cum += t.pnl; return cum; });
  const ctx = document.getElementById('perf-cumulative-chart').getContext('2d');
  if (perfCumulativeChart) perfCumulativeChart.destroy();
  perfCumulativeChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: trades.map(t => t.sell_date),
      datasets: [{label:'Cumulative P&L (₹)',data:cumData,borderColor:'#00c896',backgroundColor:'rgba(0,200,150,0.08)',fill:true,tension:.4,pointRadius:4,pointBackgroundColor:'#00c896'}]
    },
    options: chartOpts('₹')
  });
}

// ──────────────────────────────────────────────────────────────────────────────
// WATCHLIST
// ──────────────────────────────────────────────────────────────────────────────
function wlTickerSearch(val) {
  const list = document.getElementById('wl-ac-list');
  if (!val || val.length < 1) { list.style.display = 'none'; return; }
  const matches = TICKERS.filter(t =>
    t.symbol.toLowerCase().includes(val.toLowerCase()) ||
    t.name.toLowerCase().includes(val.toLowerCase())
  ).slice(0, 12);
  if (!matches.length) { list.style.display = 'none'; return; }
  list.innerHTML = matches.map(t => `
    <div class="ac-item" onmousedown="event.preventDefault();selectWLTicker('${t.symbol}','${t.name.replace(/'/g,"\\'")}','${t.industry || t.sector || ''}')">
      <div class="ac-symbol">${t.symbol}</div>
      <div class="ac-name">${t.name}</div>
    </div>
  `).join('');
  list.style.display = 'block';
}

function selectWLTicker(symbol, name, industry) {
  document.getElementById('wl-ticker').value = `${symbol} — ${name}`;
  document.getElementById('wl-ac-list').style.display = 'none';
  selectedWLTicker = {symbol, name, industry};
}

function closeWLAC()      { document.getElementById('wl-ac-list').style.display = 'none'; }


function setWatchlistLoadingProgress(percent = 0, message = 'Loading watchlist…', show = true) {
  const wrap = document.getElementById('watchlist-load-progress');
  const bar = document.getElementById('watchlist-load-progress-bar');
  const text = document.getElementById('watchlist-load-progress-text');
  if (!wrap || !bar || !text) return;
  const pct = Math.max(0, Math.min(100, Number(percent || 0)));
  if (!show) {
    wrap.classList.remove('show');
    bar.style.width = '0%';
    text.textContent = '';
    return;
  }
  wrap.classList.add('show');
  bar.style.width = `${pct}%`;
  text.textContent = message || 'Loading watchlist…';
}

function getSelectedWatchlistGroup() {
  const sel = document.getElementById('watchlist-group-select');
  return (sel && sel.value ? sel.value : activeWatchlistGroup || 'Default').trim() || 'Default';
}

function renderWatchlistGroupOptions() {
  const sel = document.getElementById('watchlist-group-select');
  if (!sel) return;
  const groups = [...new Set((watchlistGroups || ['Default']).filter(Boolean))];
  if (!groups.includes('Default')) groups.unshift('Default');
  const previous = activeWatchlistGroup || sel.value || 'Default';
  sel.innerHTML = groups.map(g => `<option value="${escapeHtml(g)}">${escapeHtml(g)}</option>`).join('');
  activeWatchlistGroup = groups.includes(previous) ? previous : groups[0];
  sel.value = activeWatchlistGroup;
}

async function loadWatchlistGroups() {
  if (!USER) return;
  const uid = USER.user_id;
  if (watchlistGroupOwnerId !== uid) {
    watchlistGroups = ['Default'];
    activeWatchlistGroup = 'Default';
    currentWatchlistItems = [];
    watchlistGroupOwnerId = uid;
    renderWatchlistGroupOptions();
  }
  try {
    const d = await fetchJsonSafe(`${API}/watchlist-groups/${encodeURIComponent(uid)}`, {cache:'no-store'});
    if (!USER || USER.user_id !== uid) return;
    watchlistGroups = d.groups && d.groups.length ? d.groups : ['Default'];
  } catch(e) {
    watchlistGroups = watchlistGroups && watchlistGroups.length ? watchlistGroups : ['Default'];
  }
  renderWatchlistGroupOptions();
}

async function createWatchlistGroup() {
  const input = document.getElementById('watchlist-group-name');
  const status = document.getElementById('watchlist-import-status');
  const name = (input?.value || '').trim();
  if (!name) { if (status) status.textContent = 'Enter a watchlist group name.'; return; }
  try {
    const d = await fetchJsonSafe(`${API}/watchlist-groups/${encodeURIComponent(USER.user_id)}`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name})
    });
    activeWatchlistGroup = d.group || name;
    if (input) input.value = '';
    await loadWatchlistGroups();
    const sel = document.getElementById('watchlist-group-select');
    if (sel) sel.value = activeWatchlistGroup;
    if (status) status.textContent = `Watchlist group ready: ${activeWatchlistGroup}`;
    await loadWatchlist(false);
  } catch(err) {
    if (status) status.textContent = `Could not create group: ${err.message}`;
  }
}


async function deleteWatchlistGroup() {
  const group = getSelectedWatchlistGroup();
  const status = document.getElementById('watchlist-import-status');
  if (!USER || !group) return;
  if (group === 'Default') {
    if (status) status.textContent = 'Default group cannot be removed.';
    return;
  }
  if (!confirm(`Remove watchlist group "${group}" and all tickers inside it?`)) return;
  setWatchlistLoadingProgress(20, `Removing ${group} group…`, true);
  try {
    await fetchJsonSafe(`${API}/watchlist-groups/${encodeURIComponent(USER.user_id)}/${encodeURIComponent(group)}`, {method:'DELETE', cache:'no-store'});
  } catch(err) {
    await fetchJsonSafe(`${API}/watchlist-groups/${encodeURIComponent(USER.user_id)}/delete`, {
      method:'POST', cache:'no-store', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({group_name: group})
    });
  }
  activeWatchlistGroup = 'Default';
  if (status) status.textContent = `Removed watchlist group: ${group}`;
  await loadWatchlistGroups();
  const sel = document.getElementById('watchlist-group-select');
  if (sel) sel.value = 'Default';
  await loadWatchlist(false);
  setLastUpdated('Watchlist group removed');
}

async function switchWatchlistGroup() {
  activeWatchlistGroup = getSelectedWatchlistGroup();
  selectedWLTicker = null;
  const input = document.getElementById('wl-ticker');
  if (input) input.value = '';
  setWatchlistLoadingProgress(15, `Loading ${activeWatchlistGroup} watchlist…`, true);
  await loadWatchlist(false);
}

async function addToWatchlist() {
  if (!selectedWLTicker) return;
  const group = getSelectedWatchlistGroup();
  const r = await fetch(`${API}/watchlist/${encodeURIComponent(USER.user_id)}?group=${encodeURIComponent(group)}`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({...selectedWLTicker, group_name: group})
  });
  if (r.ok) {
    document.getElementById('wl-ticker').value = '';
    selectedWLTicker = null;
    await loadWatchlist(false);
  }
}


function csvEscape(v) {
  const s = String(v ?? '');
  return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}

function exportPortfolioCSV() {
  const rows = holdingsData || [];
  const headers = ['Date','Ticker','Name','Industry','Buy Price','Quantity','LTP','Invested','Current Value','Unrealised P&L','Return %','Day Change %'];
  const csvRows = [headers.join(',')];
  rows.forEach(h => {
    csvRows.push([
      h.date, h.symbol, h.name || '', h.industry || '', h.buy_price, h.qty, h.ltp,
      h.invested, h.curr_value, h.pnl, h.pnl_pct, h.day_chg_pct
    ].map(csvEscape).join(','));
  });
  const blob = new Blob([csvRows.join('\n')], {type:'text/csv;charset=utf-8;'});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `apexwealth_portfolio_${USER?.email || 'holdings'}_${new Date().toISOString().slice(0,10)}.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function extractTickerFromRow(row) {
  if (Array.isArray(row)) return row[0];
  const keys = Object.keys(row || {});
  const key = keys.find(k => ['symbol','ticker','nse symbol','nse_symbol','scrip','stock'].includes(String(k).toLowerCase().trim())) || keys[0];
  return row[key];
}

function normalizeImportedTicker(v) {
  return String(v || '').trim().toUpperCase().replace(/^NSE:/,'').replace(/\.NS$|\.BO$/,'').replace(/[^A-Z0-9&-]/g, '');
}

function normalizeWatchlistSymbol(v) {
  let s = String(v || '').trim();
  try { s = decodeURIComponent(s); } catch(e) {}
  const tmp = document.createElement('textarea');
  tmp.innerHTML = s;
  s = tmp.value;
  return s.toUpperCase().replace(/\.NS$|\.BO$/,'');
}

function compactWatchlistSymbol(v) {
  return normalizeWatchlistSymbol(v).replace(/[^A-Z0-9]/g, '');
}

function tickerMeta(symbol) {
  return TICKERS.find(t => String(t.symbol || '').toUpperCase() === symbol) || {symbol, name:symbol, industry:''};
}

async function importWatchlistFile(event) {
  const file = event.target.files?.[0];
  const status = document.getElementById('watchlist-import-status');
  if (!file) return;
  if (status) status.textContent = 'Importing watchlist tickers…';
  try {
    let rows = [];
    const ext = file.name.split('.').pop().toLowerCase();
    if (ext === 'csv') {
      const text = await file.text();
      const lines = text.split(/\r?\n/).filter(Boolean);
      const first = lines[0]?.split(',').map(x => x.trim().toLowerCase()) || [];
      const hasHeader = first.some(x => ['symbol','ticker','nse symbol','scrip','stock'].includes(x));
      const idx = hasHeader ? Math.max(0, first.findIndex(x => ['symbol','ticker','nse symbol','scrip','stock'].includes(x))) : 0;
      rows = lines.slice(hasHeader ? 1 : 0).map(line => line.split(',')[idx]);
    } else {
      if (typeof XLSX === 'undefined') throw new Error('XLSX parser could not load. Please try CSV or check internet access.');
      const buf = await file.arrayBuffer();
      const wb = XLSX.read(buf, {type:'array'});
      const ws = wb.Sheets[wb.SheetNames[0]];
      const json = XLSX.utils.sheet_to_json(ws, {defval:''});
      if (json.length) rows = json.map(extractTickerFromRow);
      else rows = XLSX.utils.sheet_to_json(ws, {header:1, defval:''}).map(extractTickerFromRow);
    }
    const symbols = [...new Set(rows.map(normalizeImportedTicker).filter(Boolean))];
    const group = getSelectedWatchlistGroup();
    let added = 0, skipped = 0;
    for (const symbol of symbols) {
      const meta = tickerMeta(symbol);
      try {
        await fetchJsonSafe(`${API}/watchlist/${encodeURIComponent(USER.user_id)}?group=${encodeURIComponent(group)}`, {
          method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify({symbol, name: meta.name || symbol, industry: meta.industry || meta.sector || '', group_name: group})
        });
        added++;
      } catch(e) {
        skipped++;
      }
    }
    if (status) status.textContent = `Import complete for ${group}: ${added} added, ${skipped} skipped/already present.`;
    await loadWatchlist();
  } catch(err) {
    if (status) status.textContent = `Import failed: ${err.message}`;
  } finally {
    event.target.value = '';
  }
}

async function loadWatchlist(refreshGroups = true) {
  const uid = USER?.user_id;
  if (!uid) return;
  setWatchlistLoadingProgress(12, 'Preparing watchlist…', true);
  try {
    if (refreshGroups) {
      setWatchlistLoadingProgress(28, 'Loading your watchlist groups…', true);
      await loadWatchlistGroups();
    }
    const group = getSelectedWatchlistGroup();
    activeWatchlistGroup = group;
    setWatchlistLoadingProgress(48, `Fetching ${group} items…`, true);
    const items = await fetchJsonSafe(`${API}/watchlist/${encodeURIComponent(uid)}?group=${encodeURIComponent(group)}`, {cache:'no-store'});
    if (!USER || USER.user_id !== uid || activeWatchlistGroup !== group) return;
    setWatchlistLoadingProgress(78, 'Rendering watchlist table and heat map…', true);
    currentWatchlistItems = Array.isArray(items) ? items : [];
    renderWatchlistItems(currentWatchlistItems);
    setWatchlistLoadingProgress(100, 'Watchlist loaded.', true);
    setTimeout(() => setWatchlistLoadingProgress(0, '', false), 450);
    setLastUpdated(`Watchlist · ${group}`);
  } catch (err) {
    setWatchlistLoadingProgress(0, '', false);
    const tbody = document.getElementById('watchlist-tbody');
    if (tbody) tbody.innerHTML = `<tr><td colspan="8"><div class="empty-state"><i class="fa fa-triangle-exclamation"></i><p>${escapeHtml(err.message || 'Unable to load watchlist.')}</p></div></td></tr>`;
  }
}

function renderWatchlistItems(items) {
  const tbody = document.getElementById('watchlist-tbody');
  if (!tbody) return;
  if (!items.length) {
    tbody.innerHTML = `<tr><td colspan="8"><div class="empty-state"><i class="fa fa-star"></i><p>Your selected watchlist group is empty.</p></div></td></tr>`;
    renderWatchlistHeatmap([]);
    return;
  }
  tbody.innerHTML = items.map(item => `
    <tr data-watch-symbol="${escapeHtml(item.symbol)}">
      <td class="td-symbol"><a href="${tradingViewUrl(item.symbol)}" target="_blank" rel="noopener noreferrer" title="Open in TradingView">${escapeHtml(item.symbol)}</a></td>
      <td>${escapeHtml(item.name || '—')}</td>
      <td class="td-mono">₹${item.ltp || '—'}</td>
      <td class="td-mono">₹${item.day_high || '—'}</td>
      <td class="td-mono">₹${item.day_low || '—'}</td>
      <td class="td-mono">${fmtNum(item.volume || 0)}</td>
      <td><span class="badge ${(item.day_chg_pct||0) >= 0 ? 'badge-green' : 'badge-red'}">${(item.day_chg_pct||0) >= 0 ? '+' : ''}${item.day_chg_pct || 0}%</span></td>
      <td>
        <div class="action-icons">
          <button class="action-btn portfolio" title="Add to Portfolio" onclick="openWatchlistPortfolioModal('${encodeURIComponent(JSON.stringify({symbol:item.symbol, name:item.name || item.symbol, industry:item.industry || '', ltp:item.ltp || ''}))}')"><i class="fa fa-briefcase"></i></button>
          <button class="action-btn del" title="Remove" onclick="removeFromWatchlist('${encodeURIComponent(item.symbol)}', event)"><i class="fa fa-trash"></i></button>
        </div>
      </td>
    </tr>
  `).join('');
  renderWatchlistHeatmap(items);
}

function heatmapColor(value) {
  const v = Number(value || 0);
  if (v <= -75) return {bg:'#FF2C2C', color:'#fff'};
  if (v < -30) return {bg:'#FF4141', color:'#fff'};
  if (v < -5) return {bg:'#EEEEEE', color:'#444'};
  if (v <= 5) return {bg:'#DADBDD', color:'#444'};
  if (v > 75) return {bg:'#2E6F40', color:'#c8f0d8'};
  if (v > 30) return {bg:'#6CC284', color:'#0d2b18'};
  return {bg:'#EEEEEE', color:'#444'};
}

function heatmapCell(value) {
  const v = Number(value || 0);
  const c = heatmapColor(v);
  const sign = v > 0 ? '+' : '';
  return `<td class="hm-ret-cell"><span class="hm-ret-box" style="background:${c.bg};color:${c.color}">${sign}${v.toFixed(1)}%</span></td>`;
}

function renderWatchlistHeatmap(items) {
  const tbody = document.getElementById('watchlist-heatmap-tbody');
  const count = document.getElementById('watchlist-heatmap-count');
  if (!tbody) return;
  if (count) count.textContent = `${items.length} ticker${items.length === 1 ? '' : 's'} · Return % by timeframe`;
  if (!items.length) {
    tbody.innerHTML = `<tr><td colspan="6"><div class="empty-state"><i class="fa fa-fire"></i><p>No watchlist data for heat map.</p></div></td></tr>`;
    return;
  }
  tbody.innerHTML = items.map(item => `
    <tr>
      <td class="hm-ticker"><a href="${tradingViewUrl(item.symbol)}" target="_blank" rel="noopener noreferrer" title="Open in TradingView">${escapeHtml(item.symbol)}</a></td>
      <td class="hm-name">${escapeHtml(item.name || item.symbol)}</td>
      ${heatmapCell(item.ret_1d ?? item.day_chg_pct)}
      ${heatmapCell(item.ret_1w)}
      ${heatmapCell(item.ret_1m)}
      ${heatmapCell(item.ret_1y)}
    </tr>
  `).join('');
}


function openWatchlistPortfolioModal(encodedItem) {
  try {
    watchlistPortfolioItem = JSON.parse(decodeURIComponent(encodedItem));
  } catch(e) {
    watchlistPortfolioItem = null;
  }
  if (!watchlistPortfolioItem) return;

  const symbol = watchlistPortfolioItem.symbol || '';
  const name = watchlistPortfolioItem.name || symbol;
  const ltp = parseFloat(watchlistPortfolioItem.ltp || 0);
  document.getElementById('wl-port-symbol').value = name && name !== symbol ? `${symbol} — ${name}` : symbol;
  document.getElementById('wl-port-date').value = new Date().toISOString().split('T')[0];
  document.getElementById('wl-port-price').value = ltp > 0 ? ltp : '';
  document.getElementById('wl-port-qty').value = '';
  document.getElementById('wl-port-error').textContent = '';
  document.getElementById('wl-portfolio-modal').classList.add('open');
}

function closeWatchlistPortfolioModal() {
  document.getElementById('wl-portfolio-modal').classList.remove('open');
}

async function confirmWatchlistPortfolioAdd() {
  const errEl = document.getElementById('wl-port-error');
  errEl.textContent = '';
  if (!watchlistPortfolioItem || !watchlistPortfolioItem.symbol) { errEl.textContent = 'Invalid watchlist item'; return; }

  const date = document.getElementById('wl-port-date').value;
  const price = parseFloat(document.getElementById('wl-port-price').value);
  const qty = parseFloat(document.getElementById('wl-port-qty').value);
  if (!date) { errEl.textContent = 'Select a date'; return; }
  if (!price || price <= 0) { errEl.textContent = 'Enter a valid buy price'; return; }
  if (!qty || qty <= 0) { errEl.textContent = 'Enter a valid quantity'; return; }

  const btn = document.querySelector('#wl-portfolio-modal .btn-primary');
  const origText = btn.innerHTML;
  btn.innerHTML = '<div class="spinner" style="margin:0"></div>';
  btn.disabled = true;
  try {
    const r = await fetch(`${API}/holdings/${USER.user_id}`, {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        symbol: watchlistPortfolioItem.symbol,
        name: watchlistPortfolioItem.name || watchlistPortfolioItem.symbol,
        industry: watchlistPortfolioItem.industry || '',
        buy_price: price,
        qty,
        date
      })
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'Error adding holding');
    closeWatchlistPortfolioModal();
    await loadPortfolio();
  } catch(err) {
    errEl.textContent = err.message || 'Network error — try again';
  } finally {
    btn.innerHTML = origText;
    btn.disabled = false;
  }
}

async function removeFromWatchlist(symbol, ev) {
  if (ev) { ev.preventDefault(); ev.stopPropagation(); }
  const btn = ev?.currentTarget;
  const cleanSymbol = normalizeWatchlistSymbol(symbol || '');
  const compactSymbol = compactWatchlistSymbol(cleanSymbol);
  const group = getSelectedWatchlistGroup();
  if (!USER || !cleanSymbol) return;
  const previous = [...currentWatchlistItems];
  currentWatchlistItems = currentWatchlistItems.filter(item => compactWatchlistSymbol(item.symbol) !== compactSymbol);
  renderWatchlistItems(currentWatchlistItems);
  if (btn) btn.disabled = true;
  try {
    // Prefer JSON body delete so special NSE symbols such as J&KBANK are not affected by URL/path decoding.
    const resp = await fetchJsonSafe(`${API}/watchlist-delete/${encodeURIComponent(USER.user_id)}`, {
      method:'POST', cache:'no-store', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({symbol: cleanSymbol, group_name: group})
    });
    if (!resp || Number(resp.removed || 0) <= 0) {
      // Fallback to legacy path routes for older deployments/routes.
      await fetchJsonSafe(`${API}/watchlist/${encodeURIComponent(USER.user_id)}/${encodeURIComponent(cleanSymbol)}?group=${encodeURIComponent(group)}`, {method:'DELETE', cache:'no-store'});
    }
    setLastUpdated(`Watchlist item removed · ${group}`);
    // Confirm from Neon after purge so stale duplicates cannot be visually restored.
    await loadWatchlist(false);
  } catch(err) {
    try {
      await fetchJsonSafe(`${API}/watchlist/${encodeURIComponent(USER.user_id)}/${encodeURIComponent(cleanSymbol)}/delete?group=${encodeURIComponent(group)}`, {method:'POST', cache:'no-store'});
      setLastUpdated(`Watchlist item removed · ${group}`);
      await loadWatchlist(false);
    } catch(err2) {
      currentWatchlistItems = previous;
      renderWatchlistItems(currentWatchlistItems);
      alert(err2.message || 'Could not remove watchlist item');
    }
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// ANALYSIS
// ──────────────────────────────────────────────────────────────────────────────
function analysisTickerSearch(val) {
  const list = document.getElementById('analysis-ac-list');
  if (!val) { list.style.display = 'none'; return; }
  const matches = TICKERS.filter(t =>
    t.symbol.toLowerCase().includes(val.toLowerCase()) ||
    t.name.toLowerCase().includes(val.toLowerCase())
  ).slice(0, 10);
  if (!matches.length) { list.style.display = 'none'; return; }
  list.innerHTML = matches.map(t => `
    <div class="ac-item" onmousedown="event.preventDefault();selectAnalysisTicker('${t.symbol}','${t.name.replace(/'/g,"\\'")}')">
      <div class="ac-symbol">${t.symbol}</div>
      <div class="ac-name">${t.name}</div>
    </div>
  `).join('');
  list.style.display = 'block';
}

function selectAnalysisTicker(symbol, name) {
  document.getElementById('analysis-ticker').value = `${symbol} — ${name}`;
  document.getElementById('analysis-ac-list').style.display = 'none';
  analysisTicker = symbol;
}

function closeAnalysisAC() { document.getElementById('analysis-ac-list').style.display = 'none'; }

// Close dropdowns when clicking outside any autocomplete-wrap
document.addEventListener('click', function(e) {
  if (!e.target.closest('.autocomplete-wrap')) {
    closeAC(); closeWLAC(); closeAnalysisAC();
  }
});


function switchAnalysisTab(tab, btn) {
  activeAnalysisTab = tab;
  document.querySelectorAll('.analysis-tab-btn').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  document.querySelectorAll('.analysis-tab-panel').forEach(p => p.classList.remove('active'));
  const panel = document.getElementById(`analysis-tab-${tab}`);
  if (panel) panel.classList.add('active');
  if (!analysisTicker) return;
  if (tab === 'fundamentals') loadFundamentals(false);
  if (tab === 'snapshot') loadSnapshotScore(false, 'snapshot');
  if (tab === 'score') loadSnapshotScore(false, 'score');
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));
}

function fmtFinancialValue(value) {
  if (value === null || value === undefined || value === '' || Number.isNaN(value)) return '—';
  if (typeof value === 'number') {
    const abs = Math.abs(value);
    if (abs >= 1e7) return (value / 1e7).toLocaleString('en-IN', {maximumFractionDigits: 2}) + ' Cr';
    if (abs >= 1e5) return (value / 1e5).toLocaleString('en-IN', {maximumFractionDigits: 2}) + ' L';
    return value.toLocaleString('en-IN', {maximumFractionDigits: 2});
  }
  return escapeHtml(value);
}


const FUNDAMENTAL_HIGHLIGHT_ITEMS = {
  'annual income statement': ['EBITDA', 'EBIT', 'Basic EPS', 'Net Income', 'Gross Profit', 'Total Revenue'],
  'quarterly income statement': ['EBITDA', 'EBIT', 'Basic EPS', 'Net Income', 'Gross Profit', 'Total Revenue'],
  'quarterly balance sheet': ['Total Debt', 'Working Capital', 'Cash Cash Equivalents And Short Term Investments', 'Current Liabilities', 'Current Assets', 'Net PPE', 'Inventory', 'Accounts Receivable'],
  'annual cash flow': ['FreeCashFlow', 'CapitalExpenditure', 'CashDividendsPaid', 'InvestingCashFlow', 'OperatingCashFlow', 'NetIncomeFromContinuingOperations']
};

function normalizeFundamentalMetric(v) {
  return String(v || '').toLowerCase().replace(/[^a-z0-9]/g, '');
}

function isHighlightedFundamentalMetric(sectionTitle, metric) {
  const title = String(sectionTitle || '').toLowerCase();
  const sectionKey = Object.keys(FUNDAMENTAL_HIGHLIGHT_ITEMS).find(k => title.includes(k));
  if (!sectionKey) return false;
  const m = normalizeFundamentalMetric(metric);
  if (!m) return false;
  return FUNDAMENTAL_HIGHLIGHT_ITEMS[sectionKey].some(x => {
    const target = normalizeFundamentalMetric(x);
    // Highlight rows that contain the requested item or equivalent yfinance labels.
    // Examples: "Net Income Common Stockholders" contains "Net Income";
    // "Free Cash Flow" equals "FreeCashFlow" after normalization.
    return m === target || m.includes(target) || target.includes(m);
  });
}

function renderFundamentalTable(section) {
  const columns = section.columns || [];
  const rows = section.rows || [];
  if (!columns.length || !rows.length) {
    return `
      <div class="fundamental-card">
        <div class="fundamental-header">
          <div class="fundamental-title">${escapeHtml(section.title)}</div>
          <div class="fundamental-subtitle">No data returned by yfinance</div>
        </div>
        <div class="fundamental-empty">No data available for this statement.</div>
      </div>`;
  }
  return `
    <div class="fundamental-card">
      <div class="fundamental-header">
        <div class="fundamental-title">${escapeHtml(section.title)}</div>
        <div class="fundamental-subtitle">Last ${columns.length} periods</div>
      </div>
      <div class="fundamental-table-wrap">
        <table class="fundamental-table">
          <thead><tr><th>Metric</th>${columns.map(c => `<th>${escapeHtml(c)}</th>`).join('')}</tr></thead>
          <tbody>
            ${rows.map(row => `<tr class="${isHighlightedFundamentalMetric(section.title, row.metric) ? 'fundamental-highlight-row' : ''}"><td>${escapeHtml(row.metric)}</td>${columns.map(c => `<td>${fmtFinancialValue(row.values?.[c])}</td>`).join('')}</tr>`).join('')}
          </tbody>
        </table>
      </div>
    </div>`;
}

async function loadFundamentals(showLoading = true) {
  const host = document.getElementById('fundamentals-content');
  if (!host || !analysisTicker) return;
  if (showLoading) host.innerHTML = '<div class="fundamental-loading"><i class="fa fa-spinner fa-spin"></i> Loading fundamentals from yfinance...</div>';
  try {
    const r = await fetch(`${API}/fundamentals/${analysisTicker}`);
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || 'Unable to load fundamentals');
    const sections = [
      data.annual_income_statement,
      data.quarterly_income_statement,
      data.quarterly_balance_sheet,
      data.annual_cash_flow
    ].filter(Boolean);
    host.innerHTML = sections.map(renderFundamentalTable).join('') || '<div class="fundamental-empty">No fundamentals available.</div>';
  } catch (err) {
    host.innerHTML = `<div class="fundamental-empty">${escapeHtml(err.message || 'Unable to load fundamentals.')}</div>`;
  }
}


function fmtPctValue(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  const n = Number(value);
  const sign = n > 0 ? '+' : '';
  return `${sign}${n.toFixed(1)}%`;
}
function classForSigned(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return '';
  return n > 0 ? 'sn-pos' : n < 0 ? 'sn-neg' : '';
}
function badgeClass(value, good = 15, warn = 5) {
  const n = Number(value);
  if (!Number.isFinite(n)) return 'warn';
  if (n >= good) return 'good';
  if (n >= warn) return 'warn';
  return 'bad';
}
function renderSnapshot(data) {
  const host = document.getElementById('snapshot-content');
  const rows = data?.snapshot?.growth || [];
  const cf = data?.snapshot?.cashflow || {};
  host.innerHTML = `
    <div class="snapshot-section-title">Growth Metrics</div>
    <div class="snapshot-subtitle">Comparing latest quarter vs prior quarter (QoQ) and same quarter last year (YoY)</div>
    <div class="snapshot-table-wrap">
      <table class="snapshot-table">
        <thead><tr><th class="sn-label-col">Metric</th><th>Latest (Qtr)</th><th>Prior Qtr</th><th>Same Qtr LY</th><th class="sn-highlight">YoY %</th><th class="sn-highlight">QoQ %</th></tr></thead>
        <tbody>
          ${rows.length ? rows.map(r => `<tr>
            <td class="sn-label">${escapeHtml(r.metric)}</td><td>${fmtFinancialValue(r.latest)}</td><td>${fmtFinancialValue(r.prior)}</td><td>${fmtFinancialValue(r.same_ly)}</td>
            <td class="sn-highlight ${classForSigned(r.yoy_pct)}">${fmtPctValue(r.yoy_pct)}</td><td class="sn-highlight ${classForSigned(r.qoq_pct)}">${fmtPctValue(r.qoq_pct)}</td>
          </tr>`).join('') : '<tr><td colspan="6" class="sn-empty">No quarterly data available</td></tr>'}
        </tbody>
      </table>
    </div>
    <div class="snapshot-section-title" style="margin-top:28px">Profitability and Cash Flow</div>
    <div class="snapshot-subtitle">Ratios calculated from latest annual fundamentals and annual cash flow.</div>
    <div class="snapshot-card-grid">
      ${[
        ['Operating Margin', data?.snapshot?.profitability_cashflow?.operating_margin, 'Operating Income ÷ Revenue'],
        ['Net Margin', data?.snapshot?.profitability_cashflow?.net_margin, 'Net Income ÷ Revenue'],
        ['Revenue Volatility', data?.snapshot?.profitability_cashflow?.revenue_volatility, 'Std Dev of Revenue Growth'],
        ['EPS CAGR', data?.snapshot?.profitability_cashflow?.eps_cagr, '((Latest EPS ÷ Oldest EPS)^(1 ÷ Years) − 1) × 100']
      ].map(([label,val,desc]) => renderSnapshotMetricCard(label, val, desc)).join('')}
    </div>
    <div class="snapshot-section-title" style="margin-top:28px">Growth Quality</div>
    <div class="snapshot-subtitle">Current annual value compared with the prior annual period.</div>
    <div class="snapshot-card-grid">
      ${[
        ['Revenue Growth', data?.snapshot?.growth_quality?.revenue_growth, '(Current Revenue − Prior Revenue) ÷ Prior Revenue'],
        ['EBITDA Growth', data?.snapshot?.growth_quality?.ebitda_growth, '(Current EBITDA − Prior EBITDA) ÷ Prior EBITDA'],
        ['Net Income Growth', data?.snapshot?.growth_quality?.net_income_growth, '(Current Net Income − Prior Net Income) ÷ Prior Net Income'],
        ['Asset Growth', data?.snapshot?.growth_quality?.asset_growth, '(Current Total Assets − Prior Total Assets) ÷ Prior Total Assets']
      ].map(([label,val,desc]) => renderSnapshotMetricCard(label, val, desc)).join('')}
    </div>`;
}
function renderSnapshotMetricCard(label, val, desc) {
  const cls = badgeClass(val, 15, 5);
  const badgeText = cls === 'good' ? 'Strong' : cls === 'warn' ? 'Watch' : 'Weak';
  return `<div class="sn-cf-card"><div class="sn-cf-label">${escapeHtml(label)}</div><div class="sn-cf-value ${Number(val)>=0?'pos':'neg'}">${fmtPctValue(val)}</div><div class="sn-cf-desc">${escapeHtml(desc)}</div><div class="sn-cf-badge ${cls}">${badgeText}</div></div>`;
}
function renderGauge(prefix, score, maxScore) {
  const arc = document.getElementById(`${prefix}-gauge-arc`);
  const scoreEl = document.getElementById(`${prefix}-score`);
  if (!arc || !scoreEl) return;
  const frac = Math.max(0, Math.min(1, (Number(score) || 0) / maxScore));
  const color = frac >= .7 ? getComputedStyle(document.body).getPropertyValue('--green').trim() : frac >= .45 ? getComputedStyle(document.body).getPropertyValue('--orange').trim() : getComputedStyle(document.body).getPropertyValue('--red').trim();
  scoreEl.textContent = score ?? '—';
  scoreEl.style.color = color;
  arc.style.stroke = color;
  arc.style.strokeDashoffset = String(157 - 157 * frac);
}
function renderScore(data) {
  const host = document.getElementById('score-content');
  const s = data?.score || {};
  const rating = s.rating || 'HOLD';
  const ratingCls = rating === 'BUY' ? 'buy' : rating === 'SELL' ? 'sell' : 'hold';
  host.innerHTML = `
    <div class="se-hero">
      <div class="se-gauge-block">
        <svg class="se-arc-svg" viewBox="0 0 240 140"><path d="M25,120 A95,95 0 0,1 215,120" fill="none" stroke="var(--border)" stroke-width="18" stroke-linecap="round"/><path id="se-main-arc" d="M25,120 A95,95 0 0,1 215,120" fill="none" stroke="var(--accent)" stroke-width="18" stroke-linecap="round" stroke-dasharray="299" stroke-dashoffset="299"/></svg>
        <div class="se-score-center"><div class="se-score-num" id="se-score-num">${s.total ?? '—'}</div><div class="se-score-den">/100</div></div>
      </div>
      <div class="se-verdict-block"><div class="se-rating-badge ${ratingCls}">${rating}</div><div class="se-company-lbl">${escapeHtml(data.name || analysisTicker)}</div><div class="se-verdict-sub">Composite score from growth, profitability, cash flow, balance sheet, efficiency and price momentum.</div><div class="se-pill-row">${(s.pills||[]).map(p => `<span class="se-pill ${p.cls}">${escapeHtml(p.text)}</span>`).join('')}</div></div>
    </div>
    <div class="se-pillars-grid">${(s.pillars||[]).map(p => `<div class="se-pillar-card"><div class="se-pillar-icon">${p.icon}</div><div class="se-pillar-name">${escapeHtml(p.name)}</div><div class="se-pillar-weight">${p.weight}%</div><div class="se-pillar-bar-wrap"><div class="se-pillar-bar" style="width:${Math.max(0,Math.min(100,p.score||0))}%"></div></div><div class="se-pillar-score">${Math.round(p.score || 0)}</div><div class="se-pillar-items">${(p.items||[]).map(i => `<span class="${i.cls}">${escapeHtml(i.text)}</span>`).join('')}</div></div>`).join('')}</div>
    <div class="score-charts-row"><div class="chart-card"><div class="chart-title">Pillar Score Breakdown (Radar)</div><div class="chart-wrap" style="height:300px"><canvas id="chart-se-radar"></canvas></div></div><div class="chart-card"><div class="chart-title">Pillar Contribution to Total Score</div><div class="chart-wrap" style="height:300px"><canvas id="chart-se-contrib"></canvas></div></div></div>
    <div class="tbl-section-title">Metric-Level Scoring Detail</div>
    <div class="tbl-wrap"><table class="fundamental-table"><thead><tr><th>Metric</th><th>Value</th><th>Benchmark</th><th>Score</th><th>Signal</th></tr></thead><tbody>${(s.details||[]).map(d => `<tr><td>${escapeHtml(d.metric)}</td><td>${escapeHtml(d.value)}</td><td>${escapeHtml(d.benchmark)}</td><td>${escapeHtml(d.score)}</td><td>${escapeHtml(d.signal)}</td></tr>`).join('')}</tbody></table></div>
    <div class="snapshot-section-title" style="margin-top:28px">Quantitative Scoring Models</div>
    <div class="snapshot-subtitle">Scores computed from available yfinance quarterly, annual, balance sheet and cash flow data.</div>
    <div class="score-models-grid">
      ${renderScoreModelCard('canslim', 'CANSLIM Model', "William O'Neil growth-investing framework — 7 criteria", s.canslim || {}, 10)}
      ${renderScoreModelCard('piotroski', 'Piotroski F-Score', 'Joseph Piotroski financial strength framework — 9 binary criteria', s.piotroski || {}, 9)}
    </div>`;
  renderGauge('canslim', s.canslim?.score, 10);
  renderGauge('piotroski', s.piotroski?.score, 9);
  const mainArc = document.getElementById('se-main-arc');
  if (mainArc) mainArc.style.strokeDashoffset = String(299 - 299 * ((s.total || 0) / 100));
  renderScoreCharts(s.pillars || []);
}
function renderScoreModelCard(prefix, title, subtitle, model, maxScore) {
  const criteria = model.criteria || [];
  return `<div class="score-card"><div class="score-card-header"><div><div class="score-card-title">${title}</div><div class="score-card-subtitle">${subtitle}</div></div><div class="score-gauge-wrap"><svg class="score-gauge" viewBox="0 0 120 70"><path d="M10,60 A50,50 0 0,1 110,60" fill="none" stroke="var(--border)" stroke-width="10" stroke-linecap="round"/><path d="M10,60 A50,50 0 0,1 110,60" fill="none" stroke="var(--accent)" stroke-width="10" stroke-linecap="round" stroke-dasharray="157" stroke-dashoffset="157" id="${prefix}-gauge-arc"/></svg><div class="score-gauge-label"><span class="score-big" id="${prefix}-score">—</span><span class="score-denom">/${maxScore}</span></div></div></div><table class="score-detail-table"><thead><tr><th>Criterion</th><th>Metric Used</th><th>Result</th><th>Pass</th></tr></thead><tbody>${criteria.map(c => `<tr class="${c.pass?'sd-pass':'sd-fail'}"><td>${escapeHtml(c.criterion)}</td><td>${escapeHtml(c.metric)}</td><td class="sd-result">${escapeHtml(c.result)}</td><td>${c.pass?'✓':'✗'}</td></tr>`).join('') || '<tr><td colspan="4" class="sn-empty">No data</td></tr>'}</tbody></table>${prefix === 'piotroski' ? '<div class="piotroski-legend"><span class="pio-band bad">1–2 Weak</span><span class="pio-band warn">3–5 Average</span><span class="pio-band ok">6–7 Good</span><span class="pio-band good">8–9 Strong</span></div>' : ''}</div>`;
}
function renderScoreCharts(pillars) {
  const labels = pillars.map(p => p.name);
  const scores = pillars.map(p => Math.round(p.score || 0));
  const contrib = pillars.map(p => Math.round(((p.score || 0) * (p.weight || 0) / 100) * 100) / 100);
  const t = currentThemeColors();
  const rctx = document.getElementById('chart-se-radar')?.getContext('2d');
  const cctx = document.getElementById('chart-se-contrib')?.getContext('2d');
  if (scoreRadarChart) scoreRadarChart.destroy();
  if (scoreContribChart) scoreContribChart.destroy();
  if (rctx) {
    scoreRadarChart = new Chart(rctx, {
      type: 'radar',
      data: { labels, datasets: [{ label: 'Score', data: scores, borderColor: t.legend, backgroundColor: document.body.classList.contains('light') ? 'rgba(42,100,150,.16)' : 'rgba(41,121,255,.24)', pointBackgroundColor: '#2E6F40', pointBorderColor: t.legend }] },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { labels: { color: t.legend, font: { family: 'Syne', weight: '800', size: 12 } } }, tooltip: { backgroundColor: t.tooltipBg, borderColor: t.tooltipBorder, borderWidth: 1, titleColor: t.tooltipTitle, bodyColor: t.tooltipBody } },
        scales: { r: { suggestedMin: 0, suggestedMax: 100, ticks: { color: t.axis, backdropColor: 'transparent', font: { family: 'JetBrains Mono', weight: '800' } }, grid: { color: t.grid }, angleLines: { color: t.grid }, pointLabels: { color: t.legend, font: { size: 12, weight: '900', family: 'Syne' } } } }
      }
    });
  }
  if (cctx) {
    scoreContribChart = new Chart(cctx, {
      type: 'bar',
      data: { labels, datasets: [{ label: 'Contribution', data: contrib, backgroundColor: 'rgba(0,230,118,.45)', borderColor: '#00e676' }] },
      options: analysisChartOpts('')
    });
  }
}
let snapshotScoreCacheTicker = null;
let snapshotScoreCache = null;
async function loadSnapshotScore(showLoading = true, target = 'both') {
  if (!analysisTicker) return;
  const snapHost = document.getElementById('snapshot-content');
  const scoreHost = document.getElementById('score-content');
  if (showLoading && (target === 'snapshot' || target === 'both')) snapHost.innerHTML = '<div class="fundamental-loading"><i class="fa fa-spinner fa-spin"></i> Loading snapshot...</div>';
  if (showLoading && (target === 'score' || target === 'both')) scoreHost.innerHTML = '<div class="fundamental-loading"><i class="fa fa-spinner fa-spin"></i> Loading score...</div>';
  try {
    if (snapshotScoreCacheTicker !== analysisTicker || !snapshotScoreCache) {
      const r = await fetch(`${API}/analysis/snapshot-score/${analysisTicker}`);
      const data = await r.json();
      if (!r.ok) throw new Error(data.error || 'Unable to load Snapshot/Score');
      snapshotScoreCacheTicker = analysisTicker;
      snapshotScoreCache = data;
    }
    if (target === 'snapshot' || target === 'both') renderSnapshot(snapshotScoreCache);
    if (target === 'score' || target === 'both') renderScore(snapshotScoreCache);
  } catch (err) {
    if (target === 'snapshot' || target === 'both') snapHost.innerHTML = `<div class="fundamental-empty">${escapeHtml(err.message || 'Unable to load snapshot')}</div>`;
    if (target === 'score' || target === 'both') scoreHost.innerHTML = `<div class="fundamental-empty">${escapeHtml(err.message || 'Unable to load score')}</div>`;
  }
}
async function loadAnalysisSequence() {
  const wrap = document.getElementById('analysis-load-sequence');
  const fill = document.getElementById('analysis-load-fill');
  const label = document.getElementById('analysis-load-label');
  if (wrap) wrap.classList.add('show');
  try {
    if (label) label.textContent = 'Loading Technicals…'; if (fill) fill.style.width = '25%';
    await loadAnalysis();
    if (label) label.textContent = 'Loading Fundamentals…'; if (fill) fill.style.width = '50%';
    await loadFundamentals(false);
    if (label) label.textContent = 'Loading Snapshot…'; if (fill) fill.style.width = '75%';
    await loadSnapshotScore(false, 'snapshot');
    if (label) label.textContent = 'Loading Score…'; if (fill) fill.style.width = '100%';
    await loadSnapshotScore(false, 'score');
  } finally {
    setTimeout(() => { if (wrap) wrap.classList.remove('show'); if (fill) fill.style.width = '0%'; }, 650);
  }
}

function setAnalysisPeriod(period, btn) {
  analysisPeriod = period;
  document.querySelectorAll('.period-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  if (analysisTicker) loadAnalysis();
}

async function loadAnalysis() {
  if (!analysisTicker) return;
  document.getElementById('analysis-chart-title').textContent = `${analysisTicker} — Price Chart`;
  const r = await fetch(`${API}/chart/${analysisTicker}?period=${analysisPeriod}`);
  const data = await r.json();
  if (!data.length) return;

  const dates = data.map(d => d.date);
  const closes = data.map(d => d.close);
  const vols = data.map(d => d.volume);
  const ltp = closes[closes.length - 1];
  document.getElementById('analysis-ltp-badge').textContent = `LTP: ₹${fmtNum(ltp)}`;
  const rets = closes.map((c,i) => i === 0 ? 0 : parseFloat(((c - closes[i-1])/closes[i-1]*100).toFixed(2)));

  // Price chart
  const ctx1 = document.getElementById('analysis-price-chart').getContext('2d');
  if (analysisPriceChart) analysisPriceChart.destroy();
  analysisPriceChart = new Chart(ctx1, {
    type: 'line',
    data: {
      labels: dates,
      datasets: [{
        label: `${analysisTicker} Price`, data: closes,
        borderColor: '#00e8a3', backgroundColor: 'rgba(0,232,163,0.08)',
        fill: true, tension: 0.3, pointRadius: 0, pointHoverRadius: 5, borderWidth: 1.8
      }]
    },
    options: analysisChartOpts('₹')
  });

  // Volume
  const ctx2 = document.getElementById('analysis-vol-chart').getContext('2d');
  if (analysisVolChart) analysisVolChart.destroy();
  analysisVolChart = new Chart(ctx2, {
    type: 'bar',
    data: {labels: dates, datasets: [{label:'Volume',data:vols,backgroundColor:'rgba(80,140,255,0.5)',borderColor:'#508cff',borderWidth:0}]},
    options: analysisChartOpts('')
  });

  // Returns
  const ctx3 = document.getElementById('analysis-ret-chart').getContext('2d');
  if (analysisRetChart) analysisRetChart.destroy();
  analysisRetChart = new Chart(ctx3, {
    type: 'bar',
    data: {labels: dates, datasets: [{
      label:'Daily Return %', data: rets,
      backgroundColor: rets.map(v => v>=0?'rgba(0,200,150,0.6)':'rgba(255,77,109,0.6)'),
      borderColor: rets.map(v => v>=0?'#00c896':'#ff4d6d'), borderWidth: 0
    }]},
    options: analysisChartOpts('%')
  });

  if (activeAnalysisTab === 'fundamentals') loadFundamentals(false);
}


// ──────────────────────────────────────────────────────────────────────────────
// MARKETS
// ──────────────────────────────────────────────────────────────────────────────
async function loadMarkets() {
  // Index tabs + chart
  try {
    const r = await fetch(`${API}/market/indices`);
    marketIndicesData = await r.json();
    if (!marketIndicesData || !marketIndicesData.length) throw new Error('No index data');
    if (!marketIndicesData.some(i => i.key === selectedMarketIndex)) selectedMarketIndex = marketIndicesData[0].key;
    renderMarketIndexTabs();
    await renderMarketIndexChart();
  } catch(e) {
    document.getElementById('market-index-tabs').innerHTML = '<button class="market-index-tab active">Unable to load index data</button>';
  }

  // Movers
  try {
    const r = await fetch(`${API}/market/top-movers`);
    const {gainers, losers} = await r.json();
    document.getElementById('gainers-list').innerHTML = gainers.map(g => `
      <div class="mover-item">
        <div><div class="mover-symbol">${g.symbol}</div><div style="font-size:.75rem;color:var(--dim)">₹${g.ltp}</div></div>
        <div class="mover-pct green">+${g.day_chg_pct}%</div>
      </div>
    `).join('') || '<div style="padding:20px;color:var(--dim);text-align:center">No data</div>';
    document.getElementById('losers-list').innerHTML = losers.map(g => `
      <div class="mover-item">
        <div><div class="mover-symbol">${g.symbol}</div><div style="font-size:.75rem;color:var(--dim)">₹${g.ltp}</div></div>
        <div class="mover-pct red">${g.day_chg_pct}%</div>
      </div>
    `).join('') || '<div style="padding:20px;color:var(--dim);text-align:center">No data</div>';
  } catch(e) {}
}

function renderMarketIndexTabs() {
  const host = document.getElementById('market-index-tabs');
  host.innerHTML = marketIndicesData.map(idx => {
    const up = (idx.chg_pct || 0) >= 0;
    return `
      <button class="market-index-tab ${idx.key === selectedMarketIndex ? 'active' : ''}" onclick="selectMarketIndex('${idx.key}')">
        <span class="market-tab-name">${idx.name}</span>
        <span class="market-tab-value">${fmt(idx.value)}</span>
        <span class="market-tab-change ${up ? 'td-green' : 'td-red'}">${up ? '+' : ''}${fmt(idx.chg)} (${up ? '+' : ''}${idx.chg_pct}%)</span>
      </button>
    `;
  }).join('');
  updateMarketSummary();
}

function updateMarketSummary() {
  const idx = marketIndicesData.find(i => i.key === selectedMarketIndex) || marketIndicesData[0];
  if (!idx) return;
  const ret = idx[`ret_${selectedMarketPeriod}`] ?? idx.chg_pct ?? 0;
  document.getElementById('market-day-low').textContent = `₹${fmt(idx.day_low)}`;
  document.getElementById('market-day-high').textContent = `₹${fmt(idx.day_high)}`;
  const retEl = document.getElementById('market-day-return');
  retEl.textContent = `${ret >= 0 ? '+' : ''}${ret}%`;
  retEl.className = ret >= 0 ? 'td-green' : 'td-red';
}

async function selectMarketIndex(key) {
  selectedMarketIndex = key;
  renderMarketIndexTabs();
  await renderMarketIndexChart();
}

async function setMarketPeriod(period, btn) {
  selectedMarketPeriod = period;
  document.querySelectorAll('.market-period-btn').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  updateMarketSummary();
  await renderMarketIndexChart();
}

async function renderMarketIndexChart() {
  const canvas = document.getElementById('market-index-chart');
  if (!canvas) return;
  const idx = marketIndicesData.find(i => i.key === selectedMarketIndex) || marketIndicesData[0];
  if (!idx) return;
  updateMarketSummary();
  const r = await fetch(`${API}/market/index-chart/${selectedMarketIndex}?period=${selectedMarketPeriod}`);
  const payload = await r.json();
  const rows = payload.data || [];
  const labels = rows.map(x => x.label);
  const prices = rows.map(x => x.close);
  const t = currentThemeColors();
  const isUp = prices.length > 1 ? prices[prices.length - 1] >= prices[0] : (idx.chg_pct || 0) >= 0;
  const lineColor = isUp ? getComputedStyle(document.body).getPropertyValue('--green').trim() : '#ff4d6d';
  const fillColor = isUp ? 'rgba(45,106,79,0.10)' : 'rgba(255,77,109,0.11)';
  if (marketIndexChart) marketIndexChart.destroy();
  marketIndexChart = new Chart(canvas.getContext('2d'), {
    type: 'line',
    data: { labels, datasets: [{
      label: idx.name,
      data: prices,
      borderColor: lineColor,
      backgroundColor: fillColor,
      fill: true,
      pointRadius: 0,
      pointHoverRadius: 4,
      borderWidth: 2,
      tension: 0.28
    }]},
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: {mode: 'index', intersect: false},
      plugins: {
        legend: {display: false},
        tooltip: {
          backgroundColor: t.tooltipBg, borderColor: t.tooltipBorder, borderWidth: 1,
          titleColor: t.tooltipTitle, bodyColor: t.tooltipBody,
          callbacks: {label: ctx => `Price: ₹${fmtNum(ctx.parsed.y)}`}
        }
      },
      scales: {
        x: {grid: {display:false}, ticks: {color: t.axis, maxTicksLimit: 6, font:{size:10, weight:'800'}}, border: {display:false}},
        y: {position: 'right', grid: {color: t.grid}, ticks: {color: t.axis, font:{size:10, weight:'800'}, callback: v => fmt(v)}, border: {display:false}}
      }
    }
  });
}


// ──────────────────────────────────────────────────────────────────────────────
// CHART HELPERS
// ──────────────────────────────────────────────────────────────────────────────
const COLORS = ['#00c896','#508cff','#f0c040','#ff4d6d','#a78bfa','#fb923c','#34d399','#60a5fa','#f87171','#c084fc','#fbbf24','#4ade80'];

function currentThemeColors() {
  const isLight = document.body.classList.contains('light');
  return isLight ? {
    axis: '#1a1714', legend: '#1a1714', grid: 'rgba(26,23,20,0.10)', border: 'rgba(26,23,20,0.24)',
    tooltipBg: '#f5f2eb', tooltipBorder: 'rgba(42,100,150,0.30)', tooltipTitle: '#1a1714', tooltipBody: '#5c574f', donutBorder: '#f5f2eb'
  } : {
    axis: '#d9fff4', legend: '#eafff8', grid: 'rgba(255,255,255,0.06)', border: 'rgba(217,255,244,0.35)',
    tooltipBg: '#0d1821', tooltipBorder: 'rgba(0,230,118,0.45)', tooltipTitle: '#ffffff', tooltipBody: '#f1fff9', donutBorder: '#0d1821'
  };
}

function chartOpts(prefix) {
  const t = currentThemeColors();
  return {
    responsive: true, maintainAspectRatio: false,
    plugins: {
      legend: {labels: {color: t.legend, font: {size: 11, family: 'Syne', weight: '800'}}},
      tooltip: {
        backgroundColor: t.tooltipBg, borderColor: t.tooltipBorder, borderWidth: 1,
        titleColor: t.tooltipTitle, bodyColor: t.tooltipBody,
        callbacks: {label: (ctx) => `${ctx.dataset?.label ? ctx.dataset.label + ': ' : ''}${prefix}${fmtNum(ctx.parsed.y)}`}
      }
    },
    scales: {
      x: {grid: {color: t.grid}, ticks: {color: t.axis, font: {size: 10, weight: '800'}, maxRotation: 45}, border: {color: t.border}},
      y: {grid: {color: t.grid}, ticks: {color: t.axis, font: {size: 10, weight: '800'}, callback: v => `${prefix}${fmtNum(v)}`}, border: {color: t.border}}
    }
  };
}

function analysisChartOpts(prefix) {
  const opts = chartOpts(prefix);
  const t = currentThemeColors();
  if (opts.plugins?.legend?.labels) opts.plugins.legend.labels.color = t.legend;
  if (opts.plugins?.tooltip) {
    opts.plugins.tooltip.titleColor = t.tooltipTitle;
    opts.plugins.tooltip.bodyColor = t.tooltipBody;
    opts.plugins.tooltip.backgroundColor = t.tooltipBg;
    opts.plugins.tooltip.borderColor = t.tooltipBorder;
    opts.plugins.tooltip.callbacks = {
      title: (items) => items?.[0]?.label || '',
      label: (ctx) => {
        const value = ctx.parsed?.y ?? ctx.raw;
        const label = ctx.dataset?.label ? `${ctx.dataset.label}: ` : '';
        return `${label}${prefix}${fmtNum(value)}`;
      }
    };
    opts.plugins.tooltip.mode = 'index';
    opts.plugins.tooltip.intersect = false;
  }
  opts.interaction = { mode: 'index', intersect: false };
  return opts;
}

function doughnutOpts() {
  const t = currentThemeColors();
  return {
    responsive: true, maintainAspectRatio: false,
    plugins: {
      legend: {position: 'right', labels: {color: t.legend, font: {size: 11, family: 'Syne', weight: '800'}, padding: 12, boxWidth: 12}},
      tooltip: {backgroundColor: t.tooltipBg, borderColor: t.tooltipBorder, borderWidth: 1, titleColor: t.tooltipTitle, bodyColor: t.tooltipBody}
    }
  };
}

function dashboardAllocationDoughnutOpts(totalInvested) {
  const t = currentThemeColors();
  return {
    responsive: true,
    maintainAspectRatio: false,
    cutout: '58%',
    plugins: {
      legend: {
        position: 'right',
        labels: {
          color: t.legend,
          font: {size: 11, family: 'JetBrains Mono', weight: '800'},
          padding: 12,
          boxWidth: 12,
          boxHeight: 12
        }
      },
      tooltip: {
        backgroundColor: t.tooltipBg,
        borderColor: t.tooltipBorder,
        borderWidth: 1,
        titleColor: t.tooltipTitle,
        bodyColor: t.tooltipBody,
        callbacks: {
          label: (ctx) => {
            const pct = ctx.parsed || 0;
            const raw = ctx.dataset.rawValues?.[ctx.dataIndex] || 0;
            return `${ctx.label}: ${pct.toFixed(2)}% (${fmtNum(raw)} of ${fmtNum(totalInvested)})`;
          }
        }
      }
    }
  };
}



// ──────────────────────────────────────────────────────────────────────────────
// SCREENER
// ──────────────────────────────────────────────────────────────────────────────
let screenerActive = 'ema';
let screenerResults = { ema: [], volume: [], orb: [], ohl: [], priceaction: [] };
let screenerRunning = false;
let screenerAbortController = null;
let screenerInitialized = false;

function screenerEl(id) { return document.getElementById(id); }
function screenerStatus(message) { const el = screenerEl('screener-status'); if (el) el.textContent = message; }
function screenerSetProgress({percent=0, symbol='-', scanned=0, total=0, matches=0} = {}) {
  const pct = Math.max(0, Math.min(100, Number(percent) || 0));
  const fill = screenerEl('screener-progress-fill'); if (fill) fill.style.width = `${pct}%`;
  const text = screenerEl('screener-progress-text'); if (text) text.textContent = `${pct.toFixed(pct % 1 ? 1 : 0)}%`;
  const ticker = screenerEl('screener-current-ticker'); if (ticker) ticker.value = symbol || '-';
  const counts = screenerEl('screener-counts'); if (counts) counts.textContent = `Scanned: ${scanned || 0} / ${total || 0} | Matches: ${matches || 0}`;
}
function screenerTradingViewUrl(symbol, interval) {
  const clean = String(symbol || '').replace(/\.NS$/i, '').replace(/\.BO$/i, '').replace(/^NSE:/i, '');
  return `https://www.tradingview.com/chart/?symbol=${encodeURIComponent(clean)}&interval=${interval}`;
}
async function screenerInit() {
  if (screenerInitialized) return;
  screenerInitialized = true;
  screenerCreatePriceActionConditions();
  screenerWireAdvancedFilters();
  await screenerLoadSheets();
}
async function screenerLoadSheets() {
  try {
    screenerStatus('Loading scanner sheets…');
    const res = await fetch(`${API}/screener/sheets`, {cache:'no-store'});
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Unable to load sheets');
    const select = screenerEl('screener-sheet-select');
    if (!select) return;
    select.innerHTML = '';
    (data.sheets || []).forEach((name) => {
      const opt = document.createElement('option'); opt.value = name; opt.textContent = name; select.appendChild(opt);
    });
    select.onchange = screenerLoadSymbols;
    if ((data.sheets || []).length) await screenerLoadSymbols();
    else screenerStatus('No sheets found in ScannerData.xlsx.');
  } catch (err) { screenerStatus(`Failed to load sheet names: ${err.message}`); }
}
async function screenerLoadSymbols() {
  const sheet = screenerEl('screener-sheet-select')?.value;
  if (!sheet) return;
  try {
    screenerStatus(`Loading symbols from ${sheet}…`);
    const res = await fetch(`${API}/screener/symbols?sheet=${encodeURIComponent(sheet)}`, {cache:'no-store'});
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Unable to load symbols');
    const count = screenerEl('screener-symbol-count'); if (count) count.value = data.count || 0;
    screenerSetProgress({percent:0, symbol:'-', scanned:0, total:data.count || 0, matches:0});
    screenerStatus(`Loaded ${data.count || 0} symbols from ${sheet}.`);
  } catch (err) { screenerStatus(`Error: ${err.message}`); }
}
function screenerSwitchTab(name, btn) {
  screenerActive = name;
  document.querySelectorAll('.screener-tab-btn').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  document.querySelectorAll('.screener-panel').forEach(p => p.classList.remove('active'));
  const panel = screenerEl(`screener-panel-${name}`); if (panel) panel.classList.add('active');
  screenerStatus(`Active scanner: ${name.toUpperCase()}.`);
}
function screenerEmaConfig() {
  return { timeframe: screenerEl('screener-ema-timeframe').value, lookback_days: Number(screenerEl('screener-ema-lookback').value || 20), ema1: Number(screenerEl('screener-ema1').value || 9), ema2: Number(screenerEl('screener-ema2').value || 18), ema3: Number(screenerEl('screener-ema3').value || 27) };
}
function screenerVolumeConfig() {
  return { interval: screenerEl('screener-vol-interval').value, volume_threshold: Number(screenerEl('screener-vol-threshold').value || 2), price_threshold: Number(screenerEl('screener-price-threshold').value || 3), min_price: Number(screenerEl('screener-min-price').value || 100), rsi_threshold: Number(screenerEl('screener-rsi-threshold').value || 55), rsi_length: Number(screenerEl('screener-rsi-length').value || 14) };
}

function screenerOrbConfig() {
  return {
    run_orb: !!screenerEl('screener-run-orb')?.checked,
    run_ohl: !!screenerEl('screener-run-ohl')?.checked,
    start_time: screenerEl('screener-orb-start')?.value || '09:15',
    end_time: screenerEl('screener-orb-end')?.value || '10:00',
    vol_multiplier: Number(screenerEl('screener-orb-vol-mult')?.value || 1.5),
    interval: '15m'
  };
}
function screenerCreateSelect(options, selected, cls) {
  const select = document.createElement('select');
  if (cls) select.className = cls;
  options.forEach((value) => { const opt = document.createElement('option'); opt.value = value; opt.textContent = value; if (value === selected) opt.selected = true; select.appendChild(opt); });
  return select;
}
function screenerCreatePriceActionConditions() {
  const container = screenerEl('screener-pa-conditions'); if (!container || container.dataset.ready === '1') return;
  container.innerHTML = ''; container.dataset.ready = '1';
  const offsets = Array.from({ length: 31 }, (_, i) => `${-i} (${i === 0 ? 'current' : 'ago'})`);
  const periods = ['5 minute', '15 minute', '60 minute', 'Day', 'Week', 'Month'];
  const values = ['OPEN', 'HIGH', 'LOW', 'CLOSE'];
  const ops = ['<', '<=', '=', '>=', '>'];
  for (let i = 1; i <= 6; i += 1) {
    const row = document.createElement('div'); row.className = 'screener-condition-row'; row.dataset.row = String(i);
    const active = document.createElement('input'); active.type = 'checkbox'; active.className = 'screener-pa-active'; if (i === 1) active.checked = true;
    const offset1 = screenerCreateSelect(offsets, '0 (current)', 'screener-pa-offset1');
    const period1 = screenerCreateSelect(periods, 'Day', 'screener-pa-period1');
    const value1 = screenerCreateSelect(values, 'CLOSE', 'screener-pa-value1');
    const op = screenerCreateSelect(ops, '<', 'screener-pa-operator');
    const offset2 = screenerCreateSelect(offsets, '-1 (ago)', 'screener-pa-offset2');
    const period2 = screenerCreateSelect(periods, 'Month', 'screener-pa-period2');
    const value2 = screenerCreateSelect(values, 'HIGH', 'screener-pa-value2');
    [active, offset1, period1, value1, op, offset2, period2, value2].forEach((el) => row.appendChild(el));
    container.appendChild(row);
  }
}
function screenerPriceActionConfig() {
  screenerCreatePriceActionConditions();
  const conditions = [];
  document.querySelectorAll('.screener-condition-row').forEach((row) => {
    conditions.push({
      active: row.querySelector('.screener-pa-active')?.checked,
      offset1: row.querySelector('.screener-pa-offset1')?.value,
      period1: row.querySelector('.screener-pa-period1')?.value,
      value1: row.querySelector('.screener-pa-value1')?.value,
      operator: row.querySelector('.screener-pa-operator')?.value,
      offset2: row.querySelector('.screener-pa-offset2')?.value,
      period2: row.querySelector('.screener-pa-period2')?.value,
      value2: row.querySelector('.screener-pa-value2')?.value,
    });
  });
  return { conditions };
}
function screenerConfig(scanner) {
  if (scanner === 'ema') return screenerEmaConfig();
  if (scanner === 'volume') return screenerVolumeConfig();
  if (scanner === 'orb') return screenerOrbConfig();
  if (scanner === 'priceaction') return screenerPriceActionConfig();
  return {};
}
function screenerSetControls(running) {
  screenerRunning = running;
  document.querySelectorAll('.screener-run-btn').forEach(btn => btn.disabled = running);
  document.querySelectorAll('.screener-stop-btn').forEach(btn => btn.disabled = !running);
  const select = screenerEl('screener-sheet-select'); if (select) select.disabled = running;
}
function screenerStopScan() {
  if (!screenerRunning || !screenerAbortController) return;
  screenerAbortController.abort(); screenerStatus('Stopping scan…');
}
function screenerClearResults(scanner = screenerActive) {
  if (scanner === 'orb') {
    screenerResults.orb = [];
    screenerResults.ohl = [];
    const orbBody = screenerEl('screener-orb-tbody'); if (orbBody) orbBody.innerHTML = '';
    const ohlBody = screenerEl('screener-ohl-tbody'); if (ohlBody) ohlBody.innerHTML = '';
  } else if (scanner === 'priceaction') {
    screenerResults.priceaction = [];
    const paBody = screenerEl('screener-pa-tbody'); if (paBody) paBody.innerHTML = '';
  } else {
    screenerResults[scanner] = [];
    const tbody = screenerEl(scanner === 'ema' ? 'screener-ema-tbody' : 'screener-volume-tbody');
    if (tbody) tbody.innerHTML = '';
  }
  if (!screenerRunning) { screenerSetProgress({percent:0, symbol:'-', scanned:0, total:0, matches:0}); screenerStatus(`${scanner.toUpperCase()} results cleared.`); }
}
function screenerNumberValue(value) { const n = Number(String(value ?? '').replace('%','')); return Number.isFinite(n) ? n : 0; }
function screenerAppendEma(row) {
  screenerResults.ema.push(row);
  const tbody = screenerEl('screener-ema-tbody'); if (!tbody) return;
  const tr = document.createElement('tr');
  const interval = screenerEl('screener-ema-timeframe').value === 'Weekly' ? 'W' : (screenerEl('screener-ema-timeframe').value === 'Daily' ? 'D' : '60');
  if (screenerNumberValue(row.ema1_diff_pct) > 3 && screenerNumberValue(row.ema2_diff_pct) > 3 && screenerNumberValue(row.ema3_diff_pct) > 3) tr.classList.add('screener-ema-strong-row');
  tr.innerHTML = `<td>${escapeHtml(row.symbol)}</td><td>${escapeHtml(row.current_price)}</td><td>${escapeHtml(row.rsi14)}</td><td>${escapeHtml(row.ema1_diff_pct)}%</td><td>${escapeHtml(row.ema2_diff_pct)}%</td><td>${escapeHtml(row.ema3_diff_pct)}%</td><td><a class="screener-chart-link" href="${screenerTradingViewUrl(row.symbol, interval)}" target="_blank" rel="noopener">Open</a></td>`;
  tbody.appendChild(tr);
}
function screenerVolumeRowClass(pos) {
  const p = String(pos || '').trim();
  if (['Upper Band','Above Band','At Upper'].includes(p)) return 'screener-bb-upper-row';
  if (['Above Mid','Mid Band','Below Mid','At Middle'].includes(p)) return 'screener-bb-mid-row';
  if (['Lower Band','Below Band','At Lower'].includes(p)) return 'screener-bb-lower-row';
  return '';
}
function screenerAppendVolume(row) {
  screenerResults.volume.push(row);
  const tbody = screenerEl('screener-volume-tbody'); if (!tbody) return;
  const tr = document.createElement('tr');
  const rowClass = screenerVolumeRowClass(row.bb_position); if (rowClass) tr.classList.add(rowClass);
  const interval = screenerEl('screener-vol-interval').value === '1d' ? 'D' : '60';
  tr.innerHTML = `<td>${escapeHtml(row.symbol)}</td><td>${Number(row.prev_5_vol || 0).toLocaleString()}</td><td>${Number(row.curr_5_vol || 0).toLocaleString()}</td><td>${escapeHtml(row.current_price)}</td><td>${escapeHtml(row.volume_ratio)}</td><td>${escapeHtml(row.price_change_pct)}%</td><td>${escapeHtml(row.rsi)}</td><td>${escapeHtml(row.bb_position)}</td><td><a class="screener-chart-link" href="${screenerTradingViewUrl(row.symbol, interval)}" target="_blank" rel="noopener">Open</a></td>`;
  tbody.appendChild(tr);
}

function screenerCompare(value, op, threshold) {
  if (threshold === '' || threshold === null || threshold === undefined) return true;
  const v = Number(value); const t = Number(threshold);
  if (!Number.isFinite(v) || !Number.isFinite(t)) return true;
  if (op === '>') return v > t; if (op === '<') return v < t; if (op === '>=') return v >= t; if (op === '<=') return v <= t; if (op === '=') return Math.abs(v - t) < 0.001;
  return true;
}
function screenerPassesOrbFilters(row) {
  const signalFilter = screenerEl('screener-orb-signal-filter')?.value || 'All';
  const openFilter = screenerEl('screener-orb-openhl-filter')?.value || 'All';
  const signal = row.signal || row.action_type || '-';
  if (signalFilter !== 'All' && signalFilter !== signal) return false;
  if (openFilter !== 'All' && openFilter !== row.open_hl) return false;
  if (!screenerCompare(row.rsi14, screenerEl('screener-orb-rsi-op')?.value || '>', screenerEl('screener-orb-rsi-value')?.value)) return false;
  if (row.result_type === 'orb' && !screenerCompare(row.vol_x, screenerEl('screener-orb-volx-op')?.value || '>', screenerEl('screener-orb-volx-value')?.value)) return false;
  return true;
}
function screenerAppendOrbRowToDom(row, push = true) {
  if (push) screenerResults.orb.push(row);
  if (!screenerPassesOrbFilters(row)) return;
  const tbody = screenerEl('screener-orb-tbody'); if (!tbody) return;
  const tr = document.createElement('tr'); tr.classList.add(row.signal === 'Bullish' ? 'screener-bullish-row' : 'screener-bearish-row');
  tr.innerHTML = `<td>${escapeHtml(row.symbol)}</td><td>${escapeHtml(row.signal)}</td><td>${escapeHtml(row.breakout_level)}</td><td>${escapeHtml(row.ltp)}</td><td>${escapeHtml(row.open_hl)}</td><td>${escapeHtml(row.change_pct)}%</td><td>${escapeHtml(row.rsi14 ?? '')}</td><td>${escapeHtml(row.vol_x)}x</td><td><a class="screener-chart-link" href="${screenerTradingViewUrl(row.symbol, '15')}" target="_blank" rel="noopener">Open</a></td>`;
  tbody.appendChild(tr);
}
function screenerAppendOhlRowToDom(row, push = true) {
  if (push) screenerResults.ohl.push(row);
  if (!screenerPassesOrbFilters(row)) return;
  const tbody = screenerEl('screener-ohl-tbody'); if (!tbody) return;
  const tr = document.createElement('tr'); tr.classList.add(row.action_type === 'Bullish' ? 'screener-bullish-row' : 'screener-bearish-row');
  tr.innerHTML = `<td>${escapeHtml(row.symbol)}</td><td>${escapeHtml(row.ltp)}</td><td>${escapeHtml(row.change_pct)}%</td><td>${escapeHtml(row.rsi14 ?? '')}</td><td>${escapeHtml(row.open_hl)}</td><td>${escapeHtml(row.action_type)}</td><td><a class="screener-chart-link" href="${screenerTradingViewUrl(row.symbol, 'D')}" target="_blank" rel="noopener">Open</a></td>`;
  tbody.appendChild(tr);
}
function screenerAppendOrb(row) { if (row.result_type === 'ohl') screenerAppendOhlRowToDom(row, true); else screenerAppendOrbRowToDom(row, true); }
function screenerRedrawOrbTables() {
  const orbBody = screenerEl('screener-orb-tbody'); if (orbBody) orbBody.innerHTML = '';
  const ohlBody = screenerEl('screener-ohl-tbody'); if (ohlBody) ohlBody.innerHTML = '';
  (screenerResults.orb || []).forEach((row) => screenerAppendOrbRowToDom(row, false));
  (screenerResults.ohl || []).forEach((row) => screenerAppendOhlRowToDom(row, false));
}
function screenerPaRowClass(bbPosition) {
  const pos = String(bbPosition || '').trim();
  if (['Above Band','Upper Zone','Above Mid','At Upper'].includes(pos)) return 'screener-pa-upper-row';
  if (['At Middle'].includes(pos)) return 'screener-pa-mid-row';
  if (['Below Mid','Lower Zone','Below Band','At Lower'].includes(pos)) return 'screener-pa-lower-row';
  return '';
}
function screenerPassesPriceActionFilters(row) {
  const rsiOp = screenerEl('screener-pa-rsi-op')?.value || 'Any';
  const rsiVal = Number(screenerEl('screener-pa-rsi-value')?.value || 0);
  const bbFilter = screenerEl('screener-pa-bb-filter')?.value || 'Any';
  const chgOp = screenerEl('screener-pa-change-op')?.value || 'Any';
  const chgVal = Number(screenerEl('screener-pa-change-value')?.value || 0);
  if (rsiOp === 'Greater Than' && !(Number(row.rsi_val) > rsiVal)) return false;
  if (rsiOp === 'Less Than' && !(Number(row.rsi_val) < rsiVal)) return false;
  if (bbFilter !== 'Any' && row.bb_pos !== bbFilter) return false;
  if (chgOp !== 'Any') {
    if (chgOp === '>' && !(Number(row.change_pct) > chgVal)) return false;
    if (chgOp === '<' && !(Number(row.change_pct) < chgVal)) return false;
    if (chgOp === '>=' && !(Number(row.change_pct) >= chgVal)) return false;
    if (chgOp === '<=' && !(Number(row.change_pct) <= chgVal)) return false;
  }
  return true;
}
function screenerAppendPriceAction(row, push = true) {
  if (push) screenerResults.priceaction.push(row);
  if (!screenerPassesPriceActionFilters(row)) return;
  const tbody = screenerEl('screener-pa-tbody'); if (!tbody) return;
  const tr = document.createElement('tr'); const cls = screenerPaRowClass(row.bb_pos); if (cls) tr.classList.add(cls);
  tr.innerHTML = `<td>${escapeHtml(row.symbol)}</td><td>${escapeHtml(row.ltp)}</td><td>${escapeHtml(row.change_pct)}%</td><td>${escapeHtml(row.rsi_val ?? '')}</td><td>${escapeHtml(row.bb_pos)}</td><td>${escapeHtml(row.d_close_pct)}%</td><td>${escapeHtml(row.w_close_pct)}%</td><td>${escapeHtml(row.m_close_pct)}%</td><td>${Number(row.volume || 0).toLocaleString()}</td><td>${row.vol10day_high ? 'True' : 'False'}</td><td><a class="screener-chart-link" href="${screenerTradingViewUrl(row.symbol, '60')}" target="_blank" rel="noopener">Open</a></td>`;
  tbody.appendChild(tr);
}
function screenerRedrawPriceActionTable() {
  const tbody = screenerEl('screener-pa-tbody'); if (tbody) tbody.innerHTML = '';
  (screenerResults.priceaction || []).forEach((row) => screenerAppendPriceAction(row, false));
}
function screenerAppendResult(scanner, row) {
  if (scanner === 'ema') return screenerAppendEma(row);
  if (scanner === 'volume') return screenerAppendVolume(row);
  if (scanner === 'orb') return screenerAppendOrb(row);
  if (scanner === 'priceaction') return screenerAppendPriceAction(row, true);
}
function screenerSwitchOrbSubtab(button) {
  document.querySelectorAll('.screener-orb-sub-tab').forEach(btn => btn.classList.remove('active'));
  document.querySelectorAll('.screener-orb-sub-pane').forEach(pane => pane.classList.remove('active'));
  button.classList.add('active');
  const pane = screenerEl(button.dataset.orbSubtab); if (pane) pane.classList.add('active');
}
function screenerWireAdvancedFilters() {
  ['screener-orb-signal-filter','screener-orb-openhl-filter','screener-orb-rsi-op','screener-orb-rsi-value','screener-orb-volx-op','screener-orb-volx-value'].forEach((id) => { const el = screenerEl(id); if (el && !el.dataset.wired) { el.dataset.wired='1'; el.addEventListener(el.tagName === 'SELECT' ? 'change' : 'input', screenerRedrawOrbTables); } });
  ['screener-pa-rsi-op','screener-pa-rsi-value','screener-pa-bb-filter','screener-pa-change-op','screener-pa-change-value'].forEach((id) => { const el = screenerEl(id); if (el && !el.dataset.wired) { el.dataset.wired='1'; el.addEventListener(el.tagName === 'SELECT' ? 'change' : 'input', screenerRedrawPriceActionTable); } });
}
async function screenerRunScan(scanner) {
  if (screenerRunning) return;
  const sheet = screenerEl('screener-sheet-select')?.value;
  if (!sheet) { screenerStatus('Select a sheet first.'); return; }
  const config = screenerConfig(scanner);
  screenerClearResults(scanner); screenerSetProgress({percent:0, symbol:'-', scanned:0, total:0, matches:0}); screenerStatus(`Starting ${scanner.toUpperCase()} scan on ${sheet}…`);
  screenerAbortController = new AbortController(); screenerSetControls(true);
  try {
    const res = await fetch(`${API}/screener/scan_stream/${scanner}`, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({sheet, config}), signal:screenerAbortController.signal });
    if (!res.ok) { let data = {}; try { data = await res.json(); } catch(_) {} throw new Error(data.error || 'Scan failed'); }
    const reader = res.body.getReader(); const decoder = new TextDecoder(); let buffer = '';
    while (true) {
      const {value, done} = await reader.read(); if (done) break;
      buffer += decoder.decode(value, {stream:true});
      const lines = buffer.split('\n'); buffer = lines.pop() || '';
      for (const line of lines) {
        if (!line.trim()) continue;
        const event = JSON.parse(line);
        if (event.type === 'start') { screenerSetProgress(event); screenerStatus(`Scanning ${event.total} symbols from ${event.sheet}…`); }
        else if (event.type === 'progress') { screenerSetProgress(event); screenerStatus(event.message || `Scanning ${event.symbol}…`); }
        else if (event.type === 'result') { screenerAppendResult(scanner, event.row); screenerSetProgress(event); screenerStatus(`Match found: ${event.row.symbol}. Continuing scan…`); }
        else if (event.type === 'done') { screenerSetProgress(event); const errText = event.errors?.length ? ` ${event.errors.length} symbol errors captured.` : ''; screenerStatus(`${event.message}${errText}`); setLastUpdated('Screener'); }
      }
    }
  } catch (err) {
    if (err.name === 'AbortError') screenerStatus('Scan stopped by user. Existing results are kept.');
    else screenerStatus(`Error: ${err.message}`);
  } finally { screenerAbortController = null; screenerSetControls(false); }
}
function screenerExportCsv() {
  const rows = screenerActive === 'orb' ? [...(screenerResults.orb || []), ...(screenerResults.ohl || [])] : (screenerResults[screenerActive] || []);
  if (!rows.length) { screenerStatus('No active results to export.'); return; }
  const headers = Object.keys(rows[0]);
  const csv = [headers.join(',')].concat(rows.map(row => headers.map(h => JSON.stringify(row[h] ?? '')).join(','))).join('\n');
  const blob = new Blob([csv], {type:'text/csv;charset=utf-8;'});
  const url = URL.createObjectURL(blob); const a = document.createElement('a');
  a.href = url; a.download = `${screenerActive}_scanner_results.csv`; a.click(); URL.revokeObjectURL(url);
}


// ──────────────────────────────────────────────────────────────────────────────
// UTILS
// ──────────────────────────────────────────────────────────────────────────────
function tradingViewUrl(symbol) {
  const raw = String(symbol || '').trim();
  const clean = raw.replace(/\.NS$/i, '').replace(/\.BO$/i, '');
  return `https://www.tradingview.com/chart/?symbol=${encodeURIComponent(clean)}&interval=60`;
}

function escapeHtml(value) { return String(value ?? '').replace(/[&<>'"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','\"':'&quot;'}[ch] || ch)); }
function fmt(n) { return typeof n === 'number' ? n.toLocaleString('en-IN', {maximumFractionDigits:2}) : n; }
function fmtNum(n) {
  if (typeof n !== 'number') return n;
  if (Math.abs(n) >= 1e7) return (n/1e7).toFixed(2)+'Cr';
  if (Math.abs(n) >= 1e5) return (n/1e5).toFixed(2)+'L';
  return n.toLocaleString('en-IN', {maximumFractionDigits:2});
}

// Close modals on overlay click
document.querySelectorAll('.modal-overlay').forEach(m => m.addEventListener('click', function(e) {
  if (e.target === this) { this.classList.remove('open'); }
}));
