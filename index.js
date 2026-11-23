/**
 * video-breakdown — 视频/音频拆解（Host 侧薄壳）。
 *
 * 设计口径（与 `host/media`、`host/bilibili` 同一套，不是自创）：
 *
 * - **零 `@deepseek-ai/*` 静态导入**：本包以 `link:` 装进 profile，那里没有 harness
 *   构建产物；静态导入会让整行加载失败。
 * - **工具是薄壳**：每次调用都是 `python -m reelens.cli …`，参数拼装与 JSON 透传在这里，
 *   业务规则（阈值/校验/降级）全在 Python 侧。这条是硬约束：面板能做的每一件事，CLI 都能做。
 * - **execute 只返回规范 JSON**：PTC 模式下模型只拿到规范值（不是渲染内容），
 *   所以任何业务数据都不许只存在于 `render` 里。
 * - **两段式**：`breakdown_plan` 出清单（含"会把 N 帧发往哪个 provider"的披露）；
 *   `breakdown_run` 必须带 `plan_id`，与当前参数重算的 sha 不一致就 409。
 * - **路由命名空间独立**：`/api/video-breakdown/*`，与其它插件的前缀不重叠。
 *
 * 安全守卫（写/贵操作）：同源 + `x-video-breakdown-action: 1` + jobId 单层白名单。
 */

import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs';
import { dirname, extname, join, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { TOOLS } from './lib/capabilities.js';

export const name = 'video-breakdown';
/**
 * 硬依赖只有 `tools`。`webServer` 在 apply 内部**按需等待**，不写进 inject。
 *
 * 教训（2026-11-23，两次）：
 *  1. 原来写 `inject = ['webServer', 'tools']`，在 headless/TUI 这种没有 HTTP 服务的宿主里
 *     整行永远停在 `pending (waiting for service: webServer)`，工具一个都不注册；
 *  2. 改成 `{ required, optional }` 对象形状也不行——本机这版 cordis 只吃数组，
 *     它把对象当成服务名映射，报 `pending (waiting for services: required, optional)`。
 * 现在的做法：数组只声明 tools，路由在 apply 里 `ctx.inject(['webServer'], …)` 延迟挂载，
 * 拿不到就只注册工具（CLI/工具照样能干活）。
 */
export const inject = ['tools'];

const HERE = dirname(fileURLToPath(import.meta.url));
const PLUGIN_ROOT = HERE;

/** 数据根：默认放插件仓库内（已 gitignore），可在行 config 里覆盖。 */
let DATA_ROOT = join(PLUGIN_ROOT, 'outputs');

/** 视觉默认预算（帧）；档位在 Python 侧还有各自上限，这里只是幅度保护。 */
const DEFAULT_ITEMS = ['transcript', 'shots', 'frames', 'visual', 'subtitle_style', 'on_screen_text',
  'editing', 'audio', 'beat', 'voice', 'copywriting'];
/** 全部可选条目（`summary` 默认不开：它只在你要读内容时才有意义）。 */
const ALL_ITEMS = [...DEFAULT_ITEMS, 'summary'];

const TIERS = ['fast', 'standard', 'fine'];

/** 正在跑的作业（jobId → {child, startedAt, outDir}）。同一 jobId 不重复起。 */
const RUNNING = new Map();

/** 由 DSH credentials 服务解析凭据（apply 时注入）；服务缺席时回落进程环境。 */
let credentialResolver = null;

/** 视觉 provider → 凭据名（与 Python 侧 `vision.chain()` 的读法对齐）。 */
const VISION_CREDENTIALS = {
  AMDGPU_API_KEY: 'AMDGPU_API_KEY',
  DEEPSEEK_API_KEY: 'DEEPSEEK_API_KEY',
};

export const ROUTES = Object.freeze({
  capabilities: '/api/video-breakdown/capabilities',
  jobs: '/api/video-breakdown/jobs',
  plan: '/api/video-breakdown/plan',
  run: '/api/video-breakdown/run',
  status: '/api/video-breakdown/status',
  report: '/api/video-breakdown/report',
  frame: '/api/video-breakdown/frame',
});

// ---------------------------------------------------------------------------
// 路径与小工具
// ---------------------------------------------------------------------------

/** Python 解释器：BREAKDOWN_PYTHON → 插件内 .venv-breakdown → PATH。 */
export function resolvePython() {
  const explicit = String(process.env.BREAKDOWN_PYTHON || '').trim();
  if (explicit && existsSync(explicit)) return explicit;
  const candidates = [
    join(PLUGIN_ROOT, '.venv-breakdown', 'Scripts', 'python.exe'),
    join(PLUGIN_ROOT, '.venv-breakdown', 'bin', 'python'),
    join(PLUGIN_ROOT, '.venv', 'Scripts', 'python.exe'),
    join(PLUGIN_ROOT, '.venv', 'bin', 'python'),
  ];
  for (const candidate of candidates) {
    if (existsSync(candidate)) return candidate;
  }
  return process.platform === 'win32' ? 'python' : 'python3';
}

/** jobId 只允许单层目录名（与 media 的 rel 白名单同口径）。 */
export function safeJobId(value) {
  const text = String(value || '').trim();
  if (!/^[A-Za-z0-9._-]{1,80}$/.test(text)) return '';
  if (text === '.' || text === '..') return '';
  return text;
}

export function jobDir(jobId) {
  const safe = safeJobId(jobId);
  if (!safe) return '';
  const target = resolve(DATA_ROOT, safe);
  if (!target.startsWith(resolve(DATA_ROOT) + sep)) return '';
  return target;
}

/** 把 undefined/NaN 这类会被宿主判成非法输出的值清掉。 */
export function jsonSafe(value) {
  if (value === undefined || value === null) return null;
  if (typeof value === 'number') return Number.isFinite(value) ? value : null;
  if (typeof value === 'string' || typeof value === 'boolean') return value;
  if (Array.isArray(value)) return value.map((item) => jsonSafe(item));
  if (typeof value === 'object') {
    const out = {};
    for (const [key, item] of Object.entries(value)) out[key] = jsonSafe(item);
    return out;
  }
  return null;
}

function slugOf(source) {
  const text = String(source || '');
  const base = text.startsWith('http') ? (text.split('/').filter(Boolean).pop() || 'url') : text;
  const cleaned = base.replace(/\.[A-Za-z0-9]{1,6}$/, '').replace(/[^A-Za-z0-9._-]+/g, '-');
  return (cleaned || 'job').slice(0, 40);
}

/** 计划 id：来源 + 条目 + 档位 + 预算，任何一项变了就必须重新确认。 */
export function planId(plan) {
  const payload = JSON.stringify({
    source: String(plan.source || ''),
    items: [...(plan.items || [])].sort(),
    tier: String(plan.tier || ''),
    budget: Number(plan.vision_budget || 0),
    allowCloud: Boolean(plan.allow_cloud),
    maxSeconds: Number(plan.max_seconds || 0),
  });
  return createHash('sha256').update(payload).digest('hex');
}

function readJson(path, fallback = null) {
  try {
    return JSON.parse(readFileSync(path, 'utf8'));
  } catch {
    return fallback;
  }
}

function tail(path, limit = 4000) {
  try {
    return readFileSync(path, 'utf8').slice(-limit);
  } catch {
    return '';
  }
}

function sendJson(res, statusCode, payload, extraHeaders = {}) {
  const body = JSON.stringify(jsonSafe(payload));
  res.writeHead(statusCode, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
    ...extraHeaders,
  });
  res.end(body);
}

