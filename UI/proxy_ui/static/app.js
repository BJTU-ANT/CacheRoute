const MAX_HISTORY = 60;

const state = {
  config: null,
  timer: null,
  paused: false,
  intervalMs: 3000,
  theme: localStorage.getItem('proxy-ui-theme') || 'dark',
  rawExpanded: false,
  sortBy: 'state',
  filters: {
    aliveOnly: false,
    staleOnly: false,
    resourcesOnly: false,
    search: '',
  },
  latest: {},
  history: [],
};

const OPTIONAL_UNAVAILABLE = {
  ok: false,
  optional_unavailable: true,
  error: 'unavailable',
};

function $(id) {
  return document.getElementById(id);
}

function clampPercent(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) {
    return null;
  }
  return Math.max(0, Math.min(100, number));
}

function fmt(value, digits = 1) {
  if (value === null || value === undefined || value === '') {
    return '—';
  }
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(digits) : String(value);
}

function formatTimestamp(value) {
  if (!value) {
    return '—';
  }
  const number = Number(value);
  const milliseconds = number > 1e12 ? number : number * 1000;
  return Number.isFinite(milliseconds) ? new Date(milliseconds).toLocaleString() : String(value);
}

function ageSeconds(value) {
  if (!value) {
    return null;
  }
  const seconds = Math.max(0, Date.now() / 1000 - Number(value));
  return Number.isFinite(seconds) ? seconds : null;
}

function formatAge(value) {
  const seconds = ageSeconds(value);
  if (seconds === null) {
    return '—';
  }
  if (seconds < 60) {
    return `${seconds.toFixed(0)}s`;
  }
  return `${(seconds / 60).toFixed(1)}m`;
}

function badge(text, tone = 'muted') {
  return `<span class="badge ${tone}-badge">${text}</span>`;
}

function progressBar(value, label) {
  const percent = clampPercent(value);
  const width = percent === null ? 0 : percent;
  const text = percent === null ? 'no data' : `${percent.toFixed(1)}%`;
  return `
    <div class="metric-row">
      <span>${label}</span><strong>${text}</strong>
    </div>
    <div class="progress"><span style="width:${width}%"></span></div>
  `;
}

function sortJsonValue(value) {
  if (Array.isArray(value)) {
    return value.map(sortJsonValue);
  }
  if (value && typeof value === 'object') {
    return Object.keys(value).sort().reduce((acc, key) => {
      acc[key] = sortJsonValue(value[key]);
      return acc;
    }, {});
  }
  return value;
}

function stableJson(payload) {
  return JSON.stringify(sortJsonValue(payload || {}), null, 2);
}

async function getJson(path) {
  const response = await fetch(path, { cache: 'no-store' });
  if (!response.ok) {
    throw new Error(`${path}: HTTP ${response.status}`);
  }
  return response.json();
}

async function settleJson(path, options = {}) {
  try {
    return await getJson(path);
  } catch (error) {
    if (options.optional) {
      return { ...OPTIONAL_UNAVAILABLE, error: error.message };
    }
    return { ok: false, error: error.message };
  }
}

function hasResourceReport(instance) {
  const resource = instance.resource || {};
  return Object.values(resource).some((value) => value !== null && value !== undefined);
}

function resourceAgeTone(resource) {
  if (!resource?.resource_reported_at) {
    return ['no report yet', 'muted'];
  }
  const seconds = ageSeconds(resource.resource_reported_at);
  if (seconds === null) {
    return ['unknown age', 'muted'];
  }
  if (seconds <= 30) {
    return [`resource age: ${formatAge(resource.resource_reported_at)}`, 'ok'];
  }
  if (seconds <= 120) {
    return [`resource age: ${formatAge(resource.resource_reported_at)}`, 'warn'];
  }
  return [`resource age: ${formatAge(resource.resource_reported_at)}`, 'bad'];
}

function instanceStateBadge(instance) {
  if (instance.is_alive === true) {
    return badge('alive', 'ok');
  }
  if (instance.is_alive === false) {
    return badge('stale', 'bad');
  }
  return badge('unknown', 'warn');
}

