"""音频侧测量：响度、静音段、节拍（可选）。

为什么用 ebur128 而不是只看 loudnorm
------------------------------------
`loudnorm=print_format=json` 给的是"为了达标需要怎么调"，`input_i` 已经是积分响度，
够用；`ebur128` 则能按窗口给曲线（可看出"前 3 秒很响、后面突然压低"这种结构）。
这里主用 loudnorm（一次跑完、输出稳定），`ebur128` 留给精细档。

节拍
----
`librosa` 未安装时返回 SKIPPED 并给安装提示——**不假装有 BPM**。
BPM 只对"有持续配乐"的片段有意义，所以只有在 `beat` 为 MEASURED 且置信度不低时才
在报告里写"卡点"；否则一律 UNKNOWN。
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from . import paths, status

_TARGET_LUFS = -14.0
_MAX_TRUE_PEAK = -1.0


def _run(command: list[str], *, timeout: float = 900.0) -> str:
    proc = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, env=paths.child_env())
    return (proc.stdout or "") + (proc.stderr or "")


def wav16k(media: str, target) -> dict:
    """单独抽一条 16kHz 单声道 wav（给 ASR 用），返回状态项。

    为什么需要独立这一步：完整流程里这条 wav 是**镜头分析那次单次解码的副产物**；
    但用户只勾"字幕/口播"时镜头分析被跳过，wav 就没人生成，
    ASR 于是报"没有 16kHz 音频（单次解码可能失败）"——**为了省一步反而整件事失败**
    （2026-11-23 用户实测）。所以按需单独抽一次。
    """
    binary = paths.ffmpeg_path()
    if not binary:
        return status.skipped("没有 ffmpeg", method="ffmpeg")
    out = Path(target)
    out.parent.mkdir(parents=True, exist_ok=True)
    command = [binary, "-hide_banner", "-loglevel", "error", "-y", "-i", str(media),
               "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out)]
    try:
        subprocess.run(command, capture_output=True, timeout=1800, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return status.failed(f"抽音频失败：{exc}", method="ffmpeg")
    if not out.is_file() or out.stat().st_size <= 1024:
        return status.failed("抽出来的音频是空的（可能没有音轨）", method="ffmpeg")
    return status.field(status.MEASURED, method="ffmpeg -vn -ar 16000", path=str(out),
                        bytes=out.stat().st_size)


def loudness(media: str) -> dict:
    """整体响度（I / TP / LRA）。无音轨返回 SKIPPED。"""
    binary = paths.ffmpeg_path()
    if not binary:
        return status.skipped("找不到 ffmpeg", method="loudnorm")
    output = _run([binary, "-hide_banner", "-nostats", "-i", str(media),
                   "-af", "loudnorm=print_format=json", "-f", "null", "-"])
    start, end = output.rfind("{"), output.rfind("}")
    if start < 0 or end <= start:
        return status.skipped("没有音频流（或 loudnorm 无输出）", method="loudnorm")
    try:
        data = json.loads(output[start:end + 1])
    except ValueError:
        return status.failed("loudnorm 输出不是 JSON", method="loudnorm")

    def number(key):
        try:
            return round(float(data.get(key)), 2)
        except (TypeError, ValueError):
            return None

    return status.field(
        status.MEASURED, method="ffmpeg loudnorm",
        params={"target_lufs": _TARGET_LUFS, "max_true_peak": _MAX_TRUE_PEAK},
        input_i=number("input_i"), input_tp=number("input_tp"),
        input_lra=number("input_lra"), input_thresh=number("input_thresh"),
    )


def silence(media: str, *, noise_db: float = -35.0, min_s: float = 0.6) -> dict:
    """静音段（用于推断"停顿密度"与"是不是有纯 BGM 段"）。"""
    binary = paths.ffmpeg_path()
    if not binary:
        return status.skipped("找不到 ffmpeg", method="silencedetect")
    output = _run([binary, "-hide_banner", "-nostats", "-i", str(media),
                   "-af", f"silencedetect=noise={noise_db}dB:d={min_s}", "-f", "null", "-"])
    if "silence_start" not in output and "no audio" in output.lower():
        return status.skipped("没有音频流", method="silencedetect")
    segments: list[list[float]] = []
    current = None
    for line in output.splitlines():
        hit = re.search(r"silence_start:\s*(-?[0-9.]+)", line)
        if hit:
            current = float(hit.group(1))
            continue
        hit = re.search(r"silence_end:\s*(-?[0-9.]+)", line)
        if hit and current is not None:
            segments.append([round(max(0.0, current), 2), round(float(hit.group(1)), 2)])
            current = None
    return status.field(status.MEASURED, method="ffmpeg silencedetect",
                        params={"noise_db": noise_db, "min_s": min_s},
                        segments=segments, count=len(segments),
                        total_s=round(sum(b - a for a, b in segments), 1))


def beat(wav: str) -> dict:
    """BPM 与节拍时刻。需要 librosa（可选依赖）。"""
    try:
        import librosa  # noqa: PLC0415
    except ImportError:
        return status.skipped("没装 librosa（pip install librosa）——BPM/节拍不可用",
                              method="librosa.beat_track")
    try:
        import numpy as np  # noqa: PLC0415
        signal, rate = librosa.load(str(wav), sr=22050, mono=True)
        tempo, frames = librosa.beat.beat_track(y=signal, sr=rate)
        times = librosa.frames_to_time(frames, sr=rate)
        bpm = float(np.atleast_1d(tempo)[0])
        if not times.size or bpm <= 0:
            return status.unknown("检测不到稳定节拍（可能是纯人声/极慢配乐）",
                                  method="librosa.beat_track")
        return status.field(status.MEASURED, method="librosa.beat_track",
                            bpm=round(bpm, 1), beats=[round(float(t), 3) for t in times[:2000]],
                            beat_count=int(times.size))
    except Exception as exc:  # noqa: BLE001 - 音频分析失败不该毁掉整份报告
        return status.failed(f"{type(exc).__name__}: {exc}", method="librosa.beat_track")


def beat_sync(shots: list[dict], beat_item: dict, *, tolerance_s: float = 0.10) -> dict:
    """镜头起点与最近节拍的对齐误差——这是"卡点"唯一可量的证据。"""
    if beat_item.get("status") != status.MEASURED or not beat_item.get("beats"):
        return status.unknown("没有可用节拍，无法判定卡点", method="beat_sync")
    beats = beat_item["beats"]
    deviations = []
    for shot in shots:
        start = float(shot["start"])
        nearest = min(beats, key=lambda b: abs(b - start))
        deviations.append(abs(start - nearest))
    within = sum(1 for d in deviations if d <= tolerance_s)
    ratio = within / max(len(deviations), 1)
    return status.field(status.DERIVED, method="shot.start vs nearest beat",
                        calibrated=False, tolerance_s=tolerance_s,
                        shots=len(deviations), within=within,
                        ratio=round(ratio, 3),
                        median_dev_s=round(sorted(deviations)[len(deviations) // 2], 3))
