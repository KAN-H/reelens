/**
 * Client bundle 契约测试：不装 DSH、不开浏览器。
 *
 * 这条最容易出事故（2026-11-02 的实例：`factory` 忘了 `return module.exports`，
 * 整棵前端插件树 boot 失败，页面只剩 "Failed to load plugins"）。
 * 所以这里用最小 loader 替身把 bundle 真跑一遍，并把六条硬规则逐条断言。
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import vm from 'node:vm';

const HERE = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(HERE, 'client.js'), 'utf8');
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

/** 最小 React 替身：只要 createElement 能拼出树就够。 */
const ReactStub = {
  createElement: (type, props, ...children) => ({ type, props, children }),
  useState: (initial) => [initial, () => {}],
  useMemo: (fn) => fn(),
  useEffect: () => {},
};

let loaded = null;
const sandbox = {
  window: {
    __ModuleLoader__: {
      load(entry) {
        loaded = entry;
      },
    },
  },
  console,
  setTimeout,
  fetch: () => Promise.resolve({ json: () => Promise.resolve({ ok: true }) }),
  encodeURIComponent,
  JSON,
  Object,
  String,
  Number,
  Boolean,
  Array,
  Promise,
  Error,
};
vm.createContext(sandbox);
vm.runInContext(source, sandbox);

console.log('client bundle contract:');

check('loads exactly one bundle entry', () => {
  assert.ok(loaded && typeof loaded === 'object', '没有调用 __ModuleLoader__.load');
  assert.equal(typeof loaded.factory, 'function');
});

check('bundle id equals package name', () => {
  assert.equal(loaded.id, pkg.name);
});

check('no import statements / no process access', () => {
  assert.ok(!/^\s*import\s/m.test(source), '禁止 import');
  assert.ok(!/\bprocess\./.test(source), '禁止访问 process');
});

check('only react is required', () => {
  const required = [...source.matchAll(/require\(\s*['"]([^'"]+)['"]\s*\)/g)].map((m) => m[1]);
  assert.deepEqual([...new Set(required)], ['react']);
});

check('factory returns a plugin object (not undefined)', () => {
  const exportsObject = loaded.factory((name) => {
    if (name === 'react') return ReactStub;
    throw new Error('unexpected require: ' + name);
  });
  assert.ok(exportsObject, 'factory 必须 return module.exports');
  assert.equal(typeof exportsObject.apply, 'function');
  assert.equal(typeof exportsObject.name, 'string');
  assert.ok(Array.isArray(exportsObject.inject));
  assert.ok(exportsObject.inject.includes('slots'), 'inject 必须包含 slots');
});

check('registers main panel with the real slot signature', () => {
  const exportsObject = loaded.factory(() => ReactStub);
  const calls = [];
  const ctx = {
    slots: {
      inject(slot, fn) { calls.push(['inject', slot]); fn(); },
      register(props, component) { calls.push(['register', slotOf(props), props, typeof component]); },
    },
  };
  function slotOf(props) { return props && props.name; }
  exportsObject.apply(ctx);
  const registered = calls.filter((call) => call[0] === 'register');
  assert.equal(registered.length, 2, '应该注册 main 面板 + 侧栏图标两处');
  const main = registered.find((call) => call[1] === 'main');
  assert.ok(main, '缺 main 槽位注册');
  assert.equal(main[2].key, 'video-breakdown');
  assert.equal(main[3], 'function', 'register 的第二个参数必须是组件函数');
  const side = registered.find((call) => call[1] === 'sidebar.panellist');
  assert.ok(side, '缺 sidebar.panellist 注册');
  assert.equal(side[2].id, 'video-breakdown');
  assert.equal(typeof side[2].order, 'number');
});

check('panel renders without throwing', () => {
  const exportsObject = loaded.factory(() => ReactStub);
  let panel = null;
  const ctx = {
    slots: {
      inject: (slot, fn) => fn(),
      register: (props, component) => { if (props && props.name === 'main') panel = component; },
    },
  };
  exportsObject.apply(ctx);
  assert.equal(typeof panel, 'function');
  const tree = panel();
  assert.ok(tree && tree.type === 'div');
});

check('partial registration failure is contained', () => {
  const exportsObject = loaded.factory(() => ReactStub);
  let registered = 0;
  const ctx = {
    slots: {
      inject(slot, fn) {
        if (slot === 'main') throw new Error('boom');
        fn();
      },
      register() { registered += 1; },
    },
  };
  exportsObject.apply(ctx); // 不应抛出
  assert.equal(registered, 1, 'main 失败后侧栏图标仍应注册');
});

check('routes referenced by the panel exist on the host side', async () => {
  const host = await import(pathToFileURL(join(HERE, 'index.js')).href);
  const paths = [...source.matchAll(/'(\/(?:plan|run|status|report|frames|frame))'/g)]
    .map((match) => match[1]);
  const hostPaths = Object.values(host.ROUTES).map((route) => route.replace('/api/video-breakdown', ''));
  for (const path of new Set(paths)) {
    const normalised = path === '/frames' ? '/frame' : path;
    assert.ok(hostPaths.includes(normalised), `面板用了 ${path}，宿主没有对应路由`);
  }
});

console.log(`\n${passed} checks passed`);
