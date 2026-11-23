# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] — 2026-11-23

First public release. Two deliverables in one repository: the `reelens-core` Python
package (CLI `reelens`, no required dependencies) and the DeepSeek Harness plugin that
exposes it as six chat tools plus a panel.

### Added

- **Shots**: single-decode analysis (scene score + luminance + edge density in one ffmpeg
  pass), adaptive cut detection from the per-frame scene score, per-shot exposure / shake /
  detail proxies, and a frame-difference rhythm signal that keeps working on static-background
  videos where scene detection degenerates.
- **Frames**: per-shot extraction with dHash de-duplication, a minimum frame budget for
  videos with very few cuts, and a PIL-built contact sheet.
- **Transcript**: embedded subtitles → faster-whisper (int8 CPU) → whisper.cpp, with
  automatic language detection, an engine-independent speech-presence probe, a VAD
  fallback, and a hallucination guard that downgrades downstream sections instead of
  pretending the text is usable.
- **Visual**: a provider chain with a four-image known-answer probe (including a blank
  negative and a random nonce), frame downscaling before upload, and 429 handling that
  does not poison the 24-hour probe cache.
- **Audio / editing**: loudness, silence, BPM, beat sync, speech rate, and a one-line
  conclusion derived from the measured signals.
- **Text**: deterministic copy structure (hook, sentence stats, section split, CTAs) and an
  extractive summary (per-window key sentence, literal keyword counts, key numbers).
  Neither calls a model; both are marked `DERIVED`.
- **Report**: one `breakdown.json` + one `report.md`, every section labelled
  `MEASURED / DERIVED / ASSUMED / UNKNOWN / SKIPPED / FAILED` with its method and params;
  non-conclusive sections roll up into `unknowns[]`, and a requested-but-missing section
  makes `ok=false` with a non-zero exit code.
- **DSH plugin**: six tools (`breakdown_plan / run / status / report / frames / probe`),
  seven same-origin routes, and a three-column panel with job history, conclusion card,
  keyframe grid, shot table, transcript, summary and an explicit "unknown / degraded" list.
- **Tests**: 26 Python unit tests, 41 Node contract checks (host, client bundle, load
  smoke, PTC canonical-JSON), and a gate script set for the measurements this project
  depends on.

### Known limitations

- Transition types are not detected; `editing.transitions` stays `UNKNOWN` on purpose
  (see `docs/DETAIL.md`).
- Speaker separation, vocal/instrumental splitting, and short-video platform sources are
  out of scope for v1.
- Burned-in subtitles are read by the vision model on sampled keyframes, not by a
  per-frame OCR pass, so coverage of a subtitle-driven video is partial.
