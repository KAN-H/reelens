# Third-party notices

This project ships **no third-party binaries**. Everything below is either an optional
runtime dependency you install yourself, or an external program you point it at.

## External programs (not bundled)

| Program | License | Notes |
|---|---|---|
| FFmpeg / ffprobe | **LGPL-2.1+ or GPL**, depending on the build | We never bundle it. Many popular Windows builds are `--enable-gpl` (because of x264/x265), which makes that *binary* GPL. Check yours with `ffmpeg -L` before redistributing. The project calls it as a separate process. |
| whisper.cpp | MIT | Optional; provide the binary and a ggml model. |
| yt-dlp | Unlicense | Optional; used to fetch media by URL. |

## Python extras (`pip install -e python[...]`)

| Extra | Package | License | Notes |
|---|---|---|---|
| `download` | yt-dlp | Unlicense | |
| `asr` | faster-whisper | MIT | Pulls CTranslate2 (MIT) and model weights downloaded at first use; the **model weights** carry their own license from their publisher. |
| `audio` | librosa | ISC | |
| `scenes` | scenedetect | BSD-3-Clause | Uses OpenCV (Apache-2.0) if present. |
| `images` | pillow | MIT-CMU (HPND) | Used for dHash de-duplication and probe images. |
| — | pytest (dev) | MIT | |

## DSH runtime

The DSH plugin imports nothing from the harness at module scope; it receives services
through the plugin context. DeepSeek Harness itself is not redistributed here.