function schedulerText(scheduler) {
  if (scheduler?.ok) {
    return ['registered', 'ok'];
  }
  if (scheduler?.error === 'proxy_not_found') {
    return ['proxy_not_found', 'warn'];
  }
  if (scheduler?.error === 'proxy_id_not_configured') {
    return ['not configured', 'muted'];
  }
  return [scheduler?.error || 'unavailable', 'warn'];
}

function normalizeResources(resources) {
  return resources?.instances || [];
}

function applyFilters(instances) {
  const search = state.filters.search.trim().toLowerCase();
  return instances.filter((instance) => {
    if (state.filters.aliveOnly && instance.is_alive !== true) {
      return false;
    }
    if (state.filters.staleOnly && instance.is_alive !== false) {
      return false;
    }
    if (state.filters.resourcesOnly && !hasResourceReport(instance)) {
      return false;
    }
    if (!search) {
      return true;
    }
    return [instance.instance_id, instance.host, `${instance.host}:${instance.port}`]
      .some((value) => String(value || '').toLowerCase().includes(search));
  });
}

function sortInstances(instances) {
  const rankState = (instance) => (instance.is_alive === true ? 0 : instance.is_alive === false ? 2 : 1);
  const metric = (instance, key) => Number(instance.resource?.[key] ?? -1);
  const memoryRatio = (instance) => {
    const used = Number(instance.resource?.memory_used_mb);
    const total = Number(instance.resource?.memory_total_mb);
    return Number.isFinite(used) && Number.isFinite(total) && total > 0 ? used / total : -1;
  };
  const comparators = {
    state: (a, b) => rankState(a) - rankState(b),
    age: (a, b) => (ageSeconds(b.last_seen_at) ?? -1) - (ageSeconds(a.last_seen_at) ?? -1),
    cpu: (a, b) => metric(b, 'cpu_util') - metric(a, 'cpu_util'),
    memory: (a, b) => memoryRatio(b) - memoryRatio(a),
    gpu: (a, b) => metric(b, 'gpu_util_avg') - metric(a, 'gpu_util_avg'),
    resource: (a, b) => Number(b.resource?.resource_reported_at || 0) - Number(a.resource?.resource_reported_at || 0),
  };
  return [...instances].sort(comparators[state.sortBy] || comparators.state);
}

function summarize(instances, resources, topology) {
  const alive = instances.filter((item) => item.is_alive === true).length;
  const stale = instances.filter((item) => item.is_alive === false).length;
  const unknown = Math.max(0, instances.length - alive - stale);
  const visibleResources = resources.filter((item) => hasResourceReport(item)).length;
  const topologyLinks = Object.keys(topology?.kdn_links || {}).length;
  return { alive, stale, unknown, total: instances.length, visibleResources, topologyLinks };
}

function average(values) {
  const clean = values.filter((value) => Number.isFinite(value));
  if (!clean.length) {
    return null;
  }
  return clean.reduce((sum, value) => sum + value, 0) / clean.length;
}

function computeMetrics(instances) {
  const cpu = average(instances.map((item) => Number(item.resource?.cpu_util)));
  const memory = average(instances.map((item) => {
    const used = Number(item.resource?.memory_used_mb);
    const total = Number(item.resource?.memory_total_mb);
    return Number.isFinite(used) && Number.isFinite(total) && total > 0 ? (used / total) * 100 : NaN;
  }));
  const gpu = average(instances.map((item) => Number(item.resource?.gpu_util_avg)));
  const rx = average(instances.map((item) => Number(item.resource?.network_rx_mbps)));
  const tx = average(instances.map((item) => Number(item.resource?.network_tx_mbps)));
  return { cpu, memory, gpu, rx, tx };
}

function pushHistory(instances, resources, topology) {
  const summary = summarize(instances, resources, topology);
  state.history.push({
    at: new Date(),
    ...summary,
    ...computeMetrics(instances),
  });
  if (state.history.length > MAX_HISTORY) {
    state.history.shift();
  }
}

