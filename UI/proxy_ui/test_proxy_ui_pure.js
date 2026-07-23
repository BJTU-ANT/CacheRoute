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

function testSingleRenderSystemTopologyDeclaration() {
  const fs = require('fs');
  const source = fs.readFileSync(require('path').join(__dirname, 'static/app.js'), 'utf8');
  const matches = source.match(/function\s+renderSystemTopology\s*\(/g) || [];
  assert.strictEqual(matches.length, 1, 'exactly one renderSystemTopology declaration should exist');
}

function fakeClassList(owner) {
  return {
    add(cls) { owner.classes.add(cls); },
    remove(cls) { owner.classes.delete(cls); },
    contains(cls) { return owner.classes.has(cls); },
    toggle(cls, on) { const yes = on === undefined ? !owner.classes.has(cls) : Boolean(on); if (yes) owner.classes.add(cls); else owner.classes.delete(cls); },
  };
}

class FakeElement {
  constructor(tag = 'div', className = '') {
    this.tag = tag;
    this.classes = new Set(className ? className.split(/\s+/).filter(Boolean) : []);
    this.classList = fakeClassList(this);
    this.dataset = {};
    this.children = [];
    this.attributes = {};
    this.style = {};
    this.textContent = '';
    this.className = { baseVal: className };
  }
  append(child) { child.parent = this; this.children.push(child); return child; }
  setAttribute(name, value) { this.attributes[name] = String(value); if (name === 'transform') this.transform = String(value); }
  getAttribute(name) { return this.attributes[name]; }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter((item) => item !== this); }
  closest(selector) {
    const cls = selector.match(/\.([\w-]+)/)?.[1];
    let cur = this;
    while (cur) { if (!cls || cur.classes.has(cls)) return cur; cur = cur.parent; }
    return null;
  }
  getBoundingClientRect() { return { left: 0, top: 0, width: 980, height: 560 }; }
  addEventListener(type, handler) { this.handlers = this.handlers || {}; this.handlers[type] = this.handlers[type] || []; this.handlers[type].push(handler); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  querySelectorAll(selector) {
    const all = [];
    const visit = (node) => { all.push(node); node.children.forEach(visit); };
    this.children.forEach(visit);
    const dataLink = selector.match(/\[data-link-id="([^"]+)"\]/)?.[1];
    const dataNode = selector.match(/\[data-node-id="([^"]+)"\]/)?.[1];
    const classes = [...selector.matchAll(/\.([\w-]+)/g)].map((m) => m[1]);
    const tags = selector.split(',').map((part) => part.trim()).filter((part) => /^[a-z]+$/i.test(part));
    return all.filter((node) => {
      if (dataLink && node.dataset.linkId !== dataLink) return false;
      if (dataNode && node.dataset.nodeId !== dataNode) return false;
      if (classes.length && !classes.some((cls) => node.classes.has(cls))) return false;
      if (tags.length && !tags.includes(node.tag)) return false;
      return true;
    });
  }
  insertAdjacentHTML(_where, html) {
    if (html.includes('topology-link')) {
      const link = this.append(new FakeElement('path', 'topology-link'));
      link.dataset.linkId = html.match(/data-link-id="([^"]+)"/)?.[1];
      link.id = html.match(/id="([^"]+)"/)?.[1];
      link.append(new FakeElement('title'));
      return;
    }
    if (html.includes('topology-particle')) {
      const particle = this.append(new FakeElement('circle', 'topology-particle'));
      particle.dataset.linkId = html.match(/data-link-id="([^"]+)"/)?.[1];
      return;
    }
    if (html.includes('topology-svg-node')) {
      const node = this.append(new FakeElement('g', html.match(/class="([^"]+)"/)?.[1] || 'topology-svg-node'));
      node.dataset.nodeId = html.match(/data-node-id="([^"]+)"/)?.[1];
      const instance = html.match(/data-instance-id="([^"]+)"/)?.[1];
      if (instance) node.dataset.instanceId = instance;
      node.append(new FakeElement('title'));
      const visual = node.append(new FakeElement('g', 'topology-node-visual'));
      visual.append(new FakeElement('circle', 'node-halo'));
      visual.append(new FakeElement('use'));
      visual.append(new FakeElement('text', 'node-label'));
      visual.append(new FakeElement('text', 'node-status'));
    }
  }
  set innerHTML(value) {
    this._innerHTML = value;
    this.children = [];
    if (!String(value).includes('topology-svg')) return;
    this.append(new FakeElement('div', 'topology-toolbar topology-controls'));
    const scroll = this.append(new FakeElement('div', 'topology-scroll'));
    scroll.append(new FakeElement('div', 'topology-tooltip hidden'));
    const svg = scroll.append(new FakeElement('svg', 'topology-svg'));
    svg.pauseAnimations = () => { svg.paused = true; };
    svg.unpauseAnimations = () => { svg.unpaused = true; };
    svg.append(new FakeElement('rect', 'topology-bg'));
    const viewport = svg.append(new FakeElement('g', 'topology-viewport'));
    viewport.append(new FakeElement('g', 'topology-links'));
    viewport.append(new FakeElement('g', 'topology-particles'));
    viewport.append(new FakeElement('g', 'topology-nodes'));
    this.append(new FakeElement('div', 'topology-empty hidden'));
  }
  get innerHTML() { return this._innerHTML || ''; }
}

