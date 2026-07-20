const MAX_HISTORY = 60;

const state = {
  config: null,
  timer: null,
  paused: false,
  intervalMs: 3000,
  theme: (typeof localStorage !== 'undefined' && localStorage.getItem('proxy-ui-theme')) || 'dark',
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
  selectedInstanceId: null,
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
  const number = finiteNumber(value);
  if (number === null) {
    return null;
  }
  return Math.max(0, Math.min(100, number));
}

function fmt(value, digits = 1) {
  const number = finiteNumber(value);
  if (number === null) {
    return '—';
  }
  return number.toFixed(digits);
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

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('\"', '&quot;')
    .replaceAll("'", '&#39;');
}

function badge(text, tone = 'muted') {
  return `<span class="badge ${tone}-badge">${escapeHtml(text)}</span>`;
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


function firstString(...values) {
  for (const value of values) {
    if (typeof value === 'string' && value.trim()) {
      return value.trim();
    }
  }
  return null;
}

function summarizeModelCount(items, modelKeys = ['model', 'name', 'product_name']) {
  if (!Array.isArray(items) || !items.length) {
    return null;
  }
  const names = items.map((item) => {
    if (typeof item === 'string') {
      return item;
    }
    if (!item || typeof item !== 'object') {
      return null;
    }
    return firstString(...modelKeys.map((key) => item[key]));
  }).filter(Boolean);
  if (!names.length) {
    return `${items.length} device(s)`;
  }
  const first = names[0];
  const same = names.every((name) => name === first);
  return same ? `${items.length}×${first}` : `${items.length} devices`;
}

function hardwareSummary(instance) {
  const raw = instance.resource?.raw_resource || {};
  const devices = raw.devices && typeof raw.devices === 'object' ? raw.devices : {};
  const cpu = devices.cpu && typeof devices.cpu === 'object' ? devices.cpu : {};
  const cpuModel = firstString(cpu.model, cpu.model_name, cpu.name, instance.meta?.cpu_model);
  const gpuList = Array.isArray(devices.gpu) ? devices.gpu : [];
  const netList = Array.isArray(devices.network) ? devices.network : (Array.isArray(devices.nic) ? devices.nic : []);
  return {
    cpu: cpuModel ? `CPU: ${cpuModel}` : null,
    gpu: summarizeModelCount(gpuList, ['model', 'name', 'product_name']) ? `GPU: ${summarizeModelCount(gpuList, ['model', 'name', 'product_name'])}` : null,
    nic: summarizeModelCount(netList, ['model', 'name', 'interface', 'driver']) ? `NIC: ${summarizeModelCount(netList, ['model', 'name', 'interface', 'driver'])}` : null,
  };
}

function hardwareBadges(instance) {
  const hw = hardwareSummary(instance);
  const parts = [hw.cpu, hw.gpu, hw.nic].filter(Boolean);
  if (!parts.length) {
    return badge('device: unknown', 'muted');
  }
  return parts.map((part) => badge(part, 'muted')).join('');
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
      <span>${escapeHtml(title)}</span>
      <strong>${escapeHtml(value)}</strong>
      <small>${escapeHtml(detail)}</small>
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
      <article class="instance-card" role="button" tabindex="0" data-instance-id="${escapeHtml(instance.instance_id || '')}">
        <div class="instance-card-head">
          <div><h3>${escapeHtml(instance.instance_id || 'unknown instance')}</h3><p>${escapeHtml(instance.host || '—')}:${escapeHtml(instance.port || '—')}</p></div>
          ${instanceStateBadge(instance)}
        </div>
        <div class="badge-row">
          ${badge(`last seen: ${formatAge(instance.last_seen_at)}`, instance.is_alive === false ? 'bad' : 'muted')}
          ${badge(freshness, freshnessTone)}
          ${badge(resource.admission_state || 'admission: —', resource.admission_state ? 'muted' : 'warn')}
          ${hardwareBadges(instance)}
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
      <td>${escapeHtml(item.instance_id || '—')}</td>
      <td>${escapeHtml(item.host || '—')}:${escapeHtml(item.port || '—')}</td>
      <td>${instanceStateBadge(item)}</td>
      <td>${formatAge(item.last_seen_at)}<br><span class="muted">${formatTimestamp(item.last_seen_at)}</span></td>
      <td>${fmt(item.resource?.cpu_util)}%</td>
      <td>${fmt(item.resource?.memory_used_mb, 0)} / ${fmt(item.resource?.memory_total_mb, 0)} MB</td>
      <td>${fmt(item.resource?.gpu_util_avg)}%</td>
      <td>${badge(resourceAgeTone(item.resource || {})[0], resourceAgeTone(item.resource || {})[1])}</td>
      <td><code>${escapeHtml(JSON.stringify(item.meta || {}))}</code></td>
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
        <td>${escapeHtml(item.instance_id)}</td>
        <td>${escapeHtml(item.host)}:${escapeHtml(item.port)}</td>
        <td>${formatTimestamp(item.last_seen_at)}</td>
        <td>${fmt(resource.cpu_util)}%</td>
        <td>${fmt(resource.memory_used_mb, 0)} / ${fmt(resource.memory_total_mb, 0)} MB</td>
        <td>${fmt(resource.gpu_util_avg)}%</td>
        <td>${fmt(resource.gpu_mem_used_mb, 0)} / ${fmt(resource.gpu_mem_total_mb, 0)} MB</td>
        <td>rx ${fmt(resource.network_rx_mbps, 2)}<br>tx ${fmt(resource.network_tx_mbps, 2)}</td>
        <td>${escapeHtml(resource.admission_state || '—')}</td>
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



function stateLabel(instance) {
  if (instance?.is_alive === true) return 'alive';
  if (instance?.is_alive === false) return 'stale';
  return 'unknown';
}

function shortText(value, max = 18) {
  const text = String(value ?? '—');
  return text.length > max ? `${text.slice(0, Math.max(1, max - 1))}…` : text;
}

function finiteNumber(value) {
  if (value === null || value === undefined || typeof value === 'boolean') {
    return null;
  }
  if (typeof value === 'string' && value.trim() === '') {
    return null;
  }
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function percentOf(value, max) {
  const current = finiteNumber(value);
  const total = finiteNumber(max);
  if (current === null || total === null || total <= 0) return null;
  return clampPercent((current / total) * 100);
}

function displayNumber(value, digits = 1) {
  const number = finiteNumber(value);
  if (number === null) return '—';
  return Number.isInteger(number) ? String(number) : number.toFixed(digits);
}

function buildTopologyModel(instances, scheduler, proxyState = {}) {
  const [schedulerLabel, schedulerTone] = schedulerText(scheduler);
  const proxyId = proxyState.proxy_id || state.config?.proxy_id || state.latest.status?.proxy_id || 'current proxy';
  const proxyTone = proxyState?.ok === false ? 'bad' : (proxyState ? 'ok' : 'warn');
  const sortedInstances = [...(Array.isArray(instances) ? instances : [])]
    .sort((a, b) => String(a.instance_id || '').localeCompare(String(b.instance_id || '')));
  const nodes = [
    { id: 'scheduler', type: 'scheduler', label: 'Scheduler', status: schedulerLabel, tone: schedulerTone, raw: scheduler || {} },
    { id: 'proxy', type: 'proxy', label: 'Proxy', status: proxyState?.ok === false ? 'error' : String(proxyId), tone: proxyTone, raw: proxyState || {} },
    ...sortedInstances.map((instance) => ({
      id: `instance:${instance.instance_id || 'unknown'}`,
      type: 'instance',
      instanceId: String(instance.instance_id || 'unknown'),
      label: String(instance.instance_id || 'unknown'),
      status: stateLabel(instance),
      tone: stateLabel(instance) === 'alive' ? 'ok' : stateLabel(instance) === 'stale' ? 'bad' : 'warn',
      raw: instance,
    })),
  ];
  const links = [
    { id: 'scheduler-proxy', source: 'scheduler', target: 'proxy', status: scheduler?.ok ? 'active' : 'inactive' },
    ...sortedInstances.map((instance) => ({
      id: `proxy-${instance.instance_id || 'unknown'}`,
      source: 'proxy',
      target: `instance:${instance.instance_id || 'unknown'}`,
      status: stateLabel(instance) === 'alive' ? 'active' : stateLabel(instance) === 'stale' ? 'stale' : 'unknown',
    })),
  ];
  return { nodes, links, schedulerLabel, schedulerTone, proxyId, instances: sortedInstances };
}

function computeTopologyLayout(model, options = {}) {
  const count = model.instances.length;
  const width = Math.max(760, options.width || 980);
  const nodePadding = Number(options.nodePadding ?? 54);
  const labelPadding = Number(options.labelPadding ?? 70);
  const positions = { scheduler: { x: width / 2, y: 64 }, proxy: { x: width / 2, y: 178 } };
  const cols = count === 0 ? 0 : (count <= 4 ? count : count <= 8 ? 4 : count <= 20 ? 5 : 6);
  const rowGap = count <= 8 ? 130 : 118;
  const startY = 314;
  model.instances.forEach((instance, index) => {
    const row = Math.floor(index / cols);
    const col = index % cols;
    const rowCount = Math.min(cols, count - row * cols);
    const gapX = (width - nodePadding * 2) / Math.max(1, rowCount + 1);
    positions[`instance:${instance.instance_id || 'unknown'}`] = {
      x: nodePadding + gapX * (col + 1),
      y: startY + row * rowGap,
    };
  });
  const xs = Object.values(positions).map((pos) => pos.x);
  const ys = Object.values(positions).map((pos) => pos.y);
  const minX = Math.min(...xs) - nodePadding;
  const maxX = Math.max(...xs) + nodePadding;
  if (minX < 0 || maxX > width) {
    Object.values(positions).forEach((pos) => {
      pos.x = Math.max(nodePadding, Math.min(width - nodePadding, pos.x));
    });
  }
  const height = Math.max(360, Math.max(...ys) + labelPadding);
  return { width, height, positions, nodePadding, labelPadding };
}

function nodeTooltip(node) {
  const raw = node.raw || {};
  const resource = raw.resource || {};
  const address = raw.host ? `${raw.host}:${raw.port ?? '—'}` : (node.type === 'proxy' ? String(node.status) : '—');
  const cpu = resource.cpu_util !== null && resource.cpu_util !== undefined ? `CPU ${fmt(resource.cpu_util)}%` : null;
  const gpu = resource.gpu_util_avg !== null && resource.gpu_util_avg !== undefined ? `GPU ${fmt(resource.gpu_util_avg)}%` : null;
  return [`Type: ${node.type}`, `ID: ${node.instanceId || node.label}`, `Address: ${address}`, `State: ${node.status}`, `Last seen: ${formatTimestamp(raw.last_seen_at)}`, cpu, gpu].filter(Boolean).join('\n');
}

function renderMetricBar({ label, value, max = 100, unit = '', tone = 'accent', secondaryText = '' }) {
  const current = finiteNumber(value);
  const total = finiteNumber(max);
  const percent = current === null ? null : (total && total > 0 ? clampPercent((current / total) * 100) : null);
  const width = percent === null ? 0 : percent;
  const primary = current === null ? 'No data' : `${displayNumber(current, 2)}${unit}`;
  return `<div class="viz-metric ${escapeHtml(tone)}"><div class="metric-row"><span>${escapeHtml(label)}</span><strong>${escapeHtml(primary)}</strong></div><div class="progress"><span style="width:${width}%"></span></div><small>${escapeHtml(secondaryText || (percent === null ? '—' : `${percent.toFixed(1)}% of ${displayNumber(total, 2)}${unit}`))}</small></div>`;
}

function renderDonutChart({ label, used, total, unit = '' }) {
  const rawUsed = finiteNumber(used);
  const rawTotal = finiteNumber(total);
  if (rawUsed === null || rawTotal === null || rawTotal <= 0) {
    return `<article class="donut-card"><h3>${escapeHtml(label)}</h3><div class="empty-state compact">No data</div></article>`;
  }
  const safeUsed = Math.max(0, Math.min(rawUsed, rawTotal));
  const percent = (safeUsed / rawTotal) * 100;
  const free = Math.max(0, rawTotal - safeUsed);
  const dash = `${percent.toFixed(2)} ${Math.max(0, 100 - percent).toFixed(2)}`;
  return `<article class="donut-card" aria-label="${escapeHtml(label)} used ${percent.toFixed(1)} percent"><h3>${escapeHtml(label)}</h3><svg class="donut" viewBox="0 0 42 42" role="img"><title>${escapeHtml(label)}: ${displayNumber(rawUsed, 1)} / ${displayNumber(rawTotal, 1)} ${unit}</title><circle class="donut-free" cx="21" cy="21" r="15.9155"></circle><circle class="donut-used" cx="21" cy="21" r="15.9155" stroke-dasharray="${dash}"></circle><text x="21" y="22">${percent.toFixed(0)}%</text></svg><p><span class="dot used"></span> used ${escapeHtml(displayNumber(rawUsed, 1))}${escapeHtml(unit)} · <span class="dot free"></span> free ${escapeHtml(displayNumber(free, 1))}${escapeHtml(unit)}</p></article>`;
}

function extractQueueMetrics(instance) {
  const paths = [['load', instance?.load], ['resource', instance?.resource], ['meta', instance?.meta], ['queue', instance?.queue], ['queues', instance?.queues], ['stats', instance?.stats]];
  const keywords = /ready|waiting|queued|pending|running|inflight|prepare|prefill|decode|kv|kvcache|transfer|inject/i;
  const reject = /time|timestamp|ts|port|util|ratio|percent|age|memory|mem|gpu_util|cpu/i;
  const seen = new Set();
  const found = [];
  function visit(prefix, value, depth = 0) {
    if (!value || typeof value !== 'object' || depth > 3) return;
    Object.entries(value).forEach(([key, child]) => {
      const path = prefix ? `${prefix}.${key}` : key;
      if (child && typeof child === 'object' && !Array.isArray(child)) visit(path, child, depth + 1);
      const number = finiteNumber(child);
      if (number === null || number < 0 || !keywords.test(key) || reject.test(key)) return;
      if (seen.has(path)) return;
      seen.add(path);
      found.push({ key: path, label: key.replaceAll('_', ' '), value: number });
    });
  }
  paths.forEach(([prefix, value]) => visit(prefix, value));
  return found.sort((a, b) => a.key.localeCompare(b.key));
}

function hardwareDetails(instance) {
  const devices = instance.resource?.raw_resource?.devices || {};
  const cpu = devices.cpu && typeof devices.cpu === 'object' ? devices.cpu : {};
  const gpus = Array.isArray(devices.gpu) ? devices.gpu : [];
  const nics = Array.isArray(devices.network) ? devices.network : (Array.isArray(devices.nic) ? devices.nic : []);
  return { cpu, gpus, nics };
}

function normalizeLoadSnapshots(loadsPayload) {
  return Array.isArray(loadsPayload?.instances) ? loadsPayload.instances : [];
}

function loadSnapshotByInstanceId(loadsPayload, instanceId) {
  return normalizeLoadSnapshots(loadsPayload).find((item) => String(item.instance_id) === String(instanceId)) || null;
}

function mergedInstanceLoad(instance, loadsPayload = state.latest.loads) {
  const snapshot = loadSnapshotByInstanceId(loadsPayload, instance?.instance_id) || {};
  const baseLoad = instance?.load || {};
  return { ...baseLoad, ...snapshot, heartbeat_load: baseLoad, debug_load: snapshot };
}

function nestedNumber(payload, path) {
  return path.split('.').reduce((acc, key) => (acc && typeof acc === 'object' ? acc[key] : undefined), payload);
}

function explicitQueueMetrics(load) {
  return [
    ['inflight', 'Inflight'],
    ['qps_1m', 'QPS 1m'],
    ['prepare_queue_depth', 'Prepare queue depth'],
    ['ready_queue_depth', 'Ready queue depth'],
    ['active_prepare', 'Active prepare'],
    ['active_ready', 'Active ready'],
    ['least_load_score.total', 'Least-load score'],
  ].map(([key, label]) => ({ key, label, value: finiteNumber(nestedNumber(load, key)) }))
    .filter((item) => item.value !== null);
}

function queueMetricsForInstance(instance, loadsPayload = state.latest.loads) {
  const load = mergedInstanceLoad(instance, loadsPayload);
  const explicit = explicitQueueMetrics(load);
  return explicit.length ? explicit : extractQueueMetrics(instance);
}

function applyPausedTopologyState() {
  const container = typeof document !== 'undefined' ? $('systemTopology') : null;
  if (container) {
    container.classList.toggle('paused', state.paused);
  }
}

function gpuName(gpu) {
  return firstString(gpu.name, gpu.model, gpu.product_name) || 'GPU';
}

function gpuUtil(gpu) {
  return finiteNumber(gpu.utilization_pct ?? gpu.utilization_gpu_pct ?? gpu.gpu_util ?? gpu.util);
}

function nicName(nic) {
  return firstString(nic.iface, nic.interface, nic.name, nic.model) || 'NIC';
}

function renderSystemTopology(instances, scheduler) {
  const model = buildTopologyModel(instances, scheduler, state.latest.status || {});
  const layout = computeTopologyLayout(model);
  const linkHtml = model.links.map((link) => {
    const a = layout.positions[link.source];
    const b = layout.positions[link.target];
    if (!a || !b) return '';
    const midY = (a.y + b.y) / 2;
    const d = `M ${a.x} ${a.y + 28} C ${a.x} ${midY}, ${b.x} ${midY}, ${b.x} ${b.y - 30}`;
    return `<path class="topology-link ${escapeHtml(link.status)}" d="${d}"><title>${escapeHtml(link.source)} to ${escapeHtml(link.target)}: ${escapeHtml(link.status)}</title></path>`;
  }).join('');
  const nodeHtml = model.nodes.map((node) => {
    const pos = layout.positions[node.id];
    if (!pos) return '';
    const isInstance = node.type === 'instance';
    const aria = `${node.type} ${node.instanceId || node.label} ${node.status}`;
    return `<g class="topology-svg-node ${escapeHtml(node.type)} ${escapeHtml(node.tone)}" transform="translate(${pos.x} ${pos.y})" ${isInstance ? `role="button" tabindex="0" data-instance-id="${escapeHtml(node.instanceId)}" aria-label="${escapeHtml(aria)}"` : `aria-label="${escapeHtml(aria)}"`}>
      <title>${escapeHtml(nodeTooltip(node))}</title>
      <circle class="node-halo" r="38"></circle>
      <use href="#icon-${escapeHtml(node.type)}" x="-20" y="-24" width="40" height="40"></use>
      <text class="node-label" y="32">${escapeHtml(shortText(node.label, 18))}</text>
      <text class="node-status" y="48">${escapeHtml(shortText(node.status, 22))}</text>
    </g>`;
  }).join('');
  $('systemTopology').classList.toggle('paused', state.paused);
  $('systemTopology').innerHTML = `<div class="topology-scroll"><svg class="topology-svg" viewBox="0 0 ${layout.width} ${layout.height}" aria-labelledby="topologyTitle topologyDesc">
    <title id="topologyTitle">CacheRoute Proxy topology</title><desc id="topologyDesc">Scheduler, local Proxy, and registered Instance nodes. Instance nodes are keyboard selectable.</desc>
    <defs>
      <symbol id="icon-scheduler" viewBox="0 0 48 48"><rect x="8" y="10" width="32" height="22" rx="5"></rect><path d="M16 38h16M24 32v6M16 18h16M16 25h10"></path></symbol>
      <symbol id="icon-proxy" viewBox="0 0 48 48"><path d="M8 24h32M24 8v32M13 13l22 22M35 13L13 35"></path><circle cx="24" cy="24" r="15"></circle></symbol>
      <symbol id="icon-instance" viewBox="0 0 48 48"><rect x="10" y="8" width="28" height="32" rx="4"></rect><path d="M16 17h16M16 25h16M16 33h10"></path><circle cx="33" cy="33" r="2"></circle></symbol>
    </defs>
    <g class="topology-links">${linkHtml}</g><g class="topology-nodes">${nodeHtml}</g>
  </svg></div>${model.instances.length ? '' : '<div class="empty-state compact">No connected instances reported.</div>'}`;
}

function renderInstanceDetail(instances) {
  const panel = $('instanceDetailView');
  if (!state.selectedInstanceId) { panel.classList.add('hidden'); return; }
  const instance = instances.find((item) => String(item.instance_id) === String(state.selectedInstanceId));
  if (!instance) {
    panel.classList.remove('hidden');
    panel.innerHTML = `<button id="backToOverviewBtn">← Back to overview</button><div class="empty-state">Selected instance ${escapeHtml(state.selectedInstanceId)} is not present in the current payload.</div>`;
    return;
  }
  const resource = instance.resource || {};
  const load = mergedInstanceLoad(instance);
  const memPercent = percentOf(resource.memory_used_mb, resource.memory_total_mb);
  const gpuMemPercent = percentOf(resource.gpu_mem_used_mb, resource.gpu_mem_total_mb);
  const networkMax = Math.max(finiteNumber(resource.network_rx_mbps) || 0, finiteNumber(resource.network_tx_mbps) || 0, 1);
  const queues = queueMetricsForInstance(instance);
  const maxQueue = Math.max(1, ...queues.map((item) => item.value));
  const hw = hardwareDetails(instance);
  const staleNote = instance.is_alive === false ? '<p class="stale-note">This Instance is stale; values may reflect the last known report.</p>' : '';
  panel.classList.remove('hidden');
  panel.innerHTML = `<div class="panel-heading detail-header"><div><h2>Instance Detail: ${escapeHtml(instance.instance_id)}</h2><p>${escapeHtml(instance.host || '—')}:${escapeHtml(instance.port || '—')} · ${instanceStateBadge(instance)}</p>${staleNote}</div><button id="backToOverviewBtn">← Back to overview</button></div>
    <div class="kpi-grid">
      ${[['Status', stateLabel(instance)], ['Instance ID', instance.instance_id], ['Host:port', `${instance.host || '—'}:${instance.port || '—'}`], ['Admission', resource.admission_state || '—'], ['Registered', formatTimestamp(instance.registered_at)], ['Last seen age', formatAge(instance.last_seen_at)], ['Resource age', formatAge(resource.resource_reported_at)], ['Inflight', displayNumber(load.inflight, 0)], ['QPS 1m', displayNumber(load.qps_1m, 2)]].map(([k,v]) => `<article class="kpi-card"><span>${escapeHtml(k)}</span><strong>${escapeHtml(v)}</strong></article>`).join('')}
    </div>
    <div class="detail-grid visual-detail">
      <section><h3>Resource utilization</h3>${renderMetricBar({ label:'CPU utilization', value:resource.cpu_util, max:100, unit:'%', secondaryText:'reported resource.cpu_util' })}${renderMetricBar({ label:'System memory', value:resource.memory_used_mb, max:resource.memory_total_mb, unit:' MB', secondaryText:`${displayNumber(resource.memory_used_mb,0)} / ${displayNumber(resource.memory_total_mb,0)} MB (${memPercent === null ? '—' : memPercent.toFixed(1)+'%'})` })}${renderMetricBar({ label:'GPU utilization', value:resource.gpu_util_avg, max:100, unit:'%', secondaryText:'reported resource.gpu_util_avg' })}${renderMetricBar({ label:'GPU memory', value:resource.gpu_mem_used_mb, max:resource.gpu_mem_total_mb, unit:' MB', secondaryText:`${displayNumber(resource.gpu_mem_used_mb,0)} / ${displayNumber(resource.gpu_mem_total_mb,0)} MB (${gpuMemPercent === null ? '—' : gpuMemPercent.toFixed(1)+'%'})` })}${renderMetricBar({ label:'Network RX', value:resource.network_rx_mbps, max:networkMax, unit:' Mbps', secondaryText:'relative to current RX/TX max' })}${renderMetricBar({ label:'Network TX', value:resource.network_tx_mbps, max:networkMax, unit:' Mbps', secondaryText:'relative to current RX/TX max' })}</section>
      <section><h3>Resource composition</h3><div class="donut-grid">${renderDonutChart({ label:'System memory', used:resource.memory_used_mb, total:resource.memory_total_mb, unit:' MB' })}${renderDonutChart({ label:'GPU memory', used:resource.gpu_mem_used_mb, total:resource.gpu_mem_total_mb, unit:' MB' })}</div></section>
      <section><h3>Queue and load</h3><p class="muted">Known load fields: inflight ${escapeHtml(displayNumber(load.inflight,0))}, qps_1m ${escapeHtml(displayNumber(load.qps_1m,2))}, gpu_util ${escapeHtml(displayNumber(load.gpu_util,1))}%.</p>${queues.length ? queues.map((q) => renderMetricBar({ label:q.label, value:q.value, max:maxQueue, unit:'', secondaryText:q.key })).join('') : '<div class="empty-state compact">No detailed queue counters are exposed by the current Instance payload.</div>'}</section>
      <section><h3>Hardware</h3><div class="hardware-grid"><article><h4>CPU</h4><p>${escapeHtml(firstString(hw.cpu.model, hw.cpu.model_name, hw.cpu.name, instance.meta?.cpu_model) || 'No CPU details')}</p>${hw.cpu.cores || hw.cpu.logical_cpus ? `<small>${escapeHtml(`cores ${hw.cpu.cores || '—'} · logical ${hw.cpu.logical_cpus || '—'}`)}</small>` : ''}</article><article><h4>GPU</h4>${hw.gpus.length ? hw.gpus.map((gpu, i) => `<p>${escapeHtml(`#${gpu.index ?? i} ${gpuName(gpu) || 'GPU'}`)}${gpu.uuid ? ` · ${escapeHtml(gpu.uuid)}` : ''} · mem ${escapeHtml(displayNumber(gpu.memory_used_mb,0))}/${escapeHtml(displayNumber(gpu.memory_total_mb,0))} MB${gpu.memory_free_mb !== undefined ? ` · free ${escapeHtml(displayNumber(gpu.memory_free_mb,0))} MB` : ''} · util ${escapeHtml(displayNumber(gpuUtil(gpu),1))}%${gpu.temperature_c !== undefined ? ` · ${escapeHtml(displayNumber(gpu.temperature_c,1))}°C` : ''}${gpu.power_w !== undefined ? ` · ${escapeHtml(displayNumber(gpu.power_w,1))} W` : ''}</p>`).join('') : '<p>No GPU details</p>'}</article><article><h4>NIC</h4>${hw.nics.length ? hw.nics.map((nic) => `<p>${escapeHtml(nicName(nic) || 'NIC')}${nic.driver ? ` · ${escapeHtml(nic.driver)}` : ''}${nic.speed_mbps ? ` · ${escapeHtml(displayNumber(nic.speed_mbps,0))} Mbps` : (nic.speed ? ` · ${escapeHtml(nic.speed)}` : '')}${nic.rx_mbps !== undefined ? ` · RX ${escapeHtml(displayNumber(nic.rx_mbps,2))} Mbps` : ''}${nic.tx_mbps !== undefined ? ` · TX ${escapeHtml(displayNumber(nic.tx_mbps,2))} Mbps` : ''}</p>`).join('') : '<p>No NIC details</p>'}</article></div></section>
      <section class="wide"><details><summary>Raw Instance JSON</summary><button class="copy-inline" data-instance-copy="${escapeHtml(instance.instance_id)}">Copy JSON</button><pre>${escapeHtml(stableJson(instance))}</pre></details></section>
    </div>`;
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
    ['Loads', state.latest.loads],
    ['Topology', state.latest.topology],
    ['Scheduler', state.latest.scheduler],
  ];
  $('rawJsonPanels').classList.toggle('collapsed', !state.rawExpanded);
  $('toggleRawBtn').textContent = state.rawExpanded ? 'Collapse raw JSON' : 'Expand raw JSON';
  $('rawJsonPanels').innerHTML = entries.map(([title, payload]) => `
    <details ${state.rawExpanded ? 'open' : ''}>
      <summary>${escapeHtml(title)}</summary>
      <button class="copy-inline" data-copy-key="${escapeHtml(title)}">Copy</button>
      <pre>${escapeHtml(stableJson(payload || {}))}</pre>
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

  const finiteValues = series.flatMap((item) => item.values).filter((value) => Number.isFinite(value));
  if (!finiteValues.length) {
    context.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--muted');
    context.fillText('No metrics in rolling buffer', padding + 8, height / 2);
    return;
  }
  context.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--muted');
  context.fillText(options.yLabel || '', padding + 2, 12);
  context.fillText(historyTimeLabel(), padding + 2, height - 4);
  const maxY = options.maxY ?? Math.max(1, ...finiteValues);
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

function historyTimeLabel() {
  if (state.history.length < 2) {
    return 'recent polling samples';
  }
  return `${state.history[0].at.toLocaleTimeString()} → ${state.history[state.history.length - 1].at.toLocaleTimeString()}`;
}

function renderCharts() {
  const history = state.history;
  drawLineChart('chartCpu', [{ values: history.map((item) => item.cpu) }], { maxY: 100, colors: ['#5ddcff'], yLabel: '%' });
  drawLineChart('chartMemory', [{ values: history.map((item) => item.memory) }], { maxY: 100, colors: ['#45d483'], yLabel: '%' });
  drawLineChart('chartGpu', [{ values: history.map((item) => item.gpu) }], { maxY: 100, colors: ['#b38cff'], yLabel: '%' });
  drawLineChart('chartLiveness', [
    { values: history.map((item) => item.alive) },
    { values: history.map((item) => item.stale) },
  ], { colors: ['#45d483', '#ff6b6b'], yLabel: 'count' });
  drawLineChart('chartNetwork', [
    { values: history.map((item) => item.rx) },
    { values: history.map((item) => item.tx) },
  ], { colors: ['#5ddcff', '#ffca5f'], yLabel: 'Mbps' });
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
  renderSystemTopology(instances, state.latest.scheduler);
  renderInstanceDetail(instances);
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

  const [health, status, instances, resources, loads, topology, scheduler] = await Promise.all([
    settleJson('/api/proxy/healthz'),
    settleJson('/api/proxy/status'),
    settleJson('/api/proxy/instances?include_dead=true'),
    settleJson('/api/proxy/resources?include_dead=true'),
    settleJson('/api/proxy/loads', { optional: true }),
    settleJson('/api/proxy/topology', { optional: true }),
    settleJson('/api/scheduler/proxy', { optional: true }),
  ]);

  state.latest = { health, status, instances, resources, loads, topology, scheduler };
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
  if (typeof localStorage !== 'undefined') localStorage.setItem('proxy-ui-theme', theme);
  $('themeBtn').textContent = theme === 'dark' ? 'Light theme' : 'Dark theme';
}

function setupEventHandlers() {
  $('refreshBtn').addEventListener('click', () => refresh());
  $('pauseBtn').addEventListener('click', () => {
    state.paused = !state.paused;
    $('pauseBtn').textContent = state.paused ? 'Resume polling' : 'Pause polling';
    $('pollingState').outerHTML = badge(state.paused ? 'Paused' : 'Polling', state.paused ? 'warn' : 'ok').replace('<span', '<span id="pollingState"');
    applyPausedTopologyState();
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
    const card = event.target.closest('.instance-card[data-instance-id], .topology-svg-node[data-instance-id]');
    if (card) {
      window.location.hash = `#/instances/${encodeURIComponent(card.dataset.instanceId)}`;
      return;
    }
    if (event.target.closest('#backToOverviewBtn')) {
      window.location.hash = '#/';
      return;
    }
    const instanceCopy = event.target.closest('[data-instance-copy]');
    if (instanceCopy) {
      const instances = Array.isArray(state.latest.instances) ? state.latest.instances : [];
      const selected = instances.find((item) => String(item.instance_id) === String(instanceCopy.dataset.instanceCopy));
      copyText(stableJson(selected || {}));
      return;
    }
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
      Loads: state.latest.loads,
      Topology: state.latest.topology,
      Scheduler: state.latest.scheduler,
    };
    copyText(stableJson(payloads[key] || state.latest));
  });
  document.addEventListener('keydown', (event) => {
    const card = event.target.closest?.('.instance-card[data-instance-id], .topology-svg-node[data-instance-id]');
    if (card && (event.key === 'Enter' || event.key === ' ')) {
      event.preventDefault();
      window.location.hash = `#/instances/${encodeURIComponent(card.dataset.instanceId)}`;
    }
  });
  window.addEventListener('hashchange', syncRoute);
  syncRoute();
}

function syncRoute() {
  const match = window.location.hash.match(/^#\/instances\/(.+)$/);
  state.selectedInstanceId = match ? decodeURIComponent(match[1]) : null;
  renderInstanceDetail(Array.isArray(state.latest.instances) ? state.latest.instances : []);
  if (state.selectedInstanceId) {
    $('instanceDetailView').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
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

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { buildTopologyModel, computeTopologyLayout, renderMetricBar, renderDonutChart, extractQueueMetrics, queueMetricsForInstance, finiteNumber, clampPercent, percentOf, fmt, stateLabel, hardwareDetails, mergedInstanceLoad, applyPausedTopologyState, renderInstanceDetail, renderSystemTopology, state };
} else {
  setTheme(state.theme);
  setupEventHandlers();
  refresh().then(restartTimer);
}
