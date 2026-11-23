"""字幕/口播：三条来源按"免费且最快"排序，并给两道质量护栏。

来源顺序（为什么）
------------------
1. **内嵌字幕轨**（mkv/mp4 的软字幕）：`ffmpeg -map 0:s:0` 直接吐 SRT，**亚秒级**。
   B 站 CC 需要通过 `yt-dlp --sub-langs` 或私有桥单独取，不在这里。
2. **whisper.cpp**：本机已有二进制 + ggml 模型时可用，CPU 实测 2.9× 实时。
3. **faster-whisper**：CTranslate2 int8，CPU 实测 **7.5× 实时**（171s → 22.8s），
   是 10 分钟以上视频的默认引擎。

两道质量护栏（都是实测踩出来的）
--------------------------------
- **VAD 静默吞音**：`vad_filter=True` 在音乐/低信噪比音频上会 0.4 秒返回 **0 段**，
  看上去像"这段没有语音"。→ 先看音频能量，0 段但能量正常就**关掉 VAD 重跑一次**，
  并在结果里记 `vad_fallback`。
- **幻觉重复**：Whisper 在音乐/静音段会反复吐同一句（实测："字幕志愿者 杨茜茜"）。
  → 算 `unique_ratio`；`<0.35` 且段数 ≥4 时标 `quality=suspect`，
  报告里**不许**把 suspect 文本喂给文案分析（会污染下游结论）。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import wave
from pathlib import Path

from . import paths, status

#: 单次 ASR 的默认超时（秒）。长片靠 `max_seconds` 限制，不靠无限等。
DEFAULT_TIMEOUT_S = 3600.0
#: 认为"有语音能量"的 RMS 下限（16bit PCM，满量程 32767）。实测安静说话约 3000–8000。
SILENT_RMS = 400.0
#: 判定"疑似幻觉"的独立句占比下限。
SUSPECT_UNIQUE_RATIO = 0.35
#: 覆盖率低于这个值 → 最差也只能是 DERIVED。
LOW_COVERAGE = 0.6

_SRT_RE = re.compile(
    r"\d+\s*\n(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})\s*\n"
    r"(.*?)(?=\n\s*\n|\Z)", re.S)


def _stamp_seconds(stamp: str) -> float:
    hours, minutes, rest = stamp.split(":")
    parts = re.split(r"[,.]", rest)
    seconds = parts[0]
    millis = parts[1] if len(parts) > 1 else "0"
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis.ljust(3, "0")) / 1000.0


def parse_srt(text: str) -> list[dict]:
    out: list[dict] = []
    for start, end, body in _SRT_RE.findall(str(text or "")):
        content = " ".join(line.strip() for line in body.splitlines() if line.strip())
        if content:
            out.append({"from": round(_stamp_seconds(start), 2),
                        "to": round(_stamp_seconds(end), 2), "content": content})
    return out


def wav_energy(path: str | Path) -> float:
    """粗略 RMS（只用来判断"是不是根本没声音"，不做响度测量）。"""
    try:
        with wave.open(str(path), "rb") as handle:
            frames = handle.readframes(min(handle.getnframes(), 16000 * 60))
            width = handle.getsampwidth()
    except (OSError, EOFError, wave.Error):
        return -1.0
    if width != 2 or not frames:
        return -1.0
    import array
    samples = array.array("h")
    samples.frombytes(frames[: len(frames) - (len(frames) % 2)])
    stride = max(1, len(samples) // 200000)
    chunk = samples[::stride]
    if not chunk:
        return -1.0
    return (sum(float(x) * x for x in chunk) / len(chunk)) ** 0.5


def _run(command: list[str], *, timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, env=paths.child_env())


def embedded_subtitles(media: str | Path, *, work_dir: str | Path,
                       timeout: float = 120.0) -> dict:
    """抽第一条内嵌字幕轨 → SRT → 条目。没有字幕轨就 SKIPPED（不是失败）。"""
    binary = paths.ffmpeg_path()
    if not binary:
        return status.skipped("找不到 ffmpeg", method="ffmpeg -map 0:s")
    target = Path(work_dir) / "embedded.srt"
    Path(work_dir).mkdir(parents=True, exist_ok=True)
    try:
        _run([binary, "-hide_banner", "-loglevel", "error", "-y", "-i", str(media),
              "-map", "0:s:0", "-c:s", "srt", str(target)], timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return status.skipped(f"抽字幕轨失败：{type(exc).__name__}", method="ffmpeg -map 0:s")
    if not target.is_file() or target.stat().st_size == 0:
        return status.skipped("没有内嵌字幕轨", method="ffmpeg -map 0:s")
    entries = parse_srt(target.read_text(encoding="utf-8", errors="replace"))
    if not entries:
        return status.skipped("字幕轨存在但解析不出条目", method="ffmpeg -map 0:s")
    return status.field(status.MEASURED, method="ffmpeg -map 0:s:0", source="embedded",
                        entries=entries)


def _quality(entries: list[dict], duration_s: float) -> dict:
    if not entries:
        return {"unique_ratio": 0.0, "coverage": 0.0, "quality": "empty"}
    texts = [e["content"].strip() for e in entries if e.get("content")]
    unique = len({t for t in texts})
    ratio = unique / max(len(texts), 1)
    covered = sum(max(0.0, float(e["to"]) - float(e["from"])) for e in entries)
    coverage = covered / duration_s if duration_s > 0 else 0.0
    quality = "ok"
    if len(texts) >= 4 and ratio < SUSPECT_UNIQUE_RATIO:
        quality = "suspect"
    elif duration_s > 0 and coverage < LOW_COVERAGE * 0.5:
        quality = "suspect"
    return {"unique_ratio": round(ratio, 3), "coverage": round(coverage, 3),
            "quality": quality, "entries": len(texts), "unique": unique}


def run_whisper_cpp(wav: str | Path, *, timeout: float) -> dict:
    binary = paths.whisper_cpp()
    model = paths.whisper_model()
    if not binary or not model:
        return status.skipped(
            "whisper.cpp 未配置（BREAKDOWN_WHISPER_CPP + BREAKDOWN_WHISPER_MODEL）",
            method="whisper-cli")
    out_base = Path(wav).with_suffix(".cpp")
    # `-l auto`：语言必须自动检测。硬编码 zh 在粤语/英文素材上会直接产出幻觉
    # （2026-11-23 实测：一条粤语风声的片子被硬编成中文后，输出整段"明镜与点点"）。
    command = [binary, "-m", model, "-f", str(wav), "-oj", "-of", str(out_base), "-l", "auto"]
    if paths.whisper_no_gpu():
        command.append("-ng")
    started = time.time()
    try:
        proc = _run(command, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return status.failed(f"{type(exc).__name__}: {exc}", method="whisper-cli")
    if proc.returncode != 0:
        return status.failed(f"whisper-cli exit {proc.returncode}", method="whisper-cli")
    doc_path = Path(str(out_base) + ".json")
    if not doc_path.is_file():
        return status.failed("whisper-cli 没有产出 json", method="whisper-cli")
    doc = json.loads(doc_path.read_text(encoding="utf-8"))
    entries = []
    for segment in doc.get("transcription") or []:
        text = str((segment or {}).get("text") or "").strip()
        if not text:
            continue
        offsets = segment.get("offsets") or {}
        entries.append({"from": round(float(offsets.get("from") or 0) / 1000.0, 2),
                        "to": round(float(offsets.get("to") or 0) / 1000.0, 2),
                        "content": text})
    if not entries:
        return status.failed("whisper-cli 输出为空", method="whisper-cli")
    result = doc.get("result") if isinstance(doc.get("result"), dict) else {}
    return status.field(status.MEASURED, method="whisper.cpp", engine="whisper.cpp",
                        entries=entries, speech_present=True,
                        lang=str(result.get("language") or ""),
                        elapsed_s=round(time.time() - started, 1))


def run_faster_whisper(wav: str | Path, *, duration_s: float, timeout: float,
                       model: str = "", compute: str = "", threads: int = 0) -> dict:
    try:
        from faster_whisper import WhisperModel  # noqa: PLC0415 - 可选依赖
    except ImportError:
        return status.skipped("没装 faster-whisper（pip install faster-whisper）",
                              method="faster-whisper")
    model_name = model or os.environ.get("BREAKDOWN_FW_MODEL") or "large-v3-turbo"
    compute_type = compute or os.environ.get("BREAKDOWN_FW_COMPUTE") or "int8"
    cpu_threads = threads or int(os.environ.get("BREAKDOWN_FW_THREADS") or (os.cpu_count() or 4))
    started = time.time()
    try:
        cache = paths.hf_cache_dir()
        # `download_root` 要的是 **hub 目录**（里面才有 `models--org--name/`），
        # 不是 HF_HOME。传错这一层会发生什么：local_files_only 找不到快照 →
        # `LocalEntryNotFoundError` → 静默回落到 whisper.cpp → 转出一堆"me"。
        # 2026-11-23 实测踩过：CLI 里设了 HF_HOME 所以正常，宿主子进程就掉进这个坑。
        hub = cache / "hub" if (cache / "hub").is_dir() else cache
        local_only = any(hub.glob("models--*faster-whisper*"))
        engine = WhisperModel(model_name, device="cpu", compute_type=compute_type,
                              cpu_threads=cpu_threads, download_root=str(hub),
                              local_files_only=local_only)
        load_s = time.time() - started
        entries, meta, speech_present, vad_fallback = _transcribe(
            engine, wav, duration_s=duration_s)
    except Exception as exc:  # noqa: BLE001 - ASR 失败要可读
        return status.failed(f"{type(exc).__name__}: {exc}", method="faster-whisper")

    if not speech_present:
        # 这不是失败，是**结论**：片子里没有人声。报告必须说出来，而不是给一段幻觉。
        item = status.skipped(
            "no_speech_detected：全片与最响 15 秒的 VAD 都没有检出语音（疑似纯音乐/无口播）",
            method="faster-whisper/silero-vad")
        # `terminal` = "这是对素材的结论，不是引擎坏了" → 调用方**不许**换下一个引擎重试。
        # 少了这个标记，"没有语音"会被当成"这个引擎不可用"，于是 whisper.cpp 接着跑，
        # 编出一整段幻觉（2026-11-23 实测：81 秒白跑，还把幻觉写进报告）。
        item["terminal"] = True
        item["speech_present"] = False
        return item
    if not entries:
        return status.failed("faster-whisper 输出为空（VAD 认为有语音，但转写没有内容）",
                             method="faster-whisper")
    elapsed = time.time() - started
    item = status.field(
        status.MEASURED, method=f"faster-whisper/{model_name}/{compute_type}",
        params={"threads": cpu_threads, "compute": compute_type},
        engine="faster-whisper", entries=entries, speech_present=True,
        lang=meta.get("lang") or "", lang_confidence=meta.get("lang_probability"),
        load_s=round(load_s, 1), elapsed_s=round(elapsed, 1),
        realtime_x=round(duration_s / max(elapsed, 0.01), 2) if duration_s else None,
    )
    warnings = []
    if vad_fallback:
        warnings.append("vad_too_aggressive_fallback_to_no_vad")
    if warnings:
        item["warnings"] = warnings
    return item


def _write_slice(wav: str | Path, start_s: float, end_s: float, target: Path) -> bool:
    """从 16k 单声道 wav 里裁一段（纯标准库，不依赖 ffmpeg）。"""
    try:
        with wave.open(str(wav), "rb") as src:
            rate = src.getframerate() or 16000
            width = src.getsampwidth()
            channels = src.getnchannels()
            begin = max(0, int(start_s * rate))
            end = int(end_s * rate)
            src.setpos(min(begin, src.getnframes()))
            frames = src.readframes(max(0, end - begin))
        with wave.open(str(target), "wb") as dst:
            dst.setnchannels(channels)
            dst.setsampwidth(width)
            dst.setframerate(rate)
            dst.writeframes(frames)
        return target.is_file() and target.stat().st_size > 44
    except (OSError, EOFError, wave.Error):
        return False


def _loudest_window(wav: str | Path, *, seconds: float = 15.0) -> tuple[float, float]:
    """找"最响的一段"（秒）。用来避免把"轻声说话"误判成"没有语音"。"""
    try:
        with wave.open(str(wav), "rb") as handle:
            rate = handle.getframerate() or 16000
            width = handle.getsampwidth()
            total = handle.getnframes()
            if width != 2 or total <= rate:
                return 0.0, min(seconds, total / float(rate or 16000))
            stride = max(1, rate // 2)  # 0.5s 一窗
            import array
            scores: list[tuple[float, float]] = []
            position = 0
            while position < total:
                handle.setpos(position)
                chunk = handle.readframes(stride)
                if len(chunk) < 2:
                    break
                samples = array.array("h")
                samples.frombytes(chunk[: len(chunk) - (len(chunk) % 2)])
                if samples:
                    step = max(1, len(samples) // 4000)
                    part = samples[::step]
                    scores.append((position / float(rate),
                                   (sum(float(x) * x for x in part) / len(part)) ** 0.5))
                position += stride
    except (OSError, EOFError, wave.Error):
        return 0.0, seconds
    if not scores:
        return 0.0, seconds
    window = max(1, int(seconds / 0.5))
    best_start, best_score = scores[0][0], -1.0
    for index in range(max(1, len(scores) - window + 1)):
        chunk = scores[index:index + window]
        score = sum(value for _, value in chunk) / len(chunk)
        if score > best_score:
            best_score = score
            best_start = scores[index][0]
    return best_start, min(best_start + seconds, scores[-1][0] + 0.5)


def _transcribe(engine, wav: str | Path, *, duration_s: float) -> tuple[list[dict], dict, bool, bool]:
    """先带 VAD；0 段时**先怀疑"没有语音"**，而不是立刻关掉 VAD。

    这条规则是 2026-11-23 用真实素材换来的：一条纯音乐/无口播的 MG 片子，
    `vad_filter=True` 返回 0 段（**正确答案**），而我原先把"0 段 + 有能量"当成
    "VAD 太激进"，关掉 VAD 重跑，于是 Whisper 编出一整段"请不吝点赞订阅转发…"，
    白花 103 秒还把幻觉写进了报告。

    现在的判定顺序：
    1. 全片带 VAD → 有段就返回；
    2. 0 段且能量过低 → 直接判"没有语音"；
    3. 0 段但有能量 → 在**最响的 15 秒**上复检（防止轻声说话被 VAD 漏掉）；
    4. 复检仍 0 段 → 结论是"没有语音"，**不跑无 VAD 的全片**；
    5. 复检有段 → 才认为 VAD 偏激进，关 VAD 重跑全片。

    @returns (entries, info, speech_present, vad_fallback)
    """
    def once(target: str | Path, vad: bool) -> tuple[list[dict], dict]:
        segments, info = engine.transcribe(str(target), language=None,  # 语言自动检测
                                            vad_filter=vad, beam_size=5, word_timestamps=True)
        rows = []
        for segment in segments:
            text = str(getattr(segment, "text", "") or "").strip()
            if not text:
                continue
            rows.append({"from": round(float(segment.start), 2),
                         "to": round(float(segment.end), 2), "content": text,
                         "words": [{"from": round(float(w.start), 2),
                                    "to": round(float(w.end), 2),
                                    "w": w.word} for w in (getattr(segment, "words", None) or [])]})
        meta = {"lang": str(getattr(info, "language", "") or ""),
                "lang_probability": round(float(getattr(info, "language_probability", 0) or 0), 3),
                "duration_s": round(float(getattr(info, "duration", 0) or 0), 2)}
        return rows, meta

    entries, meta = once(wav, True)
    if entries:
        return entries, meta, True, False
    if wav_energy(wav) <= SILENT_RMS:
        return [], meta, False, False

    start, end = _loudest_window(wav, seconds=15.0)
    slice_path = Path(wav).with_suffix(".probe16k.wav")
    if _write_slice(wav, start, end, slice_path):
        probe, _ = once(slice_path, True)
        try:
            slice_path.unlink(missing_ok=True)
        except OSError:
            pass
        if not probe:
            return [], meta, False, False
    entries, meta = once(wav, False)
    return entries, meta, True, True


def speech_present_probe(wav: str | Path) -> bool | None:
    """"有没有人声"的**引擎无关**判定：用 faster-whisper 自带的 silero VAD。

    silero 的 onnx 随 `faster_whisper` 包一起装，**不需要任何 whisper 权重**，
    所以哪怕模型缓存没下、网络不通，这项判定照样成立。

    @returns True=有人声 / False=没人声 / None=判定不了（缺依赖或音频格式不符）
    """
    try:
        import numpy as np  # noqa: PLC0415
        from faster_whisper.vad import VadOptions, get_speech_timestamps  # noqa: PLC0415
    except ImportError:
        return None
    try:
        with wave.open(str(wav), "rb") as handle:
            if handle.getframerate() != 16000 or handle.getsampwidth() != 2:
                return None
            raw = handle.readframes(handle.getnframes())
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if audio.size < 16000:  # <1 秒，不值得判
            return None
        return bool(get_speech_timestamps(audio, VadOptions()))
    except Exception:  # noqa: BLE001 - 判定不了就返回 None，不阻断主流程
        return None


def transcribe(media: str | Path, *, work_dir: str | Path, duration_s: float,
               has_audio: bool, preferred: str = "", timeout: float = DEFAULT_TIMEOUT_S) -> dict:
    """按"内嵌字幕 → whisper.cpp → faster-whisper"取口播文本。

    @param preferred: 强制指定引擎（`embedded` / `whisper.cpp` / `faster-whisper`）
    @returns 带 `status` 的条目集合，并附 `quality` 护栏结论
    """
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)

    if preferred in ("", "embedded"):
        embedded = embedded_subtitles(media, work_dir=work)
        if embedded.get("status") == status.MEASURED:
            return _with_quality(embedded, duration_s, engine="embedded")
        if preferred == "embedded":
            return embedded

    if not has_audio:
        return status.skipped("没有音轨，无法转写（也没有内嵌字幕）", method="asr")

    wav = work / "audio.16k.wav"
    if not wav.is_file():
        return status.skipped("没有 16kHz 音频（单次解码可能失败）", method="asr")

    # "有没有人声"必须先判、且必须**引擎无关**（2026-11-23 事故）：
    # 原先这项判定藏在 faster-whisper 分支里，一旦该引擎不可用（模型缓存路径不对），
    # 判定就整个失效，流程落到 whisper.cpp 上编出一整段幻觉，报告还说"有人声"。
    presence = speech_present_probe(wav)
    if presence is False:
        item = status.skipped(
            "no_speech_detected：silero VAD 在全片没有检出语音（疑似纯音乐/无口播）",
            method="silero-vad（与转写引擎无关）")
        item["terminal"] = True
        item["speech_present"] = False
        return item

    order = [preferred] if preferred in {"whisper.cpp", "faster-whisper"} else \
        ["faster-whisper", "whisper.cpp"]
    problems: list[str] = []
    for engine in order:
        if engine == "whisper.cpp":
            result = run_whisper_cpp(wav, timeout=timeout)
        else:
            result = run_faster_whisper(wav, duration_s=duration_s, timeout=timeout)
        if result.get("status") == status.MEASURED:
            item = _with_quality(result, duration_s, engine=engine)
            if problems:
                # 有引擎失败过但最终成功了：把原因留在报告里，别让读者以为一切顺利
                item["attempts"] = problems
            return item
        if result.get("terminal"):
            # 对素材的结论（例如"没有人声"）：换引擎只会得到幻觉，停止
            return result
        # 记录每个引擎**为什么**被放弃：否则"回落到 whisper.cpp 转出垃圾"
        # 在报告里看不出根因（2026-11-23 实测：faster-whisper 因模型快照没找到而静默失败）。
        problems.append(f"{engine}: {result.get('status')} {result.get('reason', '')}".strip())
    final = status.failed("所有 ASR 引擎都不可用 —— " + " | ".join(problems), method="asr")
    final["attempts"] = problems
    return final


def _sniff_language(text: str, detected: str) -> tuple[str, str]:
    """用**字面**纠正语言识别，返回 (lang, note)。

    2026-11-23 实测：一条日文节目被 faster-whisper 判成 `ko`（日文里汉字多、音素接近），
    报告里写 `lang=ko` 会让人以为"这是韩语"。字面比概率可靠：
    有平假名/片假名 → ja；有谚文 → ko；只剩汉字 → zh。
    """
    if not text:
        return detected, ""
    if any("\u3040" <= ch <= "\u30ff" for ch in text):   # ひらがな / カタカナ
        if detected and detected != "ja":
            return "ja", f"字面检测修正：{detected} → ja（文本含假名）"
        return "ja", ""
    if any("\uac00" <= ch <= "\ud7af" for ch in text):   # 谚文
        if detected and detected != "ko":
            return "ko", f"字面检测修正：{detected} → ko（文本含谚文）"
        return "ko", ""
    han = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    if han > len(text) * 0.5:
        if detected and detected != "zh":
            return "zh", f"字面检测修正：{detected} → zh（几乎全是汉字）"
        return "zh", ""
    return detected, ""


def _with_quality(item: dict, duration_s: float, *, engine: str) -> dict:
    quality = _quality(item.get("entries") or [], duration_s)
    item["engine"] = engine
    item["quality_report"] = quality
    # 语言以**字面**为准（Whisper 的概率会把日文判成韩文，见 _sniff_language）
    joined = "".join(str(entry.get("content") or "") for entry in item.get("entries") or [])
    lang, note = _sniff_language(joined, str(item.get("lang") or ""))
    if lang:
        item["lang"] = lang
    if note:
        item.setdefault("warnings", []).append(note)
    # 质量可疑时不改变"测到了"这个事实，但把可引用性降级——下游必须看 quality
    if quality.get("quality") == "suspect":
        item["calibrated"] = False
        item["reason"] = ("转写疑似幻觉（独立句占比 {}，覆盖率 {}）——不可直接当正文使用"
                          .format(quality.get("unique_ratio"), quality.get("coverage")))
    return item
