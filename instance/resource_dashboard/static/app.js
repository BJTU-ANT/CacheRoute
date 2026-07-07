const state = {
  networkHistory: [],
  maxSamples: 60,
};

const $ = (id) => document.getElementById(id);
const fmt = (v, suffix = '') => (v === undefined || v === null || Number.isNaN(v) ? '-' : `${Number(v).toFixed(2)}${suffix}`);
const pct = (v) => Math.max(0, Math.min(100, Number(v || 0)));

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || data.error || `HTTP ${res.status}`);
  return data;
}

function drawGauge(canvas, value, label) {
  const ctx = canvas.getContext('2d');
  const w = canvas.width, h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  const cx = w / 2, cy = h - 18, r = Math.min(w * 0.38, h * 0.72);
  ctx.lineWidth = 18;
  ctx.lineCap = 'round';
  ctx.strokeStyle = '#334155';
  ctx.beginPath();
  ctx.arc(cx, cy, r, Math.PI, 0);
  ctx.stroke();
  ctx.strokeStyle = value > 90 ? '#ef4444' : value > 70 ? '#f59e0b' : '#38bdf8';
  ctx.beginPath();
  ctx.arc(cx, cy, r, Math.PI, Math.PI + Math.PI * pct(value) / 100);
  ctx.stroke();
  ctx.fillStyle = '#e5e7eb';
  ctx.font = '700 24px sans-serif';
  ctx.textAlign = 'center';
  ctx.fillText(`${fmt(value, '%')}`, cx, cy - 18);
  ctx.fillStyle = '#9ca3af';
  ctx.font = '12px sans-serif';
  ctx.fillText(label, cx, cy + 8);
}

function drawDonut(canvas, used, total) {
  const ctx = canvas.getContext('2d');
  const w = canvas.width, h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  const cx = w / 2, cy = h / 2, r = Math.min(w, h) * 0.34;
  const ratio = total > 0 ? used / total : 0;
  ctx.lineWidth = 24;
  ctx.strokeStyle = '#334155';
  ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2); ctx.stroke();
  ctx.strokeStyle = ratio > 0.9 ? '#ef4444' : ratio > 0.75 ? '#f59e0b' : '#22c55e';
  ctx.beginPath(); ctx.arc(cx, cy, r, -Math.PI / 2, -Math.PI / 2 + Math.PI * 2 * ratio); ctx.stroke();
  ctx.fillStyle = '#e5e7eb';
  ctx.font = '700 22px sans-serif';
  ctx.textAlign = 'center';
  ctx.fillText(`${fmt(ratio * 100, '%')}`, cx, cy + 8);
}

