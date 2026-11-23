---
name: reelens
description: "Use when a task involves understanding, summarizing or analysing a video or audio file — pulling the transcript out of a talk or podcast, listing shot cuts and keyframes, judging editing rhythm, reading on-screen text, or producing a structured breakdown report. Handles local files and URLs (Bilibili/YouTube etc.), reports every field with a trust status, and can run fully offline except for optional frame description. Triggers: 拆解视频, 提取字幕, 视频分镜, 视频总结, 播客转文字, transcript, shot list, keyframes, video breakdown."
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

```
breakdown_plan({ source, items, tier })      → what will be done, cost, vision disclosure, plan_id
breakdown_run({ source, plan_id })           → background job, returns job_id
breakdown_status({ job_id })                 → stage + elapsed
breakdown_report({ job_id, section? })       → conclusion / narration / shots / unknowns
breakdown_frames({ job_id, limit })          → keyframe paths (images back to you)
breakdown_probe({ vision })                  → what this machine can actually do
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
| Full breakdown | leave default (12 items) |
| Cheap quick look | `probe` first, then `transcript` |

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

## Vision, privacy and cost

Frame description is the only step that leaves the machine, and it is opt-in. `breakdown_plan`
shows which provider will receive the frames; `--no-vision` removes the step entirely (those
sections become `SKIPPED`, never silently absent). Domestic URLs and long videos are the main
cost drivers — prefer `--items transcript` when the question is only about what was said.

## Reporting the result

Quote the JSON, not your memory of it: `conclusion.one_line`, `source`, `shots.count`,
`narration.state`, `unknowns[]`. When a section is `DERIVED` or `ASSUMED`, pass the caveat on
to the user in the same sentence; that is the whole point of the tool.
