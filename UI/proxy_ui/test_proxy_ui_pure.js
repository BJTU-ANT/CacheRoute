const assert = require('assert');
const ui = require('./static/app.js');

function inst(id, alive, extra = {}) {
  return {
    instance_id: id,
    host: '127.0.0.1',
    port: 9000,
    is_alive: alive,
    registered_at: 1700000000,
    last_seen_at: Date.now() / 1000,
    load: { inflight: 2, qps_1m: 1.5, gpu_util: 30, ...(extra.load || {}) },
    resource: { ...(extra.resource || {}) },
    meta: { ...(extra.meta || {}) },
  };
}

function testNullSemantics() {
  const cases = [null, undefined, '', '   ', true, false, NaN, Infinity, -Infinity, [], [1], {}, new Date()];
  for (const value of cases) assert.strictEqual(ui.finiteNumber(value), null, `expected null for ${String(value)}`);
  assert.strictEqual(ui.finiteNumber(0), 0);
  assert.strictEqual(ui.finiteNumber(-3), -3);
  assert.strictEqual(ui.finiteNumber('0'), 0);
  assert.strictEqual(ui.finiteNumber('42'), 42);
  assert.strictEqual(ui.finiteNumber(' 42.5 '), 42.5);
  assert.strictEqual(ui.fmt(null), '—');
  assert.strictEqual(ui.percentOf('', 100), null);
  assert(!ui.renderMetricBar({ label: 'null', value: null, max: 100 }).includes('0.00'));
}


function testComputeMetricsNullAverages() {
  let metrics = ui.computeMetrics([
    inst('missing', true, { resource: { cpu_util: null, gpu_util_avg: null, network_rx_mbps: null, network_tx_mbps: null, memory_used_mb: null, memory_total_mb: null } }),
    inst('valid', true, { resource: { cpu_util: 80, gpu_util_avg: 60, network_rx_mbps: 10, network_tx_mbps: 5, memory_used_mb: 50, memory_total_mb: 100 } }),
  ]);
  assert.strictEqual(metrics.cpu, 80);
  assert.strictEqual(metrics.gpu, 60);
  assert.strictEqual(metrics.rx, 10);
  assert.strictEqual(metrics.tx, 5);
  assert.strictEqual(metrics.memory, 50);

  metrics = ui.computeMetrics([
    inst('all-null-a', true, { resource: { cpu_util: null, gpu_util_avg: null, network_rx_mbps: null, network_tx_mbps: null, memory_used_mb: null, memory_total_mb: null } }),
    inst('all-null-b', true, { resource: { cpu_util: '', gpu_util_avg: undefined, network_rx_mbps: false, network_tx_mbps: {}, memory_used_mb: 1, memory_total_mb: 0 } }),
  ]);
  assert.strictEqual(metrics.cpu, null);
  assert.strictEqual(metrics.gpu, null);
  assert.strictEqual(metrics.rx, null);
  assert.strictEqual(metrics.tx, null);
  assert.strictEqual(metrics.memory, null);
}

function testHasResourceReport() {
  assert.strictEqual(ui.hasResourceReport(inst('raw-empty', true, { resource: { raw_resource: {} } })), false);
  assert.strictEqual(ui.hasResourceReport(inst('reported-null', true, { resource: { resource_reported_at: null, raw_resource: { devices: {} } } })), false);
  assert.strictEqual(ui.hasResourceReport(inst('reported', true, { resource: { resource_reported_at: 1700000000, raw_resource: {} } })), true);
  assert.strictEqual(ui.hasResourceReport(inst('scalar', true, { resource: { cpu_util: 0, resource_reported_at: null } })), true);
  assert.strictEqual(ui.hasResourceReport(inst('unavailable', true, { resource: { cpu_util: null, memory_total_mb: '', gpu_util_avg: false, network_rx_mbps: {}, raw_resource: {} } })), false);
}

