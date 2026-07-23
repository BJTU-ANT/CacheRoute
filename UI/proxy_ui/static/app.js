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
  topology: {
    positions: {},
    previousPositions: {},
    pinnedNodeIds: new Set(),
    signature: '',
    viewportSignature: '',
    zoom: 1,
    panX: 0,
    panY: 0,
    hoveredNodeId: null,
    dragging: null,
    panning: null,
    model: null,
    linkPaths: {},
    fitRequested: true,
    suppressNextClick: false,
    handlersInstalled: false,
    animationFrame: null,
  },
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
  if (finiteNumber(resource.resource_reported_at) !== null) {
    return true;
  }
  const scalarKeys = [
    'cpu_util', 'memory_used_mb', 'memory_total_mb', 'memory_free_mb',
    'gpu_util_avg', 'gpu_mem_used_mb', 'gpu_mem_total_mb',
    'network_rx_mbps', 'network_tx_mbps',
  ];
  return scalarKeys.some((key) => finiteNumber(resource[key]) !== null);
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
  const cpu = average(instances.map((item) => finiteNumber(item.resource?.cpu_util)));
  const memory = average(instances.map((item) => {
    const used = finiteNumber(item.resource?.memory_used_mb);
    const total = finiteNumber(item.resource?.memory_total_mb);
    return used !== null && total !== null && total > 0 ? (used / total) * 100 : null;
  }));
  const gpu = average(instances.map((item) => finiteNumber(item.resource?.gpu_util_avg)));
  const rx = average(instances.map((item) => finiteNumber(item.resource?.network_rx_mbps)));
  const tx = average(instances.map((item) => finiteNumber(item.resource?.network_tx_mbps)));
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
  if (typeof value === 'number') {
    return Number.isFinite(value) ? value : null;
  }
  if (typeof value === 'string') {
    const trimmed = value.trim();
    if (!trimmed) return null;
    const numberPattern = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i;
    if (!numberPattern.test(trimmed)) return null;
    const number = Number(trimmed);
    return Number.isFinite(number) ? number : null;
  }
  return null;
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

function topologySignature(model) {
  return model.nodes.map((node) => node.id).sort().join('|');
}

