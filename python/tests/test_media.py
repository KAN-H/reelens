"""媒体依赖与下载结果的回归测试；不访问网络、不调用真实 ffmpeg。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reelens import asr, paths, sources, status  # noqa: E402


def test_discovers_bundled_ffmpeg_and_ffprobe(tmp_path, monkeypatch):
    repo = tmp_path / "plugin"
    platform_dir = paths._node_platform_dir()
    ffmpeg = (repo / "node_modules" / "@ffmpeg-installer" / platform_dir
              / f"ffmpeg{'.exe' if sys.platform == 'win32' else ''}")
    ffprobe = (repo / "node_modules" / "@ffprobe-installer" / platform_dir
               / f"ffprobe{'.exe' if sys.platform == 'win32' else ''}")
    ffmpeg.parent.mkdir(parents=True)
    ffprobe.parent.mkdir(parents=True)
    ffmpeg.write_bytes(b"ffmpeg")
    ffprobe.write_bytes(b"ffprobe")
    monkeypatch.setattr(paths, "REPO_DIR", repo)
    for name in ("REELENS_FFMPEG", "BREAKDOWN_FFMPEG", "REELENS_FFPROBE", "BREAKDOWN_FFPROBE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(paths.shutil, "which", lambda _name: None)

    assert paths.ffmpeg_path() == str(ffmpeg)
    assert paths.ffprobe_path() == str(ffprobe)


def test_explicit_ffmpeg_path_still_wins_over_bundle(tmp_path, monkeypatch):
    explicit = tmp_path / "custom-ffmpeg"
    explicit.write_bytes(b"custom")
    monkeypatch.setenv("REELENS_FFMPEG", str(explicit))
    monkeypatch.setattr(paths, "_bundled_npm_binary", lambda *_args: "bundled")

    assert paths.ffmpeg_path() == str(explicit)


def test_separate_download_streams_fail_instead_of_selecting_audio(tmp_path, monkeypatch):
    video = tmp_path / "source.f100023.mp4"
    audio = tmp_path / "source.f30280.m4a"

    def fake_run(_command, **_kwargs):
        video.write_bytes(b"video")
        audio.write_bytes(b"audio")
        return SimpleNamespace(
            returncode=0,
            stdout=f"{video}\n{audio}\nDemo title\n",
            stderr="ERROR: ffmpeg not found",
        )

    monkeypatch.setattr(sources, "ytdlp_path", lambda: "yt-dlp")
    monkeypatch.setattr(sources.paths, "ffmpeg_path", lambda: "")
    monkeypatch.setattr(subprocess, "run", fake_run)

    item = sources.resolve_url("https://example.invalid/video", out_dir=tmp_path)

    assert item["status"] == status.FAILED
    assert "未合并" in item["reason"]
    assert "media_path" not in item
    assert len(item["artifacts"]) == 2


def test_single_download_artifact_remains_supported(tmp_path, monkeypatch):
    media = tmp_path / "source.mp4"
    monkeypatch.setattr(sources, "ytdlp_path", lambda: "yt-dlp")
    monkeypatch.setattr(sources.paths, "ffmpeg_path", lambda: "ffmpeg")
    monkeypatch.setattr(sources, "_expected_duration", lambda *_args: 0.0)
    monkeypatch.setattr(sources, "_probe_duration", lambda *_args: 0.0)
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(
        returncode=0, stdout=f"{media}\nDemo title\n", stderr="",
    ))
    media.write_bytes(b"combined")

    item = sources.resolve_url("https://example.invalid/video", out_dir=tmp_path)

    assert item["status"] == status.MEASURED
    assert item["media_path"] == str(media)


@pytest.mark.parametrize("has_audio, expected", [
    (None, "音轨状态未知"),
    (False, "没有音轨"),
])
def test_transcribe_distinguishes_unknown_from_absent_audio(tmp_path, has_audio, expected):
    item = asr.transcribe(
        "unused.mp4", work_dir=tmp_path, duration_s=1.0,
        has_audio=has_audio, preferred="faster-whisper",
    )

    assert item["status"] == status.SKIPPED
    assert expected in item["reason"]
