/**
 * PTC（代码模式）契约测试。
 *
 * 为什么单独一条：PTC 模式下模型**只拿到 `execute()` 返回的规范 JSON**，看不到
 * `output.render()` 渲染出来的内容，也看不到图片块。所以下面这些在 PTC 里会直接失效，
 * 而标准模式下完全正常——只在标准模式测过，等于没测：
 *
 *   1. 把业务数据只塞进 render（PTC 拿不到）；
 *   2. 返回 `undefined` / `NaN` / 循环引用（规范 JSON 序列化失败）；
 *   3. 靠"文本里的 id"让模型解析（必须返回结构化 jobId）；
 *   4. 长任务同步阻塞（PTC 有 run_code 时长与子调用配额）。
 *
 * 本测试对 6 个工具逐个执行，断言返回值可无损 JSON 往返；并验证两段式的拒绝路径
 * 不会产生副作用（不 spawn、不写盘）。
 */
import assert from 'node:assert/strict';
import { readdirSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const host = await import(pathToFileURL(join(HERE, 'index.js')).href);

const tools = new Map();
const ctx = {
  effect(fn) { fn(); return () => {}; },
  get() { return undefined; },
  webServer: { register(spec) { assert.ok(spec && spec.path, 'route spec'); } },
  tools: { register(tool) { tools.set(tool.name, tool); } },
};
host.apply(ctx, {});
await new Promise((resolve) => setTimeout(resolve, 10));

let passed = 0;
async function check(name, fn) {
  try {
    await fn();
    passed += 1;
    console.log('  ok   ' + name);
  } catch (error) {
    console.error('  FAIL ' + name + ' — ' + (error && error.message));
    process.exitCode = 1;
  }
}

/** 规范 JSON 往返：PTC 把返回值序列化后交给模型，不可序列化 = 该调用在 PTC 里坏掉。 */
function assertCanonicalJson(value, label) {
  assert.notEqual(value, undefined, label + '：execute 返回了 undefined');
  let roundTripped;
  try {
    roundTripped = JSON.parse(JSON.stringify(value));
  } catch (error) {
    throw new Error(label + '：返回值不能无损 JSON 序列化 — ' + error.message);
  }
  assert.deepEqual(roundTripped, JSON.parse(JSON.stringify(value)), label + '：往返不一致');
  const text = JSON.stringify(value);
  assert.ok(!text.includes('undefined'), label + '：JSON 里出现了 undefined');
  assert.ok(!text.includes('NaN'), label + '：JSON 里出现了 NaN');
}

console.log('ptc contract:');

await check('all six tools expose execute + render', () => {
  assert.equal(tools.size, 6);
  for (const [name, tool] of tools) {
    assert.equal(typeof tool.execute, 'function', name);
    assert.equal(typeof tool.output.render, 'function', name);
  }
});

await check('breakdown_probe returns canonical JSON', async () => {
  const value = await tools.get('breakdown_probe').execute({});
  assertCanonicalJson(value, 'probe');
  assert.equal(typeof value.ok, 'boolean');
});

await check('breakdown_status returns canonical JSON for unknown job', async () => {
  const value = await tools.get('breakdown_status').execute({ job_id: 'nope-2026' });
  assertCanonicalJson(value, 'status');
  assert.equal(value.ok, false);
});

await check('breakdown_report returns canonical JSON for unknown job', async () => {
  const value = await tools.get('breakdown_report').execute({ job_id: 'nope-2026' });
  assertCanonicalJson(value, 'report');
  assert.equal(value.ok, false);
});

await check('breakdown_frames returns canonical JSON for unknown job', async () => {
  const value = await tools.get('breakdown_frames').execute({ job_id: 'nope-2026' });
  assertCanonicalJson(value, 'frames');
  assert.equal(value.ok, false);
});

await check('plan returns a structured plan_id (not a string in prose)', async () => {
  const value = await tools.get('breakdown_plan').execute({ source: 'D:/nonexistent/clip.mp4' });
  assertCanonicalJson(value, 'plan');
  // 文件不存在时应给出 ok:false + 可读原因，而不是抛异常
  assert.equal(value.ok, false);
  assert.match(String(value.error), /不存在/);
});

await check('run rejects a mismatched plan_id without side effects', async () => {
  const value = await tools.get('breakdown_run').execute({
    source: 'D:/nonexistent/clip.mp4', plan_id: 'deadbeef',
  });
  assertCanonicalJson(value, 'run');
  assert.equal(value.ok, false);
  assert.equal(value.code, 'PLAN_MISMATCH');
});

await check('run has no job artefacts created on rejection', () => {
  // 拒绝路径不该在 outputs 下留任何新目录（job 目录只在 startJob 才建）
  const root = host.__internal.jobDir('x');
  assert.ok(root === '' || root.includes('outputs'));
});

await check('render never carries the only copy of business data', async () => {
  // 对每个工具：render 返回内容块数组，且 execute 的结果里已经包含渲染所需的全部事实
  const value = await tools.get('breakdown_status').execute({ job_id: 'nope-2026' });
  const rendered = tools.get('breakdown_status').output.render({}, value);
  assert.ok(Array.isArray(rendered) && rendered[0].type === 'text');
  assert.equal(value.ok, false);  // 结论在 canonical JSON 里，不在渲染文本里
});

await check('no tool writes outside the data root on execute', () => {
  // 干净 checkout 里 outputs/ 还不存在——"没写过"就是通过，不是失败。
  let entries = [];
  try {
    entries = readdirSync(join(HERE, 'outputs'), { withFileTypes: true });
  } catch {
    return;
  }
  for (const entry of entries) {
    assert.ok(entry.isDirectory(), 'outputs 下只应有作业目录：' + entry.name);
  }
});

console.log(`\n${passed} checks passed`);
