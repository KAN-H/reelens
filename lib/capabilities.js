/**
 * 声明式能力清单（唯一真相源）：面板照它渲染，测试照它核对。
 *
 * 每条能力必须能映射到一个工具或一条路由——"能力清单里写了、实际没有"是本项目
 * 明确禁止的那类静默失败。
 */

export const TOOLS = [
  {
    name: 'breakdown_plan',
    kind: 'query',
    route: 'plan',
    summary: '拆解清单 + 预估 + 视觉外发披露（不下载、不花钱）',
    confirm: 'none',
  },
  {
    name: 'breakdown_run',
    kind: 'action',
    route: 'run',
    summary: '按已确认的 plan_id 起后台作业',
    confirm: 'plan_id',
  },
  {
    name: 'breakdown_status',
    kind: 'query',
    route: 'status',
    summary: '查作业进度与耗时',
    confirm: 'none',
  },
  {
    name: 'breakdown_report',
    kind: 'query',
    route: 'report',
    summary: '读报告（整份 Markdown 或某一段 JSON）',
    confirm: 'none',
  },
  {
    name: 'breakdown_frames',
    kind: 'query',
    route: 'frame',
    summary: '列出抽帧；标准模式下可把前几张作为图片返回',
    confirm: 'none',
  },
  {
    name: 'breakdown_probe',
    kind: 'query',
    route: 'capabilities',
    summary: '自检工具/依赖/视觉 provider',
    confirm: 'none',
  },
];

export const ITEMS = [
  { id: 'transcript', label: '字幕/口播', default: true, note: '内嵌字幕 → whisper.cpp → faster-whisper' },
  { id: 'shots', label: '镜头表', default: true, note: 'scenedetect 优先，ffmpeg 兜底' },
  { id: 'frames', label: '抽帧', default: true, note: '按镜头 55% 处抽 + dHash 去重' },
  { id: 'visual', label: '构图/景别', default: true, note: '视觉模型，需外发图片' },
  { id: 'subtitle_style', label: '字幕样式', default: true, note: '位置/描边/底色（同一次视觉调用）' },
  { id: 'on_screen_text', label: '画面文案', default: true, note: '花字/标题/角标（同一次视觉调用）' },
  { id: 'editing', label: '剪辑手法', default: true, note: '节奏统计；转场类型暂为 UNKNOWN' },
  { id: 'audio', label: '响度/静音', default: true, note: 'ffmpeg ebur128 / silencedetect' },
  { id: 'beat', label: 'BPM/卡点', default: true, note: '需要 librosa' },
  { id: 'voice', label: '配音特征', default: true, note: '由文本时长推断，标 inferred' },
  { id: 'copywriting', label: '文案结构', default: true, note: '钩子/论点/证据，逐字来自口播' },
  { id: 'summary', label: '全文摘要', default: false, note: '抽取式：分段时间窗取信息量最高的原句 + 关键词 + 关键数字（不调模型）' },
];

export function buildCapabilities({ routes = [], dataRoot = '', python = '' } = {}) {
  return {
    ok: true,
    plugin: 'video-breakdown',
    routes,
    tools: TOOLS,
    items: ITEMS,
    data_root: dataRoot,
    python,
    slots: ['main'],
  };
}
