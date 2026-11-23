# Third-party notices

This project ships **no third-party binaries and no third-party source code**. Everything
below is either an optional runtime dependency you install yourself, or an external program
you point it at. Libraries are imported or invoked as separate processes; nothing was
vendored or copied.

## External programs (not bundled)

| Program | License | Notes |
|---|---|---|
| FFmpeg / ffprobe | **LGPL-2.1+ or GPL**, depending on the build | We never bundle it. Many popular Windows builds are `--enable-gpl` (because of x264/x265), which makes that *binary* GPL. Check yours with `ffmpeg -L` before redistributing. The project calls it as a separate process. |
| whisper.cpp | MIT | Optional; provide the binary and a ggml model. |
| yt-dlp | Unlicense | Optional; used to fetch media by URL. |

## Python extras (`pip install "reelens[...]"`)

| Extra | Package | License | Notes |
|---|---|---|---|
| `download` | yt-dlp | Unlicense | |
| `asr` | faster-whisper | MIT | Pulls CTranslate2 (MIT) and model weights downloaded at first use; the **model weights** carry their own license from their publisher. |
| `audio` | librosa | ISC | |
| `scenes` | scenedetect | BSD-3-Clause | Uses OpenCV (Apache-2.0) if present. |
| `images` | pillow | MIT-CMU (HPND) | Used for dHash de-duplication, frame scaling and probe images. |
| — | pytest (dev) | MIT | |

### Model weights

Speech models are **not** included and **not** redistributed. `faster-whisper` downloads
weights on first use from their publisher; check that publisher's license (and any
acceptable-use terms) before redistributing a converted model or shipping it inside a
container image. `whisper.cpp` needs a `.bin` ggml model you supply.

## Algorithms

Implemented from published descriptions rather than copied code:

- **dHash** frame de-duplication (difference hash, as popularised by Neal Krawetz's
  *Perceptual Hash* article) — 64-bit horizontal-difference hash, Hamming distance
  threshold in `frames.py`.
- **Scene score / `select=gt(scene,…)`** and **`signalstats` / `edgedetect`** metadata
  parsing follow FFmpeg's documented filter examples.
- Adaptive cut detection (`median + k·MAD` over a per-frame score series) and the
  frame-difference rhythm signal are original to this project.

## DSH runtime

The DSH plugin imports nothing from the harness at module scope; it receives services through
the plugin context. DeepSeek Harness itself is not redistributed here.

