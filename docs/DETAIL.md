# reelens · 视频/音频拆解

把一条视频或播客拆成**结构化报告**：镜头表、抽帧与构图、字幕/口播、画面文案、剪辑节奏、音频与 BGM。

## 它做什么

| 段 | 内容 | 怎么来的 |
|---|---|---|
| 源信息 | 分辨率/旋转/帧率/编码/音轨/内嵌字幕轨 | `ffprobe` |
| 镜头表 | 每个镜头的起止、时长、切点分、用光/抖动/细节代理 | **一次解码**三路输出：`scenedetect`（优先）或 ffmpeg `select=gt(scene)` + `signalstats` + `edgedetect` |
| 抽帧 | 每镜头 55% 处（可选首帧）JPEG + dHash 跨镜头去重 + 对照图 | `ffmpeg -ss` |
| 口播 | 逐句时间轴；词级时间戳（faster-whisper 时） | 内嵌字幕轨 → whisper.cpp → faster-whisper |
| 构图/画面文案 | 景别、构图法、主体占比、版式分区、字幕位置/样式、画面文字 | 视觉模型（见下"外发"） |
| 剪辑手法 | 每分钟切换数、镜长分布；转场类型**目前标 UNKNOWN** | 由镜头时长推算 |
| 音频 | 积分响度/真峰/LRA、静音段、BPM 与节拍对齐 | `loudnorm` / `silencedetect` / `librosa` |
| 配音特征 | 语速（字/秒）、段均时长、停顿占比 | 由口播时间轴推断，**标记为推断** |

## 两段式：先清单，后执行

1. `breakdown_plan`（面板「生成清单」）：只读本地探测与自检，**不下载、不调用模型**，
   返回 `plan_id`、档位预估耗时、以及"会把 N 帧发往哪个 provider"的披露。
2. `breakdown_run`（面板「开始拆解」）：必须带 `plan_id`；与当前参数重算的 sha 不一致
   直接拒绝（`PLAN_MISMATCH`），避免"确认的是 A、跑的是 B"。

## 视觉外发（默认要你知道）

画面理解需要把帧发给视觉模型。插件按以下顺序挑选，**先验证再看图**：

1. `amdgpu`（免费额度；配置 `AMDGPU_API_KEY` 才启用）→ 图片会发送到该服务
2. `opencode` / `workbuddy` CLI（配置 `BREAKDOWN_OCP_CMD` / `BREAKDOWN_WB_CMD` 才启用）
3. 本机 VLM（`BREAKDOWN_LOCAL_VLM_BASE` + `_MODEL`，不外发）
4. 云端视觉（**只有显式允许 `--allow-cloud`** 才进链，按次计费）

每个 provider 都要先过**四张已知答案图**的探针（位置 / 随机数字 / 颜色 / **纯白负样本**）。
没过探针的 provider 不会被使用；关掉视觉只会让该段变成 `SKIPPED`，报告其余部分照常产出。

## 未测项必须说出来

报告里每个可测项都带状态：`MEASURED`（直接测到且方法已标定）、`DERIVED`（由测量推算的
代理指标，未标定）、`ASSUMED`（模型/启发式推断）、`UNKNOWN` / `SKIPPED` / `FAILED`（带原因）。
所有非结论项会汇总进 `unknowns[]`；只要该测的没测到，`ok=false` 且退出码非零——
**宁可让调用方看见失败，也不产出一份"看起来完整"的报告**。

## 已知边界（实测）

- **B 站 CC 字幕**：`yt-dlp` 匿名只返回 `danmaku`；很多视频连登录态也没有 CC，
  所以口播默认走 ASR。要 CC 需要自己的登录态（本插件不内置 cookie 传递）。
- **转场类型**（叠化/淡入淡出）：ffmpeg 的单帧 scene 分数对它基本无感，
  PySceneDetect 的两个检测器也不做类型分类。因此该字段保持 `UNKNOWN`，不写"没有转场"。
- **ASR 幻觉**：音乐/低信噪比音频上 Whisper 会反复吐同一句。插件会算"独立句占比"与
  "时间覆盖率"，可疑时标 `quality=suspect`，不把可疑文本当正文喂给下游。
- **时长**：默认上限 60 分钟（`--max-seconds` 可调）。超过时只跳过转写，其余照常。
- **GPU**：默认全程 CPU（faster-whisper int8 实测约 7.5× 实时）。

## 配置（环境变量）

| 变量 | 作用 |
|---|---|
| `BREAKDOWN_PYTHON` | 指定 Python 解释器（缺省用插件目录下的 `.venv-breakdown`） |
| `REELENS_FFMPEG` / `REELENS_FFPROBE`（旧名 `BREAKDOWN_*` 仍认） | 指定 ffmpeg/ffprobe（缺省先查插件自带的 npm 二进制，再查 PATH） |
| `BREAKDOWN_WHISPER_CPP` / `BREAKDOWN_WHISPER_MODEL` | whisper.cpp 二进制与模型 |
| `BREAKDOWN_FW_MODEL` / `BREAKDOWN_FW_COMPUTE` / `BREAKDOWN_FW_THREADS` | faster-whisper 模型/量化/线程 |
| `AMDGPU_API_KEY` | 视觉链第一档（免费额度） |
| `BREAKDOWN_VISION_CHAIN` | 用 JSON 数组完全接管视觉链顺序 |
| `BREAKDOWN_HOME` | 配置与缓存根（默认用户配置目录，不可写时退到插件目录下 `.cache`） |

本机安装：

```powershell
# 幂等：安装 npm 自带的 ffmpeg/ffprobe、Python 环境与核心包，最后自检
pwsh -File .\scripts\setup.ps1
```