async function readBody(req, limit = 1024 * 1024) {
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > limit) return null;
    chunks.push(chunk);
  }
  const text = Buffer.concat(chunks).toString('utf8');
  if (!text.trim()) return {};
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// 守卫
// ---------------------------------------------------------------------------

export function sameOrigin(req) {
  const origin = String(req.headers.origin || '');
  if (!origin) return true; // 同源导航/无 Origin 的本地请求
  try {
    const host = String(req.headers.host || '');
    return new URL(origin).host === host;
  } catch {
    return false;
  }
}

export function needActionHeader(req) {
  return String(req.headers['x-video-breakdown-action'] || '') === '1';
}

// ---------------------------------------------------------------------------
// 子进程
// ---------------------------------------------------------------------------

/** 子进程环境：注入视觉凭据（来自 DSH credentials），并强制 UTF-8。 */
async function childEnv(extra = {}) {
  const env = { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1', ...extra };
  // 视觉链的 key 优先从 DSH credentials 解析（不落盘、不写日志）。
  // 注意 `credentials.resolve()` 是**异步**的：忘了 await 会拿到 Promise，
  // 于是 `typeof value === 'string'` 永远为假 → 子进程拿不到 key → 视觉段被 SKIPPED
  // （2026-11-23 实际踩过，表现为"计划里说有 amdgpu，跑完却是 SKIPPED"）。
  for (const [envName, credName] of Object.entries(VISION_CREDENTIALS)) {
    if (env[envName]) continue;
    const value = credentialResolver ? await credentialResolver(credName) : undefined;
    if (typeof value === 'string' && value.trim()) env[envName] = value.trim();
  }
  return env;
}

/** 跑一次 CLI，返回 `{ok, code, json, stderrTail}`；超时杀子进程。 */
export async function runCli(args, { timeoutMs = 3600000, signal } = {}) {
  const python = resolvePython();
  const env = await childEnv();
  return new Promise((resolvePromise) => {
    let stdout = '';
    let stderr = '';
    let child;
    try {
      child = spawn(python, ['-m', 'reelens.cli', ...args], {
        cwd: PLUGIN_ROOT, env, windowsHide: true,
      });
    } catch (error) {
      resolvePromise({ ok: false, code: -1, json: null, stderrTail: String(error) });
      return;
    }
    const timer = setTimeout(() => {
      try { child.kill(); } catch { /* 超时兜底 */ }
    }, Math.max(1000, timeoutMs));
    const abort = () => { try { child.kill(); } catch { /* ignore */ } };
    if (signal && typeof signal.addEventListener === 'function') signal.addEventListener('abort', abort);
    child.stdout.on('data', (chunk) => { stdout += chunk.toString('utf8'); });
    child.stderr.on('data', (chunk) => { stderr += chunk.toString('utf8'); });
    child.on('close', (code) => {
      clearTimeout(timer);
      if (signal && typeof signal.removeEventListener === 'function') signal.removeEventListener('abort', abort);
      let parsed = null;
      const text = stdout.trim();
      if (text) {
        try {
          parsed = JSON.parse(text);
        } catch {
          const start = text.indexOf('{');
          if (start >= 0) {
            try { parsed = JSON.parse(text.slice(start)); } catch { parsed = null; }
          }
        }
      }
      resolvePromise({
        ok: code === 0 && parsed !== null,
        code,
        json: parsed,
        stderrTail: stderr.trim().slice(-1500),
      });
    });
  });
}

// ---------------------------------------------------------------------------
// 计划与作业
// ---------------------------------------------------------------------------

/**
 * 从用户粘贴的文本里取出真正要拆解的东西。
 *
 * 为什么需要（2026-11-23 用户实测）：B 站/抖音手机版"分享"给的是
 * **一段带标题的文案 + 链接**，比如
 *   `【中年贫血_石原里美…（中日双语）】 https://www.bilibili.com/video/BV1Pqh26pEVQ/?share_source=copy_web…`
 * 直接整段当 source 会被判成"本地文件"，报"本地文件不存在"——用户完全看不懂。
 * 所以只要有 `http(s)://` 就把它抠出来，把分享标题丢掉（标题本来也会从视频元数据拿到）。
 */
function extractSource(raw) {
  const text = String(raw || '').trim();
  if (!text) return '';
  const match = text.match(/https?:\/\/[^\s<>"'）)】]+/i);
  if (match) return match[0].replace(/[，。、；：,.）)】]+$/, '');
  // 也容忍 `BV1xxxxxxxxx` 这种裸号：补成标准链接
  const bvid = text.match(/\b(BV[0-9A-Za-z]{10})\b/);
  if (bvid) return `https://www.bilibili.com/video/${bvid[1]}/`;
  return text;
}

function normalisePlan(body) {
  const source = extractSource(body && body.source);
  const items = Array.isArray(body && body.items) && body.items.length
    ? body.items.map((item) => String(item)).filter((item) => ALL_ITEMS.includes(item))
    : [...DEFAULT_ITEMS];
  const tier = TIERS.includes(String(body && body.tier)) ? String(body.tier) : 'standard';
  const visionBudget = Math.max(0, Math.min(200, Number((body && body.vision_budget) || 0) || 0));
  const maxSeconds = Math.max(10, Math.min(4 * 3600, Number((body && body.max_seconds) || 3600) || 3600));
  return {
    source,
    items,
    tier,
    vision_budget: visionBudget,
    max_seconds: maxSeconds,
    allow_cloud: Boolean(body && body.allow_cloud),
    no_asr: Boolean(body && body.no_asr),
    no_vision: Boolean(body && body.no_vision),
  };
}

const ETA = {
  fast: { resolve_local: 1, resolve_url: 20, shots: 12, frames: 3, asr_per_min: 8, vision_per_frame: 2.5 },
  standard: { resolve_local: 1, resolve_url: 25, shots: 14, frames: 6, asr_per_min: 8, vision_per_frame: 2.5 },
  fine: { resolve_local: 1, resolve_url: 30, shots: 30, frames: 14, asr_per_min: 8, vision_per_frame: 3 },
};

/** 粗估耗时（只用来说明量级，不是承诺；口径在报告里写明"不含下载与人工确认"）。 */
export function estimate(plan, probe) {
  const table = ETA[plan.tier] || ETA.standard;
  const duration = Number((probe && probe.duration_s) || 0);
  const minutes = duration / 60;
  const isUrl = /^https?:\/\//i.test(plan.source);
  const visionFrames = plan.no_vision ? 0 : (plan.vision_budget || (plan.tier === 'fast' ? 8 : plan.tier === 'standard' ? 24 : 60));
  const parts = {
    resolve: isUrl ? table.resolve_url : table.resolve_local,
    shots: table.shots,
    frames: table.frames,
    asr: plan.no_asr ? 0 : Math.round(minutes * table.asr_per_min),
    vision: Math.round(visionFrames * table.vision_per_frame),
  };
  const total = Object.values(parts).reduce((sum, value) => sum + value, 0);
  return { excludes: ['download', 'human_confirm'], parts, total_s: total };
}

function newJobId(source) {
  const stamp = new Date().toISOString().replace(/[-:T]/g, '').slice(0, 14);
  const hash = createHash('sha256').update(`${source}:${Date.now()}`).digest('hex').slice(0, 6);
  return `${stamp}-${slugOf(source)}-${hash}`;
}

async function startJob(plan, planDigest) {
  const jobId = newJobId(plan.source);
  const outDir = jobDir(jobId);
  if (!outDir) throw new Error('jobId 不合法');
  mkdirSync(outDir, { recursive: true });
  const job = {
    schema: 'video-breakdown.job.v1',
    job_id: jobId,
    plan_id: planDigest,
    plan,
    status: 'running',
    started_at: new Date().toISOString(),
    out_dir: outDir,
  };
  writeFileSync(join(outDir, 'job.json'), JSON.stringify(job, null, 2), 'utf8');

  const args = ['run', plan.source, '--out', outDir, '--tier', plan.tier,
    '--plan-id', planDigest, '--max-seconds', String(plan.max_seconds)];
  // 条目选择必须**真的**传到 CLI：否则面板上取消勾选只是装饰，用户点完发现时间一样长
  // （2026-11-23 用户实测："只提取字幕"却还是跑了抽帧/视觉）。
  if (plan.items && plan.items.length && plan.items.length !== DEFAULT_ITEMS.length) {
    args.push('--items', plan.items.join(','));
  }
  if (plan.vision_budget) args.push('--vision-budget', String(plan.vision_budget));
  if (plan.allow_cloud) args.push('--allow-cloud');
  if (plan.no_asr) args.push('--no-asr');
  if (plan.no_vision) args.push('--no-vision');

  // 记录"哪些视觉凭据变量确实进了子进程"（只记名字与有无，不记值）——
  // 排查"计划说有 amdgpu、跑完却 SKIPPED"这类问题时，这一行是唯一证据。
  const env = await childEnv();
  const visionEnv = {};
  for (const envName of Object.keys(VISION_CREDENTIALS)) visionEnv[envName] = Boolean(env[envName]);
  job.vision_env = visionEnv;

  const child = spawn(resolvePython(), ['-m', 'reelens.cli', ...args], {
    cwd: PLUGIN_ROOT, env, windowsHide: true,
  });
  const log = [];
  const onData = (chunk) => {
    log.push(chunk.toString('utf8'));
    writeFileSync(join(outDir, 'job.log'), log.join('').slice(-20000), 'utf8');
  };
  child.stdout.on('data', onData);
  child.stderr.on('data', onData);
  RUNNING.set(jobId, { child, startedAt: Date.now(), outDir });
  child.on('close', (code) => {
    RUNNING.delete(jobId);
    const updated = {
      ...job,
      status: code === 0 ? 'done' : 'failed',
      exit_code: code,
      ended_at: new Date().toISOString(),
    };
    try {
      writeFileSync(join(outDir, 'job.json'), JSON.stringify(updated, null, 2), 'utf8');
    } catch { /* 写不进 job.json 不影响报告本身 */ }
  });
  return { jobId, outDir };
}

// ---------------------------------------------------------------------------
// 工具
// ---------------------------------------------------------------------------

function text(value) {
  return [{ type: 'text', text: String(value) }];
}

/** 图片块：把本地帧登记成 durable attachment（仅在 Native 渲染里可见）。 */
async function imageBlocks(ctx, paths, limit = 6) {
  const attachments = ctx.get('attachments');
  if (!attachments || typeof attachments.saveImage !== 'function') return null;
  const blocks = [];
  for (const path of paths.slice(0, limit)) {
    try {
      const bytes = readFileSync(path);
      const mediaType = bytes[0] === 0x89 && bytes[1] === 0x50 ? 'image/png' : 'image/jpeg';
      const ref = await attachments.saveImage({ data: bytes, mediaType, name: `breakdown-${Date.now()}` });
      blocks.push({ type: 'image', attachment: ref });
    } catch {
      /* 单帧失败不阻断其它帧 */
    }
  }
  return blocks.length ? blocks : null;
}

function summarisePlan(plan, digest, probe, eta) {
  const duration = probe && probe.duration_s ? `${Math.round(Number(probe.duration_s))}s` : '';
  const meta = probe
    ? [probe.size, probe.fps ? `${probe.fps}fps` : '', duration,
      probe.has_audio === false ? '无音轨' : ''].filter(Boolean).join(' · ')
    : '';
  const lines = [
    `拆解计划 ${digest.slice(0, 12)} · 档位 ${plan.tier}`,
    `来源：${plan.source}`,
    firstLine(probe) ? `预览：${firstLine(probe)}${meta ? '（' + meta + '）' : ''}` : '',
    `条目：${plan.items.join(' / ')}`,
    `视觉：${plan.no_vision ? '关' : `${plan.vision_budget || '按档位'} 帧${plan.allow_cloud ? '（允许云端）' : ''}`}`,
    `转写：${plan.no_asr ? '关' : '开'}`,
    `预计：约 ${eta.total_s}s（不含下载与人工确认）`,
    `确认后调用 breakdown_run，plan_id=${digest.slice(0, 12)}`,
  ].filter(Boolean);
  return lines.join('\n');
}

function firstLine(probe) {
  if (!probe || typeof probe !== 'object') return '';
  if (probe.title) return String(probe.title);
  if (probe.status) return `${probe.status}${probe.reason ? ' ' + probe.reason : ''}`;
  return '';
}

function toolDefinitions(ctx, config) {
  return [
    {
      name: 'breakdown_plan',
      description: '拆解第一步（不下载、不花钱）：给出拆解清单、档位预估耗时、视觉外发披露，返回 plan_id。用户确认后用 breakdown_run。',
      parameters: {
        type: 'object',
        properties: {
          source: { type: 'string', description: '视频/播客 URL，或本地文件绝对路径' },
          tier: { type: 'string', enum: TIERS, description: '档位，缺省 standard' },
          items: { type: 'array', items: { type: 'string' }, description: '要做的条目 id 列表' },
          vision_budget: { type: 'number', description: '送视觉模型的帧数上限（0=按档位）' },
          allow_cloud: { type: 'boolean', description: '允许把帧发往付费云端视觉模型' },
          no_asr: { type: 'boolean' },
          no_vision: { type: 'boolean' },
        },
        required: ['source'],
      },
      output: {
        schema: { type: 'object', additionalProperties: true },
        render(_args, value) {
          return text(value && value.ok ? summarisePlan(value.plan, value.plan_id, value.preview, value.eta)
            : `计划失败：${(value && value.error) || '未知原因'}`);
        },
        presentationMeta(_args, value) { return value; },
      },
      async execute(args) {
        const plan = normalisePlan(args);
        if (!plan.source) return { ok: false, error: '需要 source（URL 或本地文件）' };
        const local = !/^https?:\/\//i.test(plan.source);
        let preview = null;
        let probe = null;
        let media = null;
        if (local) {
          if (!existsSync(plan.source)) return { ok: false, error: `本地文件不存在：${plan.source}` };
          // `info` 是只读 ffprobe（不下载、不抽帧）：用它拿真实时长，估时才有意义
          const info = await runCli(['info', plan.source], { timeoutMs: 120000 });
          media = info.json;
          const stat = statSync(plan.source);
          preview = {
            title: plan.source.split(/[\\/]/).pop(),
            bytes: stat.size,
            kind: 'local',
            duration_s: media && media.duration_s,
            size: media ? `${media.display_width}x${media.display_height}` : null,
            fps: media && media.fps,
            has_audio: media ? Boolean(media.has_audio) : null,
            probe_status: media && media.status,
          };
          if (media && media.status !== 'MEASURED') {
            return jsonSafe({ ok: false, error: `探测失败：${media.reason || media.status}`, preview });
          }
        }
        const check = await runCli(['probe'], { timeoutMs: 120000 });
        probe = check.json;
        const digest = planId(plan);
        const eta = estimate(plan, media || { duration_s: 0 });
        const chains = await visionDisclosure();
        return jsonSafe({
          ok: true,
          plan_id: digest,
          plan,
          preview,
          probe_ok: Boolean(probe && probe.ok),
          eta,
          vision_chain: chains,
          note: '本步骤不下载、不抽帧、不调用模型；确认后 run 才会实际执行',
        });
      },
      isConcurrencySafe: () => true,
    },
    {
      name: 'breakdown_run',
      description: '拆解第二步：按已确认的 plan_id 起后台作业。plan_id 与当前参数不一致会拒绝（防偷改）。',
      parameters: {
        type: 'object',
        properties: {
          source: { type: 'string' },
          plan_id: { type: 'string', description: 'breakdown_plan 返回的 plan_id' },
          tier: { type: 'string', enum: TIERS },
          items: { type: 'array', items: { type: 'string' } },
          vision_budget: { type: 'number' },
          allow_cloud: { type: 'boolean' },
          no_asr: { type: 'boolean' },
          no_vision: { type: 'boolean' },
        },
        required: ['source', 'plan_id'],
      },
      output: {
        schema: { type: 'object', additionalProperties: true },
        render(_args, value) {
          if (!value || !value.ok) return text(`起作业失败：${(value && value.error) || '未知原因'}`);
          return text(`已起作业 ${value.job_id}\n输出：${value.out_dir}\n用 breakdown_status 查进度。`);
        },
        presentationMeta(_args, value) { return value; },
      },
      async execute(args) {
        const plan = normalisePlan(args);
        if (!plan.source) return { ok: false, error: '需要 source' };
        const digest = planId(plan);
        const given = String((args && args.plan_id) || '').trim();
        if (given !== digest) {
          return jsonSafe({ ok: false, code: 'PLAN_MISMATCH',
            error: 'plan_id 与当前参数不一致（参数被改过，请重新 breakdown_plan）',
            expected: digest, given });
        }
        if (RUNNING.size >= Number(config.maxConcurrent || 2)) {
          return jsonSafe({ ok: false, code: 'BUSY', error: `已有 ${RUNNING.size} 个作业在跑，等一个结束` });
        }
        const { jobId, outDir } = await startJob(plan, digest);
        return jsonSafe({ ok: true, job_id: jobId, out_dir: outDir, plan_id: digest, status: 'running' });
      },
      isConcurrencySafe: () => false,
    },
    {
      name: 'breakdown_status',
      description: '查一个拆解作业的进度（阶段、耗时、是否已产出报告）。',
      parameters: {
        type: 'object',
        properties: { job_id: { type: 'string' } },
        required: ['job_id'],
      },
      output: {
        schema: { type: 'object', additionalProperties: true },
        render(_args, value) {
          if (!value || !value.ok) return text(`查不到：${(value && value.error) || ''}`);
          return text(`作业 ${value.job_id}：${value.status}` +
            (value.timing ? `\n耗时 ${value.timing.total_s}s` : '') +
            (value.unknowns !== undefined ? `\n未测项 ${value.unknowns}` : ''));
        },
        presentationMeta(_args, value) { return value; },
      },
      async execute(args) {
        const dir = jobDir(args && args.job_id);
        if (!dir) return { ok: false, error: 'job_id 不合法' };
        const job = readJson(join(dir, 'job.json'), null);
        if (!job) return { ok: false, error: '没有这个作业' };
        const report = readJson(join(dir, 'breakdown.json'), null);
        const running = RUNNING.has(job.job_id);
        return jsonSafe({
          ok: true,
          job_id: job.job_id,
          status: running ? 'running' : job.status,
          out_dir: dir,
          started_at: job.started_at,
          ended_at: job.ended_at,
          timing: report && report.timing,
          ok_report: report ? report.ok : null,
          unknowns: report ? (report.unknowns || []).length : null,
          log_tail: running ? tail(join(dir, 'job.log'), 1200) : undefined,
        });
      },
      isConcurrencySafe: () => true,
    },
    {
      name: 'breakdown_report',
      description: '读一份拆解报告：整份 Markdown、或某一个段（shots/transcript/visual/editing/audio）。',
      parameters: {
        type: 'object',
        properties: {
          job_id: { type: 'string' },
          section: { type: 'string', description: '缺省返回摘要；给名字返回该段 JSON' },
          markdown: { type: 'boolean', description: 'true=返回 report.md 全文' },
          transcript_limit: { type: 'number' },
        },
        required: ['job_id'],
      },
      output: {
        schema: { type: 'object', additionalProperties: true },
        render(_args, value) {
          if (!value || !value.ok) return text(`读不到：${(value && value.error) || ''}`);
          if (value.markdown) return text(value.markdown);
          return text(JSON.stringify(value.section !== undefined ? value.section : value.summary, null, 2));
        },
        presentationMeta(_args, value) { return value; },
      },
      async execute(args) {
        const dir = jobDir(args && args.job_id);
        if (!dir) return { ok: false, error: 'job_id 不合法' };
        const path = join(dir, 'breakdown.json');
        if (!existsSync(path)) return { ok: false, error: '作业还没有产出报告' };
        const payload = readJson(path, null);
        if (!payload) return { ok: false, error: '报告不是合法 JSON' };
        if (args && args.markdown) {
          const markdown = readFileSync(join(dir, 'report.md'), 'utf8');
          return jsonSafe({ ok: true, markdown });
        }
        if (args && args.section) {
          const section = payload[String(args.section)];
          if (section === undefined) return jsonSafe({ ok: false, error: '没有这一段' });
          return jsonSafe({ ok: true, section });
        }
        return jsonSafe({
          ok: true,
          summary: {
            ok: payload.ok,
            missing_required: payload.missing_required || [],
            tier: payload.tier,
            shots: (payload.shots || []).length,
            unknowns: (payload.unknowns || []).length,
            transcript_segments: (payload.transcript && payload.transcript.entries || []).length,
            timing: payload.timing,
          },
          // 面板要的摘要字段：结论层、口播状态、状态计数、未测项明细、镜头表
          conclusion: payload.conclusion || null,
          narration: payload.narration || null,
          counts: payload.summary || {},
          abstract: payload.abstract || null,
          unknowns_list: (payload.unknowns || []).slice(0, 60),
          shots: (payload.shots || []).slice(0, 200),
          tier: payload.tier,
          // 缺项要带原因：只给字段名用户看不懂"为什么没做"
          requested_items: payload.requested_items || null,
          missing_reasons: (payload.missing_required || []).map((field) => ({
            field,
            reason: (payload[field] && payload[field].reason) || '没有产出',
            status: (payload[field] && payload[field].status) || 'MISSING',
          })),
        });
      },
      isConcurrencySafe: () => true,
    },
    {
      name: 'breakdown_frames',
      description: '取作业抽出来的帧：返回文件路径（标准模式还会把前几张作为图片返回，便于直接看构图）。',
      parameters: {
        type: 'object',
        properties: {
          job_id: { type: 'string' },
          limit: { type: 'number', description: '最多几张，缺省 6' },
          images: { type: 'boolean', description: '是否把图作为图片块返回（缺省 true）' },
        },
        required: ['job_id'],
      },
      output: {
        schema: { type: 'object', additionalProperties: true },
        render(_args, value) {
          if (!value || !value.ok) return text(`取帧失败：${(value && value.error) || ''}`);
          return text(`共 ${value.count} 帧：\n` + (value.frames || []).map((f) => `- ${f.shot_id} @${f.t} ${f.path}`).join('\n'));
        },
        presentationMeta(_args, value) { return value; },
      },
      async execute(args, exec) {
        const dir = jobDir(args && args.job_id);
        if (!dir) return { ok: false, error: 'job_id 不合法' };
        const report = readJson(join(dir, 'breakdown.json'), null);
        // 没有报告 ≠ "0 帧"。前者是失败，后者是事实——不许把失败伪装成空结果。
        if (!report) return { ok: false, error: '作业还没有产出报告（可能仍在跑或已失败）' };
        const framesItem = report.frames || {};
        if (framesItem.status !== 'MEASURED') {
          return jsonSafe({ ok: false, error: framesItem.reason || '这份报告里没有可用帧',
            status: framesItem.status || 'UNKNOWN' });
        }
        const frames = (framesItem.frames || []).slice(0, Math.max(1, Math.min(50, Number((args && args.limit) || 6))));
        const blocks = (args && args.images === false) ? null : await imageBlocks(ctx, frames.map((f) => f.path));
        const value = jsonSafe({ ok: true, count: (framesItem.frames || []).length, frames, dir: framesItem.dir });
        if (blocks && exec && typeof exec.render === 'function') {
          return { ...value, __images: blocks };
        }
        return value;
      },
      isConcurrencySafe: () => true,
    },
    {
      name: 'breakdown_probe',
      description: '自检：外部工具（ffmpeg/ffprobe/whisper/yt-dlp）、可选依赖、视觉 provider 是否真的看得见图。只读。',
      parameters: {
        type: 'object',
        properties: {
          vision: { type: 'boolean', description: '是否顺带验证视觉 provider（会消耗少量额度）' },
          allow_cloud: { type: 'boolean' },
        },
      },
      output: {
        schema: { type: 'object', additionalProperties: true },
        render(_args, value) {
          if (!value || !value.tools) return text(`自检失败：${(value && value.error) || '没有返回自检结果'}`);
          const tools = value.tools || {};
          const missing = Object.entries(tools)
            .filter(([, item]) => item && item.ok === false)
            .map(([name, item]) => `${name}（${item.hint || '缺失'}）`);
          const extras = (value.extras || [])
            .filter((item) => item && item.ok === false)
            .map((item) => `${item.name}（${item.hint || '缺失'}）`);
          return text(`工具：\n` + Object.entries(tools)
            .map(([key, item]) => {
              if (!item || typeof item !== 'object') return `- ${key}: ${item}`;
              return `- ${key}: ${item.ok ? 'OK' : '缺 ' + (item.hint || '')}`;
            })
            .join('\n')
            + (extras.length ? `\n可选依赖未装：${extras.join('、')}` : '')
            + (missing.length ? `\n必需项缺失：${missing.join('、')}` : '')
            + (value.vision ? `\n视觉：${(value.vision.usable || []).join(', ') || '无可用 provider'}` : ''));
        },
        presentationMeta(_args, value) { return value; },
      },
      async execute(args) {
        const result = await runCli(['probe'], { timeoutMs: 180000 });
        const payload = { ok: Boolean(result.ok), tools: (result.json && result.json.tools) || {},
          extras: (result.json && result.json.extras) || [] };
        if (args && args.vision) {
          const visionArgs = ['vision-probe', '--quick'];
          if (args.allow_cloud) visionArgs.push('--allow-cloud');
          const visionResult = await runCli(visionArgs, { timeoutMs: 300000 });
          payload.vision = visionResult.json;
        }
        return jsonSafe(payload);
      },
      isConcurrencySafe: () => true,
    },
  ];
}

/** 视觉外发披露：把"会把帧发给谁"写进计划里（评审要求的显式披露）。 */
async function visionDisclosure() {
  const chain = [];
  if (process.env.AMDGPU_API_KEY
    || (credentialResolver && await credentialResolver('AMDGPU_API_KEY'))) {
    chain.push('amdgpu（免费额度，图片会外发到 developer.amd.com.cn）');
  }
  if (process.env.BREAKDOWN_LOCAL_VLM_BASE) chain.push('local vlm（本机，不外发）');
  if (process.env.BREAKDOWN_OCP_CMD) chain.push('opencode CLI（本机 CLI）');
  if (process.env.BREAKDOWN_WB_CMD) chain.push('workbuddy CLI（本机 CLI）');
  if (!chain.length) chain.push('无（视觉段会标 SKIPPED）');
  return chain;
}

// ---------------------------------------------------------------------------
// 路由
// ---------------------------------------------------------------------------

function registerRoutes(ctx, config) {
  /**
   * 注册一条路由。
   *
   * **签名是对象**：`ctx.webServer.register({ kind: 'exact', path, handler })`。
   * 教训（2026-11-23）：最初写成 `register(path, handler)`，结果宿主报
   * `webserver: duplicate undefined route "undefined"` —— 路径被当成 `undefined`，
   * 整行激活失败、工具一个都没注册。这种错误 `node --check` 抓不到，必须在
   * `test-load.mjs` 里断言注册参数形状。
   */
  const route = (path, methods, handler, { write = false } = {}) => async (req, res) => {
    if (!sameOrigin(req)) return sendJson(res, 403, { ok: false, error: '跨源请求被拒绝' });
    if (write && !needActionHeader(req)) {
      return sendJson(res, 403, { ok: false, error: '缺少 x-video-breakdown-action: 1' });
    }
    if (methods.length && !methods.includes(String(req.method || '').toUpperCase())) {
      return sendJson(res, 405, { ok: false, error: 'method-not-allowed' },
        { allow: methods.join(', ') });
    }
    try {
      await handler(req, res);
    } catch (error) {
      sendJson(res, 500, { ok: false, error: String((error && error.message) || error) });
    }
  };

  const mount = (path, methods, handler, options) => ctx.effect(
    () => ctx.webServer.register({ kind: 'exact', path, handler: route(path, methods, handler, options) }),
    'video-breakdown: ' + path,
  );

  mount(ROUTES.capabilities, ['GET'], async (req, res) => {
    sendJson(res, 200, {
      ok: true,
      plugin: name,
      routes: Object.values(ROUTES),
      tools: TOOLS.map((tool) => tool.name),
      tiers: TIERS,
      items: DEFAULT_ITEMS,
      data_root: DATA_ROOT,
      python: resolvePython(),
      vision_chain: await visionDisclosure(),
    });
  });

  mount(ROUTES.jobs, ['GET'], async (req, res) => {
    const url = new URL(req.url, 'http://localhost');
    const limit = Math.max(1, Math.min(100, Number(url.searchParams.get('limit') || 30)));
    // 薄适配：真源仍是 CLI 的 `list`，这里不自己扫目录/拼字段
    const result = await runCli(['list', '--out', DATA_ROOT, '--limit', String(limit)],
      { timeoutMs: 60000 });
    if (!result.json) {
      return sendJson(res, 500, { ok: false, error: result.error || 'list 失败' });
    }
    sendJson(res, 200, result.json);
  });

  mount(ROUTES.plan, ['POST'], async (req, res) => {
    const body = await readBody(req);
    if (body === null) return sendJson(res, 400, { ok: false, error: '请求体不是合法 JSON' });
    const tool = toolDefinitions(ctx, config).find((item) => item.name === 'breakdown_plan');
    sendJson(res, 200, await tool.execute(body));
  }, { write: true });

  mount(ROUTES.run, ['POST'], async (req, res) => {
    const body = await readBody(req);
    if (body === null) return sendJson(res, 400, { ok: false, error: '请求体不是合法 JSON' });
    const tool = toolDefinitions(ctx, config).find((item) => item.name === 'breakdown_run');
    const result = await tool.execute(body);
    sendJson(res, result && result.ok ? 200 : 409, result);
  }, { write: true });

  mount(ROUTES.status, ['GET'], async (req, res) => {
    const url = new URL(req.url, 'http://localhost');
    const tool = toolDefinitions(ctx, config).find((item) => item.name === 'breakdown_status');
    sendJson(res, 200, await tool.execute({ job_id: url.searchParams.get('job_id') || '' }));
  });

  mount(ROUTES.report, ['GET'], async (req, res) => {
    const url = new URL(req.url, 'http://localhost');
    const tool = toolDefinitions(ctx, config).find((item) => item.name === 'breakdown_report');
    sendJson(res, 200, await tool.execute({
      job_id: url.searchParams.get('job_id') || '',
      section: url.searchParams.get('section') || '',
      markdown: url.searchParams.get('markdown') === '1',
    }));
  });

  mount(ROUTES.frame, ['GET'], async (req, res) => {
    const url = new URL(req.url, 'http://localhost');
    const dir = jobDir(url.searchParams.get('job_id') || '');
    const file = String(url.searchParams.get('file') || '');
    if (!dir || !/^[A-Za-z0-9._-]+\.(jpg|jpeg|png)$/i.test(file)) {
      return sendJson(res, 400, { ok: false, error: '参数不合法' });
    }
    const target = resolve(dir, 'frames', file);
    if (!target.startsWith(resolve(dir, 'frames') + sep) || !existsSync(target)) {
      return sendJson(res, 404, { ok: false, error: '帧不存在' });
    }
    const bytes = readFileSync(target);
    res.writeHead(200, {
      'content-type': extname(target).toLowerCase() === '.png' ? 'image/png' : 'image/jpeg',
      'cache-control': 'no-store',
      'content-length': bytes.length,
    });
    res.end(bytes);
  });
}

// ---------------------------------------------------------------------------
// 插件入口
// ---------------------------------------------------------------------------

/**
 * 刻意**不导出 `Config`**。
 *
 * 教训（2026-11-23）：最初写了 `export const Config = { schema: {...} }`，那不是
 * Cordis 认识的 schema（本机其它插件用的是 `Schema?.object({...})`，且 Schema 要从
 * harness 源码树动态 import）。结果整行**静默激活失败**：宿主日志没有本插件的错误行，
 * 路由落到鉴权兜底返回 401、工具一个都没注册——排查了大半天。
 *
 * 没有 `Config` 时 loader 不做 schema 校验，`rawConfig` 就是行里的 `config`（缺省 {}）。
 * 需要配置项时再按 `Schema?.object` 的写法加回来，并接受"取不到 Schema 时为 undefined"。
 */
export function apply(ctx, rawConfig = {}) {
  const config = rawConfig && typeof rawConfig === 'object' ? rawConfig : {};
  if (config.dataRoot) DATA_ROOT = resolve(String(config.dataRoot));

  credentialResolver = async (credName) => {
    let credentials;
    try {
      credentials = ctx.get('credentials');
    } catch {
      return undefined;  // 未注入 credentials 服务时 ctx.get 会抛，不是错误
    }
    if (!credentials || typeof credentials.resolve !== 'function') return undefined;
    try {
      // `resolve()` 是异步的，而且**可能返回字符串，也可能返回 `{ value: string }`**
      // （宿主生态里其它插件的同款处理）。只认字符串会让"key 明明在"变成
      // "子进程拿不到 key"——表现为计划里有 amdgpu、跑完却 SKIPPED（2026-11-23 踩过）。
      const resolved = await credentials.resolve(credName);
      if (typeof resolved === 'string') return resolved;
      if (resolved && typeof resolved === 'object' && typeof resolved.value === 'string') {
        return resolved.value;
      }
      return undefined;
    } catch {
      return undefined;
    }
  };

  if (ctx.tools && typeof ctx.tools.register === 'function') {
    for (const tool of toolDefinitions(ctx, config)) {
      ctx.effect(() => ctx.tools.register(tool), `video-breakdown: tool ${tool.name}`);
    }
  }
  // HTTP 路由是面板用的，不该绑死插件可用性。cordis 的规矩是：
  // **没写进 inject 的服务，连 `ctx.get(name)` / 属性访问都会抛**
  //（`cannot get property "webServer" without inject`）——
  // 所以这里唯一正确的姿势是 `ctx.inject(['webServer'], cb)`：有就回调，没有就永不回调。
  let mounted = false;
  if (typeof ctx.inject === 'function') {
    try {
      ctx.inject(['webServer'], (scoped) => {
        if (mounted) return;
        mounted = true;
        registerRoutes(scoped || ctx, config);
      });
    } catch (error) {
      console.info('[video-breakdown] 挂载 webServer 失败：' + ((error && error.message) || error));
    }
  }
  if (!mounted) {
    // 已经同步就绪时（部分测试桩/老版本）兜底直连；拿不到就算了，工具已经注册好了
    try {
      if (ctx.webServer && typeof ctx.webServer.register === 'function') {
        mounted = true;
        registerRoutes(ctx, config);
      }
    } catch {
      /* 未注入：正常，跳过 */
    }
  }
  if (!mounted) console.info('[video-breakdown] 没有 webServer，跳过 HTTP 路由（工具照常可用）');
}

/** 测试用：不走 DSH 也能核对的纯函数。 */
export const __internal = {
  ROUTES, resolvePython, safeJobId, jobDir, jsonSafe, planId, normalisePlan, estimate,
  sameOrigin, needActionHeader, runCli, TOOLS, DEFAULT_ITEMS, RUNNING,
};
