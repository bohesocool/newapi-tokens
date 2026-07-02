const API = '';
let hourlyChart = null;
let chartType = 'bar';
let pollTimer = null;
let sysTimer = null;
const SYS_REFRESH_MS = 10000;  // CPU/内存独立刷新，10s 足够「接近实时」
let _dashInFlight = false, _csInFlight = false, _sysInFlight = false;
let lastHourlyData = null;
let lastHistory = [];
let currentApiKey = '';
let trendChart = null;

// 渠道卡片 / 收益分布共用的循环调色板
const PALETTE = ['#3b82f6','#10b981','#f59e0b','#ef4444','#8b5cf6','#ec4899','#14b8a6','#f97316'];

// 读取主题相关 CSS 变量，供 Chart.js 配色；切换主题后随轮询自然刷新。
function chartTheme() {
  const isLight = document.documentElement.getAttribute('data-theme') === 'light';
  return {
    isLight,
    axisColor: cssVar('--text2') || '#6b7a99',
    gridColor: isLight ? 'rgba(15,23,42,0.06)' : 'rgba(255,255,255,0.05)',
    tipBg: cssVar('--card'),
    tipBorder: cssVar('--border'),
    tipText: cssVar('--text'),
  };
}

function toast(msg) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.add('show');
  setTimeout(() => el.classList.remove('show'), 2000);
}

// Wrap fetch: on 401 (session expired) bounce to login.
async function apiFetch(path, opts) {
  const r = await fetch(API + path, opts);
  if (r.status === 401) { location.href = '/login'; throw new Error('unauthorized'); }
  return r;
}

function cssVar(n) { return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }

// ── Theme ──
function applyTheme(t) {
  document.documentElement.setAttribute('data-theme', t);
  const b = document.getElementById('themeBtn');
  if (b) b.textContent = t === 'light' ? '☀️' : '🌙';
  if (lastHourlyData) renderChart(lastHourlyData);
}
function toggleTheme() {
  const cur = document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
  const next = cur === 'light' ? 'dark' : 'light';
  localStorage.setItem('theme', next);
  applyTheme(next);
}

// ── Refresh interval ──
function currentRefreshMs() {
  const v = localStorage.getItem('refreshMs');
  return v === null ? 30000 : parseInt(v);
}
function applyRefresh() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  if (sysTimer) { clearInterval(sysTimer); sysTimer = null; }
  const ms = currentRefreshMs();
  if (ms > 0) {
    pollTimer = setInterval(() => { loadDashboard(); loadChannelStatus(); }, ms);
    sysTimer = setInterval(loadSystem, SYS_REFRESH_MS);
  }
}
function changeRefresh() {
  localStorage.setItem('refreshMs', document.getElementById('refreshSel').value);
  applyRefresh();
}
// 标签页隐藏时暂停轮询，避免后台标签页空跑查询；可见时立刻补一次再恢复定时器
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    if (sysTimer) { clearInterval(sysTimer); sysTimer = null; }
  } else {
    loadDashboard(); loadChannelStatus(); loadSystem();
    applyRefresh();
  }
});

async function logout() {
  try { await fetch('/api/logout', { method: 'POST' }); } catch(e) {}
  location.href = '/login';
}

function switchTab(tab) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  event.target.classList.add('active');
  document.querySelectorAll('[id^="tab-"]').forEach(el => el.style.display = 'none');
  document.getElementById('tab-' + tab).style.display = '';
  if (tab === 'channels') loadChannels();
  if (tab === 'history') loadHistory();
  if (tab === 'cost') loadCost();
  if (tab === 'settings') loadSettings();
}

function switchChart(type, btn) {
  chartType = type;
  document.querySelectorAll('.chart-toggle button').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  if (lastHourlyData) renderChart(lastHourlyData);
}

function fmtMoney(v) { return '$' + (v || 0).toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2}); }
function fmtNum(n) {
  n = Math.round(n || 0);
  if (n >= 1e9) return (n/1e9).toFixed(2) + 'B';
  if (n >= 1e6) return (n/1e6).toFixed(2) + 'M';
  if (n >= 1e3) return (n/1e3).toFixed(1) + 'K';
  return n.toString();
}
function fmtGB(bytes) { return (bytes / 1073741824).toFixed(1) + ' GB'; }

function usageColor(pct) {
  if (pct >= 90) return 'var(--accent4)';
  if (pct >= 70) return 'var(--accent3)';
  return 'var(--accent2)';
}

async function loadSystem() {
  if (_sysInFlight) return;
  _sysInFlight = true;
  try {
    const resp = await apiFetch('/api/system');
    const d = await resp.json();
    const cpu = document.getElementById('sysCpu');
    const cpuBar = document.getElementById('sysCpuBar');
    cpu.textContent = d.cpu_percent + '%';
    cpuBar.style.width = Math.min(100, d.cpu_percent) + '%';
    cpuBar.style.background = usageColor(d.cpu_percent);

    const mem = document.getElementById('sysMem');
    const memBar = document.getElementById('sysMemBar');
    mem.textContent = d.mem_percent + '%';
    document.getElementById('sysMemDetail').textContent = fmtGB(d.mem_used) + ' / ' + fmtGB(d.mem_total);
    memBar.style.width = Math.min(100, d.mem_percent) + '%';
    memBar.style.background = usageColor(d.mem_percent);
  } catch (e) { /* keep last values on transient errors */ }
  finally { _sysInFlight = false; }
}

// ── CSV export ──
function downloadCSV(filename, rows) {
  const csv = rows.map(r => r.map(c => {
    const s = String(c == null ? '' : c);
    return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  }).join(',')).join('\n');
  const blob = new Blob(['﻿' + csv], {type: 'text/csv;charset=utf-8'});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = filename; a.click();
  URL.revokeObjectURL(url);
}
function exportDashboardCSV() {
  if (!lastHourlyData) { toast('暂无数据'); return; }
  const rows = [['渠道ID','名称','调用次数','消费USD','实付USD','prompt_tokens','completion_tokens']];
  Object.entries(lastHourlyData.channels).forEach(([id, c]) => {
    rows.push([id, c.name || '', c.calls || 0, (c.usd||0).toFixed(4), (c.real_cost||0).toFixed(4), c.prompt_tokens||0, c.completion_tokens||0]);
  });
  downloadCSV('今日渠道_' + lastHourlyData.today_date + '.csv', rows);
}
function exportHistoryCSV() {
  if (!lastHistory.length) { toast('暂无历史'); return; }
  const rows = [['日期','调用次数','消费USD','实付USD']];
  lastHistory.forEach(it => rows.push([it.date, it.total_calls, (it.total_usd||0).toFixed(2), (it.total_real||0).toFixed(2)]));
  downloadCSV('历史日报.csv', rows);
}

