/**
 * video-breakdown · 客户端 bundle（手写，不走构建）。
 *
 * 六条硬性契约（DSH client bundle contract）：
 *  1. 第一层只做 `window.__ModuleLoader__.load({ id, factory })`；
 *  2. id 必须逐字符等于 package.json 的 name；
 *  3. package.json 的 `exports["./client"]` 指向本文件，且 `dsh.client.platform === "web"`；
 *  4. `factory(require)` 必须 `return module.exports`，且导出对象带 `apply` / `name` / `inject`；
 *  5. 只能 `require('react')`，不许 import / process / 跨包 require；
 *  6. 组件必须**大写调用**（`h(Card, …)`），写成 `h('Card', …)` React 会当未知标签 → 整页白屏。
 *
 * 违规的后果不是"面板不显示"，而是**整棵前端插件树 boot 失败**（页面只剩
 * "Failed to load plugins"，宿主日志却完全正常）。改完必须跑
 * the DSH client-bundle checker that ships with your DeepSeek Harness install.
 *
 * 分层（见 plugin-ui playbook §1.1）：UI 是壳。所有判断都在 Host 工具 / Python CLI 里，
 * 这里只 `fetch` 本插件路由 + 展示。**没有**业务逻辑、子进程、文件 IO、SQL。
 */
