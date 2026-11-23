"""媒体探测：把 ffprobe 的原始 JSON 归一化成"拆解要用的几个事实"。

为什么自己写而不是复用某个库
----------------------------
要的事实很少（宽高/旋转/时长/帧率/编码/有没有音轨），而 ffprobe 的输出有两个坑值得
单独处理：手机竖拍是 `width×height + rotate=90`（直接 center-crop 会把人裁掉），
以及可变帧率的时长得在 `stream.duration` 与 `format.duration` 之间试探。

与早期实现的关系：**行为对齐**（既有 golden 门槛会逐字段对比），
但这里是独立实现——开源包不依赖任何私有包。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from . import paths, status


def _run(command: list[str], *, timeout: float = 60.0, cwd: Path | None = None) -> str:
    proc = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, cwd=str(cwd) if cwd else None, env=paths.child_env(),
    )
    return (proc.stdout or "") + (proc.stderr or "")


def _fraction(value: str) -> float:
    """`30000/1001` → 29.97（ffprobe 的帧率是分数）。"""
    try:
        text = str(value or "")
        if "/" in text:
            num, den = text.split("/", 1)
            return float(num) / float(den) if float(den) else 0.0
        return float(text)
    except (TypeError, ValueError):
        return 0.0


def _rotation(stream: dict) -> int:
    for item in stream.get("side_data_list") or []:
        if isinstance(item, dict) and "rotation" in item:
            try:
                return int(float(item["rotation"])) % 360
            except (TypeError, ValueError):
                pass
    try:
        return int(float((stream.get("tags") or {}).get("rotate") or 0)) % 360
    except (TypeError, ValueError):
        return 0


def probe(path: str | Path) -> dict:
    """探测一个媒体文件。

    @returns 成功：`{"status": "MEASURED", "method": "ffprobe", ...facts}`
             失败：`{"status": "FAILED"/"SKIPPED", "reason": ...}`（**不抛异常**）
    """
    target = Path(path)
    if not target.is_file():
        return status.failed(f"文件不存在：{target}", method="ffprobe")
    binary = paths.ffprobe_path()
    if not binary:
        return status.skipped("找不到 ffprobe（把 ffprobe 放进 PATH 或设 BREAKDOWN_FFPROBE）",
                              method="ffprobe")
    try:
        out = _run([binary, "-v", "error", "-print_format", "json",
                    "-show_format", "-show_streams", str(target)], timeout=60.0)
        data = json.loads(out)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return status.failed(f"ffprobe 失败：{type(exc).__name__}: {exc}", method="ffprobe")

    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    subtitle_tracks = [s for s in streams if s.get("codec_type") == "subtitle"]
    fmt = data.get("format") or {}
    if video is None:
        return status.failed("没有视频流（纯音频请用 --audio-only，尚未实现）", method="ffprobe")

    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    rotation = _rotation(video)
    display_w, display_h = (height, width) if rotation in (90, 270) else (width, height)

    duration = 0.0
    for candidate in (video.get("duration"), fmt.get("duration")):
        try:
            value = float(candidate)
            if value > 0:
                duration = value
                break
        except (TypeError, ValueError):
            continue

    return status.field(
        status.MEASURED, method="ffprobe",
        params={"streams": len(streams)},
        raw_path=str(target.resolve()),
        width=width, height=height, rotation=rotation,
        display_width=display_w, display_height=display_h,
        portrait=display_h > display_w,
        duration_s=round(max(0.0, duration), 3),
        fps=round(_fraction(video.get("r_frame_rate") or "0"), 3),
        codec=str(video.get("codec_name") or ""),
        pix_fmt=str(video.get("pix_fmt") or ""),
        video_bitrate=int(video.get("bit_rate") or 0),
        bytes=int(fmt.get("size") or 0),
        bitrate=int(fmt.get("bit_rate") or 0),
        has_audio=audio is not None,
        audio_codec=str((audio or {}).get("codec_name") or ""),
        audio_channels=int((audio or {}).get("channels") or 0),
        audio_sample_rate=int((audio or {}).get("sample_rate") or 0),
        subtitle_tracks=[
            {"index": int(s.get("index") or -1),
             "codec": str(s.get("codec_name") or ""),
             "lang": str(((s.get("tags") or {}).get("language") or ""))}
            for s in subtitle_tracks
        ],
    )