// ── Dashboard ──
async function loadDashboard() {
  if (_dashInFlight) return;
  _dashInFlight = true;
  try {
    const resp = await apiFetch('/api/hourly');
    const data = await resp.json();
    lastHourlyData = data;

    const eb = document.getElementById('errBanner');
    if (eb) {
      if (data.error) { eb.textContent = '⚠️ ' + data.error; eb.style.display = ''; }
      else { eb.style.display = 'none'; }
    }

    document.getElementById('liveTime').textContent = data.now;
    document.getElementById('todayReal').textContent = fmtMoney(data.today_total.total_real);
    document.getElementById('todayUsd').textContent = fmtMoney(data.today_total.total_usd);
    document.getElementById('todayCalls').textContent = data.today_total.total_calls.toLocaleString() + ' 次调用';
    document.getElementById('curHourReal').textContent = fmtMoney(data.current_hour.total_real);
    document.getElementById('curHourRange').textContent = data.current_hour.start + ' → ' + data.current_hour.end;

    let totalTok = 0;
    Object.values(data.channels).forEach(c => { totalTok += (c.prompt_tokens||0) + (c.completion_tokens||0); });
    document.getElementById('todayTokens').textContent = fmtNum(totalTok) + ' tok';

    const minutes = (data.today_minutes != null) ? data.today_minutes : (new Date().getHours() * 60 + new Date().getMinutes());
    document.getElementById('perMin').textContent = fmtMoney(data.today_total.total_real / Math.max(1, minutes));

    // Render chart
    renderChart(data);

    // Channel breakdown
    const breakdown = document.getElementById('channelBreakdown');
    breakdown.innerHTML = '';
    const sortedChannels = Object.entries(data.channels)
      .map(([k,v]) => ({id:k, ...v}))
      .sort((a,b) => (b.real_cost||0) - (a.real_cost||0));

    sortedChannels.forEach((ch, i) => {
      const row = document.createElement('div');
      row.className = 'ch-row';
      const errTotal = (ch.calls||0) + (ch.errors||0);
      const errRate = errTotal > 0 ? (ch.errors||0) / errTotal : 0;
      const errColor = errRate >= 0.10 ? '#ef4444' : 'var(--text2)';
      const errHtml = `<span style="color:${errColor}"> · 错误率 ${(errRate*100).toFixed(1)}% (${(ch.errors||0).toLocaleString()}失败)</span>`;
      const c = PALETTE[i % PALETTE.length];
      row.innerHTML = `
        <div class="ch-badge" style="background:${c}20;color:${c}">${ch.id}</div>
        <div class="ch-info">
          <div class="ch-name">${escapeHtml(ch.name || '渠道 ' + ch.id)}</div>
          <div class="ch-stats">${(ch.calls||0).toLocaleString()} 次 · ${fmtNum(ch.prompt_tokens||0)}+${fmtNum(ch.completion_tokens||0)} tok${errHtml}</div>
        </div>
        <div class="ch-amount">
          <div class="real">${fmtMoney(ch.real_cost||0)}</div>
          <div class="usd">消费 ${fmtMoney(ch.usd||0)}</div>
        </div>
      `;
      breakdown.appendChild(row);
    });
  } catch(e) {
    console.error('Dashboard error:', e);
  } finally { _dashInFlight = false; }
}

function renderChart(data) {
  const ctx = document.getElementById('hourlyChart');
  if (!ctx) return;

  const labels = data.hourly.map(h => h.hour + ':00');
  const realValues = data.hourly.map(h => h.real_cost || 0);

  const isBar = chartType === 'bar';
  const th = chartTheme();

  // 已存在同类型图表 → 仅更新数据与主题色，避免每轮轮询都 destroy + new。
  // 切换柱/线类型时才重建（Chart.js 不能在实例上改 type）。
  if (hourlyChart && hourlyChart._rtType === chartType) {
    hourlyChart.data.labels = labels;
    hourlyChart.data.datasets[0].data = realValues;
    hourlyChart.options.scales.x.ticks.color = th.axisColor;
    hourlyChart.options.scales.y.ticks.color = th.axisColor;
    hourlyChart.options.scales.x.grid.color = th.gridColor;
    hourlyChart.options.scales.y.grid.color = th.gridColor;
    hourlyChart.options.plugins.legend.labels.color = th.axisColor;
    hourlyChart.options.plugins.tooltip.backgroundColor = th.tipBg;
    hourlyChart.options.plugins.tooltip.borderColor = th.tipBorder;
    hourlyChart.options.plugins.tooltip.titleColor = th.tipText;
    hourlyChart.options.plugins.tooltip.bodyColor = th.tipText;
    hourlyChart.update();
    return;
  }
  if (hourlyChart) hourlyChart.destroy();

  hourlyChart = new Chart(ctx, {
    type: isBar ? 'bar' : 'line',
    data: {
      labels: labels,
      datasets: [
        {
          label: '实付 (USD)',
          data: realValues,
          backgroundColor: isBar ? 'rgba(16,185,129,0.6)' : 'rgba(16,185,129,0.1)',
          borderColor: '#10b981',
          borderWidth: isBar ? 0 : 2,
          borderRadius: isBar ? 4 : 0,
          fill: !isBar,
          tension: 0.4,
          pointRadius: isBar ? 0 : 3,
          pointBackgroundColor: '#10b981',
          pointBorderColor: '#10b981',
        }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: {
          display: true,
          position: 'top',
          labels: { color: th.axisColor, font: { size: 11 } }
        },
        tooltip: {
          backgroundColor: th.tipBg,
          borderColor: th.tipBorder,
          borderWidth: 1,
          titleColor: th.tipText,
          bodyColor: th.tipText,
          padding: 12,
          callbacks: {
            label: function(ctx) {
              return ctx.dataset.label + ': ' + fmtMoney(ctx.parsed.y);
            }
          }
        }
      },
      scales: {
        x: {
          grid: { color: th.gridColor },
          ticks: { color: th.axisColor, font: { size: 10 } }
        },
        y: {
          grid: { color: th.gridColor },
          ticks: {
            color: th.axisColor,
            font: { size: 11 },
            callback: v => '$' + v
          }
        }
      }
    }
  });
  hourlyChart._rtType = chartType;
}