function drawNetworkChart(canvas) {
  const ctx = canvas.getContext('2d');
  const w = canvas.width, h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  ctx.strokeStyle = '#263244';
  ctx.lineWidth = 1;
  for (let i = 0; i < 5; i++) {
    const y = 20 + i * (h - 40) / 4;
    ctx.beginPath(); ctx.moveTo(40, y); ctx.lineTo(w - 16, y); ctx.stroke();
  }
  const max = Math.max(1, ...state.networkHistory.flatMap(p => [p.rx, p.tx]));
  const plot = (key, color) => {
    ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.beginPath();
    state.networkHistory.forEach((p, i) => {
      const x = 40 + i * (w - 56) / Math.max(1, state.maxSamples - 1);
      const y = h - 24 - (p[key] / max) * (h - 48);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
  };
  plot('rx', '#38bdf8'); plot('tx', '#22c55e');
  ctx.fillStyle = '#9ca3af'; ctx.font = '12px sans-serif';
  ctx.fillText(`max ${fmt(max, ' Mbps')}`, 44, 16);
  ctx.fillStyle = '#38bdf8'; ctx.fillText('rx', w - 80, 16);
  ctx.fillStyle = '#22c55e'; ctx.fillText('tx', w - 48, 16);
}

function render(snapshot, agent) {
  const devices = snapshot.devices || {};
  const cpu = devices.cpu || {};
  const memory = devices.memory || {};
  const gpus = Array.isArray(devices.gpu) ? devices.gpu : [];
  const networks = Array.isArray(devices.network) ? devices.network : [];
  const capacity = snapshot.capacity_hint || {};

  $('instanceId').textContent = snapshot.instance_id || agent?.instance_id || '-';
  $('agentHealth').textContent = agent?.reachable ? 'reachable' : 'unreachable';
  $('lastUpdate').textContent = snapshot.timestamp_ms ? new Date(Number(snapshot.timestamp_ms)).toLocaleTimeString() : '-';
  const admission = capacity.admission_state || 'unknown';
  $('admissionBadge').textContent = admission;
  $('admissionBadge').className = `badge ${admission}`;

  $('cpuUtil').textContent = fmt(cpu.utilization_pct, '%');
  $('load1').textContent = fmt(cpu.load1);
  $('load5').textContent = fmt(cpu.load5);
  $('load15').textContent = fmt(cpu.load15);
  drawGauge($('cpuGauge'), Number(cpu.utilization_pct || 0), 'CPU');

  $('memUsed').textContent = fmt(memory.used_mb);
  $('memFree').textContent = fmt(memory.free_mb);
  $('memTotal').textContent = fmt(memory.total_mb);
  $('memFreeRatio').textContent = fmt((capacity.memory_free_ratio || 0) * 100, '%');
  drawDonut($('memoryDonut'), Number(memory.used_mb || 0), Number(memory.total_mb || 0));

  $('gpuList').innerHTML = gpus.length ? gpus.map(g => {
    const memRatio = g.memory_total_mb > 0 ? 100 * g.memory_used_mb / g.memory_total_mb : 0;
    return `<div class="gpu-card">
      <div class="gpu-title"><strong>GPU ${g.index}: ${g.name || '-'}</strong><span>${g.uuid || ''}</span></div>
      <div>Utilization ${fmt(g.utilization_pct, '%')}</div><div class="bar"><span style="width:${pct(g.utilization_pct)}%"></span></div>
      <div>Memory ${fmt(g.memory_used_mb)} / ${fmt(g.memory_total_mb)} MB, free ${fmt(g.memory_free_mb)} MB</div><div class="bar"><span style="width:${pct(memRatio)}%"></span></div>
      <div class="metric-grid"><div><span>Temp</span><strong>${fmt(g.temperature_c, '°C')}</strong></div><div><span>Power</span><strong>${fmt(g.power_w, ' W')}</strong></div></div>
    </div>`;
  }).join('') : '<div class="status-text">No GPU detected</div>';

  $('networkList').innerHTML = networks.length ? networks.map(n => `<div class="network-row"><strong>${n.iface || '-'}</strong><br>rx ${fmt(n.rx_mbps, ' Mbps')} / tx ${fmt(n.tx_mbps, ' Mbps')}<br>speed ${fmt(n.speed_mbps, ' Mbps')}</div>`).join('') : '<div class="status-text">No network interface detected</div>';
  const firstNet = networks[0] || {};
  state.networkHistory.push({ rx: Number(firstNet.rx_mbps || 0), tx: Number(firstNet.tx_mbps || 0) });
  if (state.networkHistory.length > state.maxSamples) state.networkHistory.shift();
  drawNetworkChart($('networkChart'));

  $('rawJson').textContent = JSON.stringify(snapshot, null, 2);
  $('agentStatus').textContent = `managed=${Boolean(agent?.managed_by_dashboard)} running=${Boolean(agent?.managed_process_running)} pid=${agent?.pid || '-'} url=${agent?.agent_url || '-'}`;
}

async function refreshSnapshot() {
  try {
    const data = await api('/api/snapshot');
    render(data.snapshot || {}, data.agent || {});
  } catch (err) {
    $('agentHealth').textContent = 'unavailable';
    $('agentStatus').textContent = String(err.message || err);
  }
}

async function refreshStatus() {
  try {
    const data = await api('/api/agent/status');
    $('agentStatus').textContent = JSON.stringify(data.status || {});
  } catch (err) {
    $('agentStatus').textContent = String(err.message || err);
  }
}

$('startAgent').addEventListener('click', async () => { await api('/api/agent/start', { method: 'POST' }); await refreshSnapshot(); });
$('stopAgent').addEventListener('click', async () => { await api('/api/agent/stop', { method: 'POST' }); await refreshStatus(); });
$('refreshSnapshot').addEventListener('click', refreshSnapshot);

refreshSnapshot();
setInterval(refreshSnapshot, 1000);
