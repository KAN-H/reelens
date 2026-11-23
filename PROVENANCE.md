# Provenance

## Origin

This repository was written for this project (video/audio breakdown). It is
**self-contained**: it does not import, vendor, or depend on any private package.

## Re-derived logic

Some algorithms were re-implemented after studying the author's own private project
(a private media toolkit by the same author) because that code cannot be published.
Nothing was copied verbatim; the behaviour was re-derived and then **verified against the
original** so the two cannot silently diverge:

| Area | What was re-derived | Verification |
|---|---|---|
| Media probing | normalised facts from `ffprobe` (rotation, display size, duration fallback) | `scripts/gates/g3_golden.py` compared both implementations on a real clip: **11/11 fields identical** |
| Shot boundaries | `select=gt(scene,TH)` + `metadata=print` parsing, edge guard, micro-shot rule | same gate: **7 vs 7 shots, max boundary deviation 0.0 s** |
| Loudness / silence | ffmpeg `loudnorm` / `silencedetect` invocations and parsing | not compared field-by-field; both are thin ffmpeg wrappers, thresholds documented in code |
| "Do not fill gaps with zeros" | the status vocabulary (`MEASURED`/`DERIVED`/…) generalises that project's `unknown`/`没测到` sentinel | `python/tests/test_core.py` asserts the invariant |

The gate scripts under `scripts/gates/` are kept in the public tree on purpose: they are the
evidence for design decisions, including the ones that concluded **not** to build a feature
(gradual-transition detection).

## Third-party code

No third-party source is copied into this repository. Optional runtime dependencies and
their licenses are listed in `THIRD_PARTY_NOTICES.md`.

## Assets

`icon.svg` was drawn for this project. The probe images are generated at runtime by code in
`python/src/reelens/vision.py` — no external image assets are shipped.

## License history

First publication: Apache-2.0 (see `LICENSE`). Because the re-derived logic comes from the
same author's private project, there is no additional attribution requirement beyond the
license itself.
