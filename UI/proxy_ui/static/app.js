const state = {
  config: null,
  timer: null,
};

const OPTIONAL_UNAVAILABLE = {
  ok: false,
  optional_unavailable: true,
  error: 'unavailable',
};

function $(id) {
  return document.getElementById(id);
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

function formatAge(lastSeenAt) {
  if (!lastSeenAt) {
    return '—';
  }
  const ageSeconds = Math.max(0, Date.now() / 1000 - Number(lastSeenAt));
  return Number.isFinite(ageSeconds) ? `${ageSeconds.toFixed(0)}s` : '—';
}

function pill(text, className) {
  return `<span class="pill ${className}">${text}</span>`;
}

async function getJson(path, options = {}) {
  const response = await fetch(path, { cache: 'no-store' });
  if (!response.ok) {
    const error = new Error(`${path}: HTTP ${response.status}`);
    error.optional = Boolean(options.optional);
    throw error;
  }
  return response.json();
}

async function settleJson(path, options = {}) {
  try {
    return await getJson(path, options);
  } catch (error) {
    if (options.optional) {
      return {
        ...OPTIONAL_UNAVAILABLE,
        error: error.message,
      };
    }
    return {
      ok: false,
      error: error.message,
    };
  }
}

function instanceState(instance) {
  if (instance.is_alive === true) {
    return pill('alive', 'ok');
  }
  if (instance.is_alive === false) {
    return pill('stale', 'bad');
  }
  return pill('unknown', 'warn');
}

function renderCards(status, instances, resources, scheduler) {
  const alive = status?.alive_instances ?? instances.filter((item) => item.is_alive === true).length;
  const total = status?.total_instances ?? instances.length;
  const stale = status?.expired_instances ?? instances.filter((item) => item.is_alive === false).length;
  const resourceRows = resources.instances || [];
  const visibleResources = resourceRows.filter((item) => {
    const resource = item.resource || {};
    return Object.values(resource).some((value) => value !== null && value !== undefined);
  }).length;

  let schedulerText = 'unavailable';
  let schedulerClass = 'warn';
  if (scheduler?.ok) {
    schedulerText = 'registered';
    schedulerClass = 'ok';
  } else if (scheduler?.error) {
    schedulerText = scheduler.error;
  }

  const cards = [
    ['Proxy CP', status?.ok === false ? 'error' : 'ok', status?.ok === false ? 'bad' : 'ok'],
    ['Instances', `${alive}/${total} alive`, stale ? 'warn' : 'ok'],
    ['Resource reports', `${visibleResources} visible`, visibleResources ? 'ok' : 'warn'],
    ['Scheduler reg', schedulerText, schedulerClass],
  ];

  $('summaryCards').innerHTML = cards
    .map(([label, value, className]) => (
      `<div class="card"><span class="muted">${label}</span><strong>${value}</strong>${pill(className, className)}</div>`
    ))
    .join('');
}

function renderInstances(items) {
  $('instanceSummary').textContent = `${items.length} instance record(s), including expired entries when returned by the Proxy control plane.`;

  const rows = items.map((item) => `
    <tr>
      <td>${item.instance_id || '—'}</td>
      <td>${item.host || '—'}:${item.port || '—'}</td>
      <td>${instanceState(item)}</td>
      <td>${formatTimestamp(item.registered_at)}</td>
      <td>${formatTimestamp(item.last_seen_at)}<br><span class="muted">age ${formatAge(item.last_seen_at)}</span></td>
      <td>
        inflight=${fmt(item.load?.inflight, 0)}<br>
        qps_1m=${fmt(item.load?.qps_1m, 2)}<br>
        gpu=${fmt(item.load?.gpu_util, 1)}
      </td>
      <td><code>${JSON.stringify(item.meta || {})}</code></td>
    </tr>
  `).join('');

  $('instanceTable').innerHTML = `
    <thead>
      <tr><th>Instance ID</th><th>Address</th><th>State</th><th>Registered</th><th>Last seen</th><th>Load</th><th>Meta</th></tr>
    </thead>
    <tbody>${rows || '<tr><td colspan="7">No instances reported yet.</td></tr>'}</tbody>
  `;
}

function renderResources(payload) {
  const items = payload.instances || [];
  $('resourceSummary').textContent = `${items.length} resource snapshot row(s). Missing fields are shown as em dashes.`;

  const rows = items.map((item) => {
    const resource = item.resource || {};
    return `
      <tr>
        <td>${item.instance_id}</td>
        <td>${item.host}:${item.port}</td>
        <td>${formatTimestamp(item.last_seen_at)}</td>
        <td>${fmt(resource.cpu_util)}%</td>
        <td>
          ${fmt(resource.memory_used_mb, 0)} / ${fmt(resource.memory_total_mb, 0)} MB<br>
          free ${fmt((resource.memory_free_ratio ?? 0) * 100)}%
        </td>
        <td>${fmt(resource.gpu_util_avg)}%</td>
        <td>${fmt(resource.gpu_mem_used_mb, 0)} / ${fmt(resource.gpu_mem_total_mb, 0)} MB</td>
        <td>rx ${fmt(resource.network_rx_mbps, 2)} Mbps<br>tx ${fmt(resource.network_tx_mbps, 2)} Mbps</td>
        <td>${resource.admission_state || '—'}</td>
        <td>
          resource_ts=${fmt(resource.resource_ts_ms, 0)}<br>
          reported_at=${formatTimestamp(resource.resource_reported_at)}<br>
          reported_id=${resource.reported_instance_id || '—'}<br>
          mono_ms=${fmt(resource.resource_report_monotonic_ms, 0)} wall_ms=${fmt(resource.resource_report_wall_time_ms, 0)}
        </td>
      </tr>
    `;
  }).join('');

  $('resourceTable').innerHTML = `
    <thead>
      <tr>
        <th>Instance ID</th><th>Address</th><th>Last seen</th><th>CPU</th><th>Memory</th>
        <th>GPU util</th><th>GPU memory</th><th>Network</th><th>Admission</th><th>Report metadata</th>
      </tr>
    </thead>
    <tbody>${rows || '<tr><td colspan="10">No resource snapshots yet.</td></tr>'}</tbody>
  `;
}

function renderTopology(payload) {
  if (payload?.optional_unavailable) {
    $('topologyPanel').textContent = JSON.stringify({ unavailable: true, error: payload.error }, null, 2);
    return;
  }
  $('topologyPanel').textContent = JSON.stringify(payload || {}, null, 2);
}

async function refresh() {
  $('errorBox').textContent = '';

  if (!state.config) {
    state.config = await settleJson('/api/config');
  }

  const [health, status, instances, resources, topology, scheduler] = await Promise.all([
    settleJson('/api/proxy/healthz'),
    settleJson('/api/proxy/status'),
    settleJson('/api/proxy/instances?include_dead=true'),
    settleJson('/api/proxy/resources?include_dead=true'),
    settleJson('/api/proxy/topology', { optional: true }),
    settleJson('/api/scheduler/proxy', { optional: true }),
  ]);

  const instanceRows = Array.isArray(instances) ? instances : [];
  const resourcePayload = resources?.instances ? resources : { instances: [] };

  renderCards(status, instanceRows, resourcePayload, scheduler);
  $('statusPanel').textContent = JSON.stringify({
    ui_config: state.config,
    health,
    status,
    scheduler,
  }, null, 2);
  renderInstances(instanceRows);
  renderResources(resourcePayload);
  renderTopology(topology);
  $('lastUpdated').textContent = `Last updated: ${new Date().toLocaleString()}`;

  const requiredErrors = [health, status, instances, resources]
    .filter((payload) => payload && payload.ok === false)
    .map((payload) => payload.error);
  $('errorBox').textContent = requiredErrors.join(' | ');
}

$('refreshBtn').addEventListener('click', refresh);
refresh().then(() => {
  const intervalMs = state.config?.poll_interval_ms || 3000;
  state.timer = setInterval(refresh, intervalMs);
});