function testTopologyScenarios() {
  let model = ui.buildTopologyModel([], { error: 'proxy_id_not_configured' }, { proxy_id: 'proxy-a' });
  assert.strictEqual(model.instances.length, 0);
  assert.strictEqual(model.links[0].status, 'inactive');

  model = ui.buildTopologyModel([inst('one', true)], { ok: true }, { proxy_id: 'proxy-a', ok: true });
  assert.strictEqual(model.links[0].status, 'active');
  assert.strictEqual(model.links[1].status, 'active');
  assert.strictEqual(model.nodes.find((n) => n.type === 'proxy').tone, 'ok');
  assert.strictEqual(ui.stateLabel(model.instances[0]), 'alive');

  model = ui.buildTopologyModel([inst('alive', true), inst('stale', false), inst('unknown', undefined)], { ok: true }, { ok: false });
  assert.deepStrictEqual(model.instances.map((i) => i.instance_id), ['alive', 'stale', 'unknown']);
  assert.deepStrictEqual(model.links.slice(1).map((l) => l.status), ['active', 'stale', 'unknown']);
  assert.strictEqual(model.nodes.find((n) => n.type === 'proxy').tone, 'bad');

  for (let count = 0; count <= 30; count += 1) {
    const items = Array.from({ length: count }, (_, i) => inst(`node-${String(i).padStart(2, '0')}`, i % 2 === 0));
    const layout = ui.computeTopologyLayout(ui.buildTopologyModel(items, { ok: true }, { ok: true }));
    const nodePadding = layout.nodePadding;
    const labelPadding = layout.labelPadding;
    for (const pos of Object.values(layout.positions)) {
      assert(pos.x - nodePadding >= 0, `count=${count} left bound`);
      assert(pos.x + nodePadding <= layout.width, `count=${count} right bound`);
      assert(pos.y - nodePadding >= 0, `count=${count} top bound`);
      assert(pos.y + labelPadding <= layout.height, `count=${count} bottom bound`);
    }
  }
  for (const count of [2, 3, 4, 5, 6, 7, 8]) {
    const layout = ui.computeTopologyLayout(ui.buildTopologyModel(Array.from({ length: count }, (_, i) => inst(`arc-${i}`, true)), { ok: true }, {}));
    assert(layout.height >= 360, `count=${count} height`);
  }
}

function testMetricsAndMissingData() {
  assert.strictEqual(ui.clampPercent(-5), 0);
  assert.strictEqual(ui.clampPercent(105), 100);
  assert.strictEqual(ui.clampPercent('bad'), null);
  assert.strictEqual(ui.percentOf(5, 0), null);
  assert.strictEqual(ui.percentOf(150, 100), 100);
  assert(!ui.renderMetricBar({ label: 'Memory', value: 1, max: 0, unit: ' MB' }).includes('NaN'));
  assert(ui.renderMetricBar({ label: 'GPU', value: undefined }).includes('No data'));
  assert(ui.renderDonutChart({ label: 'GPU memory', used: undefined, total: undefined }).includes('No data'));
  assert(ui.renderDonutChart({ label: 'Memory', used: 120, total: 100, unit: ' MB' }).includes('120'));
}

function testQueueExtractionAndLoadMerge() {
  const loads = { ok: true, metric_source: { queue_depth: 'proxy_queue_manager' }, instances: [{ instance_id: 'known', inflight: null, qps_1m: '2.5', prepare_queue_depth: 3, ready_queue_depth: 4, active_prepare: 1, active_ready: 0, least_load_score: { total: 9.25 } }] };
  const metrics = ui.queueMetricsForInstance(inst('known', true, { load: { inflight: null, qps_1m: null } }), loads);
  assert(metrics.some((q) => q.key === 'prepare_queue_depth' && q.value === 3));
  assert(metrics.some((q) => q.key === 'active_ready' && q.value === 0));
  assert(!metrics.some((q) => q.key === 'least_load_score.total'));
  assert(!metrics.some((q) => q.key === 'inflight'));
  assert.deepStrictEqual(ui.queueMetricsForInstance(inst('only-load', true)).map((q) => q.key), []);

  assert.deepStrictEqual(ui.extractQueueMetrics(inst('none', true)).map((q) => q.key), ['load.inflight']);
  const nested = inst('nested', true, {
    meta: { queues: { ready: 3, waiting: 2, port: 7000, decode_time_ms: 55, gpu_util: 90 } },
    resource: { cpu_util: 70, memory_total_mb: 0 },
  });
  const keys = ui.extractQueueMetrics(nested).map((q) => q.key);
  assert(keys.includes('meta.queues.ready'));
  assert(keys.includes('meta.queues.waiting'));
  assert(!keys.includes('meta.queues.port'));
  assert(!keys.includes('meta.queues.decode_time_ms'));
  assert(!keys.includes('resource.cpu_util'));
}

