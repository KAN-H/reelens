/**
 * 加载冒烟：用一个假 ctx 把 `apply()` 真跑一遍。
 *
 * `node --check` 只证明语法对；这条证明**插件真的能挂载**：注册出 6 条路由与 6 个工具，
 * 且 `/capabilities` 路由能返回合法 JSON。
 *
 * 这里断言的是**宿主真实的调用形状**，不是"能跑就行"：
 *   - `ctx.webServer.register({ kind:'exact', path, handler })` ← 对象参数！
 *     写成 `register(path, handler)` 会得到 `duplicate undefined route "undefined"`，
 *     整行静默激活失败（2026-11-23 实际踩过）。
 *   - `ctx.slots.register(props, Component)` 同理（见 test-client.mjs）。
 *   - `ctx.tools.register(tool)` 的 tool 必须带 output.render / isConcurrencySafe。
 */
import assert from 'node:assert/strict';
import { join, dirname } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const host = await import(pathToFileURL(join(HERE, 'index.js')).href);

const routes = new Map();
const tools = [];
let effects = 0;

const ctx = {
  effect(fn, _label) {
    effects += 1;
    const cleanup = fn();
    return typeof cleanup === 'function' ? cleanup : () => {};
  },
  get(name) { return name === 'webServer' ? ctx.webServer : undefined; },
  // cordis 的真实语义：只有 inject 里声明过的服务能拿到；没声明就永不回调。
  inject(names, callback) {
    assert.ok(Array.isArray(names), 'ctx.inject 第一参必须是数组');
    if (names.includes('webServer')) callback(ctx);
  },
  webServer: {
    register(spec) {
      // 真实签名：一个对象。这里不宽松处理——形状错了就要立刻失败。
      assert.ok(spec && typeof spec === 'object', 'webServer.register 必须收到对象');
      assert.equal(spec.kind, 'exact', 'kind 必须是 exact');
      assert.equal(typeof spec.path, 'string', 'path 必须是字符串');
      assert.ok(spec.path.startsWith('/api/video-breakdown/'), 'path 前缀不对：' + spec.path);
      assert.equal(typeof spec.handler, 'function', 'handler 必须是函数');
      routes.set(spec.path, spec.handler);
    },
  },
  tools: {
    register(tool) { tools.push(tool); },
  },
};

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

async function checkAsync(name, fn) {
  try {
    await fn();
    passed += 1;
    console.log('  ok   ' + name);
  } catch (error) {
    console.error('  FAIL ' + name + ' — ' + (error && error.message));
    process.exitCode = 1;
  }
}

console.log('load smoke:');
host.apply(ctx, {});
await new Promise((resolve) => setTimeout(resolve, 10));

check('apply registered 6 tools with full shape', () => {
  assert.equal(tools.length, 6, '注册到的工具：' + tools.map((t) => t.name).join(','));
  for (const tool of tools) {
    assert.equal(typeof tool.execute, 'function', tool.name);
    assert.equal(typeof tool.description, 'string', tool.name);
    assert.ok(tool.parameters && tool.parameters.type === 'object', tool.name);
    assert.ok(tool.output && typeof tool.output.render === 'function', tool.name);
    assert.equal(typeof tool.isConcurrencySafe, 'function', tool.name);
  }
});

check('apply registered 7 routes as objects (kind/path/handler)', () => {
  assert.equal(routes.size, 7, '注册到的路由：' + [...routes.keys()].join(','));
  for (const key of Object.keys(host.ROUTES)) {
    assert.ok(routes.has(host.ROUTES[key]), '缺路由 ' + host.ROUTES[key]);
  }
});

function fakeReq(headers = {}, method = 'GET', url = '/') {
  return { headers, method, url, async *[Symbol.asyncIterator]() { /* 无 body */ } };
}