function renderSummaryCards(summary, status, scheduler) {
  const [schedulerLabel, schedulerTone] = schedulerText(scheduler);
  const proxyTone = status?.ok === false ? 'bad' : 'ok';
  const cards = [
    ['Proxy status', status?.ok === false ? 'error' : 'healthy', proxyTone, `ttl=${status?.ttl_s ?? '—'}s`],
    ['Instances', `${summary.alive}/${summary.total} alive`, summary.stale ? 'warn' : 'ok', `${summary.stale} stale, ${summary.unknown} unknown`],
    ['Resource reports', `${summary.visibleResources} visible`, summary.visibleResources ? 'ok' : 'warn', 'current snapshot rows'],
    ['Stale instances', String(summary.stale), summary.stale ? 'bad' : 'ok', 'TTL-derived state'],
    ['Topology links', String(summary.topologyLinks), summary.topologyLinks ? 'ok' : 'muted', 'best KDN links'],
    ['Scheduler', schedulerLabel, schedulerTone, 'optional control plane'],
  ];

  $('summaryCards').innerHTML = cards.map(([title, value, tone, detail]) => `
    <article class="summary-card ${tone}">
      <span>${title}</span>
      <strong>${value}</strong>
      <small>${detail}</small>
    </article>
  `).join('');
}

function renderTopBadges(status, scheduler) {
  const [schedulerLabel, schedulerTone] = schedulerText(scheduler);
  $('proxyHealthBadge').outerHTML = badge(`Proxy: ${status?.ok === false ? 'error' : 'healthy'}`, status?.ok === false ? 'bad' : 'ok').replace('<span', '<span id="proxyHealthBadge"');
  $('schedulerBadge').outerHTML = badge(`Scheduler: ${schedulerLabel}`, schedulerTone).replace('<span', '<span id="schedulerBadge"');
}

function renderInstanceCards(instances) {
  const filtered = sortInstances(applyFilters(instances));
  $('instanceSummary').textContent = `${filtered.length}/${instances.length} instance(s) shown after local filters.`;

  if (!filtered.length) {
    $('instanceCards').innerHTML = '<div class="empty-state">No instances match the current filters.</div>';
    return;
  }

  $('instanceCards').innerHTML = filtered.map((instance) => {
    const resource = instance.resource || {};
    const [freshness, freshnessTone] = resourceAgeTone(resource);
    const memoryPercent = Number(resource.memory_total_mb) > 0
      ? (Number(resource.memory_used_mb) / Number(resource.memory_total_mb)) * 100
      : null;
    const gpuMemoryPercent = Number(resource.gpu_mem_total_mb) > 0
      ? (Number(resource.gpu_mem_used_mb) / Number(resource.gpu_mem_total_mb)) * 100
      : null;

    return `
      <article class="instance-card">
        <div class="instance-card-head">
          <div><h3>${instance.instance_id || 'unknown instance'}</h3><p>${instance.host || '—'}:${instance.port || '—'}</p></div>
          ${instanceStateBadge(instance)}
        </div>
        <div class="badge-row">
          ${badge(`last seen: ${formatAge(instance.last_seen_at)}`, instance.is_alive === false ? 'bad' : 'muted')}
          ${badge(freshness, freshnessTone)}
          ${badge(resource.admission_state || 'admission: —', resource.admission_state ? 'muted' : 'warn')}
        </div>
        ${progressBar(resource.cpu_util, 'CPU')}
        ${progressBar(memoryPercent, 'Memory')}
        ${progressBar(resource.gpu_util_avg, 'GPU util')}
        ${progressBar(gpuMemoryPercent, 'GPU memory')}
        <div class="mini-metrics">
          <span>RX ${fmt(resource.network_rx_mbps, 2)} Mbps</span>
          <span>TX ${fmt(resource.network_tx_mbps, 2)} Mbps</span>
          <span>inflight ${fmt(instance.load?.inflight, 0)}</span>
        </div>
      </article>
    `;
  }).join('');
}

