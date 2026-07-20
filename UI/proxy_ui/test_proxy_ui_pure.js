const assert = require('assert');
const ui = require('./static/app.js');

function inst(id, alive, extra = {}) {
  return {
    instance_id: id,
    host: '127.0.0.1',
    port: 9000,
    is_alive: alive,
    last_seen_at: Date.now() / 1000,
    load: { inflight: 2, qps_1m: 1.5, gpu_util: 30, ...(extra.load || {}) },
    resource: { ...(extra.resource || {}) },
    meta: { ...(extra.meta || {}) },
  };
}

function testTopologyScenarios() {
  let model = ui.buildTopologyModel([], { error: 'proxy_id_not_configured' }, { proxy_id: 'proxy-a' });
  assert.strictEqual(model.instances.length, 0);
  assert.strictEqual(model.links[0].status, 'inactive');

  model = ui.buildTopologyModel([inst('one', true)], { ok: true }, { proxy_id: 'proxy-a' });
  assert.strictEqual(model.links[0].status, 'active');
  assert.strictEqual(model.links[1].status, 'active');
  assert.strictEqual(ui.stateLabel(model.instances[0]), 'alive');

  model = ui.buildTopologyModel([inst('alive', true), inst('stale', false), inst('unknown', undefined)], { ok: true }, {});
  assert.deepStrictEqual(model.instances.map((i) => i.instance_id), ['alive', 'stale', 'unknown']);
  assert.deepStrictEqual(model.links.slice(1).map((l) => l.status), ['active', 'stale', 'unknown']);

  const many = Array.from({ length: 24 }, (_, i) => inst(`node-${String(i).padStart(2, '0')}`, i % 2 === 0));
  const layoutA = ui.computeTopologyLayout(ui.buildTopologyModel(many, { ok: true }, {}));
  const layoutB = ui.computeTopologyLayout(ui.buildTopologyModel([...many].reverse(), { ok: true }, {}));
  assert.deepStrictEqual(layoutA.positions['instance:node-03'], layoutB.positions['instance:node-03']);
  assert(layoutA.height >= 360);
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

function testQueueExtraction() {
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

function testThemeAndReducedMotionStaticHooks() {
  const fs = require('fs');
  const css = fs.readFileSync(require('path').join(__dirname, 'static/style.css'), 'utf8');
  assert(css.includes("[data-theme='light']"));
  assert(css.includes('@media (prefers-reduced-motion: reduce)'));
  assert(css.includes('--topology-scheduler'));
}

testTopologyScenarios();
testMetricsAndMissingData();
testQueueExtraction();
testThemeAndReducedMotionStaticHooks();
console.log('proxy-ui pure function tests passed');