function fakeRes() {
  const res = {
    statusCode: 0, headers: {}, body: '',
    writeHead(code, headers) { res.statusCode = code; res.headers = headers || {}; },
    setHeader(key, value) { res.headers[key] = value; },
    end(body) { res.body = body || ''; },
  };
  return res;
}

await checkAsync('capabilities route answers JSON', async () => {
  const handler = routes.get(host.ROUTES.capabilities);
  const res = fakeRes();
  handler(fakeReq({ host: '127.0.0.1:3080' }), res);
  await new Promise((resolve) => setTimeout(resolve, 30));
  assert.equal(res.statusCode, 200);
  const payload = JSON.parse(res.body);
  assert.equal(payload.ok, true);
  assert.equal(payload.tools.length, 6);
  assert.ok(Array.isArray(payload.vision_chain));
});

await checkAsync('write route rejects cross-origin', async () => {
  const handler = routes.get(host.ROUTES.plan);
  const res = fakeRes();
  handler(fakeReq({ origin: 'http://evil.test', host: '127.0.0.1:3080' }, 'POST'), res);
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(res.statusCode, 403);
  assert.match(JSON.parse(res.body).error, /跨源/);
});

await checkAsync('write route rejects missing action header', async () => {
  const handler = routes.get(host.ROUTES.plan);
  const res = fakeRes();
  handler(fakeReq({ host: '127.0.0.1:3080' }, 'POST'), res);
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(res.statusCode, 403);
  assert.match(JSON.parse(res.body).error, /x-video-breakdown-action/);
});

await checkAsync('read route rejects wrong method', async () => {
  const handler = routes.get(host.ROUTES.capabilities);
  const res = fakeRes();
  handler(fakeReq({ host: '127.0.0.1:3080' }, 'POST'), res);
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(res.statusCode, 405);
  // HTTP 头名大小写不敏感；假 res 不做归一化，所以两种都认
  const allow = res.headers.Allow || res.headers.allow;
  assert.equal(allow, 'GET');
});

await checkAsync('status route rejects illegal job id', async () => {
  const handler = routes.get(host.ROUTES.status);
  const res = fakeRes();
  handler(fakeReq({ host: '127.0.0.1:3080' }, 'GET', '/api/video-breakdown/status?job_id=../etc'), res);
  await new Promise((resolve) => setTimeout(resolve, 30));
  assert.equal(res.statusCode, 200);
  assert.equal(JSON.parse(res.body).ok, false);
});

await checkAsync('frame route rejects traversal', async () => {
  const handler = routes.get(host.ROUTES.frame);
  const res = fakeRes();
  handler(fakeReq({ host: '127.0.0.1:3080' }, 'GET', '/api/video-breakdown/frame?job_id=1&file=../../x.jpg'), res);
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(res.statusCode, 400);
});

console.log(`\n${passed} checks passed (effects=${effects})`);

// ---------------------------------------------------------------------------
// headless/TUI 可用性：没有 webServer 服务时，工具必须照常注册、不许抛错。
// （2026-11-23：inject 里硬写 webServer 会让整行永远 pending，headless 里工具全无。）
// ---------------------------------------------------------------------------
const headlessTools = [];
const headlessCtx = {
  effect(fn) { fn(); return () => {}; },
  get() { return undefined; },
  tools: { register(tool) { headlessTools.push(tool.name); } },
};
let headlessThrew = null;
try {
  host.apply(headlessCtx, {});
} catch (error) {
  headlessThrew = error;
}
console.log('headless smoke:');
if (headlessThrew) {
  console.error('  FAIL apply without webServer threw — ' + headlessThrew.message);
  process.exitCode = 1;
} else if (headlessTools.length !== 6) {
  console.error('  FAIL expected 6 tools without webServer, got ' + headlessTools.length);
  process.exitCode = 1;
} else {
  passed += 1;
  console.log('  ok   no webServer: 6 tools registered, no throw');
}
console.log(`\n${passed} checks passed total`);