function renderInstanceTable(instances) {
  const filtered = sortInstances(applyFilters(instances));
  const rows = filtered.map((item) => `
    <tr>
      <td>${item.instance_id || '—'}</td>
      <td>${item.host || '—'}:${item.port || '—'}</td>
      <td>${instanceStateBadge(item)}</td>
      <td>${formatAge(item.last_seen_at)}<br><span class="muted">${formatTimestamp(item.last_seen_at)}</span></td>
      <td>${fmt(item.resource?.cpu_util)}%</td>
      <td>${fmt(item.resource?.memory_used_mb, 0)} / ${fmt(item.resource?.memory_total_mb, 0)} MB</td>
      <td>${fmt(item.resource?.gpu_util_avg)}%</td>
      <td>${badge(resourceAgeTone(item.resource || {})[0], resourceAgeTone(item.resource || {})[1])}</td>
      <td><code>${JSON.stringify(item.meta || {})}</code></td>
    </tr>
  `).join('');

  $('instanceTable').innerHTML = `
    <thead>
      <tr><th>Instance ID</th><th>Address</th><th>State</th><th>Last seen</th><th>CPU</th><th>Memory</th><th>GPU</th><th>Resource</th><th>Meta</th></tr>
    </thead>
    <tbody>${rows || '<tr><td colspan="9">No instances match the current filters.</td></tr>'}</tbody>
  `;
}

function renderResourceTable(resources) {
  $('resourceSummary').textContent = `${resources.length} resource snapshot row(s). Missing fields are shown as em dashes.`;
  const rows = resources.map((item) => {
    const resource = item.resource || {};
    return `
      <tr>
        <td>${item.instance_id}</td>
        <td>${item.host}:${item.port}</td>
        <td>${formatTimestamp(item.last_seen_at)}</td>
        <td>${fmt(resource.cpu_util)}%</td>
        <td>${fmt(resource.memory_used_mb, 0)} / ${fmt(resource.memory_total_mb, 0)} MB</td>
        <td>${fmt(resource.gpu_util_avg)}%</td>
        <td>${fmt(resource.gpu_mem_used_mb, 0)} / ${fmt(resource.gpu_mem_total_mb, 0)} MB</td>
        <td>rx ${fmt(resource.network_rx_mbps, 2)}<br>tx ${fmt(resource.network_tx_mbps, 2)}</td>
        <td>${resource.admission_state || '—'}</td>
        <td>${badge(resourceAgeTone(resource)[0], resourceAgeTone(resource)[1])}</td>
      </tr>
    `;
  }).join('');

  $('resourceTable').innerHTML = `
    <thead>
      <tr><th>Instance ID</th><th>Address</th><th>Last seen</th><th>CPU</th><th>Memory</th><th>GPU util</th><th>GPU memory</th><th>Network Mbps</th><th>Admission</th><th>Freshness</th></tr>
    </thead>
    <tbody>${rows || '<tr><td colspan="10">No resource snapshots yet.</td></tr>'}</tbody>
  `;
}

function renderTopology(payload) {
  if (payload?.optional_unavailable) {
    $('topologyPanel').textContent = stableJson({ unavailable: true, error: payload.error });
    return;
  }
  $('topologyPanel').textContent = stableJson(payload || {});
}

function renderScheduler(payload) {
  $('schedulerPanel').textContent = stableJson(payload || {});
}

function renderRawPanels() {
  const entries = [
    ['Proxy health', state.latest.health],
    ['Proxy status', state.latest.status],
    ['Instances', state.latest.instances],
    ['Resources', state.latest.resources],
    ['Topology', state.latest.topology],
    ['Scheduler', state.latest.scheduler],
  ];
  $('rawJsonPanels').classList.toggle('collapsed', !state.rawExpanded);
  $('toggleRawBtn').textContent = state.rawExpanded ? 'Collapse raw JSON' : 'Expand raw JSON';
  $('rawJsonPanels').innerHTML = entries.map(([title, payload]) => `
    <details ${state.rawExpanded ? 'open' : ''}>
      <summary>${title}</summary>
      <button class="copy-inline" data-copy-key="${title}">Copy</button>
      <pre>${stableJson(payload || {})}</pre>
    </details>
  `).join('');
}