function withFakeDocument(elements, fn) {
  const original = global.document;
  global.document = { getElementById: (id) => elements[id] || null };
  try { fn(); } finally { global.document = original; }
}

function testHardwareAndPausedDom() {
  const panel = { classList: { add() {}, remove() {} }, innerHTML: '' };
  const instance = inst('gpu-real', true, { resource: { raw_resource: { devices: { gpu: [{ index: 0, name: 'NVIDIA H100', uuid: 'GPU-1', utilization_pct: 77, memory_used_mb: 40960, memory_total_mb: 81920, memory_free_mb: 40960, temperature_c: 61, power_w: 350 }], network: [{ iface: 'eth0', rx_mbps: 12.5, tx_mbps: 7.25, speed_mbps: 100000 }] } } } });
  ui.state.selectedInstanceId = 'gpu-real';
  ui.state.latest.loads = { instances: [] };
  withFakeDocument({ instanceDetailView: panel }, () => ui.renderInstanceDetail([instance]));
  assert(panel.innerHTML.includes('NVIDIA H100'));
  assert(panel.innerHTML.includes('40960/81920'));
  assert(panel.innerHTML.includes('util 77%'));
  assert(panel.innerHTML.includes('GPU-1'));
  assert(panel.innerHTML.includes('61°C'));
  assert(panel.innerHTML.includes('350 W'));
  assert(panel.innerHTML.includes('eth0'));
  assert(panel.innerHTML.includes('100000 Mbps'));
  assert(panel.innerHTML.includes('No detailed queue counters are exposed by the current Instance load snapshot.'));

  const container = { classList: { value: false, toggle(cls, on) { this.value = cls === 'paused' && on; } } };
  ui.state.paused = true;
  withFakeDocument({ systemTopology: container }, () => ui.applyPausedTopologyState());
  assert.strictEqual(container.classList.value, true);
  ui.state.paused = false;
  withFakeDocument({ systemTopology: container }, () => ui.applyPausedTopologyState());
  assert.strictEqual(container.classList.value, false);
}

function testThemeAndReducedMotionStaticHooks() {
  const fs = require('fs');
  const css = fs.readFileSync(require('path').join(__dirname, 'static/style.css'), 'utf8');
  assert(css.includes("[data-theme='light']"));
  assert(css.includes('@media (prefers-reduced-motion: reduce)'));
  assert(css.includes('--topology-scheduler'));
  ['.topology-svg-node.is-hovered', '.topology-svg-node.is-neighbor', '.topology-svg-node.is-dimmed', '.topology-svg-node.is-dragging', '.topology-link.is-related', '.topology-link.is-dimmed', '.topology-particle.is-related', '.topology-tooltip', '.topology-controls'].forEach((selector) => assert(css.includes(selector), `missing CSS selector ${selector}`));
}

testNullSemantics();
testComputeMetricsNullAverages();
testHasResourceReport();
testTopologyScenarios();
testMetricsAndMissingData();
testQueueExtractionAndLoadMerge();
testHardwareAndPausedDom();
testThemeAndReducedMotionStaticHooks();
function distance(a, b) { return Math.hypot(a.x - b.x, a.y - b.y); }