function withTopologyDom(fn) {
  const originalDocument = global.document;
  const originalCss = global.CSS;
  const originalRaf = global.requestAnimationFrame;
  const originalCancel = global.cancelAnimationFrame;
  const originalMatchMedia = global.matchMedia;
  const container = new FakeElement('div', 'topology-diagram');
  global.document = { getElementById: (id) => (id === 'systemTopology' ? container : null), body: new FakeElement('body') };
  global.CSS = { escape: (value) => String(value).replace(/"/g, '\\"') };
  global.requestAnimationFrame = (cb) => { cb(Date.now() + 300); return 1; };
  global.cancelAnimationFrame = () => {};
  global.matchMedia = () => ({ matches: true });
  try { fn(container); } finally { global.document = originalDocument; global.CSS = originalCss; global.requestAnimationFrame = originalRaf; global.cancelAnimationFrame = originalCancel; global.matchMedia = originalMatchMedia; }
}

function testRenderSystemTopologyEffectiveShell() {
  withTopologyDom((container) => {
    ui.state.latest.status = { ok: true, proxy_id: 'proxy-a', ttl_s: 10 };
    ui.state.topology.positions = {};
    ui.state.topology.signature = '';
    ui.state.topology.handlersInstalled = false;
    ui.renderSystemTopology([inst('dom-a', true), inst('dom-b', false)], { ok: true });
    ['.topology-controls', '.topology-tooltip', '.topology-viewport', '.topology-links', '.topology-particles', '.topology-nodes'].forEach((selector) => assert(container.querySelector(selector), `missing ${selector}`));
    assert.strictEqual(container.querySelectorAll('.topology-svg-node').length, 4);
    assert(container.querySelector('.topology-node-visual'));
    const transform = container.querySelector('.topology-svg-node').getAttribute('transform');
    container.querySelector('.topology-svg-node').classList.add('is-hovered');
    assert.strictEqual(container.querySelector('.topology-svg-node').getAttribute('transform'), transform, 'hover must not replace outer translate transform');
    const shell = container.querySelector('.topology-svg');
    const handlers = container.handlers?.pointerover?.length || 0;
    const oldPositions = { ...ui.state.topology.positions };
    ui.renderSystemTopology([inst('dom-a', true, { resource: { cpu_util: 99 } }), inst('dom-b', false)], { ok: true });
    assert.strictEqual(container.querySelector('.topology-svg'), shell, 'metric-only render reuses shell');
    assert.strictEqual(container.handlers.pointerover.length, handlers, 'handlers are not installed twice');
    assert.deepStrictEqual(ui.state.topology.positions, oldPositions, 'metric-only changes preserve positions');
    assert(container.querySelectorAll('.topology-particle').some((item) => item.className.baseVal.includes('active')));
    assert(container.querySelectorAll('.topology-particle').some((item) => item.className.baseVal.includes('stale')));
    ui.renderSystemTopology([inst('dom-a', true)], { ok: true });
    assert(!container.querySelector('.topology-svg-node[data-node-id="instance:dom-b"]'), 'removed nodes are removed');
  });
}

function testPauseAnimationsDomFixture() {
  withTopologyDom((container) => {
    ui.renderSystemTopology([inst('pause-a', true)], { ok: true });
    const svg = container.querySelector('.topology-svg');
    ui.state.paused = true;
    ui.applyPausedTopologyState();
    assert.strictEqual(svg.paused, true);
    ui.state.paused = false;
    ui.applyPausedTopologyState();
    assert.strictEqual(svg.unpaused, true);
  });
}

function testHoverLifecycleHelpers() {
  const container = new FakeElement('div', 'topology-diagram');
  const nodeA = container.append(new FakeElement('g', 'topology-svg-node'));
  nodeA.dataset.nodeId = 'instance:a';
  const childA = nodeA.append(new FakeElement('circle', 'node-halo'));
  const nodeB = container.append(new FakeElement('g', 'topology-svg-node'));
  nodeB.dataset.nodeId = 'instance:b';
  let hover = 'instance:a';
  const originalHover = ui.state.topology.hoveredNodeId;
  const originalModel = ui.state.topology.model;
  ui.state.topology.model = ui.buildTopologyModel([inst('a', true), inst('b', true)], { ok: true }, { ok: true });
  ui.state.topology.hoveredNodeId = hover;
  ui.handleTopologyPointerOut(container, { target: childA, relatedTarget: nodeA });
  assert.strictEqual(ui.state.topology.hoveredNodeId, hover, 'child movement keeps hover');
  ui.handleTopologyPointerOut(container, { target: childA, relatedTarget: nodeB });
  assert.strictEqual(ui.state.topology.hoveredNodeId, 'instance:b', 'node-to-node switches hover');
  ui.state.topology.hoveredNodeId = 'instance:a';
  ui.handleTopologyPointerOut(container, { target: childA, relatedTarget: container });
  assert.strictEqual(ui.state.topology.hoveredNodeId, null, 'node-to-background clears hover');
  ui.state.topology.hoveredNodeId = 'instance:a';
  ui.handleTopologyPointerOut(container, { target: childA, relatedTarget: null });
  assert.strictEqual(ui.state.topology.hoveredNodeId, null, 'node-to-outside clears hover');
  ui.state.topology.hoveredNodeId = originalHover;
  ui.state.topology.model = originalModel;
}

function testProxyTooltipCountsAndPinnedCollisions() {
  const model = ui.buildTopologyModel([inst('alive', true), inst('stale', false), inst('unknown', undefined)], { ok: true }, { ok: true, proxy_id: 'proxy-a', ttl_s: 7 });
  const rows = ui.buildTooltipModel(model.nodes.find((n) => n.id === 'proxy'), model).rows;
  const byLabel = Object.fromEntries(rows.map((row) => [row.label, row.value]));
  assert.strictEqual(byLabel.TTL, '7');
  assert.strictEqual(byLabel['Alive Instance count'], '1');
  assert.strictEqual(byLabel['Stale Instance count'], '1');
  assert.strictEqual(byLabel['Unknown Instance count'], '1');
  const fallback = ui.buildTooltipModel({ type: 'proxy', raw: { ttl_seconds: 9 }, status: '' }, { instances: [], links: [] }).rows;
  assert.strictEqual(fallback.find((row) => row.label === 'TTL').value, '9');
  assert.strictEqual(ui.buildTooltipModel({ type: 'proxy', raw: { ttl_s: null }, status: '' }, { instances: [], links: [] }).rows.find((row) => row.label === 'TTL').value, '—');
  const bounds = { width: 400, height: 300, nodePadding: 10, labelPadding: 10 };
  let positions = { pinned: { x: 100, y: 100 }, free: { x: 110, y: 100 } };
  ui.resolveCollisions([{ id: 'pinned' }, { id: 'free' }], positions, bounds, 80, new Set(['pinned']));
  assert.deepStrictEqual(positions.pinned, { x: 100, y: 100 });
  assert(Math.hypot(positions.free.x - positions.pinned.x, positions.free.y - positions.pinned.y) >= 79);
  positions = { a: { x: 100, y: 100 }, b: { x: 110, y: 100 } };
  ui.resolveCollisions([{ id: 'a' }, { id: 'b' }], positions, bounds, 80, new Set(['a', 'b']));
  assert.deepStrictEqual(positions, { a: { x: 100, y: 100 }, b: { x: 110, y: 100 } });
  positions = { a: { x: 100, y: 100 }, b: { x: 110, y: 100 } };
  ui.resolveCollisions([{ id: 'a' }, { id: 'b' }], positions, bounds, 80, new Set());
  assert(positions.a.x < 100 && positions.b.x > 110, 'unpinned collision moves both nodes');
}

testSingleRenderSystemTopologyDeclaration();
testRenderSystemTopologyEffectiveShell();
testPauseAnimationsDomFixture();
testHoverLifecycleHelpers();
testProxyTooltipCountsAndPinnedCollisions();
console.log('proxy-ui pure function tests passed');