// ── Trend ──
async function loadTrend() {
  try {
    const days = document.getElementById('trendDays').value;
    const resp = await apiFetch('/api/trend?days=' + days);
    renderTrend(await resp.json());
  } catch(e) { console.error('Trend error:', e); }
}
async function loadTrendRange() {
  const s = document.getElementById('trendStart').value;
  const e = document.getElementById('trendEnd').value;
  if (!s || !e) { toast('请选择开始和结束日期'); return; }
  try {
    const resp = await apiFetch('/api/trend?start=' + s + '&end=' + e);
    renderTrend(await resp.json());
  } catch(err) { console.error(err); toast('查询失败'); }
}
function renderTrend(series) {
  const ctx = document.getElementById('trendChart');
  if (!ctx) return;
  const labels = series.map(d => d.date.slice(5));
  const real = series.map(d => d.total_real || 0);
  if (trendChart) trendChart.destroy();
  const th = chartTheme();
  trendChart = new Chart(ctx, {
    type: 'line',
    data: { labels, datasets: [
      { label: '实付 (USD)', data: real, borderColor: '#10b981', backgroundColor: 'rgba(16,185,129,0.1)', borderWidth: 2, tension: 0.3, fill: true, pointRadius: 2 },
    ]},
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { labels: { color: th.axisColor, font: { size: 11 } } },
        tooltip: { callbacks: { label: c => c.dataset.label + ': ' + fmtMoney(c.parsed.y) } }
      },
      scales: {
        x: { grid: { color: th.gridColor }, ticks: { color: th.axisColor, font: { size: 10 } } },
        y: { grid: { color: th.gridColor }, ticks: { color: th.axisColor, callback: v => '$' + v } }
      }
    }
  });
  const tr = real.reduce((a, b) => a + b, 0);
  document.getElementById('trendSummary').textContent =
    `区间合计：实付 ${fmtMoney(tr)} · 共 ${series.length} 天`;
}

