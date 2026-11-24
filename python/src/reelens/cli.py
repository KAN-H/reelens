"""命令行入口：`breakdown run/probe/report/frames`。

设计约束（来自 v3 方案）
------------------------
- **每个动词只做一件事**，宿主插件是薄壳：拼参数 + 解析 JSON，业务规则全在这里。
- **失败不抛异常**：所有可见动词都以 JSON 返回 `{ok, status, reason}`，
  退出码 0=成功、1=有未测项但报告已产出、2=参数/环境错误。
- `run` 的两段式在宿主侧（plan/confirm）；CLI 只提供 `--plan-id` 让宿主做一致性校验。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

from . import (audio, asr, frames, paths, probe as probe_mod, report, shots, sources,
               status, text as text_mod, vision)

TIERS = {
    # frames: 抽帧上限；per_shot: 1=中点 2=首帧+中点；beat: librosa；
    # vision: 送给视觉模型的帧数上限（0=不做）；sheet: 对照图
    "fast": {"frames": 12, "min_frames": 8, "per_shot": 1, "beat": False, "sheet": False, "vision": 8},
    "standard": {"frames": 40, "min_frames": 12, "per_shot": 1, "beat": True, "sheet": True, "vision": 24},
    "fine": {"frames": 100, "min_frames": 20, "per_shot": 2, "beat": True, "sheet": True, "vision": 60},
}
#: 默认允许拆解的最长时长（秒）。57 分钟的片子即使 7.5× 实时也要 7.6 分钟。
DEFAULT_MAX_SECONDS = 3600.0


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def probe_command(args) -> int:
    payload = {"ok": True, "status": status.MEASURED, "method": "self-check",
               "tools": paths.tool_report(), "generated_at": _now()}
    engines = []
    for name, module, ok, hint in (
        ("yt-dlp", "yt_dlp", bool(sources.ytdlp_path()), "pip install yt-dlp"),
        ("faster-whisper", "faster_whisper", _module_exists("faster_whisper"), "pip install faster-whisper"),
        ("scenedetect", "scenedetect", _module_exists("scenedetect"), "pip install scenedetect[opencv]"),
        ("librosa", "librosa", _module_exists("librosa"), "pip install librosa"),
        ("PIL", "PIL", _module_exists("PIL"), "pip install pillow"),
    ):
        engines.append({"name": name, "module": module, "ok": ok, "hint": "" if ok else hint})
    payload["extras"] = engines
    payload["ok"] = all(item["ok"] for item in engines
                        if item["module"] in {"yt_dlp", "faster_whisper", "PIL"})
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 1


def _module_exists(name: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(name) is not None


def _editing(shots_item: dict, audio_pack: dict, transcript: dict) -> dict:
    rhythm = shots.relative_stats(shots_item.get("shots") or [])
    beat_sync = audio.beat_sync(shots_item.get("shots") or [], audio_pack.get("beat") or {})
    voice = _voice(transcript, audio_pack.get("silence") or {})
    return {
        "rhythm": rhythm,
        "frame_diff": shots_item.get("rhythm"),
        "transitions": status.unknown("v1 未做转场识别（渐变检测需要人工标定，见 G5）",
                                      method="scene 阈值"),
        "beat_sync": beat_sync,
        "voice": voice,
    }


def _voice(transcript: dict, silence_item: dict) -> dict:
    """配音特征：语速/停顿/段均时长。**全部是推断**，不是实测声学量。"""
    if transcript.get("status") != status.MEASURED:
        return status.unknown("没有口播文本，无法推断配音特征", method="text-derived")
    entries = transcript.get("entries") or []
    if not entries:
        return status.unknown("口播为空", method="text-derived")
    chars = sum(len(e.get("content") or "") for e in entries)
    span = sum(max(0.0, float(e["to"]) - float(e["from"])) for e in entries)
    pauses = []
    for previous, current in zip(entries, entries[1:]):
        gap = float(current["from"]) - float(previous["to"])
        if gap > 0.15:
            pauses.append(gap)
    return status.field(
        status.DERIVED, method="transcript 时长/字符统计", calibrated=False,
        chars=chars,
        chars_per_s=round(chars / span, 2) if span else None,
        mean_segment_s=round(span / len(entries), 2),
        pauses=len(pauses),
        pause_ratio=round(sum(pauses) / span, 3) if span else None,
        silence_total_s=(silence_item.get("total_s")
                         if silence_item.get("status") == status.MEASURED else None),
    )


def run_command(args) -> int:
    started = time.time()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    work = out / "work"
    tier = TIERS.get(args.tier) or TIERS["standard"]
    timing: dict = {"started_at": _now(),
                    "excludes": ["download", "human_confirm"]}

    requested = ({item.strip() for item in str(args.items or "").split(",") if item.strip()}
                 or None)

    def asked(*names: str) -> bool:
        return requested is None or bool(requested.intersection(names))

    def stage(name: str, fn):
        tick = time.time()
        result = fn()
        timing[f"{name}_s"] = round(time.time() - tick, 1)
        return result

    source = stage("resolve", lambda: sources.resolve(args.source, out_dir=out,
                                                      max_height=args.max_height))
    if source.get("status") != status.MEASURED:
        payload = report.assemble(
            source=source, probe=status.skipped("没有媒体文件"),
            shots_item=status.skipped("没有媒体文件"), frames_item=status.skipped("没有媒体文件"),
            transcript=status.skipped("没有媒体文件"), audio={}, editing={},
            visual=status.skipped("没有媒体文件"), copywriting=status.skipped("没有媒体文件"),
            tier=args.tier,
            plan_id=args.plan_id or "", timing=timing, requested=requested,
            evidence={"ffmpeg": paths.ffmpeg_path(), "ffprobe": paths.ffprobe_path()})
        written = report.write(payload, out)
        print(json.dumps({"ok": False, "stage": "resolve", "source": source, **written},
                         ensure_ascii=False))
        return 2

    media = source["media_path"]
    info = stage("probe", lambda: probe_mod.probe(media))
    duration = float(info.get("duration_s") or 0.0)

    if asked("shots", "frames", "visual", "subtitle_style", "on_screen_text", "editing"):
        shots_item = stage("shots", lambda: shots.analyze(
            media, duration_s=duration, has_audio=bool(info.get("has_audio")), work_dir=work))
    else:
        shots_item = status.skipped("未勾选镜头表（只提字幕时不需要）", method="scenedetect")
    if shots_item.get("status") != status.MEASURED:
        shots_item.setdefault("shots", [])

    if asked("frames", "visual", "subtitle_style", "on_screen_text"):
        frames_item = stage("frames", lambda: frames.extract(
            media, shots_item.get("shots") or [], out_dir=out,
            max_frames=int(args.frames or tier["frames"]), per_shot=tier["per_shot"],
            min_frames=int(tier.get("min_frames") or 0), duration_s=duration))
        if tier["sheet"] and frames_item.get("status") == status.MEASURED:
            frames_item["contact_sheet"] = frames.contact_sheet(
                [f["path"] for f in frames_item.get("frames") or []], out / "frames" / "sheet.jpg")
    else:
        frames_item = status.skipped("未勾选抽帧（只提字幕时不需要）", method="ffmpeg -ss")

    # 镜头分析被跳过时，16k wav 没人生成——ASR 需要它，所以按需单独抽一次
    if not asked("shots") and asked("transcript", "copywriting", "voice") \
            and bool(info.get("has_audio")):
        stage("audio16k", lambda: audio.wav16k(media, work / "audio.16k.wav"))

    if not asked("transcript", "copywriting", "voice", "summary"):
        transcript = status.skipped("未勾选口播/文案（不需要转写）", method="asr")
    elif args.no_asr:
        transcript = status.skipped("调用方用 --no-asr 关掉了转写", method="asr")
    elif duration > float(args.max_seconds) and not args.allow_long:
        transcript = status.skipped(
            f"时长 {round(duration)}s 超过上限 {int(args.max_seconds)}s（加 --allow-long 或调大 --max-seconds）",
            method="asr")
    else:
        transcript = stage("asr", lambda: asr.transcribe(
            media, work_dir=work, duration_s=duration,
            has_audio=info.get("has_audio"), preferred=args.asr_engine))

    audio_pack = {
        "loudness": (stage("loudness", lambda: audio.loudness(media))
                     if asked("audio") else status.skipped("未勾选响度/静音", method="ffmpeg")),
        "silence": (stage("silence", lambda: audio.silence(media))
                    if asked("audio") else status.skipped("未勾选响度/静音", method="ffmpeg")),
        "beat": (stage("beat", lambda: audio.beat(str(work / "audio.16k.wav")))
                 if asked("beat") and tier["beat"] else status.skipped("未勾选 BPM/卡点或该档位不测", method="librosa")),
    }

    editing = _editing(shots_item, audio_pack, transcript)
    copywriting = (stage("copywriting", lambda: text_mod.build(transcript))
                   if asked("copywriting") else status.skipped("未勾选文案结构", method="text"))
    abstract = (stage("summary", lambda: text_mod.summary(transcript, duration_s=duration))
                if asked("summary") else status.skipped("未勾选全文摘要", method="text"))
    if not asked("visual", "subtitle_style", "on_screen_text"):
        visual = status.skipped("未勾选画面理解（不需要视觉模型）", method="vlm")
    elif args.no_vision:
        visual = status.skipped("调用方用 --no-vision 关掉了视觉描述", method="vlm")
    elif frames_item.get("status") != status.MEASURED:
        visual = status.skipped("没有抽到帧，无法做视觉描述", method="vlm")
    else:
        visual = stage("vision", lambda: vision.describe_frames(
            frames_item.get("frames") or [], allow_cloud=args.allow_cloud,
            budget=int(args.vision_budget or tier["vision"])))
        if visual.get("status") == status.MEASURED:
            visual["summary"] = vision.summarise(visual)

    payload = report.assemble(
        source=source, probe=info, shots_item=shots_item, frames_item=frames_item,
        transcript=transcript, audio=audio_pack, editing=editing, visual=visual,
        copywriting=copywriting, abstract=abstract, requested=requested,
        tier=args.tier, plan_id=args.plan_id or "",
        timing={**timing, "total_s": round(time.time() - started, 1)},
        evidence={"ffmpeg": paths.ffmpeg_path(), "ffprobe": paths.ffprobe_path(),
                  "whisper_cpp": paths.whisper_cpp(), "ytdlp": sources.ytdlp_path(),
                  "python": sys.version.split()[0]},
    )
    written = report.write(payload, out, transcript_limit=args.transcript_limit)
    print(json.dumps({"ok": payload["ok"], "tier": args.tier, **written,
                      "missing_required": payload.get("missing_required") or [],
                      "timing": payload["timing"]}, ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 1


def vision_probe_command(args) -> int:
    """逐个验证视觉 provider 是否"真的看得见"（四张已知答案图，数字带随机 nonce）。"""
    items = vision.chain(allow_cloud=args.allow_cloud)
    if not items:
        print(json.dumps({"ok": False, "status": status.SKIPPED,
                          "reason": "没有可验证的 provider（设 AMDGPU_API_KEY 或 BREAKDOWN_VISION_CHAIN）",
                          "hint": "视觉链支持 openai 兼容端点 / CLI 模板 / 本地 VLM"},
                         ensure_ascii=False, indent=2))
        return 1
    rows = []
    for provider in items:
        verdict = vision.verify(provider, force=args.force, quick=args.quick)
        rows.append({"provider": provider.get("name"), "tier": provider.get("tier"),
                     "ok": bool(verdict.get("ok")), "cached": verdict.get("cached"),
                     "elapsed_s": verdict.get("elapsed_s"),
                     "reason": verdict.get("reason"),
                     "cases": verdict.get("cases")})
    usable = [row["provider"] for row in rows if row["ok"]]
    payload = {"ok": bool(usable), "usable": usable, "results": rows,
               "chain": [provider.get("name") for provider in items]}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if usable else 1


def info_command(args) -> int:
    """只读探测一个媒体文件（不下载、不抽帧、不调用模型）——给"拆解计划"估时用。"""
    result = probe_mod.probe(args.source)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == status.MEASURED else 1


def list_command(args) -> int:
    """列出产物目录里的作业（面板左侧列表用）。只读，不碰网络。"""
    root = Path(args.out)
    limit = max(1, min(int(args.limit or 30), 200))
    rows: list[dict] = []
    if root.is_dir():
        jobs = sorted((p for p in root.iterdir() if (p / "job.json").is_file()),
                      key=lambda p: (p / "job.json").stat().st_mtime, reverse=True)
        for job_dir in jobs[:limit]:
            try:
                job = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            report_doc = None
            if (job_dir / "breakdown.json").is_file():
                try:
                    report_doc = json.loads((job_dir / "breakdown.json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    report_doc = None
            source = (report_doc or {}).get("source") or {}
            conclusion = (report_doc or {}).get("conclusion") or {}
            rows.append({
                "job_id": job_dir.name,
                "status": job.get("status"),
                "tier": job.get("tier") or (report_doc or {}).get("tier"),
                "started_at": job.get("started_at"),
                "ended_at": job.get("ended_at"),
                "title": source.get("title") or source.get("url") or job_dir.name,
                "duration_s": ((report_doc or {}).get("probe") or {}).get("duration_s"),
                "shots": len((report_doc or {}).get("shots") or []),
                "ok": (report_doc or {}).get("ok"),
                "narration": (report_doc or {}).get("narration"),
                "conclusion": conclusion.get("type"),
                "total_s": ((report_doc or {}).get("timing") or {}).get("total_s"),
                "has_report": report_doc is not None,
            })
    print(json.dumps({"ok": True, "count": len(rows), "root": str(root), "jobs": rows},
                     ensure_ascii=False, indent=2))
    return 0


def report_command(args) -> int:
    path = Path(args.dir) / "breakdown.json"
    if not path.is_file():
        print(json.dumps({"ok": False, "error": f"没有 {path}"}, ensure_ascii=False))
        return 2
    payload = json.loads(path.read_text(encoding="utf-8"))
    if args.section:
        node = payload.get(args.section)
        print(json.dumps(node if node is not None else {"error": "没有这一段"},
                         ensure_ascii=False, indent=2))
        return 0 if node is not None else 2
    if args.markdown:
        print(report.render_markdown(payload, transcript_limit=args.transcript_limit))
        return 0
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def frames_command(args) -> int:
    path = Path(args.dir) / "breakdown.json"
    if not path.is_file():
        print(json.dumps({"ok": False, "error": f"没有 {path}"}, ensure_ascii=False))
        return 2
    payload = json.loads(path.read_text(encoding="utf-8"))
    frames_item = payload.get("frames") or {}
    print(json.dumps({"ok": frames_item.get("status") == status.MEASURED,
                      "dir": frames_item.get("dir"),
                      "frames": frames_item.get("frames") or []}, ensure_ascii=False, indent=2))
    return 0


def clean_command(args) -> int:
    target = Path(args.dir)
    if not target.exists():
        print(json.dumps({"ok": True, "removed": False, "reason": "不存在"}, ensure_ascii=False))
        return 0
    shutil.rmtree(target, ignore_errors=True)
    print(json.dumps({"ok": True, "removed": True, "dir": str(target)}, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reelens",
                                     description="视频/音频拆解：镜头、抽帧、口播、节奏、音频")
    sub = parser.add_subparsers(dest="command", required=True)

    p_probe = sub.add_parser("probe", help="自检：外部工具与可选依赖")
    p_probe.set_defaults(func=probe_command)

    p_info = sub.add_parser("info", help="只读探测一个媒体文件（给计划估时用）")
    p_info.add_argument("source")
    p_info.set_defaults(func=info_command)

    p_list = sub.add_parser("list", help="列出产物目录里的作业（只读）")
    p_list.add_argument("--out", required=True, help="产物根目录")
    p_list.add_argument("--limit", type=int, default=30)
    p_list.set_defaults(func=list_command)

    p_vision = sub.add_parser("vision-probe", help="验证视觉 provider（已知答案图，含负样本）")
    p_vision.add_argument("--force", action="store_true", help="忽略 24h 缓存重新探测")
    p_vision.add_argument("--quick", action="store_true", help="只跑 2 张（数字 + 负样本）")
    p_vision.add_argument("--allow-cloud", action="store_true")
    p_vision.set_defaults(func=vision_probe_command)

    p_run = sub.add_parser("run", help="拆解一条来源（URL 或本地文件）")
    p_run.add_argument("source")
    p_run.add_argument("--out", required=True)
    p_run.add_argument("--tier", choices=list(TIERS), default="standard")
    p_run.add_argument("--items", default="",
                       help="逗号分隔的条目白名单（如 transcript,copywriting）；留空=全量")
    p_run.add_argument("--frames", type=int, default=0, help="覆盖该档位的抽帧上限")
    p_run.add_argument("--max-seconds", type=float, default=DEFAULT_MAX_SECONDS)
    p_run.add_argument("--allow-long", action="store_true", help="允许超过时长上限")
    p_run.add_argument("--max-height", type=int, default=480, help="URL 取源的最高分辨率")
    p_run.add_argument("--asr-engine", default="", choices=["", "embedded", "whisper.cpp", "faster-whisper"])
    p_run.add_argument("--no-asr", action="store_true")
    p_run.add_argument("--no-vision", action="store_true")
    p_run.add_argument("--vision-budget", type=int, default=0)
    p_run.add_argument("--allow-cloud", action="store_true",
                       help="允许把帧发往付费云端视觉模型（默认只在免费/本地档内降级）")
    p_run.add_argument("--plan-id", default="")
    p_run.add_argument("--transcript-limit", type=int, default=0)
    p_run.set_defaults(func=run_command)

    p_report = sub.add_parser("report", help="读一份已产出的报告")
    p_report.add_argument("dir")
    p_report.add_argument("--section", default="")
    p_report.add_argument("--markdown", action="store_true")
    p_report.add_argument("--transcript-limit", type=int, default=0)
    p_report.set_defaults(func=report_command)

    p_frames = sub.add_parser("frames", help="列出抽帧")
    p_frames.add_argument("dir")
    p_frames.set_defaults(func=frames_command)

    p_clean = sub.add_parser("clean", help="删除一个产物目录")
    p_clean.add_argument("dir")
    p_clean.set_defaults(func=clean_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    paths.ensure_tmp()
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print(json.dumps({"ok": False, "error": "已中断"}, ensure_ascii=False))
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI 边界必须给 JSON 而不是 traceback
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                         ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