function stableHash(value) {
  let hash = 2166136261;
  const text = String(value ?? '');
  for (let i = 0; i < text.length; i += 1) {
    hash ^= text.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  return hash >>> 0;
}

function seededUnitValue(seed, salt = '') {
  return stableHash(`${seed}:${salt}`) / 4294967295;
}

function normalizeVector(dx, dy) {
  const length = Math.hypot(dx, dy);
  if (!length) return { x: 1, y: 0, length: 0 };
  return { x: dx / length, y: dy / length, length };
}

function topologyBounds(options = {}) {
  const width = Math.max(720, Number(options.width) || 980);
  const height = Math.max(360, Number(options.height) || 560);
  const nodePadding = Number(options.nodePadding ?? 58);
  const labelPadding = Number(options.labelPadding ?? 78);
  return { width, height, nodePadding, labelPadding };
}

function preferredInstanceRadius(count) {
  return Math.min(330, 145 + Math.sqrt(Math.max(1, count)) * 34 + Math.max(0, count - 12) * 2.4);
}

function clampTopologyPosition(pos, bounds) {
  return {
    x: Math.max(bounds.nodePadding, Math.min(bounds.width - bounds.nodePadding, pos.x)),
    y: Math.max(bounds.nodePadding, Math.min(bounds.height - bounds.labelPadding, pos.y)),
  };
}

function createInitialTopologyPositions(model, options = {}, existing = {}) {
  const bounds = topologyBounds(options);
  const center = { x: bounds.width / 2, y: bounds.height / 2 + 12 };
  const count = model.instances.length;
  const radius = preferredInstanceRadius(count);
  const positions = {};
  const keep = options.preserveExisting !== false;
  model.nodes.forEach((node, index) => {
    if (keep && existing[node.id]) {
      positions[node.id] = clampTopologyPosition(existing[node.id], bounds);
      return;
    }
    if (node.type === 'proxy') {
      positions[node.id] = clampTopologyPosition({ x: center.x + (seededUnitValue(node.id, 'jx') - 0.5) * 18, y: center.y + (seededUnitValue(node.id, 'jy') - 0.5) * 18 }, bounds);
    } else if (node.type === 'scheduler') {
      positions[node.id] = clampTopologyPosition({ x: center.x - Math.min(230, radius * 0.86), y: center.y - Math.min(170, radius * 0.62) }, bounds);
    } else {
      const idx = Math.max(0, model.instances.findIndex((item) => `instance:${item.instance_id || 'unknown'}` === node.id));
      const golden = Math.PI * (3 - Math.sqrt(5));
      const angle = idx * golden + seededUnitValue(`${node.type}:${node.id}`, 'angle') * 0.9;
      const r = radius * (0.72 + seededUnitValue(node.id, 'radius') * 0.36);
      positions[node.id] = clampTopologyPosition({
        x: center.x + Math.cos(angle) * r + (seededUnitValue(node.id, 'x') - 0.5) * 42,
        y: center.y + Math.sin(angle) * r * 0.82 + (seededUnitValue(node.id, 'y') - 0.5) * 42,
      }, bounds);
    }
    if (!positions[node.id]) positions[node.id] = clampTopologyPosition({ x: center.x + index * 4, y: center.y }, bounds);
  });
  return positions;
}

function applyRepulsion(nodes, positions, velocities, strength = 7200) {
  for (let i = 0; i < nodes.length; i += 1) {
    for (let j = i + 1; j < nodes.length; j += 1) {
      const a = nodes[i]; const b = nodes[j];
      const delta = normalizeVector(positions[a.id].x - positions[b.id].x, positions[a.id].y - positions[b.id].y);
      const distance = Math.max(32, delta.length);
      const force = strength / (distance * distance);
      velocities[a.id].x += delta.x * force; velocities[a.id].y += delta.y * force;
      velocities[b.id].x -= delta.x * force; velocities[b.id].y -= delta.y * force;
    }
  }
}

function applySpringForces(links, positions, velocities, model) {
  const count = model.instances.length;
  links.forEach((link) => {
    const a = positions[link.source]; const b = positions[link.target];
    if (!a || !b) return;
    const delta = normalizeVector(b.x - a.x, b.y - a.y);
    const wanted = link.id === 'scheduler-proxy' ? 210 : Math.min(360, preferredInstanceRadius(count) * 0.88);
    const force = (delta.length - wanted) * (link.id === 'scheduler-proxy' ? 0.018 : 0.014);
    velocities[link.source].x += delta.x * force; velocities[link.source].y += delta.y * force;
    velocities[link.target].x -= delta.x * force; velocities[link.target].y -= delta.y * force;
  });
}

function applyRadialForce(model, positions, velocities) {
  const proxy = positions.proxy;
  if (!proxy) return;
  const radius = preferredInstanceRadius(model.instances.length);
  model.nodes.filter((node) => node.type === 'instance').forEach((node) => {
    const pos = positions[node.id];
    const delta = normalizeVector(pos.x - proxy.x, pos.y - proxy.y);
    const wanted = radius * (0.78 + seededUnitValue(node.id, 'radial') * 0.32);
    const force = (delta.length - wanted) * 0.01;
    velocities[node.id].x -= delta.x * force;
    velocities[node.id].y -= delta.y * force;
  });
}

function resolveCollisions(nodes, positions, bounds, minSpacing = 92) {
  for (let i = 0; i < nodes.length; i += 1) {
    for (let j = i + 1; j < nodes.length; j += 1) {
      const a = positions[nodes[i].id]; const b = positions[nodes[j].id];
      const delta = normalizeVector(b.x - a.x, b.y - a.y);
      const overlap = minSpacing - Math.max(1, delta.length);
      if (overlap > 0) {
        b.x += delta.x * overlap * 0.5; b.y += delta.y * overlap * 0.5;
        a.x -= delta.x * overlap * 0.5; a.y -= delta.y * overlap * 0.5;
      }
    }
  }
  nodes.forEach((node) => { positions[node.id] = clampTopologyPosition(positions[node.id], bounds); });
}

function relaxTopologyLayout(model, positions, options = {}) {
  const bounds = topologyBounds(options);
  const pinned = options.pinnedNodeIds || new Set();
  const iterations = Math.max(1, Math.min(150, Number(options.iterations) || 110));
  const nodes = model.nodes;
  const center = { x: bounds.width / 2, y: bounds.height / 2 };
  const result = Object.fromEntries(Object.entries(positions).map(([id, pos]) => [id, { ...pos }]));
  for (let tick = 0; tick < iterations; tick += 1) {
    const velocities = Object.fromEntries(nodes.map((node) => [node.id, { x: 0, y: 0 }]));
    applyRepulsion(nodes, result, velocities, 8200);
    applySpringForces(model.links, result, velocities, model);
    applyRadialForce(model, result, velocities);
    nodes.forEach((node) => {
      if (pinned.has(node.id)) return;
      const pos = result[node.id];
      velocities[node.id].x += (center.x - pos.x) * 0.004;
      velocities[node.id].y += (center.y - pos.y) * 0.004;
      pos.x += Math.max(-18, Math.min(18, velocities[node.id].x)) * 0.82;
      pos.y += Math.max(-18, Math.min(18, velocities[node.id].y)) * 0.82;
      result[node.id] = clampTopologyPosition(pos, bounds);
    });
    resolveCollisions(nodes.filter((node) => !pinned.has(node.id)), result, bounds, 92);
  }
  return result;
}

function computeTopologyLayout(model, options = {}) {
  const bounds = topologyBounds({ width: options.width, height: options.height || Math.max(520, 420 + Math.sqrt(model.instances.length) * 56) });
  const initial = createInitialTopologyPositions(model, bounds, options.existingPositions || {});
  const positions = relaxTopologyLayout(model, initial, { ...bounds, pinnedNodeIds: options.pinnedNodeIds || new Set(), iterations: options.iterations || 110 });
  return { ...bounds, positions, nodePadding: bounds.nodePadding, labelPadding: bounds.labelPadding };
}

function deterministicLinkCurvature(linkId) {
  const direction = seededUnitValue(linkId, 'curve') < 0.5 ? -1 : 1;
  const magnitude = 26 + seededUnitValue(linkId, 'curve-size') * 34;
  return direction * magnitude;
}

function edgePoint(source, target, radius = 42) {
  const delta = normalizeVector(target.x - source.x, target.y - source.y);
  return { x: source.x + delta.x * radius, y: source.y + delta.y * radius };
}

function computeLinkPath(link, positions, radii = {}) {
  const a = positions[link.source]; const b = positions[link.target];
  if (!a || !b) return '';
  const sourceRadius = radii[link.source] || 42;
  const targetRadius = radii[link.target] || 42;
  const start = edgePoint(a, b, sourceRadius);
  const end = edgePoint(b, a, targetRadius);
  const delta = normalizeVector(end.x - start.x, end.y - start.y);
  const curve = deterministicLinkCurvature(link.id) * (link.id === 'scheduler-proxy' ? 1.25 : 1);
  const cx = (start.x + end.x) / 2 + -delta.y * curve;
  const cy = (start.y + end.y) / 2 + delta.x * curve;
  return `M ${start.x.toFixed(2)} ${start.y.toFixed(2)} Q ${cx.toFixed(2)} ${cy.toFixed(2)} ${end.x.toFixed(2)} ${end.y.toFixed(2)}`;
}

function computeHoverRelationships(model, activeNodeId) {
  const nodeIds = new Set(model.nodes.map((node) => node.id));
  const relatedNodes = new Set();
  const relatedLinks = new Set();
  if (!activeNodeId || !nodeIds.has(activeNodeId)) return { activeNodeId: null, relatedNodes, relatedLinks, dimmedNodes: new Set(), dimmedLinks: new Set() };
  relatedNodes.add(activeNodeId);
  if (activeNodeId === 'proxy') {
    model.nodes.forEach((node) => relatedNodes.add(node.id));
    model.links.forEach((link) => relatedLinks.add(link.id));
  } else {
    model.links.forEach((link) => {
      if (link.source === activeNodeId || link.target === activeNodeId) {
        relatedLinks.add(link.id);
        relatedNodes.add(link.source);
        relatedNodes.add(link.target);
      }
    });
  }
  return {
    activeNodeId,
    relatedNodes,
    relatedLinks,
    dimmedNodes: new Set([...nodeIds].filter((id) => !relatedNodes.has(id))),
    dimmedLinks: new Set(model.links.map((link) => link.id).filter((id) => !relatedLinks.has(id))),
  };
}

function clampZoom(value) {
  return Math.max(0.45, Math.min(2.5, finiteNumber(value) ?? 1));
}

function dragExceededThreshold(dx, dy, threshold = 5) {
  return Math.hypot(dx, dy) >= threshold;
}

function fitTopologyToView(positions, viewport, padding = 60) {
  const nodes = Object.values(positions || {});
  const width = Math.max(1, viewport.width || 980);
  const height = Math.max(1, viewport.height || 560);
  if (!nodes.length) return { zoom: 1, panX: 0, panY: 0 };
  const minX = Math.min(...nodes.map((p) => p.x)) - padding;
  const maxX = Math.max(...nodes.map((p) => p.x)) + padding;
  const minY = Math.min(...nodes.map((p) => p.y)) - padding;
  const maxY = Math.max(...nodes.map((p) => p.y)) + padding + 28;
  const graphWidth = Math.max(1, maxX - minX);
  const graphHeight = Math.max(1, maxY - minY);
  const zoom = clampZoom(Math.min(width / graphWidth, height / graphHeight));
  return { zoom, panX: (width - graphWidth * zoom) / 2 - minX * zoom, panY: (height - graphHeight * zoom) / 2 - minY * zoom };
}

function tooltipValue(value) {
  if (value === null || value === undefined || value === '') return '—';
  if (typeof value === 'boolean' || Array.isArray(value) || typeof value === 'object') return '—';
  return String(value);
}

function nodeTooltip(node) {
  return buildTooltipModel(node).rows.map((row) => `${row.label}: ${row.value}`).join('\n');
}

function buildTooltipModel(node, model = state.topology.model) {
  const raw = node.raw || {};
  const resource = raw.resource || {};
  const load = node.type === 'instance' ? mergedInstanceLoad(raw) : {};
  const fmtPercent = (value) => finiteNumber(value) === null ? '—' : `${fmt(value)}%`;
  const fmtMb = (used, total) => `${displayNumber(used)} / ${displayNumber(total)} MB`;
  if (node.type === 'instance') {
    return { title: `Instance ${tooltipValue(node.instanceId)}`, rows: [
      ['Instance ID', node.instanceId], ['Host and port', raw.host ? `${raw.host}:${raw.port ?? '—'}` : ''], ['State', node.status],
      ['CPU utilization', fmtPercent(resource.cpu_util)], ['System memory', fmtMb(resource.memory_used_mb, resource.memory_total_mb)],
      ['GPU utilization', fmtPercent(resource.gpu_util_avg ?? load.gpu_util)], ['GPU memory', fmtMb(resource.gpu_mem_used_mb, resource.gpu_mem_total_mb)],
      ['Inflight', load.inflight], ['QPS 1m', load.qps_1m], ['Prepare queue depth', load.prepare_queue_depth], ['Ready queue depth', load.ready_queue_depth],
      ['Active prepare', load.active_prepare], ['Active ready', load.active_ready], ['Least-load score', nestedNumber(load, 'least_load_score.total')],
      ['Last seen age', formatAge(raw.last_seen_at)], ['Resource report age', formatAge(resource.resource_reported_at)],
    ].map(([label, value]) => ({ label, value: tooltipValue(value) })) };
  }
  if (node.type === 'proxy') {
    const total = model?.instances?.length ?? 0;
    const alive = model?.instances?.filter((item) => item.is_alive === true).length ?? 0;
    return { title: 'Proxy', rows: [
      ['Proxy ID', model?.proxyId || raw.proxy_id || node.status], ['Health state', raw.ok === false ? 'error' : 'ok'], ['TTL', raw.ttl_seconds ?? raw.ttl],
      ['Alive Instance count', alive], ['Total Instance count', total], ['Stale Instance count', Math.max(0, total - alive)],
      ['Resource report count', (model?.instances || []).filter(hasResourceReport).length], ['Topology link count', model?.links?.length],
    ].map(([label, value]) => ({ label, value: tooltipValue(value) })) };
  }
  return { title: 'Scheduler', rows: [
    ['Registration state', node.status], ['Configured Proxy ID', state.config?.proxy_id || model?.proxyId],
    ['Scheduler response state', raw.ok === false ? 'error' : raw.ok === true ? 'ok' : raw.error],
    ['Last known Proxy registration', raw.proxy_id || raw.registered_proxy_id || raw.proxy || raw.data?.proxy_id],
  ].map(([label, value]) => ({ label, value: tooltipValue(value) })) };
}

function tooltipHtml(node) {
  const model = buildTooltipModel(node);
  return `<div class="topology-tooltip-title">${escapeHtml(model.title)}</div><dl>${model.rows.map((row) => `<div><dt>${escapeHtml(row.label)}</dt><dd>${escapeHtml(row.value)}</dd></div>`).join('')}</dl>`;
}

function topologyViewportSize(container) {
  const rect = container?.getBoundingClientRect?.() || { width: 980, height: 560 };
  return { width: Math.max(720, Math.round(rect.width || 980)), height: Math.max(420, Math.round((rect.height && rect.height > 120 ? rect.height : 560))) };
}

function ensureTopologyShell(container) {
  if (container.querySelector?.('.topology-svg')) return;
  container.innerHTML = `<div class="topology-toolbar topology-controls" aria-label="Topology controls">
    <button type="button" data-topology-action="zoom-in" aria-label="Zoom in topology">+</button>
    <button type="button" data-topology-action="zoom-out" aria-label="Zoom out topology">−</button>
    <button type="button" data-topology-action="fit" aria-label="Fit topology to view">Fit</button>
    <button type="button" data-topology-action="reset" aria-label="Reset topology layout">Reset</button>
  </div><div class="topology-scroll"><div class="topology-tooltip hidden" role="tooltip"></div><svg class="topology-svg" tabindex="0" aria-labelledby="topologyTitle topologyDesc">
    <title id="topologyTitle">CacheRoute Proxy topology</title><desc id="topologyDesc">Interactive Scheduler, Proxy, and Instance network topology.</desc>
    <defs>
      <symbol id="icon-scheduler" viewBox="0 0 48 48"><rect x="8" y="10" width="32" height="22" rx="5"></rect><path d="M16 38h16M24 32v6M16 18h16M16 25h10"></path></symbol>
      <symbol id="icon-proxy" viewBox="0 0 48 48"><path d="M8 24h32M24 8v32M13 13l22 22M35 13L13 35"></path><circle cx="24" cy="24" r="15"></circle></symbol>
      <symbol id="icon-instance" viewBox="0 0 48 48"><rect x="10" y="8" width="28" height="32" rx="4"></rect><path d="M16 17h16M16 25h16M16 33h10"></path><circle cx="33" cy="33" r="2"></circle></symbol>
      <filter id="particleGlow"><feGaussianBlur stdDeviation="2" result="blur"></feGaussianBlur><feMerge><feMergeNode in="blur"></feMergeNode><feMergeNode in="SourceGraphic"></feMergeNode></feMerge></filter>
    </defs><rect class="topology-bg" x="0" y="0" width="100%" height="100%"></rect><g class="topology-viewport"><g class="topology-links"></g><g class="topology-particles"></g><g class="topology-nodes"></g></g>
  </svg></div><div class="topology-empty empty-state compact hidden">No connected instances reported.</div>`;
  installTopologyHandlers(container);
}

function renderTopologyStructure(container, model) {
  const linksGroup = container.querySelector('.topology-links');
  const nodesGroup = container.querySelector('.topology-nodes');
  const particlesGroup = container.querySelector('.topology-particles');
  const existingNodes = new Set([...nodesGroup.querySelectorAll('.topology-svg-node')].map((el) => el.dataset.nodeId));
  model.links.forEach((link) => {
    if (!linksGroup.querySelector(`[data-link-id="${CSS.escape(link.id)}"]`)) {
      linksGroup.insertAdjacentHTML('beforeend', `<path id="topology-path-${escapeHtml(link.id)}" class="topology-link" data-link-id="${escapeHtml(link.id)}"><title></title></path>`);
      particlesGroup.insertAdjacentHTML('beforeend', `<circle r="4" class="topology-particle" data-link-id="${escapeHtml(link.id)}"><animateMotion dur="3s" repeatCount="indefinite" rotate="auto"><mpath href="#topology-path-${escapeHtml(link.id)}"></mpath></animateMotion></circle>`);
    }
  });
  [...linksGroup.querySelectorAll('.topology-link')].forEach((el) => { if (!model.links.some((l) => l.id === el.dataset.linkId)) el.remove(); });
  [...particlesGroup.querySelectorAll('.topology-particle')].forEach((el) => { if (!model.links.some((l) => l.id === el.dataset.linkId)) el.remove(); });
  model.nodes.forEach((node) => {
    if (existingNodes.has(node.id)) return;
    const isInstance = node.type === 'instance';
    nodesGroup.insertAdjacentHTML('beforeend', `<g class="topology-svg-node ${escapeHtml(node.type)}" data-node-id="${escapeHtml(node.id)}" ${isInstance ? `role="button" tabindex="0" data-instance-id="${escapeHtml(node.instanceId)}"` : 'tabindex="0"'}><title></title><circle class="node-halo" r="38"></circle><use href="#icon-${escapeHtml(node.type)}" x="-20" y="-24" width="40" height="40"></use><text class="node-label" y="32"></text><text class="node-status" y="48"></text></g>`);
  });
  [...nodesGroup.querySelectorAll('.topology-svg-node')].forEach((el) => { if (!model.nodes.some((n) => n.id === el.dataset.nodeId)) el.remove(); });
}

function updateTopologyMetrics(container, model) {
  model.nodes.forEach((node) => {
    const el = container.querySelector(`.topology-svg-node[data-node-id="${CSS.escape(node.id)}"]`);
    if (!el) return;
    el.className.baseVal = `topology-svg-node ${node.type} ${node.tone}`;
    el.setAttribute('aria-label', `${node.type} ${node.instanceId || node.label} ${node.status}`);
    el.querySelector('title').textContent = nodeTooltip(node);
    el.querySelector('.node-label').textContent = shortText(node.label, 18);
    el.querySelector('.node-status').textContent = shortText(node.status, 22);
  });
  model.links.forEach((link) => {
    const el = container.querySelector(`.topology-link[data-link-id="${CSS.escape(link.id)}"]`);
    const particle = container.querySelector(`.topology-particle[data-link-id="${CSS.escape(link.id)}"]`);
    if (el) { el.className.baseVal = `topology-link ${link.status}`; el.querySelector('title').textContent = `${link.source} to ${link.target}: ${link.status}`; }
    if (particle) particle.className.baseVal = `topology-particle ${link.status}`;
  });
}

function updateTopologyTransforms(container) {
  const viewport = container.querySelector('.topology-viewport');
  if (viewport) viewport.setAttribute('transform', `translate(${state.topology.panX} ${state.topology.panY}) scale(${state.topology.zoom})`);
}

function updateTopologyLinkPaths(container, model) {
  state.topology.linkPaths = {};
  model.links.forEach((link) => {
    const d = computeLinkPath(link, state.topology.positions);
    state.topology.linkPaths[link.id] = d;
    const el = container.querySelector(`.topology-link[data-link-id="${CSS.escape(link.id)}"]`);
    if (el) el.setAttribute('d', d);
  });
}

function applyTopologyPositions(container) {
  Object.entries(state.topology.positions).forEach(([id, pos]) => {
    const el = container.querySelector(`.topology-svg-node[data-node-id="${CSS.escape(id)}"]`);
    if (el) el.setAttribute('transform', `translate(${pos.x.toFixed(2)} ${pos.y.toFixed(2)})`);
  });
  updateTopologyLinkPaths(container, state.topology.model);
}

function animateTopologyPositions(container, nextPositions) {
  const reduced = typeof matchMedia !== 'undefined' && matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (reduced) { state.topology.positions = nextPositions; applyTopologyPositions(container); return; }
  const start = { ...state.topology.positions };
  const started = performance.now();
  cancelAnimationFrame(state.topology.animationFrame);
  function frame(now) {
    const t = Math.min(1, (now - started) / 260);
    const eased = 1 - Math.pow(1 - t, 3);
    Object.entries(nextPositions).forEach(([id, next]) => {
      const prev = start[id] || next;
      state.topology.positions[id] = { x: prev.x + (next.x - prev.x) * eased, y: prev.y + (next.y - prev.y) * eased };
    });
    applyTopologyPositions(container);
    if (t < 1) state.topology.animationFrame = requestAnimationFrame(frame);
  }
  state.topology.animationFrame = requestAnimationFrame(frame);
}

function applyTopologyHover(container, nodeId) {
  state.topology.hoveredNodeId = nodeId || null;
  const rel = computeHoverRelationships(state.topology.model || { nodes: [], links: [] }, nodeId);
  container.querySelectorAll('.topology-svg-node').forEach((el) => {
    const id = el.dataset.nodeId;
    el.classList.toggle('is-hovered', id === nodeId);
    el.classList.toggle('is-neighbor', nodeId && rel.relatedNodes.has(id) && id !== nodeId);
    el.classList.toggle('is-dimmed', rel.dimmedNodes.has(id));
  });
  container.querySelectorAll('.topology-link, .topology-particle').forEach((el) => {
    const id = el.dataset.linkId;
    el.classList.toggle('is-related', rel.relatedLinks.has(id));
    el.classList.toggle('is-dimmed', rel.dimmedLinks.has(id));
  });
}

function showTopologyTooltip(container, nodeId, event) {
  const node = state.topology.model?.nodes.find((item) => item.id === nodeId);
  const tooltip = container.querySelector('.topology-tooltip');
  if (!node || !tooltip) return;
  tooltip.innerHTML = tooltipHtml(node);
  tooltip.classList.remove('hidden');
  positionTopologyTooltip(container, event || { clientX: 0, clientY: 0 });
}

function positionTopologyTooltip(container, event) {
  const tooltip = container.querySelector('.topology-tooltip');
  const scroll = container.querySelector('.topology-scroll');
  if (!tooltip || tooltip.classList.contains('hidden') || !scroll?.getBoundingClientRect) return;
  const rect = scroll.getBoundingClientRect();
  const tip = tooltip.getBoundingClientRect();
  const x = Math.max(8, Math.min(rect.width - tip.width - 8, (event.clientX || rect.left + 12) - rect.left + 16));
  const y = Math.max(8, Math.min(rect.height - tip.height - 8, (event.clientY || rect.top + 12) - rect.top + 16));
  tooltip.style.transform = `translate(${x}px, ${y}px)`;
}

function hideTopologyTooltip(container) {
  container.querySelector('.topology-tooltip')?.classList.add('hidden');
}

function installTopologyHandlers(container) {
  if (state.topology.handlersInstalled) return;
  state.topology.handlersInstalled = true;
  container.addEventListener('pointerover', (event) => {
    const node = event.target.closest?.('.topology-svg-node[data-node-id]');
    if (!node || state.topology.dragging) return;
    applyTopologyHover(container, node.dataset.nodeId); showTopologyTooltip(container, node.dataset.nodeId, event);
  });
  container.addEventListener('pointermove', (event) => {
    if (state.topology.dragging) {
      const drag = state.topology.dragging; const dx = (event.clientX - drag.startX) / state.topology.zoom; const dy = (event.clientY - drag.startY) / state.topology.zoom;
      if (!drag.active && dragExceededThreshold(event.clientX - drag.startX, event.clientY - drag.startY)) { drag.active = true; drag.node.classList.add('is-dragging'); document.body.classList.add('topology-no-select'); }
      if (drag.active) { state.topology.positions[drag.nodeId] = clampTopologyPosition({ x: drag.nodeStart.x + dx, y: drag.nodeStart.y + dy }, topologyBounds({ width: drag.width, height: drag.height })); applyTopologyPositions(container); }
      return;
    }
    if (state.topology.panning) {
      const pan = state.topology.panning; state.topology.panX = pan.startPanX + event.clientX - pan.startX; state.topology.panY = pan.startPanY + event.clientY - pan.startY; updateTopologyTransforms(container); return;
    }
    positionTopologyTooltip(container, event);
  });
  container.addEventListener('pointerout', (event) => {
    const node = event.target.closest?.('.topology-svg-node[data-node-id]');
    if (node && !container.contains(event.relatedTarget)) { applyTopologyHover(container, null); hideTopologyTooltip(container); }
  });
  container.addEventListener('focusin', (event) => { const node = event.target.closest?.('.topology-svg-node[data-node-id]'); if (node) { applyTopologyHover(container, node.dataset.nodeId); showTopologyTooltip(container, node.dataset.nodeId, event); } });
  container.addEventListener('focusout', () => { applyTopologyHover(container, null); hideTopologyTooltip(container); });
  container.addEventListener('pointerdown', (event) => {
    const svg = container.querySelector('.topology-svg'); const viewport = topologyViewportSize(container);
    const node = event.target.closest?.('.topology-svg-node[data-node-id]');
    if (node) { const pos = state.topology.positions[node.dataset.nodeId]; state.topology.dragging = { node, nodeId: node.dataset.nodeId, startX: event.clientX, startY: event.clientY, nodeStart: { ...pos }, active: false, width: viewport.width, height: viewport.height }; node.setPointerCapture?.(event.pointerId); return; }
    if (event.target.closest?.('.topology-bg, .topology-svg')) { state.topology.panning = { startX: event.clientX, startY: event.clientY, startPanX: state.topology.panX, startPanY: state.topology.panY }; svg?.setPointerCapture?.(event.pointerId); container.classList.add('is-panning'); }
  });
  container.addEventListener('pointerup', (event) => {
    if (state.topology.dragging) { const drag = state.topology.dragging; if (drag.active) { state.topology.pinnedNodeIds.add(drag.nodeId); state.topology.suppressNextClick = true; event.preventDefault(); } drag.node.classList.remove('is-dragging'); document.body.classList.remove('topology-no-select'); state.topology.dragging = null; }
    if (state.topology.panning) { state.topology.panning = null; container.classList.remove('is-panning'); }
  });
  container.addEventListener('wheel', (event) => { event.preventDefault(); zoomTopology(container, event.deltaY < 0 ? 1.12 : 0.88, event); }, { passive: false });
  container.addEventListener('click', (event) => {
    const action = event.target.closest?.('[data-topology-action]')?.dataset.topologyAction;
    if (!action) return;
    if (action === 'zoom-in') zoomTopology(container, 1.18);
    if (action === 'zoom-out') zoomTopology(container, 0.84);
    if (action === 'fit') applyFitToView(container);
    if (action === 'reset') { state.topology.pinnedNodeIds.clear(); state.topology.positions = {}; state.topology.signature = ''; state.topology.fitRequested = true; renderSystemTopology(Array.isArray(state.latest.instances) ? state.latest.instances : [], state.latest.scheduler); }
  });
}

function zoomTopology(container, factor, event) {
  const scroll = container.querySelector('.topology-scroll'); const rect = scroll?.getBoundingClientRect?.() || { left: 0, top: 0, width: 980, height: 560 };
  const old = state.topology.zoom; const next = clampZoom(old * factor);
  const px = event ? event.clientX - rect.left : rect.width / 2; const py = event ? event.clientY - rect.top : rect.height / 2;
  state.topology.panX = px - ((px - state.topology.panX) / old) * next;
  state.topology.panY = py - ((py - state.topology.panY) / old) * next;
  state.topology.zoom = next; updateTopologyTransforms(container);
}

function applyFitToView(container) {
  const viewport = topologyViewportSize(container);
  const fit = fitTopologyToView(state.topology.positions, viewport, 70);
  Object.assign(state.topology, fit); updateTopologyTransforms(container);
}

function renderSystemTopology(instances, scheduler) {
  const container = $('systemTopology');
  if (!container) return;
  ensureTopologyShell(container);
  const model = buildTopologyModel(instances, scheduler, state.latest.status || {});
  state.topology.model = model;
  const viewport = topologyViewportSize(container);
  const signature = topologySignature(model);
  const viewportSignature = `${Math.round(viewport.width / 40)}x${Math.round(viewport.height / 40)}`;
  renderTopologyStructure(container, model);
  const missing = model.nodes.some((node) => !state.topology.positions[node.id]);
  const changed = signature !== state.topology.signature || viewportSignature !== state.topology.viewportSignature || missing;
  if (changed) {
    const oldPositions = state.topology.positions || {};
    const layout = computeTopologyLayout(model, { ...viewport, existingPositions: oldPositions, pinnedNodeIds: state.topology.pinnedNodeIds, preserveExisting: true });
    state.topology.previousPositions = oldPositions;
    state.topology.signature = signature;
    state.topology.viewportSignature = viewportSignature;
    animateTopologyPositions(container, layout.positions);
  } else {
    applyTopologyPositions(container);
  }
  updateTopologyMetrics(container, model);
  updateTopologyTransforms(container);
  applyTopologyHover(container, state.topology.hoveredNodeId);
  container.classList.toggle('paused', state.paused);
  container.querySelector('.topology-empty')?.classList.toggle('hidden', model.instances.length > 0);
  if (state.topology.fitRequested) { state.topology.fitRequested = false; setTimeout(() => applyFitToView(container), 0); }
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

function explicitLoadMetrics(load) {
  return [
    ['inflight', 'Inflight'],
    ['qps_1m', 'QPS 1m'],
    ['gpu_util', 'Heartbeat GPU util'],
    ['least_load_score.total', 'Least-load score'],
  ].map(([key, label]) => ({ key, label, value: finiteNumber(nestedNumber(load, key)) }))
    .filter((item) => item.value !== null);
}

function explicitDetailedQueueMetrics(load) {
  return [
    ['prepare_queue_depth', 'Prepare queue depth'],
    ['ready_queue_depth', 'Ready queue depth'],
    ['active_prepare', 'Active prepare'],
    ['active_ready', 'Active ready'],
  ].map(([key, label]) => ({ key, label, value: finiteNumber(nestedNumber(load, key)) }))
    .filter((item) => item.value !== null);
}


function queueMetricsForInstance(instance, loadsPayload = state.latest.loads) {
  const load = mergedInstanceLoad(instance, loadsPayload);
  const explicit = explicitDetailedQueueMetrics(load);
  const explicitKeys = new Set(explicit.map((item) => item.key));
  const loadOnlyKeys = new Set(['load.inflight', 'load.qps_1m', 'load.gpu_util', 'inflight', 'qps_1m', 'gpu_util', 'least_load_score.total']);
  const heuristic = extractQueueMetrics(instance)
    .filter((item) => !explicitKeys.has(item.key) && !loadOnlyKeys.has(item.key));
  return [...explicit, ...heuristic];
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
  const loadMetrics = explicitLoadMetrics(load);
  const queues = queueMetricsForInstance(instance);
  const maxQueue = Math.max(1, ...queues.map((item) => item.value));
  const hw = hardwareDetails(instance);
  const staleNote = instance.is_alive === false ? '<p class="stale-note">This Instance is stale; values may reflect the last known report.</p>' : '';
  panel.classList.remove('hidden');
  panel.innerHTML = `<div class="panel-heading detail-header"><div><h2>Instance Detail: ${escapeHtml(instance.instance_id)}</h2><p>${escapeHtml(instance.host || '—')}:${escapeHtml(instance.port || '—')} · ${instanceStateBadge(instance)}</p>${staleNote}</div><button id="backToOverviewBtn">← Back to overview</button></div>
    <div class="kpi-grid">
      ${[['Status', stateLabel(instance)], ['Instance ID', instance.instance_id], ['Host:port', `${instance.host || '—'}:${instance.port || '—'}`], ['Admission', resource.admission_state || '—'], ['Registered', formatTimestamp(instance.registered_at)], ['Last seen age', formatAge(instance.last_seen_at)], ['Resource age', formatAge(resource.resource_reported_at)], ['Inflight', displayNumber(load.inflight, 0)], ['QPS 1m', displayNumber(load.qps_1m, 2)], ['GPU util', displayNumber(load.gpu_util, 1)], ['Least-load score', displayNumber(nestedNumber(load, 'least_load_score.total'), 2)]].map(([k,v]) => `<article class="kpi-card"><span>${escapeHtml(k)}</span><strong>${escapeHtml(v)}</strong></article>`).join('')}
    </div>
    <div class="detail-grid visual-detail">
      <section><h3>Resource utilization</h3>${renderMetricBar({ label:'CPU utilization', value:resource.cpu_util, max:100, unit:'%', secondaryText:'reported resource.cpu_util' })}${renderMetricBar({ label:'System memory', value:resource.memory_used_mb, max:resource.memory_total_mb, unit:' MB', secondaryText:`${displayNumber(resource.memory_used_mb,0)} / ${displayNumber(resource.memory_total_mb,0)} MB (${memPercent === null ? '—' : memPercent.toFixed(1)+'%'})` })}${renderMetricBar({ label:'GPU utilization', value:resource.gpu_util_avg, max:100, unit:'%', secondaryText:'reported resource.gpu_util_avg' })}${renderMetricBar({ label:'GPU memory', value:resource.gpu_mem_used_mb, max:resource.gpu_mem_total_mb, unit:' MB', secondaryText:`${displayNumber(resource.gpu_mem_used_mb,0)} / ${displayNumber(resource.gpu_mem_total_mb,0)} MB (${gpuMemPercent === null ? '—' : gpuMemPercent.toFixed(1)+'%'})` })}${renderMetricBar({ label:'Network RX', value:resource.network_rx_mbps, max:networkMax, unit:' Mbps', secondaryText:'relative to current RX/TX max' })}${renderMetricBar({ label:'Network TX', value:resource.network_tx_mbps, max:networkMax, unit:' Mbps', secondaryText:'relative to current RX/TX max' })}</section>
      <section><h3>Resource composition</h3><div class="donut-grid">${renderDonutChart({ label:'System memory', used:resource.memory_used_mb, total:resource.memory_total_mb, unit:' MB' })}${renderDonutChart({ label:'GPU memory', used:resource.gpu_mem_used_mb, total:resource.gpu_mem_total_mb, unit:' MB' })}</div></section>
      <section><h3>Load metrics</h3>${loadMetrics.length ? loadMetrics.map((metric) => `<article class="kpi-card inline-kpi"><span>${escapeHtml(metric.label)}</span><strong>${escapeHtml(displayNumber(metric.value, 2))}</strong><small>${escapeHtml(metric.key)}</small></article>`).join('') : '<div class="empty-state compact">No load metrics are exposed by the current Instance load snapshot.</div>'}</section>
      <section><h3>Detailed queue counters</h3>${queues.length ? queues.map((q) => renderMetricBar({ label:q.label, value:q.value, max:maxQueue, unit:'', secondaryText:q.key })).join('') : '<div class="empty-state compact">No detailed queue counters are exposed by the current Instance load snapshot.</div>'}</section>
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
    if (state.topology.suppressNextClick) { state.topology.suppressNextClick = false; event.preventDefault(); return; }
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
  module.exports = { buildTopologyModel, topologySignature, stableHash, seededUnitValue, createInitialTopologyPositions, relaxTopologyLayout, clampTopologyPosition, normalizeVector, edgePoint, deterministicLinkCurvature, computeLinkPath, computeHoverRelationships, buildTooltipModel, fitTopologyToView, clampZoom, dragExceededThreshold, computeTopologyLayout, renderMetricBar, renderDonutChart, extractQueueMetrics, queueMetricsForInstance, computeMetrics, hasResourceReport, finiteNumber, clampPercent, percentOf, fmt, stateLabel, hardwareDetails, mergedInstanceLoad, applyPausedTopologyState, renderInstanceDetail, renderSystemTopology, state };
} else {
  setTheme(state.theme);
  setupEventHandlers();
  refresh().then(restartTimer);
}
