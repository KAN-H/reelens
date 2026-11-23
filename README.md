# reelens

Turn one video or podcast into a **structured breakdown report**: shot table, frames and
composition, transcript, on-screen text, editing rhythm, audio and BGM — with every field
labelled by how it was measured.

Two deliverables in one repo:

- `python/` — **`reelens-core`**, a dependency-free Python package + `reelens` CLI.
  Works standalone; no DSH required.
- the root package — a **DeepSeek Harness (DSH) plugin**: six chat tools plus a panel
  (`/api/video-breakdown/*` — the route namespace keeps the functional name). It is a thin shell over the CLI.

> Status: pre-1.0. The library and CLI are covered by tests; the DSH panel has been
> verified by contract tests but the rendered UI still needs a manual pass.

## What it measures

| Section | Content | How |
|---|---|---|
| Source | resolution, rotation, fps, codec, audio tracks, embedded subtitle tracks | `ffprobe` |
| Shots | per-shot start/end, cut score, exposure/shake/detail proxies | **one decode, three filter branches**: `scenedetect` (preferred) or ffmpeg `select=gt(scene)`, plus `signalstats` and `edgedetect` |
| Frames | JPEG at 55% of each shot (optionally first frame too), dHash de-duplication, contact sheet | `ffmpeg -ss` |
| Transcript | sentence timeline; word timestamps when faster-whisper is used | embedded subtitle track → whisper.cpp → faster-whisper |
| Visual | shot size, composition, subject ratio, layout zones, subtitle position/style, on-screen text | a vision model (see *Image egress*) |
| Editing | cuts per minute, shot-length distribution; **transition type stays `UNKNOWN`** | derived from shot durations |
| Audio | integrated loudness / true peak / LRA, silence segments, BPM and beat alignment | `loudnorm`, `silencedetect`, `librosa` |
| Voice | speaking rate, mean segment length, pause ratio | derived from the transcript, marked as inferred |
| Copy | hook (first 12 s verbatim), sentence count, question ratio, section split on pauses, CTAs, numeric data points | **deterministic** derivation from the transcript — no model call; data points must appear verbatim in the source and are dropped when vague ("about 300") or fewer than two |

## Install

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e python[all]                        # or drop [all] for a minimal install
```

`ffmpeg`/`ffprobe` must be on `PATH` (or point `BREAKDOWN_FFMPEG` / `BREAKDOWN_FFPROBE`
at them). Everything else is optional and degrades to `SKIPPED` with a reason.

Check what this machine can do:

```bash
reelens probe
reelens vision-probe            # four known-answer images, including a blank negative
```

## Use it from the CLI

```bash
reelens run "https://www.bilibili.com/video/BV..." --out out/bv --tier standard
reelens report out/bv --markdown
reelens frames out/bv
```

Tiers: `fast` (fewer frames, no beat analysis), `standard`, `fine` (more frames, both
first and mid frames per shot). `--no-asr` / `--no-vision` skip a section explicitly;
`--max-seconds` caps transcription length (default 60 min).

## Use it inside DSH

```bash
dsh plugin add <path-to-this-repo>     # or add the link: entry to the profile package.json
```

After the plugin row activates, **reload the page**: the Host half (tools and routes) is live
immediately, but the client bundle is loaded when the page boots, so the sidebar entry and
panel only appear after a refresh.

Then either ask the agent, or open the **Video breakdown** panel. The panel is a three-column
workspace: **job history** on the left (title, status, duration, shot count, relative time),
the **workspace** in the middle (plan → progress → report with a one-line conclusion, metric
cards, keyframe grid, shot table and an explicit "unknown / degraded" list), and a **frame
inspector** on the right that opens when you click a thumbnail. Every action maps to a route
and ultimately to a `breakdown` CLI command; the panel adds no business logic of its own.Then either ask the agent, or open the **Video breakdown** panel:

1. `breakdown_plan` — builds the checklist, an ETA, and the image-egress disclosure.
   Nothing is downloaded and no model is called.
2. `breakdown_run` — starts the job. It **requires the `plan_id`**; if any parameter
   changed, the run is rejected (`PLAN_MISMATCH`).
3. `breakdown_status` / `breakdown_report` / `breakdown_frames` — progress, report,
   frames (the panel shows them as images).

## Status semantics — "measured" is not "correct"

Every measurable field carries a status:

| Status | Meaning | Usable as a conclusion |
|---|---|---|
| `MEASURED` | directly measured, method established | yes |
| `DERIVED` | proxy computed from measurements (image quality, rhythm), *not calibrated* | yes, labelled as a proxy |
| `ASSUMED` | model or heuristic inference (e.g. voice emotion) | reference only |
| `UNKNOWN` / `SKIPPED` / `FAILED` | not measured / not requested / failed — always with a reason | no |

All non-conclusive fields are collected into `unknowns[]`; if a required section is
missing, `ok` is `false` and the CLI exits non-zero. The tool would rather tell you it
does not know than hand you a report that *looks* complete.

## Image egress

Frame understanding sends images somewhere. The chain is tried in order, and **every
provider must pass a probe first**:

1. `AMDGPU_API_KEY` — free tier of an OpenAI-compatible vision gateway (images leave the machine)
2. `BREAKDOWN_OCP_CMD` / `BREAKDOWN_WB_CMD` — a local agent CLI, if you configure one
3. `BREAKDOWN_LOCAL_VLM_BASE` + `BREAKDOWN_LOCAL_VLM_MODEL` — a local OpenAI-compatible VLM
4. cloud vision — **only** with `--allow-cloud`, since it is billed and off-machine

The probe sends four known-answer images (position, a random number, colour, and a blank
**negative** that must answer `NONE`). A provider that does not pass is never used.
Turn vision off entirely with `--no-vision`; that section becomes `SKIPPED` and the rest
of the report is unaffected.

## Known limitations (measured, not guessed)

- **Bilibili CC subtitles**: anonymous `yt-dlp` returns only `danmaku`, and many videos
  have no CC even when logged in — so transcription defaults to ASR.
- **Transition type** (dissolve/fade): ffmpeg's single-frame scene score barely reacts to
  gradual changes, and PySceneDetect's detectors classify boundaries but not transition
  types. The field stays `UNKNOWN` instead of claiming "no transitions".
- **ASR hallucination**: on music-heavy audio Whisper repeats one sentence. We compute
  unique-sentence ratio and time coverage and mark `quality=suspect` instead of feeding it
  downstream.
- **Shot boundary sensitivity**: ffmpeg's default `gt(scene,0.30)` under-detected roughly
  half of the cuts on a real 171 s clip (7 vs 11 with `ContentDetector`), which is why
  scenedetect is preferred when installed.
- CPU only by default: faster-whisper int8 measured ≈7.5× realtime on a 24-thread CPU.

## Layout

```
index.js client.js lib/ locale/ docs/   DSH plugin (thin shell + panel)
python/src/reelens/                   core package: probe, shots, frames, asr, audio, vision, report
python/tests/                           stdlib-only tests (no network, no ffmpeg)
scripts/gates/                          the measurement gates that justified the design
test-host.mjs test-client.mjs test-load.mjs
```

`scripts/gates/` is unusual and deliberate: each file documents a decision that was made
*after* measuring, for example that gradual transitions cannot be detected reliably with
these tools, or that a provider probe must include a blank negative image.

## License

Apache-2.0. See `LICENSE`, `THIRD_PARTY_NOTICES.md`, and `PROVENANCE.md`.