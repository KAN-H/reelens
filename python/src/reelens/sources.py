"""取源：本地文件 / 任意 URL（yt-dlp）/ 后续的 RSS。

为什么 v1 只做这两条
--------------------
`yt-dlp` 一个工具就覆盖 B 站、YouTube、多数通用站与播客直链，这是"最快拿到能拆的
媒体"的唯一现实选择。抖音/小红书需要签名博弈与 cookie，**v1 明确不做**（失败时给
可读失败码，而不是让人以为"这个站坏了"）。

两个实测细节
------------
1. **B 站匿名拿不到 CC 字幕**（`--list-subs` 只返回 danmaku），所以这里只负责"拿到
   媒体文件"；字幕/口播交给 `asr.py` 的引擎链。要 CC 必须带登录态——那是私有桥的事。
2. yt-dlp 合流需要 ffmpeg。本机 ffmpeg 不在 PATH 上，所以显式传
   `--ffmpeg-location <ffmpeg 所在目录>`。
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from . import paths, status

#: 视频默认只要 480p：拆解看的是构图/版式/字幕，不需要 1080p；码率低 3–5 倍。
DEFAULT_MAX_HEIGHT = 480
#: 下载超时（秒）。直播/超长视频靠调用方的 max_seconds 拦，不在这里无限等。
DOWNLOAD_TIMEOUT_S = 1800.0

_URL_RE = re.compile(r"^https?://", re.I)
_MEDIA_EXTS = {
    ".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv", ".m4v", ".ts",
    ".m4a", ".mp3", ".aac", ".opus", ".ogg", ".wav", ".flac",
}


def is_url(text: str) -> bool:
    return bool(_URL_RE.match(str(text or "").strip()))


def ytdlp_path() -> str:
    """yt-dlp 可执行文件。顺序：BREAKDOWN_YTDLP → 当前解释器同目录 → PATH。

    为什么要看"解释器同目录"：用 `pip install` 装进 venv 时，`yt-dlp.exe` 落在
    `<venv>/Scripts/`（POSIX 是 `bin/`），而**子进程的 PATH 里通常没有它**——
    于是模块明明装了、`shutil.which` 却找不到，探针会误报"没装"（实测踩过）。
    """
    import shutil
    import sys

    explicit = str(os.environ.get("BREAKDOWN_YTDLP") or "").strip()
    if explicit and Path(explicit).is_file():
        return explicit
    beside = Path(sys.executable).parent
    for name in ("yt-dlp.exe", "yt-dlp", "yt-dlp.cmd"):
        candidate = beside / name
        if candidate.is_file():
            return str(candidate)
    return shutil.which("yt-dlp") or ""


def resolve_local(path: str | Path) -> dict:
    target = Path(path)
    if not target.is_file():
        return status.failed(f"本地文件不存在：{target}", method="local")
    return status.field(status.MEASURED, method="local", kind="local",
                        media_path=str(target.resolve()),
                        title=target.stem, url="",
                        bytes=target.stat().st_size)


def resolve_url(url: str, *, out_dir: str | Path, max_height: int = DEFAULT_MAX_HEIGHT,
                timeout: float = DOWNLOAD_TIMEOUT_S) -> dict:
    binary = ytdlp_path()
    if not binary:
        return status.skipped(
            "没装 yt-dlp（pip install yt-dlp），只有本地文件可用", method="yt-dlp")
    work = Path(out_dir)
    work.mkdir(parents=True, exist_ok=True)
    fmt = f"bv*[height<={max_height}]+ba/b[height<={max_height}]/b"
    command = [
        binary, "--no-warnings", "--no-playlist",
        "-f", fmt, "--merge-output-format", "mp4",
        "-o", str(work / "source.%(ext)s"),
        "--print", "after_move:filepath",
        "--print", "title",
    ]
    ffmpeg = paths.ffmpeg_path()
    if ffmpeg:
        command += ["--ffmpeg-location", str(Path(ffmpeg).parent)]
    command.append(url)

    try:
        proc = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, env=paths.child_env())
    except (OSError, subprocess.SubprocessError) as exc:
        return status.failed(f"yt-dlp 启动失败：{type(exc).__name__}: {exc}", method="yt-dlp")

    stdout = (proc.stdout or "").strip().splitlines()
    media = ""
    printed_media: list[str] = []
    title = ""
    for line in stdout:
        if line and Path(line).is_file():
            media = line
            printed_media.append(line)
        elif line and not title and not line.startswith("["):
            title = line
    candidates = sorted(work.glob("source.*"), key=lambda p: p.stat().st_mtime, reverse=True)
    media_candidates = [p for p in candidates if p.suffix.lower() in _MEDIA_EXTS]
    # yt-dlp 在找不到 ffmpeg 时可能仍 returncode=0，但留下视频流 + 音频流两个文件，
    # 并对每一路都打印 filepath。旧代码取最后一路（通常 .m4a）当完整视频，后续便谎称
    # "没有音轨/没有画面"。分离流没合并是取源失败，绝不能用单一路径冒充完整素材。
    separate_streams = (len(set(printed_media)) > 1
                        or (not printed_media and len(media_candidates) > 1))
    if proc.returncode == 0 and separate_streams:
        tail = " / ".join((proc.stderr or "").strip().splitlines()[-3:])
        if ffmpeg:
            reason = ("yt-dlp 下载了多个媒体文件但未合并，不能把其中一路当成完整素材"
                      + (f"；输出尾部：{tail}" if tail else ""))
        else:
            reason = ("视频/音频分离流已下载，但找不到 ffmpeg，未合并成可拆解的媒体；"
                      "请安装插件 npm 依赖，或设 REELENS_FFMPEG / BREAKDOWN_FFMPEG")
        return status.field(status.FAILED, reason=reason, method="yt-dlp",
                            artifacts=[str(p) for p in media_candidates])
    if not media:
        media = str(media_candidates[0]) if media_candidates else ""
    if proc.returncode != 0 or not media or not Path(media).is_file():
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        return status.failed(
            "yt-dlp 下载失败（站点改版/需要登录/需要 cookie 都可能）：" + " / ".join(tail),
            method="yt-dlp")

    item = status.field(status.MEASURED, method="yt-dlp", kind="url",
                        media_path=media, url=url, title=title or "未命名",
                        params={"max_height": max_height, "format": fmt})

    # **下载到的是不是完整视频**：B 站未登录时常只下发几十秒的试看片段。
    # 2026-11-23 实锤：同一个 45 分钟链接，一次拿到 109MB/2694s，另一次只拿到 3.5MB/49.9s，
    # 而报告照样写"转写 1 句、覆盖率 7%"——把"下载残缺"伪装成"素材没人声"。
    expected = _expected_duration(binary, url)
    actual = _probe_duration(media)
    if expected and actual and actual < expected * 0.9:
        item["duration_mismatch"] = {
            "expected_s": round(expected, 1), "actual_s": round(actual, 1),
            "ratio": round(actual / expected, 3),
            "reason": "只下载到片段（站点限流/需要登录 cookie），后续结论只代表这一段",
        }
        item["warnings"] = ["partial_download"]
    return item


def _expected_duration(binary: str, url: str) -> float:
    """只读元数据拿"应有片长"（`--skip-download`，不占带宽）。"""
    try:
        proc = subprocess.run([binary, "--no-warnings", "--no-playlist",
                               "--skip-download", "--print", "%(duration)s", url],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=120, env=paths.child_env())
        return float((proc.stdout or "").strip().splitlines()[0])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return 0.0


def _probe_duration(media: str | Path) -> float:
    try:
        from . import probe as probe_mod  # noqa: PLC0415 - 避免循环导入
        info = probe_mod.probe(media)
        return float(info.get("duration_s") or 0.0)
    except Exception:  # noqa: BLE001
        return 0.0


def resolve(source: str, *, out_dir: str | Path, max_height: int = DEFAULT_MAX_HEIGHT) -> dict:
    """统一入口：URL → yt-dlp；否则当本地路径。"""
    text = str(source or "").strip().strip('"').strip("'")
    if not text:
        return status.failed("没有给来源（URL 或本地文件路径）", method="resolve")
    if is_url(text):
        return resolve_url(text, out_dir=out_dir, max_height=max_height)
    return resolve_local(text)
