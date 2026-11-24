"""报告装配：把各阶段的测量结果拼成 `breakdown.json` + `report.md`。

两条纪律
--------
1. **`ok` 只代表"该测的都测到了且可信"**。任一 required 项不是 MEASURED/DERIVED，
   就 `ok=false` 并以非零退出码返回——宁可让调用方看见失败，也不产出一份
   "看起来完整"的报告。
2. **`unknowns[]` 由 `status.collect_unknowns` 自动汇总**，不靠人工维护；
   收尾时如果发现"非结论项没进 unknowns"，那是 bug 不是数据问题。

报告顺序按用户要求：源信息 → 字幕/口播全文 → 镜头表 → 构图与画面文案（视觉段）
→ 剪辑手法 → 配音 → 音频/BGM → 未测项 → 证据。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import status

SCHEMA = "video-breakdown.v1"

#: 报告里必须成立才能算 ok 的项（缺一个就不是"完整拆解"）。
#: 注意 `shots` 是**列表**、其状态在 `shots_meta` 里——把两者分开判，避免
#: "列表非空但每项都是 unknown" 被误判为通过。
REQUIRED = ("probe", "shots", "frames", "transcript")


def is_no_speech(item: dict | None) -> bool:
    """这一项是不是"片子里本来就没有人声"的**结论**（而不是转写失败）。

    纯音乐 / 纯 MG 的片子是很常见的输入。把它们判成"报告不完整"是错的：
    该报告的每一项都测到了，只是其中一项的正确答案是"没有人声"。
    """
    if not isinstance(item, dict):
        return False
    if item.get("speech_present") is False:
        return True
    return item.get("status") == status.SKIPPED and "no_speech" in str(item.get("reason") or "")


def narration_state(transcript: dict | None) -> str:
    if is_no_speech(transcript):
        return "absent"
    if status.is_conclusive(transcript or {}):
        return "present"
    return "unknown"


#: 需要抽帧的条目（视觉三件套都离不开帧）
FRAME_ITEMS = ("frames", "visual", "subtitle_style", "on_screen_text")
#: 需要口播的条目
TEXT_ITEMS = ("transcript", "copywriting", "voice", "summary")


def _missing_required(payload: dict, requested=None) -> list[str]:
    """只在**用户真正要求的条目**上判"报告是否完整"。

    为什么要带 requested（2026-11-23 用户实测）：面板上只勾"字幕/口播"时，
    抽帧与镜头本来就不该做；如果还按全量清单判，`ok` 永远 false——
    等于逼着用户去补他没要的东西。
    """
    want = None if requested is None else set(requested)
    problems: list[str] = []

    def asked(*names: str) -> bool:
        return want is None or bool(want.intersection(names))

    if not status.is_conclusive(payload.get("probe")):
        problems.append("probe")
    if asked("shots", *FRAME_ITEMS):
        meta = payload.get("shots_meta") or {}
        if not (status.is_conclusive(meta) and (payload.get("shots") or [])):
            problems.append("shots")
    if asked(*FRAME_ITEMS):
        if not status.is_conclusive(payload.get("frames")):
            problems.append("frames")
    if asked(*TEXT_ITEMS):
        transcript = payload.get("transcript")
        # "没有人声"是结论，不是缺项；只有既没测到、又不是"没人声"才算缺
        if not status.is_conclusive(transcript) and not is_no_speech(transcript):
            problems.append("transcript")
    return problems


def conclusion(payload: dict) -> dict:
    """一句话结论 + 3 个依据。**全部由已测到的字段推出**（DERIVED），不调模型。

    为什么需要它：用户拿到一份"源信息/字幕/镜头/音频"的字段表，还得自己在脑子里拼成
    "这是什么片子"。这条把拼装结果写出来，并列出它依据的字段，读者可以反驳。
    """
    probe = payload.get("probe") or {}
    editing = payload.get("editing") or {}
    rhythm = editing.get("rhythm") or {}
    frame_diff = editing.get("frame_diff") or {}
    visual = payload.get("visual") or {}
    transcript = payload.get("transcript") or {}
    narration = narration_state(transcript)

    shot_rate = float(frame_diff.get("shot_rate_per_min") or rhythm.get("cut_rate_per_min") or 0)
    change_rate = float(frame_diff.get("change_rate_per_min") or 0)
    degenerate = bool(frame_diff.get("degenerate"))
    subtitle_ratio = float((((visual.get("summary") or {}) or {}).get("subtitle_ratio")) or 0)
    texts = ((visual.get("summary") or {}) or {}).get("on_screen_text") or []

    signals = {
        "narration": narration,
        "shot_rate_per_min": round(shot_rate, 2),
        "change_rate_per_min": round(change_rate, 2),
        "shot_detection_degenerate": degenerate,
        "subtitle_frame_ratio": subtitle_ratio,
        "on_screen_texts": len(texts),
    }

    if narration == "absent" and degenerate:
        kind = "纯 MG / 字幕驱动（无口播，靠画面元素与字幕推进）"
    elif narration == "absent" and shot_rate < 2:
        kind = "无口播的低速画面（可能是一镜到底或静态演示）"
    elif narration == "present" and shot_rate >= 4:
        kind = "实拍/口播为主（切点密集）"
    elif narration == "present" and degenerate:
        kind = "口播 + 字幕驱动（话在讲，画面靠元素变化）"
    elif narration == "present":
        kind = "口播为主、画面变化较少"
    elif narration == "absent":
        kind = "无口播，画面按常规节奏切换"
    else:
        kind = "无法判定（口播状态未知）"

    highlights = []
    if change_rate:
        highlights.append(f"帧差 {change_rate} 次/分显著变化")
    if degenerate:
        highlights.append(f"但镜头切分只有 {shot_rate} 切/分（检测退化，别当成剪辑很慢）")
    if subtitle_ratio >= 0.5:
        highlights.append(f"{round(subtitle_ratio * 100)}% 的采样帧带字幕/花字")
    if texts:
        highlights.append(f"抽到 {len(texts)} 条画面文案")
    if narration == "absent":
        highlights.append("全片无人声")

    item = status.field(
        status.DERIVED, method="由 probe/editing/visual/transcript 组合判定", calibrated=False,
        type=kind, mode="纯画面" if narration != "present" else "口播驱动",
        one_liner=f"{kind}。" + ("；".join(highlights) if highlights else ""),
        signals=signals,
        duration_s=probe.get("duration_s"),
        orientation="竖" if probe.get("portrait") else "横" if probe.get("portrait") is False else None,
    )
    return item


def assemble(*, source: dict, probe: dict, shots_item: dict, frames_item: dict,
             transcript: dict, audio: dict, editing: dict, visual: dict,
             tier: str, plan_id: str, timing: dict, evidence: dict,
             copywriting: dict | None = None, abstract: dict | None = None,
             requested=None) -> dict:
    payload = {
        "schema": SCHEMA,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "tier": tier,
        "plan_id": plan_id,
        "source": source,
        "probe": probe,
        "shots": (shots_item.get("shots") or []),
        "shots_meta": {k: v for k, v in shots_item.items() if k != "shots"},
        "frames": frames_item,
        "transcript": transcript,
        "audio": audio,
        "editing": editing,
        "visual": visual,
        "copywriting": copywriting if copywriting is not None else status.skipped("未生成文案结构"),
        "abstract": abstract if abstract is not None else status.skipped("未生成摘要（未勾选 summary）"),
        "timing": timing,
        "evidence": evidence,
    }
    payload["unknowns"] = status.collect_unknowns(payload)
    payload["narration"] = narration_state(payload.get("transcript"))
    payload["conclusion"] = conclusion(payload)
    payload["requested_items"] = list(requested) if requested else None
    missing = _missing_required(payload, requested)
    payload["ok"] = not missing
    if missing:
        payload["missing_required"] = missing
    payload["summary"] = status.summarise(
        [value for value in _walk_statuses(payload)])
    return payload


def _walk_statuses(node):
    if isinstance(node, dict):
        if node.get("status"):
            yield node
        for value in node.values():
            yield from _walk_statuses(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_statuses(value)


def _fmt_ts(seconds: float) -> str:
    total = max(0, int(round(float(seconds or 0))))
    return f"{total // 60:02d}:{total % 60:02d}"


def render_markdown(payload: dict, *, transcript_limit: int = 0) -> str:
    probe = payload.get("probe") or {}
    lines: list[str] = []
    lines.append(f"# 视频拆解报告 · {payload.get('source', {}).get('title') or '未命名'}")
    lines.append("")
    lines.append(f"- 生成时间：{payload.get('generated_at')}　档位：`{payload.get('tier')}`")
    lines.append(f"- schema：`{payload.get('schema')}`　plan：`{str(payload.get('plan_id'))[:16]}`")
    flags = "✅ ok" if payload.get("ok") else "⚠️ 未通过（缺 " + ", ".join(payload.get("missing_required") or []) + "）"
    lines.append(f"- 结论：{flags}")
    summary = payload.get("summary") or {}
    lines.append("- 状态计数：" + " · ".join(f"{k}={v}" for k, v in summary.items() if v))
    narration = payload.get("narration") or narration_state(payload.get("transcript"))
    lines.append("- 口播：" + {"absent": "**无人声**（纯音乐/纯画面）", "present": "有人声",
                            "unknown": "未判定"}.get(narration, narration))
    conclusion_item = payload.get("conclusion") or {}
    if conclusion_item.get("one_liner"):
        lines.append("")
        lines.append(f"> **{conclusion_item['one_liner']}**")
        signals = conclusion_item.get("signals") or {}
        lines.append(">")
        lines.append("> 依据：" + "、".join(f"{key}={value}" for key, value in signals.items()))
    lines.append("")

    lines.append("## 1. 源信息")
    lines.append("")
    if probe.get("status") == status.MEASURED:
        orient = "竖" if probe.get("portrait") else "横"
        lines.append(f"- {probe.get('display_width')}×{probe.get('display_height')}（{orient}）"
                     f" · {probe.get('fps')}fps · {probe.get('duration_s')}s · `{probe.get('codec')}`"
                     f" · 音轨 {'有' if probe.get('has_audio') else '无'}")
        if probe.get("subtitle_tracks"):
            lines.append(f"- 内嵌字幕轨：{len(probe['subtitle_tracks'])} 条")
        mismatch = (payload.get("source") or {}).get("duration_mismatch")
        if mismatch:
            lines.append(f"- ⚠️ **只下载到 {mismatch['actual_s']}s，视频实际 {mismatch['expected_s']}s**"
                         f"（{mismatch['reason']}）——后面的结论只代表这一段")
    else:
        lines.append(f"- ✗ 探测失败：{probe.get('reason')}")
    if payload.get("source", {}).get("url"):
        lines.append(f"- 来源：{payload['source']['url']}")
    lines.append("")

    lines.append("## 2. 字幕 / 口播")
    lines.append("")
    transcript = payload.get("transcript") or {}
    if is_no_speech(transcript):
        lines.append(f"- **本片没有人声**：{transcript.get('reason')}")
        lines.append("- 因此这里不提供逐句口播；文字信息请看第 4 节的画面文案（on_screen_text）。")
    elif transcript.get("status") == status.MEASURED:
        quality = transcript.get("quality_report") or {}
        lines.append(f"- 来源：`{transcript.get('engine')}` · {quality.get('entries')} 段"
                     f" · 覆盖 {round(float(quality.get('coverage') or 0) * 100)}%"
                     f" · 独立句占比 {quality.get('unique_ratio')}")
        if quality.get("quality") == "suspect":
            lines.append(f"- ⚠️ **疑似幻觉**：{transcript.get('reason')}")
        entries = transcript.get("entries") or []
        shown = entries if not transcript_limit else entries[:transcript_limit]
        lines.append("")
        lines.append("```text")
        for entry in shown:
            lines.append(f"[{_fmt_ts(entry['from'])}–{_fmt_ts(entry['to'])}] {entry['content']}")
        if transcript_limit and len(entries) > transcript_limit:
            lines.append(f"…（共 {len(entries)} 段，已截断）")
        lines.append("```")
    else:
        lines.append(f"- ✗ 未取得口播：{transcript.get('status')} — {transcript.get('reason')}")
    lines.append("")

    lines.append("## 3. 镜头表")
    lines.append("")
    shots_meta = payload.get("shots_meta") or {}
    if shots_meta.get("status") == status.MEASURED:
        lines.append(f"- 方法：{shots_meta.get('method')}　耗时 {shots_meta.get('timing_s')}s"
                     f"　阈值 {shots_meta.get('params', {}).get('threshold')}")
        rhythm = payload.get("editing", {}).get("rhythm") or {}
        if rhythm.get("status") == status.DERIVED:
            lines.append(f"- 节奏：{rhythm.get('shots')} 镜 · 平均 {rhythm.get('mean')}s"
                         f" · 中位 {rhythm.get('median')}s · p90 {rhythm.get('p90')}s"
                         f" · 每分钟 {rhythm.get('cut_rate_per_min')} 切")
        lines.append("")
        lines.append("| id | 起 | 止 | 时长 | 切点分 | 用光均值 | 抖动 | 细节 | 裁决 |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for shot in payload.get("shots") or []:
            quality = shot.get("quality") or {}
            lines.append("| {} | {:.2f} | {:.2f} | {:.2f} | {} | {} | {} | {} | {} |".format(
                shot.get("id"), shot.get("start", 0), shot.get("end", 0), shot.get("dur", 0),
                shot.get("cut_score"), quality.get("yavg"), quality.get("shake"),
                quality.get("edge"), shot.get("verdict")))
    else:
        lines.append(f"- ✗ 切分失败：{shots_meta.get('status')} — {shots_meta.get('reason')}")
    lines.append("")

    lines.append("## 4. 帧与画面")
    lines.append("")
    frames_item = payload.get("frames") or {}
    if frames_item.get("status") == status.MEASURED:
        lines.append(f"- 抽帧：{frames_item.get('kept')} 张（去重丢弃 {frames_item.get('dropped')} 张）"
                     f" → `{frames_item.get('dir')}`")
        sheet = frames_item.get("contact_sheet") or {}
        if sheet.get("status") == status.MEASURED:
            lines.append(f"- 对照图：`{sheet.get('path')}`")
    else:
        lines.append(f"- ✗ 抽帧失败：{frames_item.get('reason')}")
    visual = payload.get("visual") or {}
    lines.append(f"- 构图/版式/画面文案：{visual.get('status')} — {visual.get('reason') or '见 visual 段'}")
    lines.append("")

    abstract = payload.get("abstract") or {}
    if abstract.get("status") == status.DERIVED:
        lines.append("## 2b. 摘要（抽取式）")
        lines.append("")
        lines.append(f"- {abstract.get('sentence_count')} 句 · {abstract.get('chars')} 字 · "
                     f"{abstract.get('duration_s')}s · 分 {(abstract.get('params') or {}).get('sections')} 段取要点")
        for row in abstract.get("outline") or []:
            lines.append(f"- `{row['from']}s` {row['point']}")
        if abstract.get("keywords"):
            lines.append("- 关键词（字面统计）：" + "、".join(
                f"{item['word']}×{item['count']}" for item in abstract["keywords"][:10]))
        if abstract.get("key_numbers"):
            lines.append("- 关键数字：" + "、".join(
                item.get("literal", "") for item in abstract["key_numbers"][:8]))
        lines.append(f"- 方法：{abstract.get('method')}（**不是模型写的总结，可回原文核对**）")
        lines.append("")

    lines.append("## 5. 剪辑手法")
    lines.append("")
    editing = payload.get("editing") or {}
    frame_diff = editing.get("frame_diff") or {}
    if frame_diff.get("status") == status.DERIVED:
        lines.append(f"- 帧差节奏：**{frame_diff.get('change_rate_per_min')} 次/分**显著变化"
                     f"（镜头切分 {frame_diff.get('shot_rate_per_min')} 切/分）")
        if frame_diff.get("degenerate"):
            lines.append(f"  - ⚠️ {frame_diff.get('degenerate_reason')}")
    transitions = editing.get("transitions") or {}
    lines.append(f"- 转场识别：{transitions.get('status')} — {transitions.get('reason') or ''}")
    beat_item = editing.get("beat_sync") or {}
    lines.append(f"- 卡点：{beat_item.get('status')} — "
                 f"{beat_item.get('reason') or '命中率 ' + str(beat_item.get('ratio'))}")
    lines.append("")

    copywriting = payload.get("copywriting") or {}
    lines.append("## 5b. 文案结构")
    lines.append("")
    if copywriting.get("status") in (status.DERIVED, status.MEASURED, status.ASSUMED):
        if copywriting.get("status") == status.ASSUMED:
            lines.append(f"- ⚠️ **仅供参考**：{copywriting.get('reason')}")
        lines.append(f"- 钩子（前 12s 原文）：{copywriting.get('hook') or '（空）'}")
        lines.append(f"- 句数 {copywriting.get('sentences')} · 每分钟 {copywriting.get('chars_per_min')} 字"
                     f" · 问句占比 {copywriting.get('question_ratio')}"
                     f" · 分段 {len(copywriting.get('sections') or [])}")
        if copywriting.get("enumerations"):
            lines.append(f"- 结构标记：{'、'.join(copywriting['enumerations'])}")
        if copywriting.get("cta"):
            lines.append(f"- 引导语命中：{'、'.join(copywriting['cta'])}")
        points = (copywriting.get("datapoints") or {})
        if points.get("status") in (status.DERIVED, status.MEASURED):
            lines.append(f"- 数据点 {points.get('count')} 个："
                         + "、".join(item["literal"] for item in (points.get("points") or [])[:8]))
        else:
            lines.append(f"- 数据点：{points.get('status')} — {points.get('reason')}")
        lines.append(f"- 方法：{copywriting.get('method')}（**确定性推导，非模型判断**）")
    else:
        lines.append(f"- {copywriting.get('status')} — {copywriting.get('reason')}")
    lines.append("")

    lines.append("## 6. 配音")
    lines.append("")
    voice = editing.get("voice") or {}
    lines.append(f"- {voice.get('status')} — {voice.get('reason') or ''}")
    if voice.get("status") == status.DERIVED:
        lines.append(f"- 语速 {voice.get('chars_per_s')} 字/秒 · 停顿 {voice.get('pause_ratio')}"
                     f" · 段均 {voice.get('mean_segment_s')}s（**推断**，非实测）")
    lines.append("")

    lines.append("## 7. 音频 / BGM")
    lines.append("")
    loud = (payload.get("audio") or {}).get("loudness") or {}
    if loud.get("status") == status.MEASURED:
        lines.append(f"- 响度 {loud.get('input_i')} LUFS · 真峰 {loud.get('input_tp')} dBTP"
                     f" · LRA {loud.get('input_lra')}")
    else:
        lines.append(f"- 响度：{loud.get('status')} — {loud.get('reason')}")
    silence = (payload.get("audio") or {}).get("silence") or {}
    lines.append(f"- 静音段：{silence.get('count')} 段（共 {silence.get('total_s')}s）"
                 if silence.get("status") == status.MEASURED else
                 f"- 静音段：{silence.get('status')} — {silence.get('reason')}")
    beat_item = (payload.get("audio") or {}).get("beat") or {}
    lines.append(f"- BPM：{beat_item.get('bpm')}" if beat_item.get("status") == status.MEASURED
                 else f"- BPM：{beat_item.get('status')} — {beat_item.get('reason')}")
    lines.append("")

    lines.append("## 8. 未测与失败项")
    lines.append("")
    unknowns = payload.get("unknowns") or []
    if not unknowns:
        lines.append("- （无）")
    for item in unknowns[:60]:
        lines.append(f"- `{item['field']}` — {item['code']}：{item['reason']}")
    if len(unknowns) > 60:
        lines.append(f"- …还有 {len(unknowns) - 60} 条，见 breakdown.json")
    lines.append("")

    lines.append("## 9. 证据")
    lines.append("")
    for key, value in (payload.get("evidence") or {}).items():
        lines.append(f"- {key}: `{value}`")
    timing = payload.get("timing") or {}
    lines.append(f"- 耗时：{json.dumps(timing, ensure_ascii=False)}")
    lines.append("")
    return "\n".join(lines)


def write(payload: dict, out_dir: str | Path, *, transcript_limit: int = 0) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / "breakdown.json"
    md_path = out / "report.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(payload, transcript_limit=transcript_limit), encoding="utf-8")
    return {"ok": bool(payload.get("ok")), "json": str(json_path), "markdown": str(md_path),
            "unknowns": len(payload.get("unknowns") or []),
            "missing_required": payload.get("missing_required") or []}