function drawLineChart(canvasId, series, options = {}) {
  const canvas = $(canvasId);
  const context = canvas.getContext('2d');
  const width = canvas.width;
  const height = canvas.height;
  const padding = 24;
  context.clearRect(0, 0, width, height);
  context.strokeStyle = getComputedStyle(document.documentElement).getPropertyValue('--line');
  context.lineWidth = 1;
  context.beginPath();
  context.moveTo(padding, padding);
  context.lineTo(padding, height - padding);
  context.lineTo(width - padding, height - padding);
  context.stroke();

  const maxY = options.maxY ?? Math.max(1, ...series.flatMap((item) => item.values).filter((value) => Number.isFinite(value)));
  const colors = options.colors || ['#5ddcff', '#ffca5f'];
  series.forEach((line, lineIndex) => {
    context.strokeStyle = colors[lineIndex % colors.length];
    context.lineWidth = 2;
    context.beginPath();
    let started = false;
    line.values.forEach((value, index) => {
      if (!Number.isFinite(value)) {
        return;
      }
      const x = padding + (index / Math.max(1, line.values.length - 1)) * (width - padding * 2);
      const y = height - padding - (Math.max(0, value) / maxY) * (height - padding * 2);
      if (!started) {
        context.moveTo(x, y);
        started = true;
      } else {
        context.lineTo(x, y);
      }
    });
    context.stroke();
  });
}

function renderCharts() {
  const history = state.history;
  drawLineChart('chartCpu', [{ values: history.map((item) => item.cpu) }], { maxY: 100, colors: ['#5ddcff'] });
  drawLineChart('chartMemory', [{ values: history.map((item) => item.memory) }], { maxY: 100, colors: ['#45d483'] });
  drawLineChart('chartGpu', [{ values: history.map((item) => item.gpu) }], { maxY: 100, colors: ['#b38cff'] });
  drawLineChart('chartLiveness', [
    { values: history.map((item) => item.alive) },
    { values: history.map((item) => item.stale) },
  ], { colors: ['#45d483', '#ff6b6b'] });
  drawLineChart('chartNetwork', [
    { values: history.map((item) => item.rx) },
    { values: history.map((item) => item.tx) },
  ], { colors: ['#5ddcff', '#ffca5f'] });
}

function renderApiSummary() {
  const config = state.config || {};
  $('apiSummary').textContent = `Proxy CP ${config.proxy_cp_url || 'unknown'} · Scheduler CP ${config.scheduler_cp_url || 'unknown'} · poll ${state.intervalMs / 1000}s`;
  if (config.proxy_cp_url) {
    $('proxyApiLink').href = `${config.proxy_cp_url}/debug/status`;
  }
}

function renderAll() {
  const instances = Array.isArray(state.latest.instances) ? state.latest.instances : [];
  const resources = normalizeResources(state.latest.resources);
  const topology = state.latest.topology?.optional_unavailable ? {} : state.latest.topology;
  const summary = summarize(instances, resources, topology);

  renderTopBadges(state.latest.status, state.latest.scheduler);
  renderSummaryCards(summary, state.latest.status, state.latest.scheduler);
  renderInstanceCards(instances);
  renderInstanceTable(instances);
  renderResourceTable(resources);
  renderTopology(state.latest.topology);
  renderScheduler(state.latest.scheduler);
  renderRawPanels();
  renderCharts();
  renderApiSummary();

  const requiredErrors = [state.latest.health, state.latest.status, state.latest.instances, state.latest.resources]
    .filter((payload) => payload && payload.ok === false)
    .map((payload) => payload.error);
  $('errorBox').textContent = requiredErrors.join(' | ');
}

async function refresh() {
  if (state.paused) {
    return;
  }
  $('errorBox').textContent = '';

  if (!state.config) {
    state.config = await settleJson('/api/config');
    if (state.config?.poll_interval_ms) {
      state.intervalMs = state.config.poll_interval_ms;
      $('intervalSelect').value = String(state.intervalMs);
    }
  }

  const [health, status, instances, resources, topology, scheduler] = await Promise.all([
    settleJson('/api/proxy/healthz'),
    settleJson('/api/proxy/status'),
    settleJson('/api/proxy/instances?include_dead=true'),
    settleJson('/api/proxy/resources?include_dead=true'),
    settleJson('/api/proxy/topology', { optional: true }),
    settleJson('/api/scheduler/proxy', { optional: true }),
  ]);

  state.latest = { health, status, instances, resources, topology, scheduler };
  pushHistory(Array.isArray(instances) ? instances : [], normalizeResources(resources), topology);
  $('lastUpdated').textContent = `Last refresh: ${new Date().toLocaleTimeString()}`;
  renderAll();
}

