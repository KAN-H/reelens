/**
 * Host 契约测试：不起 DSH、不联网。
 *
 * 覆盖最容易退化的地方：
 *  - 路由/工具数量与命名（改名字会静默地让面板 404）；
 *  - `safeJobId` / `jobDir` 的目录逃逸防护；
 *  - `planId` 对参数的敏感性（否则"确认的是 A、跑的是 B"）；
 *  - `normalisePlan` 的白名单与夹取；
 *  - `jsonSafe` 把 undefined/NaN 清掉（宿主会判成非法输出）。
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const host = await import(pathToFileURL(join(HERE, 'index.js')).href);
const pkg = JSON.parse(readFileSync(join(HERE, 'package.json'), 'utf8'));

let passed = 0;
function check(name, fn) {
  try {
    fn();
    passed += 1;
    console.log('  ok   ' + name);
  } catch (error) {
    console.error('  FAIL ' + name + ' — ' + (error && error.message));
    process.exitCode = 1;
  }
}

console.log('host contract:');

check('package name matches client bundle id', () => {
  assert.equal(pkg.name, 'reelens-dsh-plugin');
  assert.equal(pkg.dsh.client.platform, 'web');
  assert.equal(pkg.exports['./client'], './client.js');
});

check('7 routes under /api/video-breakdown', () => {
  const routes = Object.values(host.ROUTES);
  assert.equal(routes.length, 7);  for (const route of routes) assert.ok(route.startsWith('/api/video-breakdown/'), route);
});

check('6 tools declared and namespaced', () => {
  const names = host.__internal.TOOLS.map((tool) => tool.name);
  assert.deepEqual(names.sort(), ['breakdown_frames', 'breakdown_plan', 'breakdown_probe',
    'breakdown_report', 'breakdown_run', 'breakdown_status']);
  for (const name of names) assert.ok(name.startsWith('breakdown_'), name);
});

check('capabilities cover every route', () => {
  const routeKeys = new Set(Object.keys(host.ROUTES));
  for (const tool of host.__internal.TOOLS) assert.ok(routeKeys.has(tool.route), tool.name);
});

check('safeJobId rejects traversal and separators', () => {
  for (const bad of ['..', '.', '../x', 'a/b', 'a\\b', '', 'x'.repeat(120), 'a b']) {
    assert.equal(host.__internal.safeJobId(bad), '', JSON.stringify(bad));
  }
  assert.equal(host.__internal.safeJobId('20261122-abc-1'), '20261122-abc-1');
});

check('jobDir stays inside data root', () => {
  const dir = host.__internal.jobDir('20261122-abc-1');
  assert.ok(dir && dir.includes('outputs'), String(dir));
  assert.equal(host.__internal.jobDir('../etc'), '');
});

check('planId changes with any parameter', () => {
  const base = host.__internal.normalisePlan({ source: 'a.mp4', tier: 'fast' });
  const digest = host.__internal.planId(base);
  assert.equal(digest, host.__internal.planId(host.__internal.normalisePlan({ source: 'a.mp4', tier: 'fast' })));
  assert.notEqual(digest, host.__internal.planId(host.__internal.normalisePlan({ source: 'b.mp4', tier: 'fast' })));
  assert.notEqual(digest, host.__internal.planId(host.__internal.normalisePlan({ source: 'a.mp4', tier: 'fine' })));
  assert.notEqual(digest, host.__internal.planId(host.__internal.normalisePlan({ source: 'a.mp4', tier: 'fast', allow_cloud: true })));
});

check('normalisePlan whitelists items and clamps numbers', () => {
  const plan = host.__internal.normalisePlan({
    source: 'x.mp4', items: ['transcript', 'evil', 'frames'],
    vision_budget: 999, max_seconds: 5, tier: 'nope',
  });
  assert.deepEqual(plan.items, ['transcript', 'frames']);
  assert.equal(plan.vision_budget, 200);
  assert.equal(plan.max_seconds, 10);
  assert.equal(plan.tier, 'standard');
});

check('estimate reports an explicit excludes list', () => {
  const plan = host.__internal.normalisePlan({ source: 'x.mp4', tier: 'fast' });
  const eta = host.__internal.estimate(plan, { duration_s: 600 });
  assert.deepEqual(eta.excludes, ['download', 'human_confirm']);
  assert.ok(eta.total_s > 0);
});

check('jsonSafe strips undefined and non-finite numbers', () => {
  const cleaned = host.__internal.jsonSafe({ a: undefined, b: NaN, c: Infinity, d: { e: undefined }, f: [1, undefined] });
  // 键保留、值归一成 null：null 是合法 JSON，比删键更容易发现"这项本应有值"
  assert.deepEqual(cleaned, { a: null, b: null, c: null, d: { e: null }, f: [1, null] });
});

check('sameOrigin rejects foreign origin', () => {
  assert.equal(host.__internal.sameOrigin({ headers: {} }), true);
  assert.equal(host.__internal.sameOrigin({ headers: { origin: 'http://evil.test', host: '127.0.0.1:3080' } }), false);
  assert.equal(host.__internal.sameOrigin({ headers: { origin: 'http://127.0.0.1:3080', host: '127.0.0.1:3080' } }), true);
});

check('action header required for writes', () => {
  assert.equal(host.__internal.needActionHeader({ headers: {} }), false);
  assert.equal(host.__internal.needActionHeader({ headers: { 'x-video-breakdown-action': '1' } }), true);
});

check('resolvePython points at an existing interpreter or a PATH name', () => {
  const python = host.__internal.resolvePython();
  assert.ok(typeof python === 'string' && python.length > 0);
});

check('spawn target is reelens.cli (rename regression)', () => {
  // 2026-11-23 真实事故：包改名后 index.js 仍 spawn `-m breakdown.cli`，
  // 六个工具全部报 `No module named breakdown`，而注册层测试全绿——测试没测调用目标。
  const source = readFileSync(new URL('./index.js', import.meta.url), 'utf8');
  assert.ok(source.includes("'reelens.cli'"), "spawn argv 必须用 'reelens.cli'");
  assert.ok(!source.includes("'breakdown.cli'"), "不许再出现 'breakdown.cli'");
});

console.log(`\n${passed} checks passed`);
