#!/usr/bin/env python3
"""G4 时延基线：同一段音频上对比 whisper.cpp（CPU）与 faster-whisper（CPU int8）。

为什么要测这一条
----------------
B 站很多视频**没有 CC 字幕**（G2 实测：匿名 yt-dlp 只拿到 danmaku；带登录态也不一定有），
所以 ASR 是默认路径而不是兜底。ASR 的"实时倍率"直接决定三档时延承诺是否成立：
whisper.cpp 实测约 1.8×（171s 音频 ≈ 95s），57 分钟的视频就要 32 分钟 —— 不可接受。

本脚本只做测量，不落仓库产物；结果打印成 JSON 供能力基线留档。

用法
----
    python scripts/gates/g4_asr_bench.py --wav .tmp/bench/audio16k.wav
    python scripts/gates/g4_asr_bench.py --wav x.wav --engine fw --model small
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_WHISPER_CLI = Path(os.environ.get("REELENS_WHISPER_CPP")
                           or os.environ.get("BREAKDOWN_WHISPER_CPP") or "")
DEFAULT_WHISPER_MODEL = Path(os.environ.get("REELENS_WHISPER_MODEL")
                             or os.environ.get("BREAKDOWN_WHISPER_MODEL") or "")


def wav_seconds(path: Path) -> float:
    import wave
    with wave.open(str(path), "rb") as handle:
        return handle.getnframes() / float(handle.getframerate() or 16000)


def run_whisper_cpp(wav: Path, *, cli: Path, model: Path, timeout: float) -> dict:
    if not cli.is_file() or not model.is_file():
        return {"engine": "whisper.cpp", "ok": False, "reason": "binary_or_model_missing",
                "cli": str(cli), "model": str(model)}
    out_base = wav.with_suffix(".bench")
    command = [str(cli), "-m", str(model), "-f", str(wav), "-oj", "-of", str(out_base), "-l", "zh"]
    if str(os.environ.get("REELENS_WHISPER_NO_GPU") or "").strip().lower() in {"1", "true", "yes", "on"}:
        command.append("-ng")
    started = time.time()
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        elapsed = time.time() - started
        if proc.returncode != 0:
            return {"engine": "whisper.cpp", "ok": False, "elapsed_s": round(elapsed, 1),
                    "reason": f"exit_{proc.returncode}", "stderr": (proc.stderr or "")[-300:]}
        doc = json.loads(Path(str(out_base) + ".json").read_text(encoding="utf-8"))
        entries = doc.get("transcription") or []
        return {"engine": "whisper.cpp", "ok": True, "elapsed_s": round(elapsed, 1),
                "segments": len(entries),
                "first": str((entries[0] or {}).get("text") or "").strip()[:40] if entries else ""}
    except subprocess.TimeoutExpired:
        return {"engine": "whisper.cpp", "ok": False, "reason": "timeout", "timeout_s": timeout}


def run_faster_whisper(wav: Path, *, model: str, compute: str, threads: int,
                       language: str, vad: bool) -> dict:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        return {"engine": "faster-whisper", "ok": False, "reason": f"not_installed: {exc}"}
    started = time.time()
    try:
        loaded = WhisperModel(model, device="cpu", compute_type=compute, cpu_threads=threads)
        load_s = time.time() - started
        started = time.time()
        segments, info = loaded.transcribe(
            str(wav), language=language, vad_filter=vad, beam_size=5,
            word_timestamps=True,
        )
        rows = list(segments)
        elapsed = time.time() - started
        return {
            "engine": "faster-whisper", "ok": True,
            "model": model, "compute": compute, "threads": threads, "vad": vad,
            "load_s": round(load_s, 1), "elapsed_s": round(elapsed, 1),
            "segments": len(rows),
            "duration_s": round(float(getattr(info, "duration", 0) or 0), 1),
            "lang": getattr(info, "language", ""),
            "first": (rows[0].text.strip()[:40] if rows else ""),
            "words": sum(len(getattr(r, "words", []) or []) for r in rows),
        }
    except Exception as exc:  # noqa: BLE001 - 基线要能报告任何失败
        return {"engine": "faster-whisper", "ok": False,
                "reason": f"{type(exc).__name__}: {exc}"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="G4：ASR 引擎时延基线")
    parser.add_argument("--wav", required=True)
    parser.add_argument("--engine", choices=["cpp", "fw", "both"], default="both")
    parser.add_argument("--model", default="large-v3-turbo", help="faster-whisper 模型名/目录")
    parser.add_argument("--compute", default="int8")
    parser.add_argument("--threads", type=int, default=0, help="缺省=CPU 核数")
    parser.add_argument("--timeout", type=float, default=1800.0)
    args = parser.parse_args(argv)

    wav = Path(args.wav)
    if not wav.is_file():
        print(json.dumps({"ok": False, "error": f"没有这个 wav：{wav}"}, ensure_ascii=False))
        return 2
    seconds = wav_seconds(wav)
    threads = args.threads or (os.cpu_count() or 4)
    results = []

    if args.engine in {"cpp", "both"}:
        results.append(run_whisper_cpp(wav, cli=DEFAULT_WHISPER_CLI,
                                       model=DEFAULT_WHISPER_MODEL, timeout=args.timeout))
    if args.engine in {"fw", "both"}:
        results.append(run_faster_whisper(wav, model=args.model, compute=args.compute,
                                          threads=threads, language="zh", vad=True))

    for item in results:
        if item.get("ok") and seconds > 0:
            item["realtime_x"] = round(seconds / max(item["elapsed_s"], 0.01), 2)
    report = {
        "schema": "video-breakdown.gate.g4.v1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "wav": str(wav), "audio_s": round(seconds, 1), "cpu_count": os.cpu_count(),
        "results": results,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if any(r.get("ok") for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