window.__ModuleLoader__.load({
  id: 'reelens-dsh-plugin',
  factory(require) {
    const module = { exports: {} };
    const exports = module.exports;
    const React = require('react');
    const h = React.createElement;

    const BASE = '/api/video-breakdown';

    const ITEMS = [
      ['transcript', '字幕/口播', 'ASR 或内嵌字幕'],
      ['shots', '镜头表', '切点与画质'],
      ['frames', '抽帧', '关键帧 + 去重'],
      ['visual', '构图/景别', '视觉模型读帧'],
      ['subtitle_style', '字幕样式', '花字/位置'],
      ['on_screen_text', '画面文案', '烧录文字'],
      ['editing', '剪辑手法', '节奏/卡点'],
      ['audio', '响度/静音', 'loudnorm'],
      ['beat', 'BPM/卡点', 'librosa'],
      ['voice', '配音特征', '语速/停顿'],
      ['copywriting', '文案结构', '钩子/数据点'],
      ['summary', '全文摘要', '抽取式要点/关键词，不调模型'],
    ];

    const TIERS = [
      ['fast', '快', '少抽帧、不做 BPM'],
      ['standard', '标准', '推荐，含 BPM 与对照图'],
      ['fine', '精细', '每镜头 2 帧、视觉预算翻倍'],
    ];

    // ---------------------------------------------------------------------
    // 设计令牌。颜色全部走 CSS 变量，暗色/亮色各一套；其它地方**不写 raw hex**。
    // 优先用 DSH 的 --dsw-alias-*（带 fallback），保证与其它面板同族。
    // ---------------------------------------------------------------------
    const TOKENS = `
.vb-root {
  --vb-accent: #3f6fdb;
  --vb-accent-ink: #ffffff;
  --vb-ok: #1a7f4b;      --vb-ok-soft: rgba(26,127,75,.12);
  --vb-warn: #a86400;    --vb-warn-soft: rgba(168,100,0,.12);
  --vb-bad: #b42318;     --vb-bad-soft: rgba(180,35,24,.12);
  --vb-ink: #1b1f27;
  --vb-ink2: #5b6473;
  --vb-ink3: #8a93a3;
  --vb-line: rgba(16,24,40,.10);
  --vb-fill: rgba(16,24,40,.04);
  --vb-fill2: rgba(16,24,40,.07);
  --vb-surface: #ffffff;
  --vb-surface2: #f7f8fa;
  --vb-shadow: 0 10px 30px rgba(16,24,40,.10);
  --vb-mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  display: flex; height: 100%; min-height: 0; color: var(--dsw-alias-label-primary, var(--vb-ink));
  background: var(--dsw-alias-bg-page, transparent);
  font-size: 13px; line-height: 1.5;
}
@media (prefers-color-scheme: dark) {
  .vb-root {
    --vb-accent: #7ba4f7; --vb-accent-ink: #0b1020;
    --vb-ok: #5fd39b;    --vb-ok-soft: rgba(95,211,155,.14);
    --vb-warn: #f0b45e;  --vb-warn-soft: rgba(240,180,94,.14);
    --vb-bad: #ff8a7a;   --vb-bad-soft: rgba(255,138,122,.14);
    --vb-ink: #eef1f6; --vb-ink2: #a9b3c4; --vb-ink3: #7c8698;
    --vb-line: rgba(255,255,255,.10);
    --vb-fill: rgba(255,255,255,.05);
    --vb-fill2: rgba(255,255,255,.09);
    --vb-surface: #151a24; --vb-surface2: #1b2130;
    --vb-shadow: 0 14px 40px rgba(0,0,0,.45);
  }
}
.vb-root * { box-sizing: border-box; }
.vb-scroll { overflow: auto; overscroll-behavior: contain; }
.vb-scroll::-webkit-scrollbar { width: 10px; height: 10px; }
.vb-scroll::-webkit-scrollbar-thumb { background: var(--vb-fill2); border-radius: 8px; }
.vb-btn { font: inherit; cursor: pointer; border-radius: 10px; border: 1px solid var(--vb-line);
  background: var(--vb-fill); color: inherit; padding: 7px 13px; transition: background .16s, opacity .16s; }
.vb-btn:hover:not(:disabled) { background: var(--vb-fill2); }
.vb-btn:disabled { opacity: .45; cursor: not-allowed; }
.vb-btn-primary { background: var(--vb-accent); border-color: transparent; color: var(--vb-accent-ink); font-weight: 600; }
.vb-btn-primary:hover:not(:disabled) { background: var(--vb-accent); filter: brightness(1.06); }
.vb-chip { font: inherit; font-size: 12px; cursor: pointer; border-radius: 999px; padding: 4px 11px;
  border: 1px solid var(--vb-line); background: transparent; color: var(--vb-ink2); }
.vb-chip[data-on="1"] { background: var(--vb-fill2); color: var(--vb-ink); border-color: transparent; font-weight: 600; }
.vb-row { display: flex; gap: 9px; align-items: center; flex-wrap: wrap; }
.vb-item { transition: background .16s; }
.vb-item:hover { background: var(--vb-fill); }
.vb-item[data-active="1"] { background: var(--vb-fill2); }
.vb-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.vb-table th { text-align: left; font-weight: 600; color: var(--vb-ink3); padding: 6px 8px;
  position: sticky; top: 0; background: var(--vb-surface2); backdrop-filter: blur(8px); }
.vb-table td { padding: 5px 8px; border-top: 1px solid var(--vb-line); font-variant-numeric: tabular-nums; }
.vb-thumb { width: 100%; display: block; border-radius: 8px; background: var(--vb-fill2); aspect-ratio: 16/9; object-fit: cover; }
.vb-thumbbtn { padding: 0; border: 1px solid var(--vb-line); border-radius: 10px; overflow: hidden;
  background: var(--vb-surface2); cursor: pointer; text-align: left; color: inherit; font: inherit; }
.vb-thumbbtn:hover { border-color: var(--vb-accent); }
`;

    let tokensInjected = false;
    function ensureTokens() {
      if (tokensInjected || typeof document === 'undefined') return;
      tokensInjected = true;
      const style = document.createElement('style');
      style.setAttribute('data-plugin', 'video-breakdown');
      style.textContent = TOKENS;
      document.head.appendChild(style);
    }

    // ---------------------------------------------------------------------
    // 数据面：只 fetch 本插件路由
    // ---------------------------------------------------------------------
    function api(path, options) {
      return fetch(BASE + path, options)
        .then((response) => response.json().catch(() => ({ ok: false, error: 'HTTP ' + response.status })))
        .catch((error) => ({ ok: false, error: String((error && error.message) || error) }));
    }

    function postJson(path, body) {
      return api(path, {
        method: 'POST',
        headers: { 'content-type': 'application/json', 'x-video-breakdown-action': '1' },
        body: JSON.stringify(body || {}),
      });
    }

    // ---------------------------------------------------------------------
    // 小工具
    // ---------------------------------------------------------------------
    function fmtDuration(seconds) {
      if (!seconds && seconds !== 0) return '—';
      const total = Math.round(Number(seconds));
      const m = Math.floor(total / 60);
      const s = total % 60;
      return m ? m + '′' + String(s).padStart(2, '0') + '″' : s + '″';
    }

    function fmtRelative(iso) {
      if (!iso) return '';
      const then = new Date(iso).getTime();
      if (!then) return '';
      const diff = Math.max(0, Date.now() - then) / 1000;
      if (diff < 60) return '刚刚';
      if (diff < 3600) return Math.round(diff / 60) + ' 分钟前';
      if (diff < 86400) return Math.round(diff / 3600) + ' 小时前';
      return Math.round(diff / 86400) + ' 天前';
    }

    const STATUS_TONE = {
      done: 'ok', failed: 'bad', running: 'warn', queued: 'warn',
      MEASURED: 'ok', DERIVED: 'ok', ASSUMED: 'warn', UNKNOWN: 'warn',
      SKIPPED: 'muted', FAILED: 'bad',
    };

    function toneVars(tone) {
      if (tone === 'ok') return { color: 'var(--vb-ok)', background: 'var(--vb-ok-soft)' };
      if (tone === 'warn') return { color: 'var(--vb-warn)', background: 'var(--vb-warn-soft)' };
      if (tone === 'bad') return { color: 'var(--vb-bad)', background: 'var(--vb-bad-soft)' };
      if (tone === 'muted') return { color: 'var(--vb-ink3)', background: 'var(--vb-fill)' };
      return { color: 'var(--vb-ink2)', background: 'var(--vb-fill)' };
    }

    // ---------------------------------------------------------------------
    // 组件
    // ---------------------------------------------------------------------
    function Chip(props) {
      const style = Object.assign({
        display: 'inline-flex', alignItems: 'center', gap: 5, fontSize: 11.5, lineHeight: 1,
        padding: '4px 9px', borderRadius: 999, whiteSpace: 'nowrap',
      }, toneVars(props.tone || 'plain'), props.style || {});
      return h('span', { style: style, title: props.title || '' }, props.children);
    }

    function Btn(props) {
      const className = 'vb-btn' + (props.primary ? ' vb-btn-primary' : '');
      return h('button', {
        className: className, style: props.style, disabled: props.disabled,
        onClick: props.onClick, title: props.title || '',
      }, props.children);
    }

    function Card(props) {
      return h('section', {
        style: Object.assign({
          border: '1px solid var(--vb-line)', borderRadius: 16, background: 'var(--vb-surface)',
          padding: props.pad === false ? 0 : '13px 15px', minWidth: 0,
        }, props.style || {}),
      },
        props.title
          ? h('header', { style: { display: 'flex', alignItems: 'center', gap: 8, marginBottom: 9 } },
            h('h3', { style: { margin: 0, fontSize: 12.5, fontWeight: 650, letterSpacing: '.01em' } }, props.title),
            props.extra || null)
          : null,
        props.children);
    }

    function KV(props) {
      return h('div', { style: { display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0 } },
        h('span', { style: { fontSize: 11, color: 'var(--vb-ink3)' } }, props.k),
        h('span', { style: { fontSize: 12.5, fontWeight: 600, fontVariantNumeric: 'tabular-nums' } }, props.v));
    }

    function Empty(props) {
      return h('div', {
        style: {
          display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
          gap: 7, padding: '38px 18px', color: 'var(--vb-ink3)', textAlign: 'center',
        },
      },
        h('div', { style: { fontSize: 26, opacity: 0.5 } }, props.icon || '◍'),
        h('div', { style: { fontSize: 13, color: 'var(--vb-ink2)' } }, props.title || '还没有内容'),
        props.hint ? h('div', { style: { fontSize: 12 } }, props.hint) : null,
        props.action || null);
    }

    function ErrorCard(props) {
      return h('div', {
        style: {
          border: '1px solid var(--vb-bad)', background: 'var(--vb-bad-soft)', borderRadius: 12,
          padding: '10px 12px', color: 'var(--vb-bad)', fontSize: 12,
        },
      },
        h('div', { style: { fontWeight: 650, marginBottom: 3 } }, props.title || '出错了'),
        h('div', { style: { fontFamily: 'var(--vb-mono)', wordBreak: 'break-word' } }, String(props.error || '')),
        props.hint ? h('div', { style: { marginTop: 5, color: 'var(--vb-ink2)' } }, props.hint) : null);
    }

    /**
     * 视图级 ErrorBoundary：任何渲染异常都变成一张错误卡片，而不是整页白。
     *
     * 基类要容错：契约测试用的极简 React stub **可能没有 `React.Component`**，
     * 直接 `class X extends React.Component` 会在加载期抛
     * "Class extends value undefined"，把所有测试一起带崩（本插件实际踩过）。
     */
    const ReactBase = (React && React.Component) || class {
      constructor(props) { this.props = props; this.state = {}; }
      setState(patch) {
        this.state = Object.assign({}, this.state, typeof patch === 'function' ? patch(this.state) : patch);
      }
    };

    class Boundary extends ReactBase {
      constructor(props) {
        super(props);
        this.state = { error: null };
      }
      static getDerivedStateFromError(error) { return { error: error }; }
      componentDidCatch(error) { console.error('[video-breakdown] view crashed', error); }
      render() {
        if (this.state.error) {
          return h(ErrorCard, {
            title: '界面渲染出错（能力未受影响，可用工具/CLI 继续）',
            error: (this.state.error && this.state.error.message) || String(this.state.error),
          });
        }
        return this.props.children;
      }
    }

    // ---------------------------------------------------------------------
    // 主面板
    // ---------------------------------------------------------------------
    function Panel() {
      ensureTokens();

      const [caps, setCaps] = React.useState(null);
      const [jobs, setJobs] = React.useState([]);
      const [jobsError, setJobsError] = React.useState('');
      const [activeJob, setActiveJob] = React.useState('');
      const [source, setSource] = React.useState('');
      const [tier, setTier] = React.useState('standard');
      const [items, setItems] = React.useState(ITEMS.map((row) => row[0]));
      const [allowCloud, setAllowCloud] = React.useState(false);
      const [plan, setPlan] = React.useState(null);
      const [status, setStatus] = React.useState(null);
      const [report, setReport] = React.useState(null);
      const [frames, setFrames] = React.useState([]);
      const [transcript, setTranscript] = React.useState(null);
      const [copywriting, setCopywriting] = React.useState(null);
      const [detail, setDetail] = React.useState(null);
      const [busy, setBusy] = React.useState('');
      const [error, setError] = React.useState('');
      const [elapsed, setElapsed] = React.useState(0);

      const running = Boolean(status && status.status === 'running');

      // 能力与历史：进面板就拉一次
      React.useEffect(() => {
        let alive = true;
        api('/capabilities').then((value) => { if (alive && value && value.ok) setCaps(value); });
        refreshJobs();
        return () => { alive = false; };
      }, []);

      // 运行中每秒跳一次，证明"还活着"
      React.useEffect(() => {
        if (!running) return undefined;
        const timer = setInterval(() => setElapsed((n) => n + 1), 1000);
        return () => clearInterval(timer);
      }, [running]);

      function refreshJobs() {
        api('/jobs?limit=40').then((value) => {
          if (value && value.ok) { setJobs(value.jobs || []); setJobsError(''); }
          else setJobsError((value && value.error) || '读取历史失败');
        });
      }

      function toggleItem(id) {
        setItems((current) => (current.includes(id) ? current.filter((x) => x !== id) : current.concat([id])));
      }

      function payload() {
        return { source: source.trim(), tier: tier, items: items, allow_cloud: allowCloud };
      }

      async function buildPlan() {
        setBusy('plan'); setError(''); setPlan(null); setReport(null); setFrames([]); setStatus(null);
        const result = await postJson('/plan', payload());
        setBusy('');
        if (!result || !result.ok) { setError((result && result.error) || '生成清单失败'); return; }
        setPlan(result);
      }

      async function startRun() {
        if (!plan || !plan.plan_id) return;
        setBusy('run'); setError(''); setElapsed(0);
        const result = await postJson('/run', Object.assign(payload(), { plan_id: plan.plan_id }));
        setBusy('');
        if (!result || !result.ok) { setError((result && result.error) || '起作业失败'); return; }
        setStatus({ job_id: result.job_id, status: 'running' });
        poll(result.job_id);
      }

      async function poll(jobId) {
        const current = await api('/status?job_id=' + encodeURIComponent(jobId));
        setStatus(current);
        if (!current) return;
        if (current.status === 'running') { setTimeout(() => poll(jobId), 2000); return; }
        await loadJob(jobId);
        refreshJobs();
      }

      async function loadJob(jobId) {
        setActiveJob(jobId);
        setDetail(null);
        const summary = await api('/report?job_id=' + encodeURIComponent(jobId));
        setReport(summary && summary.ok ? summary : null);
        const frameDoc = await api('/report?job_id=' + encodeURIComponent(jobId) + '&section=frames');
        const section = (frameDoc && frameDoc.ok && frameDoc.section) || {};
        setFrames(Array.isArray(section.frames) ? section.frames : []);
        // 口播与文案结构：只勾"字幕"时这两段就是**全部产出**，必须在面板里能读
        const transcriptDoc = await api('/report?job_id=' + encodeURIComponent(jobId) + '&section=transcript');
        setTranscript((transcriptDoc && transcriptDoc.ok && transcriptDoc.section) || null);
        const copyDoc = await api('/report?job_id=' + encodeURIComponent(jobId) + '&section=copywriting');
        setCopywriting((copyDoc && copyDoc.ok && copyDoc.section) || null);
        const jobStatus = await api('/status?job_id=' + encodeURIComponent(jobId));
        setStatus(jobStatus);
      }

      const planInfo = plan && plan.ok ? plan : null;
      const reportInfo = report && report.ok ? report : null;
      const conclusion = (reportInfo && reportInfo.conclusion) || (status && status.conclusion) || null;

      // ---------------------------------------------------------------- 侧栏
      const aside = h('aside', {
        style: {
          flex: '0 0 264px', width: 264, minWidth: 0, borderRight: '1px solid var(--vb-line)',
          display: 'flex', flexDirection: 'column', background: 'var(--vb-surface2)',
        },
      },
        h('div', { style: { padding: '12px 12px 8px', display: 'flex', alignItems: 'center', gap: 8 } },
          h('div', { style: { fontWeight: 680, fontSize: 13 } }, '拆解记录'),
          h('span', { style: { flex: 1 } }),
          h(Btn, {
            style: { padding: '4px 9px', fontSize: 12 }, onClick: refreshJobs, title: '刷新',
          }, '↻')),

        h('div', { className: 'vb-scroll', style: { flex: 1, minHeight: 0, padding: '0 8px 8px' } },
          jobsError
            ? h(ErrorCard, { title: '读取历史失败', error: jobsError, hint: '点右上角 ↻ 重试' })
            : jobs.length
              ? jobs.map((job) => {
                const tone = STATUS_TONE[job.status] || 'plain';
                return h('button', {
                  key: job.job_id, className: 'vb-item', 'data-active': activeJob === job.job_id ? '1' : '0',
                  onClick: () => loadJob(job.job_id),
                  style: {
                    display: 'block', width: '100%', textAlign: 'left', padding: '9px 10px', marginBottom: 2,
                    border: 'none', background: 'transparent', borderRadius: 10, cursor: 'pointer',
                    color: 'inherit', font: 'inherit',
                  },
                },
                  h('div', {
                    style: {
                      fontSize: 12.5, fontWeight: 560, overflow: 'hidden', textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                    },
                  }, job.title || job.job_id),
                  h('div', { style: { display: 'flex', gap: 6, alignItems: 'center', marginTop: 4, flexWrap: 'wrap' } },
                    h(Chip, { tone: tone }, job.status || '?'),
                    job.duration_s ? h('span', { style: { fontSize: 11, color: 'var(--vb-ink3)' } }, fmtDuration(job.duration_s)) : null,
                    job.shots ? h('span', { style: { fontSize: 11, color: 'var(--vb-ink3)' } }, job.shots + ' 镜') : null,
                    h('span', { style: { flex: 1 } }),
                    h('span', { style: { fontSize: 11, color: 'var(--vb-ink3)' } }, fmtRelative(job.started_at))));
              })
              : h(Empty, { icon: '◎', title: '还没有拆解记录', hint: '在右边贴一个链接或本地路径' })),

        h('div', {
          style: {
            borderTop: '1px solid var(--vb-line)', padding: '10px 12px',
            display: 'flex', flexDirection: 'column', gap: 7,
          },
        },
          h('div', { style: { display: 'flex', gap: 6, flexWrap: 'wrap' } },
            caps
              ? h(Chip, { tone: 'ok' }, caps.tools.length + ' 个工具')
              : h(Chip, { tone: 'muted' }, '工具未就绪'),
            caps ? h(Chip, { tone: 'plain' }, caps.tiers.join(' / ')) : null),
          h('div', { style: { fontSize: 11, color: 'var(--vb-ink3)', lineHeight: 1.45 } },
            '清单确认制：plan_id 就是这次确认的凭据，改任何参数都要重新生成。')));

      // ---------------------------------------------------------------- 顶栏
      const tierSegment = h('div', {
        style: {
          display: 'inline-flex', padding: 2, gap: 2, borderRadius: 10,
          background: 'var(--vb-fill)', border: '1px solid var(--vb-line)',
        },
      }, TIERS.map((row) => h('button', {
        key: row[0], className: 'vb-chip', 'data-on': tier === row[0] ? '1' : '0',
        title: row[2], onClick: () => setTier(row[0]),
        style: { border: 'none', background: tier === row[0] ? 'var(--vb-surface)' : 'transparent', borderRadius: 8, padding: '5px 12px' },
      }, row[1])));

      const header = h('header', {
        style: {
          padding: '13px 16px 11px', borderBottom: '1px solid var(--vb-line)',
          display: 'flex', flexDirection: 'column', gap: 9, background: 'var(--vb-surface)',
        },
      },
        h('div', { style: { display: 'flex', gap: 9, alignItems: 'center' } },
          h('div', { style: { fontWeight: 700, fontSize: 15, letterSpacing: '-.01em' } }, '视频 / 音频拆解'),
          h('span', { style: { flex: 1 } }),
          tierSegment,
          h('label', {
            style: { display: 'inline-flex', gap: 5, alignItems: 'center', fontSize: 12, color: 'var(--vb-ink2)' },
            title: '允许把帧发往付费云端视觉模型',
          },
            h('input', { type: 'checkbox', checked: allowCloud, onChange: (e) => setAllowCloud(e.target.checked) }),
            '云端视觉')),

        h('div', { style: { display: 'flex', gap: 8 } },
          h('input', {
            value: source, onChange: (event) => setSource(event.target.value),
            onKeyDown: (event) => { if (event.key === 'Enter' && source.trim()) buildPlan(); },
            placeholder: 'B 站 / YouTube / 播客链接，或本地文件绝对路径',
            style: {
              flex: 1, minWidth: 0, padding: '9px 12px', borderRadius: 11, font: 'inherit',
              border: '1px solid var(--vb-line)', background: 'var(--vb-fill)', color: 'inherit',
            },
          }),
          h(Btn, {
            primary: true, disabled: busy === 'plan' || !source.trim(), onClick: buildPlan,
          }, busy === 'plan' ? '生成中…' : '生成清单'),
          h(Btn, {
            disabled: busy === 'run' || !planInfo, onClick: startRun,
            title: planInfo ? '按这份清单开始（会下载并调用模型）' : '先点「生成清单」',
          }, busy === 'run' ? '起作业…' : '开始拆解')),

        // 条目：一行 chips。**勾选含义必须一眼可见**——上一版只有底色差别，
        // 用户点一下"字幕"其实是把它**取消**了，却以为"点了就是要它"（2026-11-23 真实反馈）。
        // 所以每颗 chip 前面加 ✓ / —，右边再加"已选 N/11"与三个预设。
        h('div', { className: 'vb-row', style: { alignItems: 'center' } },
          h('span', { style: { fontSize: 11.5, color: 'var(--vb-ink3)', marginRight: 2 } },
            '要做哪些（点一下 = 取消 / 恢复）'),
          h('span', { style: { flex: 1 } }),
          h('span', { style: { fontSize: 11.5, color: 'var(--vb-ink2)' } },
            '已选 ' + items.length + '/' + ITEMS.length)),

        h('div', { className: 'vb-row' },
          ...ITEMS.map((row) => {
            const on = items.includes(row[0]);
            return h('button', {
              key: row[0], className: 'vb-chip', 'data-on': on ? '1' : '0',
              title: row[2] + (on ? '（已选，点一下取消）' : '（未选，点一下加上）'),
              onClick: () => toggleItem(row[0]),
              style: { opacity: on ? 1 : 0.55 },
            }, (on ? '✓ ' : '— ') + row[1]);
          })),

        h('div', { className: 'vb-row', style: { marginTop: 2 } },
          h('button', {
            className: 'vb-chip', 'data-on': '0', style: { fontSize: 11.5 },
            title: '只跑 ASR/字幕 + 确定性文案结构，不抽帧、不调视觉模型',
            onClick: () => setItems(['transcript', 'copywriting', 'summary']),
          }, '只要字幕'),
          h('button', {
            className: 'vb-chip', 'data-on': '0', style: { fontSize: 11.5 },
            title: '还原全量 11 项',
            onClick: () => setItems(ITEMS.map((row) => row[0])),
          }, '全量'),
          h('button', {
            className: 'vb-chip', 'data-on': '0', style: { fontSize: 11.5 },
            title: '不转写，只看画面（抽帧 + 视觉理解 + 剪辑节奏）',
            onClick: () => setItems(['shots', 'frames', 'visual', 'on_screen_text', 'editing']),
          }, '只看画面')),

        error ? h(ErrorCard, { title: '操作失败', error: error }) : null);

      // ---------------------------------------------------------------- 主体
      let body;
      if (running) {
        body = h('div', { style: { padding: 16, display: 'flex', flexDirection: 'column', gap: 12 } },
          h(Card, { title: '正在拆解' },
            h('div', { className: 'vb-row', style: { marginBottom: 8 } },
              h(Chip, { tone: 'warn' }, 'running'),
              h('span', { style: { fontSize: 12, color: 'var(--vb-ink2)' } }, '已 ' + elapsed + ' 秒，请勿关闭窗口'),
              h('span', { style: { flex: 1 } }),
              h('span', { style: { fontFamily: 'var(--vb-mono)', fontSize: 11, color: 'var(--vb-ink3)' } },
                (status && status.job_id) || '')),
            h('div', {
              style: {
                height: 4, borderRadius: 999, overflow: 'hidden', background: 'var(--vb-fill2)',
              },
            }, h('div', {
              style: {
                height: '100%', width: Math.min(96, 8 + elapsed * 1.6) + '%',
                background: 'var(--vb-accent)', transition: 'width .8s linear',
              },
            })),
            status && status.log_tail
              ? h('pre', {
                style: {
                  marginTop: 9, marginBottom: 0, padding: 9, borderRadius: 9, background: 'var(--vb-fill)',
                  fontFamily: 'var(--vb-mono)', fontSize: 11, color: 'var(--vb-ink2)',
                  maxHeight: 180, overflow: 'auto', whiteSpace: 'pre-wrap',
                },
              }, status.log_tail)
              : null));
      } else if (reportInfo) {
        const summary = reportInfo.summary || {};
        const counts = reportInfo.counts || summary || {};
        const shots = reportInfo.shots || [];
        const unknowns = reportInfo.unknowns_list || [];
        body = h('div', { style: { padding: 16, display: 'flex', flexDirection: 'column', gap: 12 } },
          (reportInfo.missing_reasons || []).length
            ? h(ErrorCard, {
              title: '这份报告不完整（作业被判失败）',
              error: reportInfo.missing_reasons
                .map((row) => row.field + ' [' + row.status + '] ' + row.reason).join('\n'),
              hint: '常见原因：片长超过 60 分钟会自动跳过转写；或该条目依赖的上一段没成。',
            })
            : null,
          conclusion
            ? h(Card, { title: '结论', extra: h(Chip, { tone: 'ok' }, conclusion.type || 'DERIVED') },
              h('div', { style: { fontSize: 13.5, lineHeight: 1.6 } }, conclusion.one_liner || conclusion),
              conclusion.signals
                ? h('div', {
                  style: {
                    marginTop: 8, fontFamily: 'var(--vb-mono)', fontSize: 11,
                    color: 'var(--vb-ink3)', lineHeight: 1.6,
                  },
                }, Object.keys(conclusion.signals).map((key) => key + '=' + conclusion.signals[key]).join('  '))
                : null)
            : null,

          h('div', {
            style: {
              display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(96px, 1fr))', gap: 9,
            },
          },
            h(Card, null, h(KV, { k: '镜头', v: shots.length || counts.shots || '—' })),
            h(Card, null, h(KV, { k: '抽帧', v: summary.frames || frames.length || '—' })),
            h(Card, null, h(KV, {
              k: '口播',
              v: reportInfo.narration === 'absent'
                ? '无人声'
                : (summary.transcript_segments !== undefined ? summary.transcript_segments + ' 段' : '—'),
            })),
            h(Card, null, h(KV, { k: '未测项', v: summary.unknowns !== undefined ? summary.unknowns : unknowns.length })),
            h(Card, null, h(KV, { k: '总耗时', v: (summary.timing && summary.timing.total_s) || '—' })),
            h(Card, null, h(KV, { k: '档位', v: reportInfo.tier || '—' }))),

          frames.length
            ? h(Card, {
              title: '关键帧', extra: h(Chip, { tone: 'plain' }, frames.length + ' 张 · 点开看细节'),
            },
              h('div', {
                style: {
                  display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(132px, 1fr))', gap: 8,
                },
              }, frames.slice(0, 40).map((frame) => h('button', {
                key: frame.path || frame.t, className: 'vb-thumbbtn',
                onClick: () => setDetail(frame),
                title: frame.shot_id + ' @' + frame.t + 's',
              },
                h('img', {
                  className: 'vb-thumb', loading: 'lazy',
                  src: BASE + '/frame?job_id=' + encodeURIComponent(activeJob) +
                    '&file=' + encodeURIComponent(String(frame.path || '').split(/[\\/]/).pop()),
                  alt: frame.shot_id,
                }),
                h('div', { style: { padding: '5px 7px', fontSize: 11, color: 'var(--vb-ink3)' } },
                  frame.shot_id + ' @' + frame.t + 's')))))
            : null,

          // 口播全文：只勾"字幕/口播"时这就是**全部产出**，必须能直接读
          transcript
            ? h(Card, {
              title: '口播 / 字幕',
              extra: h('div', { style: { display: 'flex', gap: 6, marginLeft: 8 } },
                h(Chip, { tone: STATUS_TONE[transcript.status] || 'muted' }, transcript.status || '?'),
                transcript.engine ? h(Chip, { tone: 'plain' }, transcript.engine) : null,
                transcript.lang ? h(Chip, { tone: 'plain' }, transcript.lang) : null,
                transcript.quality_report && transcript.quality_report.quality === 'suspect'
                  ? h(Chip, { tone: 'warn', title: '覆盖率过低或重复度过高，内容不可直接当正文' }, '疑似幻觉')
                  : null),
            },
              transcript.entries && transcript.entries.length
                ? h('div', { className: 'vb-scroll', style: { maxHeight: 320, display: 'flex', flexDirection: 'column', gap: 5 } },
                  transcript.entries.map((entry, index) => h('div', {
                    key: index, style: { display: 'flex', gap: 9, alignItems: 'baseline' },
                  },
                    h('span', {
                      style: {
                        flex: '0 0 92px', textAlign: 'right', fontFamily: 'var(--vb-mono)',
                        fontSize: 11, color: 'var(--vb-ink3)', fontVariantNumeric: 'tabular-nums',
                      },
                    }, entry.from + '–' + entry.to + 's'),
                    h('span', { style: { fontSize: 12.5, lineHeight: 1.6 } }, entry.content))))
                : h('div', { style: { fontSize: 12, color: 'var(--vb-ink2)', lineHeight: 1.6 } },
                  transcript.reason || '这一段没有内容'),
              transcript.attempts && transcript.attempts.length
                ? h('div', { style: { marginTop: 8, fontSize: 11, color: 'var(--vb-warn)' } },
                  '注：' + transcript.attempts.join('；'))
                : null)
            : null,

          copywriting && (copywriting.status === 'DERIVED' || copywriting.status === 'ASSUMED')
            ? h(Card, {
              title: '文案结构',
              extra: h(Chip, { tone: copywriting.status === 'ASSUMED' ? 'warn' : 'ok' }, copywriting.status),
            },
              h('div', { style: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(110px, 1fr))', gap: 9 } },
                h(KV, { k: '句数', v: copywriting.sentences }),
                h(KV, { k: '每分钟字数', v: copywriting.chars_per_min || '—' }),
                h(KV, { k: '问句占比', v: copywriting.question_ratio }),
                h(KV, { k: '分段', v: (copywriting.sections || []).length })),
              copywriting.hook
                ? h('div', {
                  style: {
                    marginTop: 9, padding: '8px 10px', borderRadius: 9, background: 'var(--vb-fill)',
                    fontSize: 12.5, lineHeight: 1.6,
                  },
                }, h('span', { style: { color: 'var(--vb-ink3)', marginRight: 6 } }, '前 12s 钩子：'), copywriting.hook)
                : null,
              (copywriting.enumerations || []).length || (copywriting.cta || []).length
                ? h('div', { className: 'vb-row', style: { marginTop: 8 } },
                  ...(copywriting.enumerations || []).slice(0, 8).map((word) => h(Chip, { key: 'e' + word, tone: 'plain' }, word)),
                  ...(copywriting.cta || []).map((word) => h(Chip, { key: 'c' + word, tone: 'warn' }, word)))
                : null,
              copywriting.status === 'ASSUMED'
                ? h('div', { style: { marginTop: 8, fontSize: 11.5, color: 'var(--vb-warn)' } }, '⚠️ ' + (copywriting.reason || ''))
                : null)
            : null,

          // 抽取式摘要：只在勾了 summary 时出现
          reportInfo.abstract && reportInfo.abstract.status === 'DERIVED'
            ? h(Card, {
              title: '摘要（抽取式，非模型总结）',
              extra: h(Chip, { tone: 'ok' }, (reportInfo.abstract.outline || []).length + ' 段要点'),
            },
              h('div', { style: { display: 'flex', flexDirection: 'column', gap: 6 } },
                (reportInfo.abstract.outline || []).map((row, index) => h('div', {
                  key: index, style: { display: 'flex', gap: 9, alignItems: 'baseline' },
                },
                  h('span', {
                    style: {
                      flex: '0 0 52px', textAlign: 'right', fontFamily: 'var(--vb-mono)',
                      fontSize: 11, color: 'var(--vb-ink3)',
                    },
                  }, row.from + 's'),
                  h('span', { style: { fontSize: 12.5, lineHeight: 1.6 } }, row.point)))),
              (reportInfo.abstract.keywords || []).length
                ? h('div', { className: 'vb-row', style: { marginTop: 9 } },
                  ...(reportInfo.abstract.keywords || []).map((item) => h(Chip, {
                    key: item.word, tone: 'plain',
                  }, item.word + '×' + item.count)))
                : null)
            : null,

          shots.length
            ? h(Card, { title: '镜头表', pad: false },
              h('div', { className: 'vb-scroll', style: { maxHeight: 280 } },
                h('table', { className: 'vb-table' },
                  h('thead', null, h('tr', null,
                    ['#', '起', '止', '时长', '切点', '用光', '裁决'].map((label) => h('th', { key: label }, label)))),
                  h('tbody', null, shots.map((shot) => h('tr', null,
                    h('td', null, shot.id),
                    h('td', null, shot.start),
                    h('td', null, shot.end),
                    h('td', null, shot.dur),
                    h('td', null, shot.cut_score),
                    h('td', null, shot.luma_mean),
                    h('td', null, h(Chip, { tone: shot.verdict === 'good' ? 'ok' : 'warn' }, shot.verdict))))))))
            : null,

          unknowns.length
            ? h(Card, { title: '未测 / 降级项' },
              h('div', { style: { display: 'flex', flexDirection: 'column', gap: 6 } },
                unknowns.map((item, index) => h('div', { key: index, style: { display: 'flex', gap: 8, alignItems: 'baseline' } },
                  h(Chip, { tone: STATUS_TONE[item.code] || 'muted' }, item.code),
                  h('span', { style: { fontSize: 12, color: 'var(--vb-ink2)' } }, item.field + ' — ' + (item.reason || ''))))))
            : null);
      } else if (planInfo) {
        body = h('div', { style: { padding: 16, display: 'flex', flexDirection: 'column', gap: 12 } },
          h(Card, { title: '拆解清单（还没开始）', extra: h(Chip, { tone: 'warn' }, '待确认') },
            h('div', {
              style: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(120px, 1fr))', gap: 10 },
            },
              h(KV, { k: '预计耗时', v: (planInfo.eta && planInfo.eta.total_s) + ' 秒' }),
              h(KV, { k: '档位', v: planInfo.plan.tier }),
              h(KV, { k: '条目', v: planInfo.plan.items.length + ' 项' }),
              h(KV, { k: '工具链', v: planInfo.probe_ok ? '自检通过' : '有缺失' })),
            planInfo.preview
              ? h('div', {
                style: {
                  marginTop: 10, padding: 10, borderRadius: 10, background: 'var(--vb-fill)',
                  fontSize: 12, color: 'var(--vb-ink2)', lineHeight: 1.65,
                },
              },
                h('div', { style: { fontWeight: 650, color: 'var(--vb-ink)' } }, planInfo.preview.title),
                [
                  planInfo.preview.size,
                  planInfo.preview.fps ? planInfo.preview.fps + 'fps' : '',
                  planInfo.preview.duration_s ? fmtDuration(planInfo.preview.duration_s) : '',
                  planInfo.preview.has_audio === false ? '无音轨' : '',
                ].filter(Boolean).join(' · '))
              : null,
            h('div', { style: { marginTop: 10, fontSize: 12, color: 'var(--vb-ink2)' } },
              '视觉外发：' + ((planInfo.vision_chain || []).join(' / ') || '无可用 provider')),
            h('div', { style: { marginTop: 4, fontSize: 11.5, color: 'var(--vb-ink3)' } },
              '片长超过 ' + Math.round((planInfo.plan.max_seconds || 3600) / 60) + ' 分钟会自动跳过转写（可在计划里调大）。'),
            h('div', { style: { marginTop: 4, fontFamily: 'var(--vb-mono)', fontSize: 11, color: 'var(--vb-ink3)' } },
              'plan_id ' + planInfo.plan_id)));
      } else {
        body = h(Empty, {
          icon: '▤', title: '贴一个链接或本地文件，先生成清单',
          hint: '清单会说清楚「拆什么、用什么档位、会不会把画面发到外部服务」。确认后才真正开始。',
        });
      }

      // ---------------------------------------------------------------- 详情抽屉
      const drawer = detail
        ? h('aside', {
          style: {
            flex: '0 0 340px', width: 340, borderLeft: '1px solid var(--vb-line)',
            background: 'var(--vb-surface2)', display: 'flex', flexDirection: 'column', minWidth: 0,
          },
        },
          h('div', {
            style: { padding: '11px 13px', borderBottom: '1px solid var(--vb-line)', display: 'flex', alignItems: 'center', gap: 8 },
          },
            h('div', { style: { fontWeight: 650, fontSize: 12.5 } }, detail.shot_id + ' @' + detail.t + 's'),
            h('span', { style: { flex: 1 } }),
            h(Btn, { style: { padding: '3px 8px', fontSize: 12 }, onClick: () => setDetail(null) }, '✕')),
          h('div', { className: 'vb-scroll', style: { flex: 1, minHeight: 0, padding: 13, display: 'flex', flexDirection: 'column', gap: 11 } },
            h('img', {
              src: BASE + '/frame?job_id=' + encodeURIComponent(activeJob) +
                '&file=' + encodeURIComponent(String(detail.path || '').split(/[\\/]/).pop()),
              style: { width: '100%', borderRadius: 10, background: 'var(--vb-fill2)' }, alt: detail.shot_id,
            }),
            Object.keys(detail).filter((key) => key !== 'path').map((key) => h('div', { key: key },
              h('div', { style: { fontSize: 11, color: 'var(--vb-ink3)' } }, key),
              h('div', {
                style: {
                  fontSize: 12, wordBreak: 'break-word',
                  fontFamily: Array.isArray(detail[key]) || typeof detail[key] === 'object' ? 'var(--vb-mono)' : 'inherit',
                },
              }, Array.isArray(detail[key]) ? detail[key].join(' / ') : String(detail[key]))))))
        : null;

      return h('div', { className: 'vb-root' },
        aside,
        h('main', { style: { flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column' } },
          header,
          h('div', { className: 'vb-scroll', style: { flex: 1, minHeight: 0 } },
            h(Boundary, null, body))),
        drawer);
    }

    /** 侧栏图标：点它 dispatch 的 key 必须与 main 面板的 key 一致。 */
    function SidebarIcon() {
      return h('svg', {
        viewBox: '0 0 24 24', width: 20, height: 20, fill: 'none', stroke: 'currentColor',
        'stroke-width': 1.7, 'stroke-linecap': 'round', 'stroke-linejoin': 'round',
      },
        h('rect', { x: 2.5, y: 5.5, width: 12, height: 9, rx: 2 }),
        h('path', { d: 'M2.5 8.5h12M2.5 11.5h12' }),
        h('circle', { cx: 18, cy: 9, r: 3.2 }),
        h('path', { d: 'M20.4 11.4l1.6 1.6' }),
        h('path', { d: 'M6 18.5h7' }));
    }

    function apply(ctx) {
      // Fail soft：两处注册各自 try/catch——任一处失败不该让整块面板消失
      try {
        ctx.slots.inject('main', function () {
          return ctx.slots.register({ name: 'main', key: 'video-breakdown' }, Panel);
        });
      } catch (error) {
        console.warn('[video-breakdown] main panel registration failed', error);
      }
      try {
        ctx.slots.inject('sidebar.panellist', function () {
          return ctx.slots.register(
            { name: 'sidebar.panellist', id: 'video-breakdown', order: 9, label: '拆解' },
            SidebarIcon,
          );
        });
      } catch (error) {
        console.warn('[video-breakdown] sidebar icon registration failed', error);
      }
    }

    exports.apply = apply;
    exports.name = 'video-breakdown';
    exports.inject = ['slots'];
    return module.exports;
  },
});