// ── Channel status monitor ──
let lastBalances = {};
let controlConfigured = false;   // new-api 启停控制是否已配置
let lastControlStatus = {};      // {channel_id: enabled_bool}
async function loadChannelStatus() {
  if (_csInFlight) return;
  _csInFlight = true;
  try {
    const [statusR, balR, ctrlR] = await Promise.all([
      apiFetch('/api/channel-status?minutes=60'),
      apiFetch('/api/channels/balances').catch(() => null),
      apiFetch('/api/channels/control-status').catch(() => null),
    ]);
    if (balR) { try { lastBalances = await balR.json(); } catch(e) {} }
    if (ctrlR) {
      try {
        const c = await ctrlR.json();
        controlConfigured = !!c.configured;
        lastControlStatus = c.statuses || {};
      } catch(e) {}
    }
    renderChannelStatus(await statusR.json());
  } catch(e) { console.error('Channel status error:', e); }
  finally { _csInFlight = false; }
}
async function refreshCardBalance(id, btn) {
  btn.disabled = true; btn.textContent = '…';
  try {
    const r = await (await apiFetch('/api/channels/' + id + '/balance/refresh', {method:'POST'})).json();
    lastBalances[id] = { ...(lastBalances[id] || {}), configured: true,
      value: r.value, error: r.error || '', checked_at: r.checked_at };
    toast(r.error ? ('查询失败: ' + r.error) : ('余额 $' + Number(r.value).toFixed(2)));
  } catch(e) { toast('刷新失败'); }
  if (lastStatusData) renderChannelStatus(lastStatusData);
}
async function toggleChannelStatus(id, enable, btn) {
  if (!controlConfigured) { toast('请先在设置页配置 new-api 控制凭据'); return; }
  btn.disabled = true; const orig = btn.textContent; btn.textContent = '…';
  try {
    const resp = await apiFetch('/api/channels/' + id + '/status', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({status: enable ? 1 : 2}),
    });
    const r = await resp.json();
    if (!resp.ok || !r.ok) {
      // 后端调用 new-api 失败 —— 如实报错，不再假报“已启用/已暂停”
      toast(r.detail || ('操作失败 (HTTP ' + resp.status + ')'));
      return;
    }
    lastControlStatus[id] = !!r.enabled;
    toast(enable ? '渠道已启用' : '渠道已暂停');
  } catch(e) {
    toast('操作失败');
  } finally {
    btn.disabled = false; btn.textContent = orig;
  }
  if (lastStatusData) renderChannelStatus(lastStatusData);
}
let lastStatusData = null;  // 缓存最近一份渠道状态，供启停后局部刷新重渲染
// ── 渠道卡片拖拽排序：仅顶部标题区(.cs-top)可拖，顺序持久化到 localStorage ──
let _csDragId = null, _csDragFromTop = false;  // mousedown 时记录按点是否落在标题区(.cs-top)
function getCsOrder() { try { return JSON.parse(localStorage.getItem('csOrder') || '[]'); } catch(e) { return []; } }
function saveCsOrder(arr) { localStorage.setItem('csOrder', JSON.stringify(arr)); }
function resetCsOrder() { localStorage.removeItem('csOrder'); if (lastStatusData) renderChannelStatus(lastStatusData); }
// 找指针"之后"的第一张卡片（文档顺序），作为插入锚点：dragging 将插到它前面；null 表示插末尾。
// 二维 grid 判据：指针在卡片上方整行 (y<top)，或与卡片同行 (y<bottom) 且在其左半 (x<中线)。
function _csAfterEl(wrap, x, y) {
  for (const c of wrap.querySelectorAll('.cs-card:not(.dragging)')) {
    const b = c.getBoundingClientRect();
    if (y < b.top || (y < b.bottom && x < b.left + b.width / 2)) return c;
  }
  return null;
}
function _csClearDrag(wrap) {
  wrap.querySelectorAll('.cs-card').forEach(c => c.classList.remove('dragging', 'drag-over'));
  _csDragId = null;
  _csDragFromTop = false;
}
// 事件委托到稳定的 #channelStatus 容器（grid 每次刷新都会重建，不能绑在 grid 上）
function bindCsDnD(wrap) {
  if (wrap._dndBound) return;
  wrap._dndBound = true;
  // dragstart 的 e.target 总是被拖卡片本身(.cs-card)，无法据此判断按点；用 mousedown 记录是否按在标题区(.cs-top)
  wrap.addEventListener('mousedown', e => {
    _csDragFromTop = !!(e.target.closest('.cs-top') && e.target.closest('.cs-card'));
  });
  wrap.addEventListener('dragstart', e => {
    const card = e.target.closest('.cs-card');
    if (!card) return;
    if (!_csDragFromTop) { e.preventDefault(); return; }  // 仅从标题区发起的拖拽才放行
    _csDragId = card.dataset.id;
    card.classList.add('dragging');
    e.dataTransfer.effectAllowed = 'move';
    try { e.dataTransfer.setData('text/plain', _csDragId); } catch(_) {}
  });
  wrap.addEventListener('dragover', e => {
    if (!_csDragId) return;
    e.preventDefault();
    const dragging = wrap.querySelector('.cs-card.dragging');
    if (!dragging) return;
    const after = _csAfterEl(wrap, e.clientX, e.clientY);
    wrap.querySelectorAll('.cs-card.drag-over').forEach(c => c.classList.remove('drag-over'));
    const grid = wrap.querySelector('.cs-cards');
    if (after) { after.classList.add('drag-over'); after.parentNode.insertBefore(dragging, after); }
    else if (grid) { grid.appendChild(dragging); }
  });
  wrap.addEventListener('drop', e => {
    if (!_csDragId) return;
    e.preventDefault();
    const grid = wrap.querySelector('.cs-cards');
    if (grid) saveCsOrder([...grid.children].map(c => c.dataset.id).filter(Boolean));
    _csClearDrag(wrap);
  });
  wrap.addEventListener('dragend', () => _csClearDrag(wrap));
}
function buildStatusBars(cells) {
  let success = 0, total = 0;
  const barsHtml = (cells || []).map(c => {
    success += c.success || 0; total += c.total || 0;
    let cls, tip;
    if (c.rate === null || !c.total) {
      cls = '';
      tip = `${c.t}\n无请求`;
    } else {
      const r = c.rate * 100;
      const errors = c.total - c.success;
      const errRate = 100 - r;
      cls = r >= 95 ? 'green' : (r >= 90 ? 'yellow' : 'red');
      tip = `${c.t}\n成功 ${c.success.toLocaleString()} 次\n失败 ${errors.toLocaleString()} 次\n错误率 ${errRate.toFixed(1)}%`;
    }
    return `<div class="cs-bar ${cls}" data-tip='${escapeAttr(tip)}'></div>`;
  }).join('');
  return { success, total, barsHtml, pct: total > 0 ? (success / total * 100) : null };
}
const fmtDur = s => (s == null || isNaN(s)) ? '—' : (s >= 60 ? `${Math.floor(s / 60)}m${Math.round(s % 60)}s` : `${s.toFixed(1)}s`);
const fmtFrt = ms => (ms == null || isNaN(ms)) ? '—' : (ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`);
function renderOverallStatus(data, refreshLbl) {
  const el = document.getElementById('overallStatus');
  if (!el) return;
  if (data.error) {
    el.innerHTML = '<div style="color:var(--text2);font-size:13px">数据库查询失败，暂无总状态</div>';
    return;
  }
  const groups = [
    data.overall && data.overall.mini ? data.overall.mini : {name: 'Mini 模型', cells: []},
    data.overall && data.overall.other ? data.overall.other : {name: '非 Mini 模型', cells: []},
  ];
  el.innerHTML = groups.map(g => {
    const stat = buildStatusBars(g.cells);
    const color = stat.pct === null ? 'var(--text2)' : (stat.pct >= 95 ? 'var(--accent2)' : (stat.pct >= 90 ? 'var(--accent3)' : 'var(--accent4)'));
    return `
      <div class="os-row">
        <div class="os-name">${escapeHtml(g.name)}</div>
        <div class="os-rate" style="color:${color}">${stat.pct === null ? '—' : stat.pct.toFixed(2)}<small>${stat.pct === null ? '' : '%'}</small></div>
        <div class="os-metrics">
          <div class="os-metric">
            <div class="ml">平均时长</div>
            <div class="mv" title="近 60 分钟成功请求的平均总耗时">${fmtDur(g.avg_dur)}</div>
          </div>
          <div class="os-metric">
            <div class="ml">平均首字</div>
            <div class="mv" title="近 60 分钟成功请求的平均首字时延 (TTFT)">${fmtFrt(g.avg_frt)}</div>
          </div>
        </div>
        <div class="os-bars">
          <div class="cs-striprow"><span>近 60 分钟</span><span>${refreshLbl}</span></div>
          <div class="cs-bars">${stat.barsHtml}</div>
        </div>
      </div>`;
  }).join('');
  bindCsTooltip(el);
}
function renderChannelStatus(data) {
  const wrap = document.getElementById('channelStatus');
  if (!wrap) return;
  lastStatusData = data;
  const rpmEl = document.getElementById('curRpm');
  if (rpmEl) rpmEl.textContent = data.error ? '—' : (data.total_rpm || 0).toLocaleString();
  const rpmMiniEl = document.getElementById('curRpmMini');
  if (rpmMiniEl) rpmMiniEl.textContent = data.error ? '—' : (data.mini_rpm || 0).toLocaleString();
  const rpmOtherEl = document.getElementById('curRpmOther');
  if (rpmOtherEl) rpmOtherEl.textContent = data.error ? '—' : (data.other_rpm || 0).toLocaleString();
  const ms = currentRefreshMs();
  const refreshLbl = ms > 0 ? `${Math.round(ms / 1000)}S 后刷新` : '自动刷新关闭';
  renderOverallStatus(data, refreshLbl);
  if (data.error) {
    wrap.innerHTML = '<div style="color:var(--text2);font-size:13px">数据库查询失败，暂无渠道状态</div>';
    return;
  }
  // 排序：先按 localStorage 保存的自定义顺序，未保存的新渠道按 ID 升序追加到末尾
  const allIds = Object.keys(data.channels);
  const saved = getCsOrder();
  const ids = [];
  saved.forEach(id => { if (allIds.indexOf(id) !== -1) ids.push(id); });
  allIds.filter(id => saved.indexOf(id) === -1).sort((a, b) => a - b).forEach(id => ids.push(id));
  if (!ids.length) {
    wrap.innerHTML = '<div style="color:var(--text2);font-size:13px">暂无渠道</div>';
    return;
  }
  // status by window availability: green ≥95 正常 / amber ≥90 降级 / red 异常 / null 无数据
  const STATUS = {
    green:  { txt: '正常',  glyph: '✓', c: 'var(--accent2)', bg: 'rgba(16,185,129,.13)' },
    yellow: { txt: '降级',  glyph: '!', c: 'var(--accent3)', bg: 'rgba(245,158,11,.15)' },
    red:    { txt: '异常',  glyph: '✕', c: 'var(--accent4)', bg: 'rgba(239,68,68,.13)' },
    none:   { txt: '无数据', glyph: '–', c: 'var(--text2)',  bg: 'rgba(127,127,127,.12)' },
  };
  const callIc = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>';
  const rpmIc = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>';
  const errIc = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>';
  const durIc = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>';
  const frtIc = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>';

  const grid = document.createElement('div');
  grid.className = 'cs-cards';
  ids.forEach((id, i) => {
    const ch = data.channels[id];
    let s = 0, t = 0;
    const barsHtml = ch.cells.map(c => {
      s += c.success; t += c.total;
      let cls, tip;
      if (c.rate === null || c.total === 0) {
        cls = '';
        tip = `${c.t}\n无请求`;
      } else {
        const r = c.rate * 100;
        const errors = c.total - c.success;
        const errRate = 100 - r;
        cls = r >= 95 ? 'green' : (r >= 90 ? 'yellow' : 'red');
        tip = `${c.t}\n成功 ${c.success.toLocaleString()} 次\n失败 ${errors.toLocaleString()} 次\n错误率 ${errRate.toFixed(1)}%`;
      }
      return `<div class="cs-bar ${cls}" data-tip='${escapeAttr(tip)}'></div>`;
    }).join('');

    const errors = t - s;
    const pct = t > 0 ? (s / t * 100) : null;
    const key = pct === null ? 'none' : (pct >= 95 ? 'green' : (pct >= 90 ? 'yellow' : 'red'));
    const st = STATUS[key];
    const palette = PALETTE[i % PALETTE.length];
    const bal = lastBalances[id] || {};
    // new-api 启停控制：仅在配置了控制凭据时显示状态徽标与启停按钮
    const ctrlKnown = controlConfigured && (id in lastControlStatus);
    const enabled = !!lastControlStatus[id];
    const ctrlBadge = ctrlKnown
      ? `<span class="cs-badge2" style="background:${enabled ? 'rgba(16,185,129,.13)' : 'rgba(127,127,127,.14)'};color:${enabled ? 'var(--accent2)' : 'var(--text2)'}">${enabled ? '已启用' : '已暂停'}</span>`
      : '';
    const ctrlBtns = controlConfigured
      ? `<div style="display:flex;gap:8px;margin-top:10px">
           <button class="btn btn-sm" style="${enabled ? 'opacity:.45;pointer-events:none' : ''}" onclick="toggleChannelStatus(${id}, true, this)">启动</button>
           <button class="btn btn-sm btn-ghost" style="${!enabled ? 'opacity:.45;pointer-events:none' : ''}" onclick="toggleChannelStatus(${id}, false, this)">暂停</button>
         </div>`
      : '';
    let balHtml = '';
    if (bal.configured) {
      let balInner;
      if (bal.error) {
        balInner = `<span style="color:var(--accent4)" title="${escapeAttr(bal.error)}">查询失败</span>`;
      } else if (bal.value !== null && bal.value !== undefined) {
        balInner = `<b style="color:var(--accent2);font-size:15px">$${Number(bal.value).toFixed(2)}</b>`;
      } else {
        balInner = '<span style="color:var(--text2)">待刷新</span>';
      }
      balHtml = `
      <div class="cs-avail" style="border-top:1px dashed var(--border);margin-top:10px;padding-top:10px">
        <span class="al">账号余额 · ${bal.type}${bal.checked_at ? ' · ' + bal.checked_at.slice(5, 16) : ''}</span>
        <span style="display:flex;align-items:center;gap:8px">${balInner}
          <button class="icon-btn" style="width:26px;height:26px;font-size:12px" title="刷新余额" onclick="refreshCardBalance(${id}, this)">↻</button>
        </span>
      </div>`;
    }
    const card = document.createElement('div');
    card.className = 'cs-card';
    card.draggable = true;
    card.dataset.id = id;
    card.innerHTML = `
      <div class="cs-top">
        <span class="cs-ic" style="background:${st.bg};color:${st.c}">${st.glyph}</span>
        <span class="cs-title">${escapeHtml(ch.name || '渠道 ' + id)}</span>
        <span class="cs-badge2" style="background:${st.bg};color:${st.c}">${st.txt}</span>
        ${ctrlBadge}
      </div>
      <div class="cs-tags">
        <span class="cs-tag" style="color:${palette}">#${id}</span>
        <span class="cs-tag">×${(ch.rate || 0)}</span>
      </div>
      <div class="cs-metrics">
        <div class="cs-metric">
          <div class="ml">${rpmIc} 当前 RPM</div>
          <div class="mv" style="color:${(ch.rpm||0) > 0 ? 'var(--accent)' : 'inherit'}">${(ch.rpm||0).toLocaleString()}</div>
        </div>
        <div class="cs-metric">
          <div class="ml">${callIc} 60 分调用</div>
          <div class="mv">${t.toLocaleString()}</div>
        </div>
        <div class="cs-metric">
          <div class="ml">${errIc} 失败</div>
          <div class="mv" style="color:${errors > 0 ? 'var(--accent4)' : 'inherit'}">${errors.toLocaleString()}</div>
        </div>
        <div class="cs-metric">
          <div class="ml">${durIc} 平均时长</div>
          <div class="mv" title="近 60 分钟成功请求的平均总耗时">${fmtDur(ch.avg_dur)}</div>
        </div>
        <div class="cs-metric">
          <div class="ml">${frtIc} 平均首字</div>
          <div class="mv" title="近 60 分钟成功请求的平均首字时延 (TTFT)">${fmtFrt(ch.avg_frt)}</div>
        </div>
      </div>
      <div class="cs-avail">
        <span class="al">可用性 · 近 60 分</span>
        <span class="av" style="color:${st.c}">${pct === null ? '—' : pct.toFixed(2)}<small>${pct === null ? '' : '%'}</small></span>
      </div>
      ${balHtml}
      ${ctrlBtns}
      <div class="cs-striprow"><span>近 60 分钟</span><span>${refreshLbl}</span></div>
      <div class="cs-bars">${barsHtml}</div>
      <div class="cs-foot"><span>PAST</span><span>NOW</span></div>`;
    grid.appendChild(card);
  });
  wrap.innerHTML = '';
  wrap.appendChild(grid);
  bindCsTooltip(wrap);
  bindCsDnD(wrap);
}

// Custom hover tooltip for the status bars — replaces the laggy native title:
// snaps above the hovered bar and slides smoothly between bars (OpenRouter-style).
let _csTip = null;
function bindCsTooltip(host) {
  if (!_csTip) {
    _csTip = document.createElement('div');
    _csTip.id = 'csTip';
    document.body.appendChild(_csTip);
  }
  if (host._csBound) return;  // delegate once; host element is stable across refreshes
  host._csBound = true;
  const show = (bar) => {
    _csTip.textContent = bar.dataset.tip;
    const r = bar.getBoundingClientRect();
    const x = r.left + r.width / 2, y = r.top - 10;
    _csTip.style.transform = `translate(${x}px, ${y}px) translate(-50%, -100%)`;
    _csTip.style.opacity = '1';
  };
  host.addEventListener('mouseover', e => {
    const bar = e.target.closest('.cs-bar');
    if (bar) show(bar);
  });
  host.addEventListener('mouseout', e => {
    if (e.target.closest('.cs-bar') && _csTip) _csTip.style.opacity = '0';
  });
}

// ── Channels ──
async function loadChannels() {
  const resp = await apiFetch('/api/channels');
  const channels = await resp.json();
  let balances = {};
  try { balances = await (await apiFetch('/api/channels/balances')).json(); } catch(e) {}
  const tbody = document.getElementById('channelsBody');
  tbody.innerHTML = '';
  channels.forEach(ch => {
    const b = balances[ch.id] || {};
    let balCell;
    if (!b.configured) {
      balCell = '<span style="color:var(--text2)">未配置</span>';
    } else if (b.error) {
      balCell = `<span style="color:var(--accent4)" title="${escapeAttr(b.error)}">⚠ 失败</span>`;
    } else if (b.value !== null && b.value !== undefined) {
      balCell = `<b style="color:var(--accent2)">$${Number(b.value).toFixed(2)}</b> <span style="color:var(--text2);font-size:11px">${b.type}</span>`;
    } else {
      balCell = `<span style="color:var(--text2)">${b.type} · 待刷新</span>`;
    }
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${ch.id}</td>
      <td><input class="name-input" value="${escapeAttr(ch.name)}" onchange="updateChannel(${ch.id}, 'name', this.value)"></td>
      <td><input class="rate-input" type="number" step="0.001" value="${ch.rate}" onchange="updateChannel(${ch.id}, 'rate', this.value)"></td>
      <td>${balCell}</td>
      <td style="color:var(--text2)">${ch.updated_at || ''}</td>
      <td style="white-space:nowrap">
        <button class="btn btn-ghost btn-sm" onclick="openBalModal(${ch.id}, '${escapeAttr(ch.name || ('渠道 ' + ch.id))}')">余额配置</button>
        <button class="btn btn-danger btn-sm" onclick="deleteChannel(${ch.id})">删除</button>
      </td>
    `;
    tbody.appendChild(tr);
  });
  loadRateHistory();
}

