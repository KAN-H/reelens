---
name: reelens
description: "把视频/播客拆成带可信度状态的结构化报告：镜头切点、关键帧、字幕全文、画面文字、节奏、音频与文案结构；支持本地文件与 B 站/YouTube 等链接，除可选的画面描述外全程离线。This skill should be used when a task involves understanding, summarizing or analysing a video or audio file — extracting a transcript from a talk or podcast, listing shot cuts and keyframes, judging editing rhythm, reading on-screen text, or producing a structured breakdown report. Triggers: 拆解视频, 提取字幕, 视频分镜, 视频总结, 播客转文字, transcript, shot list, keyframes, video breakdown."
license: Apache-2.0
metadata:
  version: "0.1.1"
  repository: "https://github.com/KAN-H/reelens"
---

# reelens — turn one video into a structured, honest breakdown report

`reelens` produces **one JSON + one Markdown report** per video/podcast, covering shots,
keyframes, transcript, on-screen text, editing rhythm, audio and copy structure. Its
distinguishing rule: **every field carries a status describing how far you may trust it.**

## Status vocabulary (read this before quoting any number)

| Status | Meaning | May be quoted as fact? |
|---|---|---|
| `MEASURED` | measured directly, method calibrated | yes |
| `DERIVED` | proxy computed from measurements | as a proxy, say so |
| `ASSUMED` | inference | no — reference only |
| `UNKNOWN` / `SKIPPED` / `FAILED` | not measured, with a reason | no |

Anything non-conclusive is also listed in `unknowns[]`. If a section you requested is
missing, `ok` is `false` and the CLI exits non-zero — **never paper over that**; report the
gap instead of inventing the section.

## Two ways in

### 1. Inside DeepSeek Harness (plugin)

Six tools; the workflow is plan → run → report:

```text
breakdown_plan({ source, items, tier })      → 清单 + 预估耗时(total_s) + 视觉外发披露 + plan_id
breakdown_run({ source, plan_id })           → 后台作业，返回 job_id
breakdown_status({ job_id })                 → 阶段 + 已耗时
breakdown_report({ job_id, section? })       → 结论 / 口播状态 / 镜头 / unknowns
breakdown_frames({ job_id, limit })          → 关键帧路径（也把图返回给你）
breakdown_probe({ vision })                  → 这台机器实际能干什么
```

`breakdown_plan` is mandatory before `breakdown_run`: it returns a `plan_id` bound to the
parameters, so the user confirms exactly what will run. Changing `items` or `tier` afterwards
means a new plan.

### 2. As a CLI (no harness needed)

```bash
pip install "reelens[asr,scenes,audio,images]"
reelens probe                                   # ffmpeg? whisper? vision? say what is missing
reelens run "https://www.bilibili.com/video/BV..." --out out/clip --tier standard
reelens run /path/to/podcast.mp3 --out out/pod --items transcript,summary
reelens report out/clip --markdown
```

`--items` genuinely gates the work: asking only for `transcript` skips shots, frames and the
vision model (a 45-minute audio then takes ~9 minutes instead of ~15).

## Picking `items`

| Goal | `items` |
|---|---|
| Just the text of a talk/podcast | `transcript` (+ `summary` if asked to summarise) |
| 二创 / editing reference | `shots,frames,editing,audio` |
| Full breakdown | 插件留默认（**11 项，不含 `summary`**）；CLI 的 `--items` 留空 = **全 12 项（含 `summary`）** |
| Cheap quick look | `probe` first, then `transcript` |

> `summary` 默认是关的（`lib/capabilities.js` 里 `default: false`），插件默认值不含它。
> 想让插件也出摘要，必须在 `items` 里显式带上 `summary`。

Add `summary` when the user asks "what is this about" — it is extractive (no model call), so
every sentence can be traced back to the transcript.

## Pitfalls this tool already knows about — repeat them to the user

- **Duration mismatch** → the download was partial, so "no speech" is false. Check
  `source.duration_mismatch` before concluding anything about audio.
- **`shot_detection_degenerate`** → static background with moving layers (MG / subtitle-driven
  video). Scene detection reports few cuts while the frame-difference signal shows many
  changes. "Few cuts" does **not** mean "slow editing" here.
- **Music-only video** → `narration: absent` and the transcript is `SKIPPED`; do not present a
  Whisper hallucination as speech.
- **Burned-in subtitles** are read by a vision model on sampled keyframes, not per-frame OCR,
  so coverage of a subtitle-driven video is partial — say so.
- **Transitions** are `UNKNOWN` on purpose; do not guess "cross-fade" from a cut list.

## When NOT to use

- 纯文本、代码、文档、issue 的分析 —— 这个仓库只处理**音视频**，别拿它当通用文本工具。
- 需要**逐帧 OCR 当法律证据**、或要求字幕 100% 覆盖率 —— 内置字幕走抽样关键帧，覆盖率有限，
  要精确请另行 OCR 并向用户说明。
- 直播流、无法下载或受保护的源 —— `reelens` 必须先把媒体落到本地磁盘。
- 机器上没有 `ffmpeg`/`ffprobe` 且不允许安装 —— 先跑 `probe` 把缺什么告诉用户，不要硬跑。

## Vision, privacy and cost

Frame description is the only step that leaves the machine, and it is opt-in. `breakdown_plan`
shows which provider will receive the frames; `--no-vision` removes the step entirely (those
sections become `SKIPPED`, never silently absent). Domestic URLs and long videos are the main
cost drivers — prefer `--items transcript` when the question is only about what was said.

## Reporting the result

Quote the JSON, not your memory of it: `conclusion.one_line`, `source`, `shots.count`,
`narration.state`, `unknowns[]`. When a section is `DERIVED` or `ASSUMED`, pass the caveat on
to the user in the same sentence; that is the whole point of the tool.