function restartTimer() {
  if (state.timer) {
    clearInterval(state.timer);
  }
  state.timer = setInterval(refresh, state.intervalMs);
}

function setTheme(theme) {
  state.theme = theme;
  document.documentElement.dataset.theme = theme;
  localStorage.setItem('proxy-ui-theme', theme);
  $('themeBtn').textContent = theme === 'dark' ? 'Light theme' : 'Dark theme';
}

function setupEventHandlers() {
  $('refreshBtn').addEventListener('click', () => refresh());
  $('pauseBtn').addEventListener('click', () => {
    state.paused = !state.paused;
    $('pauseBtn').textContent = state.paused ? 'Resume polling' : 'Pause polling';
    $('pollingState').outerHTML = badge(state.paused ? 'Paused' : 'Polling', state.paused ? 'warn' : 'ok').replace('<span', '<span id="pollingState"');
    if (!state.paused) {
      refresh();
    }
  });
  $('intervalSelect').addEventListener('change', (event) => {
    state.intervalMs = Number(event.target.value);
    restartTimer();
    renderApiSummary();
  });
  $('themeBtn').addEventListener('click', () => setTheme(state.theme === 'dark' ? 'light' : 'dark'));
  $('aliveOnlyToggle').addEventListener('change', (event) => {
    state.filters.aliveOnly = event.target.checked;
    if (event.target.checked) {
      state.filters.staleOnly = false;
      $('staleOnlyToggle').checked = false;
    }
    renderAll();
  });
  $('staleOnlyToggle').addEventListener('change', (event) => {
    state.filters.staleOnly = event.target.checked;
    if (event.target.checked) {
      state.filters.aliveOnly = false;
      $('aliveOnlyToggle').checked = false;
    }
    renderAll();
  });
  $('resourcesOnlyToggle').addEventListener('change', (event) => {
    state.filters.resourcesOnly = event.target.checked;
    renderAll();
  });
  $('searchInput').addEventListener('input', (event) => {
    state.filters.search = event.target.value;
    renderAll();
  });
  $('sortSelect').addEventListener('change', (event) => {
    state.sortBy = event.target.value;
    renderAll();
  });
  $('clearHistoryBtn').addEventListener('click', () => {
    state.history = [];
    renderCharts();
  });
  $('toggleRawBtn').addEventListener('click', () => {
    state.rawExpanded = !state.rawExpanded;
    renderRawPanels();
  });
  $('copyDiagnosticsBtn').addEventListener('click', () => copyText(stableJson(state.latest)));
  document.querySelectorAll('.tab').forEach((tab) => {
    tab.addEventListener('click', () => activateTab(tab.dataset.tab));
  });
  document.addEventListener('click', (event) => {
    const copyButton = event.target.closest('[data-copy], [data-copy-key]');
    if (!copyButton) {
      return;
    }
    const key = copyButton.dataset.copy || copyButton.dataset.copyKey;
    const payloads = {
      topology: state.latest.topology,
      scheduler: state.latest.scheduler,
      'Proxy health': state.latest.health,
      'Proxy status': state.latest.status,
      Instances: state.latest.instances,
      Resources: state.latest.resources,
      Topology: state.latest.topology,
      Scheduler: state.latest.scheduler,
    };
    copyText(stableJson(payloads[key] || state.latest));
  });
}

function activateTab(tabName) {
  document.querySelectorAll('.tab').forEach((tab) => tab.classList.toggle('active', tab.dataset.tab === tabName));
  document.querySelectorAll('.tab-panel').forEach((panel) => panel.classList.toggle('active', panel.id === `tab-${tabName}`));
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    $('errorBox').textContent = 'Copied diagnostics JSON to clipboard.';
  } catch (error) {
    $('errorBox').textContent = `Copy failed: ${error.message}`;
  }
}

setTheme(state.theme);
setupEventHandlers();
refresh().then(restartTimer);