function escapeAttr(s) {
  return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/'/g, '&#39;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// ── Balance config modal ──
let balModalChId = null;
async function openBalModal(id, name) {
  balModalChId = id;
  document.getElementById('balModalTitle').textContent = `余额查询 · ${name}`;
  document.getElementById('balResult').textContent = '';
  // reset fields
  ['balUrl','balRt','balAccountS','balPasswordS','balAccountN','balPasswordN'].forEach(i => document.getElementById(i).value = '');
  document.getElementById('balType').value = '';
  try {
    const cfg = await (await apiFetch('/api/channels/' + id + '/balance-config')).json();
    document.getElementById('balType').value = cfg.bal_type || '';
    document.getElementById('balUrl').value = cfg.bal_url || '';
    if (cfg.bal_type === 'sub2api') {
      document.getElementById('balRt').value = cfg.bal_rt || '';
      document.getElementById('balAccountS').value = cfg.bal_account || '';
      document.getElementById('balPasswordS').value = cfg.bal_password || '';
    } else if (cfg.bal_type === 'newapi') {
      document.getElementById('balAccountN').value = cfg.bal_account || '';
      document.getElementById('balPasswordN').value = cfg.bal_password || '';
    }
  } catch(e) {}
  onBalTypeChange();
  document.getElementById('balModal').classList.add('show');
}
function closeBalModal() {
  document.getElementById('balModal').classList.remove('show');
  balModalChId = null;
}
function onBalTypeChange() {
  const t = document.getElementById('balType').value;
  document.getElementById('balFields').style.display = t ? 'block' : 'none';
  document.querySelectorAll('#balFields .bal-grp').forEach(g => {
    g.classList.toggle('show', g.dataset.grp === t);
  });
}
// Build the config payload from the form depending on selected type.
function balPayload() {
  const t = document.getElementById('balType').value;
  const p = { bal_type: t, bal_url: document.getElementById('balUrl').value.trim(),
              bal_account: '', bal_password: '', bal_rt: '' };
  if (t === 'sub2api') {
    p.bal_rt = document.getElementById('balRt').value.trim();
    p.bal_account = document.getElementById('balAccountS').value.trim();
    p.bal_password = document.getElementById('balPasswordS').value;
  } else if (t === 'newapi') {
    p.bal_account = document.getElementById('balAccountN').value.trim();
    p.bal_password = document.getElementById('balPasswordN').value;
  }
  return p;
}
async function saveBalanceConfig(closeAfter) {
  if (balModalChId == null) return;
  const p = balPayload();
  await apiFetch('/api/channels/' + balModalChId + '/balance-config',
    {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify(p)});
  toast('配置已保存');
  if (closeAfter) { closeBalModal(); loadChannels(); }
}
async function saveAndTestBalance() {
  if (balModalChId == null) return;
  const btn = document.getElementById('balTestBtn');
  const res = document.getElementById('balResult');
  btn.disabled = true;
  res.innerHTML = '<span style="color:var(--text2)">正在保存并查询…</span>';
  try {
    await saveBalanceConfig(false);
    const r = await (await apiFetch('/api/channels/' + balModalChId + '/balance/refresh', {method:'POST'})).json();
    if (r.error) {
      res.innerHTML = `<span style="color:var(--accent4)">✕ ${escapeHtml(r.error)}</span>`;
    } else {
      res.innerHTML = `<span style="color:var(--accent2)">✓ 当前余额 $${Number(r.value).toFixed(2)}</span> <span style="color:var(--text2)">(${escapeHtml(r.checked_at)})</span>`;
      loadChannels();
    }
  } catch(e) {
    res.innerHTML = '<span style="color:var(--accent4)">✕ 请求失败</span>';
  } finally {
    btn.disabled = false;
  }
}

async function loadRateHistory() {
  try {
    const resp = await apiFetch('/api/rate-history?limit=100');
    const items = await resp.json();
    const tb = document.getElementById('rateHistBody');
    if (!tb) return;
    tb.innerHTML = '';
    if (!items.length) {
      tb.innerHTML = '<tr><td colspan="4" style="color:var(--text2)">暂无变更记录</td></tr>';
      return;
    }
    items.forEach(it => {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td style="color:var(--text2)">${it.changed_at}</td>
        <td>${it.channel_id} ${it.name || ''}</td>
        <td>${it.old_rate}</td>
        <td style="color:var(--accent2)">${it.new_rate}</td>
      `;
      tb.appendChild(tr);
    });
  } catch(e) { console.error('Rate history error:', e); }
}

async function updateChannel(id, field, value) {
  const body = {};
  body[field] = field === 'rate' ? parseFloat(value) : value;
  await apiFetch('/api/channels/' + id, {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  toast('已更新');
  loadChannels();
}

async function deleteChannel(id) {
  if (!confirm('确认删除渠道 ' + id + '?')) return;
  await apiFetch('/api/channels/' + id, {method:'DELETE'});
  toast('已删除');
  loadChannels();
}

async function addChannel() {
  const id = parseInt(document.getElementById('newChId').value);
  const name = document.getElementById('newChName').value;
  const rate = parseFloat(document.getElementById('newChRate').value);
  if (!id) { toast('请输入渠道ID'); return; }
  await apiFetch('/api/channels', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({id, name, rate})});
  toast('已添加');
  document.getElementById('newChId').value = '';
  document.getElementById('newChName').value = '';
  document.getElementById('newChRate').value = '';
  loadChannels();
}

// 从 new-api 拉取全量渠道：新增的入库，已存在的仅刷新名称（不动系数/余额配置）
async function syncChannels() {
  const btn = document.getElementById('syncChBtn');
  const orig = btn.textContent;
  btn.disabled = true; btn.textContent = '同步中…';
  try {
    const r = await (await apiFetch('/api/channels/sync', {method:'POST'})).json();
    toast(`同步完成：新增 ${r.added || 0}，改名 ${r.renamed || 0}，共 ${r.total || 0} 个`);
  } catch (e) {
    toast('同步失败：' + (e.message || e));
  } finally {
    btn.disabled = false; btn.textContent = orig;
  }
  loadChannels();
}

// ── History ──
async function loadHistory() {
  const resp = await apiFetch('/api/history');
  const items = await resp.json();
  lastHistory = items;
  const list = document.getElementById('historyList');
  list.innerHTML = '';
  if (items.length === 0) {
    list.innerHTML = '<div style="text-align:center;padding:20px;color:var(--text2)">暂无历史数据</div>';
    return;
  }
  items.forEach(item => {
    const li = document.createElement('li');
    li.className = 'history-item';
    li.onclick = () => showHistoryDetail(item.date);
    li.innerHTML = `
      <div>
        <div class="history-date">${item.date}</div>
        <div class="history-stats">${item.total_calls.toLocaleString()} 次 · 消费 ${fmtMoney(item.total_usd)}</div>
      </div>
      <div class="history-real">${fmtMoney(item.total_real)}</div>
    `;
    list.appendChild(li);
  });
}

async function showHistoryDetail(date) {
  const resp = await apiFetch('/api/daily/' + date);
  const data = await resp.json();
  let msg = `${date} 日报\n\n`;
  Object.entries(data.channels).forEach(([id, ch]) => {
    msg += `渠道${id} ${ch.name||''}: 实付 ${fmtMoney(ch.real_cost)} | 消费 ${fmtMoney(ch.usd)} | ${ch.calls}次\n`;
  });
  msg += `\n实付合计: ${fmtMoney(data.total_real)}\n消费合计: ${fmtMoney(data.total_usd)}`;
  alert(msg);
}

// ── Cost (收入/支出记账) ──
let costChart = null;
function loadCost() {
  // Default the date field to today.
  const d = document.getElementById('costDate');
  if (!d.value) {
    const t = new Date();
    d.value = `${t.getFullYear()}-${String(t.getMonth()+1).padStart(2,'0')}-${String(t.getDate()).padStart(2,'0')}`;
  }
  loadCostRecords();
  loadCostWeekly();
}
async function loadCostRecords() {
  try {
    const resp = await apiFetch('/api/cost/records');
    const items = await resp.json();
    const tb = document.getElementById('costBody');
    let inc = 0, exp = 0, incN = 0, expN = 0;
    tb.innerHTML = '';
    if (!items.length) {
      tb.innerHTML = '<tr><td colspan="5" style="color:var(--text2)">暂无记录</td></tr>';
    }
    items.forEach(it => {
      if (it.type === 'income') { inc += it.amount; incN++; }
      else { exp += it.amount; expN++; }
      const tr = document.createElement('tr');
      const typeTxt = it.type === 'income'
        ? '<span style="color:var(--accent2)">收入</span>'
        : '<span style="color:var(--accent4)">支出</span>';
      tr.innerHTML = `
        <td style="white-space:nowrap">${it.date}</td>
        <td>${typeTxt}</td>
        <td style="font-weight:600">${fmtMoney(it.amount)}</td>
        <td style="color:var(--text2)">${escapeHtml(it.note || '')}</td>
        <td><button class="btn btn-danger btn-sm" onclick="deleteCostRecord(${it.id})">删除</button></td>
      `;
      tb.appendChild(tr);
    });
    document.getElementById('costTotalIncome').textContent = fmtMoney(inc);
    document.getElementById('costTotalExpense').textContent = fmtMoney(exp);
    document.getElementById('costNet').textContent = fmtMoney(inc - exp);
    document.getElementById('costIncomeCount').textContent = incN;
    document.getElementById('costExpenseCount').textContent = expN;
  } catch(e) { console.error('Cost records error:', e); }
}
async function addCostRecord(e) {
  e.preventDefault();
  const type = document.getElementById('costType').value;
  const amount = parseFloat(document.getElementById('costAmount').value);
  const date = document.getElementById('costDate').value;
  const note = document.getElementById('costNote').value;
  if (!amount || amount <= 0) { toast('请输入有效金额'); return false; }
  if (!date) { toast('请选择日期'); return false; }
  try {
    await apiFetch('/api/cost/records', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({type, amount, date, note})});
    toast('已添加');
    document.getElementById('costAmount').value = '';
    document.getElementById('costNote').value = '';
    loadCostRecords();
    loadCostWeekly();
  } catch(e) { toast('添加失败'); }
  return false;
}
async function deleteCostRecord(id) {
  if (!confirm('确认删除这条记录？')) return;
  try {
    await apiFetch('/api/cost/records/' + id, {method:'DELETE'});
    toast('已删除');
    loadCostRecords();
    loadCostWeekly();
  } catch(e) { toast('删除失败'); }
}
async function loadCostWeekly() {
  try {
    const weeks = document.getElementById('costWeeks').value;
    const resp = await apiFetch('/api/cost/weekly?weeks=' + weeks);
    const d = await resp.json();
    renderCostChart(d);
  } catch(e) { console.error('Cost weekly error:', e); }
}
function renderCostChart(d) {
  const ctx = document.getElementById('costChart');
  if (!ctx) return;
  const labels = d.weeks.map(w => w.week_start.slice(5));
  const income = d.weeks.map(w => w.income || 0);
  const expense = d.weeks.map(w => w.expense || 0);
  if (costChart) costChart.destroy();
  const th = chartTheme();
  costChart = new Chart(ctx, {
    type: 'bar',
    data: { labels, datasets: [
      { label: '收入', data: income, backgroundColor: 'rgba(16,185,129,0.6)', borderRadius: 4, borderWidth: 0 },
      { label: '支出', data: expense, backgroundColor: 'rgba(239,68,68,0.6)', borderRadius: 4, borderWidth: 0 },
    ]},
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { labels: { color: th.axisColor, font: { size: 11 } } },
        tooltip: { callbacks: { label: c => c.dataset.label + ': ' + fmtMoney(c.parsed.y) } }
      },
      scales: {
        x: { grid: { color: th.gridColor }, ticks: { color: th.axisColor, font: { size: 10 } } },
        y: { grid: { color: th.gridColor }, ticks: { color: th.axisColor, callback: v => '$' + v } }
      }
    }
  });
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

// ── Settings ──
async function loadSettings() {
  try {
    const resp = await apiFetch('/api/settings');
    const d = await resp.json();
    currentApiKey = d.api_key || '';
    document.getElementById('apiKeyVal').value = currentApiKey;
    renderCurl();
    loadWebhook();
    loadControl();
  } catch(e) { console.error(e); }
}
async function loadControl() {
  try {
    const resp = await apiFetch('/api/settings/control');
    const d = await resp.json();
    document.getElementById('ctrlUrl').value = d.url || '';
    document.getElementById('ctrlToken').value = '';
    document.getElementById('ctrlToken').placeholder = d.has_token
      ? '已配置（留空表示不修改）'
      : '管理员 access_token（访问令牌）';
    document.getElementById('ctrlUser').value = d.user_id || '';
    const hint = document.getElementById('ctrlHint');
    if (hint) {
      hint.textContent = d.configured ? '✓ 已配置' : '未配置';
      hint.style.color = d.configured ? 'var(--accent2)' : 'var(--accent4)';
    }
  } catch(e) { console.error('Control load error:', e); }
}
async function saveControl() {
  const body = {
    url: document.getElementById('ctrlUrl').value,
    user_id: document.getElementById('ctrlUser').value,
  };
  const tok = document.getElementById('ctrlToken').value;
  if (tok) body.token = tok;
  try {
    await apiFetch('/api/settings/control', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    toast('控制凭据已保存');
    loadControl();
    loadChannelStatus();
  } catch(e) { toast('保存失败'); }
}
function renderCurl() {
  const origin = location.origin;
  document.getElementById('curlBearer').textContent =
    `curl -H "Authorization: Bearer ${currentApiKey}" \\\n     ${origin}/api/hourly`;
  document.getElementById('curlHeader').textContent =
    `curl -H "X-API-Key: ${currentApiKey}" \\\n     ${origin}/api/hourly`;
}
function copyText(id) {
  navigator.clipboard.writeText(document.getElementById(id).textContent).then(() => toast('已复制'));
}
function copyApiKey() {
  navigator.clipboard.writeText(currentApiKey).then(() => toast('已复制 API Key'));
}
async function regenKey() {
  if (!confirm('重置后旧 Key 立即失效，所有使用旧 Key 的外部调用都需要更新。确认重置？')) return;
  const resp = await apiFetch('/api/settings/regenerate-key', {method:'POST'});
  const d = await resp.json();
  currentApiKey = d.api_key;
  document.getElementById('apiKeyVal').value = currentApiKey;
  renderCurl();
  toast('API Key 已重置');
}
async function changePassword(e) {
  e.preventDefault();
  const oldp = document.getElementById('oldPw').value;
  const newp = document.getElementById('newPw').value;
  // Plain fetch: 401 here means wrong old password, not an expired session.
  const resp = await fetch('/api/settings/password', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({old_password: oldp, new_password: newp})
  });
  if (resp.ok) {
    toast('密码已修改');
    document.getElementById('oldPw').value = '';
    document.getElementById('newPw').value = '';
  } else {
    const d = await resp.json().catch(() => ({}));
    toast(d.detail || '修改失败');
  }
  return false;
}

// ── Webhook ──
async function loadWebhook() {
  try {
    const resp = await apiFetch('/api/settings/webhook');
    const d = await resp.json();
    document.getElementById('webhookUrl').value = d.url || '';
    document.getElementById('pushHourly').checked = !!d.push_hourly;
    document.getElementById('pushDaily').checked = !!d.push_daily;
    document.getElementById('pushError').checked = !!d.push_error;
  } catch(e) { console.error('Webhook load error:', e); }
}
async function saveWebhook() {
  const body = {
    url: document.getElementById('webhookUrl').value,
    push_hourly: document.getElementById('pushHourly').checked,
    push_daily: document.getElementById('pushDaily').checked,
    push_error: document.getElementById('pushError').checked,
  };
  try {
    await apiFetch('/api/settings/webhook', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    toast('Webhook 已保存');
  } catch(e) { toast('保存失败'); }
}
async function testWebhook() {
  try {
    const r = await fetch('/api/settings/webhook/test', {method:'POST'});
    if (r.ok) { toast('测试推送成功'); }
    else { const d = await r.json().catch(() => ({})); toast(d.detail || '测试失败'); }
  } catch(e) { toast('测试失败'); }
}

// ── Init ──
// chart.umd.min.js is loaded with `defer`, which executes after parsing but
// before DOMContentLoaded — so Chart is guaranteed available by this point.
function init() {
  applyTheme(localStorage.getItem('theme') === 'light' ? 'light' : 'dark');
  document.getElementById('refreshSel').value = String(currentRefreshMs());
  loadDashboard();
  loadChannelStatus();
  loadSystem();
  loadTrend();
  applyRefresh();
}
window.addEventListener('DOMContentLoaded', init);
