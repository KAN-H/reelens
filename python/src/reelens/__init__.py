"""video-breakdown —— 把一条视频/音频拆成结构化报告（镜头/抽帧/口播/节奏/音频）。

设计要点（读代码前先看这三条）
------------------------------
1. **每个可测项都带 `status`**（`MEASURED / DERIVED / ASSUMED / UNKNOWN / SKIPPED / FAILED`）。
   "测到"不等于"测对"：未经标定的代理指标只能是 `DERIVED`。见 `status.py`。
2. **失败不静默**：拿不到的东西写成 `UNKNOWN/SKIPPED` 并附原因，绝不填 0 或空数组冒充。
   `report.assemble()` 会把这些项汇总进 `unknowns[]`，并据此决定 `ok`。
3. **不依赖任何私有包**：外部工具（ffmpeg/yt-dlp/whisper/faster-whisper）都走
   `paths.py` 的解析顺序，本机细节不写死。
"""

__version__ = "0.1.0"

from . import audio, asr, frames, paths, probe, report, shots, sources, status  # noqa: F401

__all__ = ["audio", "asr", "frames", "paths", "probe", "report", "shots", "sources",
           "status", "__version__"]