function testForceTopologyPureHelpers() {
  assert.strictEqual(ui.stableHash('proxy:a'), ui.stableHash('proxy:a'));
  assert.notStrictEqual(ui.seededUnitValue('instance:a', 'angle'), ui.seededUnitValue('instance:b', 'angle'));
  const model = ui.buildTopologyModel(Array.from({ length: 8 }, (_, i) => inst(`force-${i}`, i % 2 === 0)), { ok: true }, { ok: true, proxy_id: 'proxy-a' });
  const sig = ui.topologySignature(model);
  assert(sig.includes('scheduler') && sig.includes('proxy'));
  const a = ui.computeTopologyLayout(model, { width: 1000, height: 620 });
  const b = ui.computeTopologyLayout(model, { width: 1000, height: 620 });
  assert.deepStrictEqual(a.positions, b.positions, 'same node set should produce same layout');
  const metricChanged = ui.buildTopologyModel(Array.from({ length: 8 }, (_, i) => inst(`force-${i}`, i % 2 === 0, { resource: { cpu_util: i * 3 } })), { ok: true }, { ok: true, proxy_id: 'proxy-a' });
  assert.strictEqual(ui.topologySignature(metricChanged), sig, 'metric changes preserve topology signature');
  const added = ui.buildTopologyModel(Array.from({ length: 9 }, (_, i) => inst(`force-${i}`, true)), { ok: true }, { ok: true, proxy_id: 'proxy-a' });
  const addLayout = ui.computeTopologyLayout(added, { width: 1000, height: 620, existingPositions: a.positions, iterations: 20 });
  assert(distance(a.positions.proxy, addLayout.positions.proxy) < 40, 'adding one node preserves proxy position');
  assert(distance(a.positions['instance:force-0'], addLayout.positions['instance:force-0']) < 90, 'adding one node preserves existing instance approximately');
  Object.values(a.positions).forEach((pos) => {
    assert(pos.x >= a.nodePadding && pos.x <= a.width - a.nodePadding);
    assert(pos.y >= a.nodePadding && pos.y <= a.height - a.labelPadding);
  });
  const nodes = Object.keys(a.positions);
  for (let i = 0; i < nodes.length; i += 1) for (let j = i + 1; j < nodes.length; j += 1) assert(distance(a.positions[nodes[i]], a.positions[nodes[j]]) >= 78, 'minimum collision spacing');
  assert(a.positions.scheduler.x < a.positions.proxy.x && a.positions.scheduler.y < a.positions.proxy.y, 'scheduler above-left anchor');
  assert(Math.abs(a.positions.proxy.x - a.width / 2) < a.width * 0.18, 'proxy central placement');
  const instanceDistances = model.nodes.filter((n) => n.type === 'instance').map((n) => distance(a.positions[n.id], a.positions.proxy));
  assert(Math.max(...instanceDistances) - Math.min(...instanceDistances) > 20, 'instances have irregular radial distribution');
}

function testLinkHoverTooltipFitZoomDragHelpers() {
  const model = ui.buildTopologyModel([inst('one', true), inst('two', false)], { ok: true }, { ok: true, proxy_id: 'proxy-a' });
  const layout = ui.computeTopologyLayout(model, { width: 900, height: 560 });
  assert.strictEqual(ui.deterministicLinkCurvature('proxy-one'), ui.deterministicLinkCurvature('proxy-one'));
  const link = model.links.find((item) => item.target === 'instance:one');
  const d = ui.computeLinkPath(link, layout.positions);
  assert(d.startsWith('M '));
  const nums = d.match(/-?\d+(?:\.\d+)?/g).map(Number);
  assert(distance({ x: nums[0], y: nums[1] }, layout.positions[link.source]) > 30, 'link starts at edge, not center');
  assert(distance({ x: nums.at(-2), y: nums.at(-1) }, layout.positions[link.target]) > 30, 'link ends at edge, not center');
  let rel = ui.computeHoverRelationships(model, 'scheduler');
  assert(rel.relatedNodes.has('proxy') && rel.relatedLinks.has('scheduler-proxy') && rel.dimmedNodes.has('instance:one'));
  rel = ui.computeHoverRelationships(model, 'proxy');
  assert.strictEqual(rel.dimmedNodes.size, 0); assert.strictEqual(rel.dimmedLinks.size, 0);
  rel = ui.computeHoverRelationships(model, 'instance:one');
  assert(rel.relatedNodes.has('proxy') && rel.relatedLinks.has('proxy-one') && rel.dimmedNodes.has('scheduler') && rel.dimmedNodes.has('instance:two'));
  const tooltip = ui.buildTooltipModel(model.nodes.find((n) => n.id === 'instance:one'));
  assert(tooltip.rows.some((row) => row.label === 'GPU memory' && row.value.includes('—')));
  assert.strictEqual(ui.buildTooltipModel({ type: 'instance', raw: { resource: { cpu_util: null } }, instanceId: 'nulls', status: 'unknown' }).rows.find((row) => row.label === 'CPU utilization').value, '—');
  const fit = ui.fitTopologyToView(layout.positions, { width: 800, height: 500 });
  assert(fit.zoom >= 0.45 && fit.zoom <= 2.5);
  assert.strictEqual(ui.clampZoom(0.1), 0.45);
  assert.strictEqual(ui.clampZoom(10), 2.5);
  assert.strictEqual(ui.dragExceededThreshold(3, 4), true);
  assert.strictEqual(ui.dragExceededThreshold(2, 2), false);
}

testForceTopologyPureHelpers();
testLinkHoverTooltipFitZoomDragHelpers();
console.log('proxy-ui pure function tests passed');
