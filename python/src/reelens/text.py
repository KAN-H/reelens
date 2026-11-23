"""文案结构与数据点：**不用模型**，全部由口播文本确定性推导。

为什么不用模型
--------------
"这段文案的钩子是什么"是判断，模型做得更好——但这里刻意先用确定性方法，原因是：
1. 拆解报告是要被信任的产物。模型写出的"钩子"如果错了，读的人无法察觉；
   而"前 12 秒说了什么""每分钟几个问句""有没有关注引导"是可以核对的。
2. 数据点必须**逐字来自原文**（这是本项目在另一个模块里已经付过代价的纪律：
   提示词是请求，机械校验才是保证）。数字由正则抽、再逐个回原文核验，天然满足。
3. 模型不可用时报告仍完整——不会因为"没配 key"就少一整段。

所以这里只做三件事，且每件都能指出依据：
- **骨架**：开头 12 秒原文（钩子）、句数/每分钟句数、问句比例、枚举标记、CTA 标记；
- **分段**：按停顿（句间 >1.2s）与时长把口播切成若干节，节内取首句作"小标题候选"；
- **数据点**：正则抽数字（含"亿/万/%/倍"等单位），过三道机械防线后输出。

状态：全部 `DERIVED`（由文本推算，未经人工标定），并带 `method` 与示例原文。
"""
from __future__ import annotations

import re
from typing import Optional

from . import status

#: 判定"钩子"的时间窗（秒）。短视频的前 12 秒基本决定去留。
HOOK_WINDOW_S = 12.0
#: 节与节之间的停顿阈值（秒）。超过就当作一段落。
SECTION_GAP_S = 1.2
#: 模糊限定词：出现在数字前后，说明这不是"确切的数"，不能当数据点。
#: 例："约300人"（前）、"300多人"（后）、"300左右"（后）。
VAGUE_BEFORE = ("约", "近", "大约", "大概", "差不多", "近乎", "不到", "超过")
VAGUE_AFTER = ("多", "余", "左右", "上下", "以内", "以上")

CTA_MARKERS = ("关注", "点赞", "投币", "收藏", "转发", "评论区", "三连", "订阅", "小铃铛",
               "转发给", "分享给", "点个", "在看")
#: 枚举/结构标记（口播里的"第一/其次/最后"是结构信号）。
ENUM_MARKERS = ("第一", "第二", "第三", "首先", "其次", "然后", "接着", "最后", "总之",
                "总结", "另外", "所以", "但是", "因为")

_UNIT = r"(?:亿|万|千|百|%|％|倍|块|元|美元|秒|分钟|小时|天|年|个月|度|分|人|次|条|个)"
_NUM = re.compile(rf"(?<![A-Za-z0-9.])(\d+(?:\.\d+)?)\s*({_UNIT})?")


def _join(entries: list[dict]) -> str:
    return "".join(str(entry.get("content") or "") for entry in entries)


def _sentences(entries: list[dict]) -> list[dict]:
    """把条目按中文句末标点切句，保留每条的第一个时间戳。"""
    out: list[dict] = []
    for entry in entries:
        text = str(entry.get("content") or "")
        for piece in re.split(r"(?<=[。！？!?；;])", text):
            piece = piece.strip()
            if len(piece) < 2:
                continue
            out.append({"from": entry.get("from"), "to": entry.get("to"), "text": piece})
    return out


def skeleton(transcript: dict) -> dict:
    """口播骨架：钩子原文、句数、语速、问句/枚举/CTA 比例。"""
    if transcript.get("status") != status.MEASURED:
        return status.unknown("没有口播文本，无法推导文案结构", method="transcript-derived")
    entries = transcript.get("entries") or []
    if not entries:
        return status.unknown("口播为空", method="transcript-derived")
    sentences = _sentences(entries)
    total_chars = sum(len(s["text"]) for s in sentences)
    duration = max((float(e.get("to") or 0) for e in entries), default=0.0)
    hook = "".join(str(e.get("content") or "") for e in entries
                   if float(e.get("from") or 0) < HOOK_WINDOW_S).strip()
    questions = sum(1 for s in sentences if re.search(r"[？?]", s["text"]))
    cta_hits = [word for word in CTA_MARKERS if word in _join(entries)]
    enum_hits = [word for word in ENUM_MARKERS if word in _join(entries)]
    return status.field(
        status.DERIVED, method="transcript 句法统计", calibrated=False,
        hook=hook[:200],
        hook_chars=len(hook),
        sentences=len(sentences),
        chars=total_chars,
        chars_per_min=round(total_chars / (duration / 60.0), 1) if duration else None,
        question_ratio=round(questions / max(len(sentences), 1), 3),
        enumerations=enum_hits,
        cta=cta_hits,
        first_sentence=(sentences[0]["text"] if sentences else ""),
    )


def sections(transcript: dict) -> dict:
    """按停顿切节，节内首句作为"小标题候选"。"""
    if transcript.get("status") != status.MEASURED:
        return status.unknown("没有口播文本", method="transcript-derived")
    entries = transcript.get("entries") or []
    if not entries:
        return status.unknown("口播为空", method="transcript-derived")
    blocks: list[list[dict]] = [[entries[0]]]
    for previous, current in zip(entries, entries[1:]):
        gap = float(current.get("from") or 0) - float(previous.get("to") or 0)
        if gap > SECTION_GAP_S:
            blocks.append([current])
        else:
            blocks[-1].append(current)
    rows = []
    for index, block in enumerate(blocks, start=1):
        text = _join(block)
        rows.append({
            "index": index,
            "from": round(float(block[0].get("from") or 0), 2),
            "to": round(float(block[-1].get("to") or 0), 2),
            "chars": len(text),
            "lead": text[:60],
        })
    return status.field(status.DERIVED, method="按句间停顿 >%.1fs 切节" % SECTION_GAP_S,
                        calibrated=False, sections=rows, count=len(rows))


def datapoints(transcript: dict) -> dict:
    """抽数字数据点：三道机械防线（逐字在原文、单位一致、至少 2 个）。"""
    if transcript.get("status") != status.MEASURED:
        return status.skipped("没有口播文本，无法抽数据点", method="regex")
    text = _join(transcript.get("entries") or [])
    if not text:
        return status.skipped("口播为空", method="regex")

    found: list[dict] = []
    seen: set[str] = set()
    for match in _NUM.finditer(text):
        number, unit = match.group(1), match.group(2) or ""
        literal = match.group(0).strip()
        if literal in seen:
            continue
        seen.add(literal)
        # 防线 1：数值必须逐字出现在原文（正则是从原文抽的，这里再核一次，防后续改动引入偏差）
        if number not in text:
            continue
        # 防线 3：必须能解析成**确切的数**——"约/近/多/左右"这类模糊表述全丢
        try:
            value = float(number)
        except ValueError:
            continue
        before = text[max(0, match.start() - 3):match.start()]
        after = text[match.end():match.end() + 2]
        if any(word in before for word in VAGUE_BEFORE) or any(word in after for word in VAGUE_AFTER):
            continue
        start = max(0, match.start() - 30)
        found.append({"literal": literal, "value": value, "unit": unit,
                      "context": text[start:match.end() + 10].strip()})

    if len(found) < 2:
        return status.skipped(f"只找到 {len(found)} 个可用数据点（<2 不构成数据图）",
                              method="regex+逐字核验")
    # 防线 2：单位一致（同尾缀、且同为有量级或同为无量级）——不一致的分组分别返回
    groups: dict[str, list[dict]] = {}
    for item in found:
        groups.setdefault(item["unit"], []).append(item)
    return status.field(status.DERIVED, method="regex+逐字核验+单位分组", calibrated=False,
                        count=len(found), groups={key or "(无量级)": len(value)
                                                  for key, value in groups.items()},
                        points=found[:60])


#: 摘要用的分段数（8 段对 3–60 分钟的片子都够读）
SUMMARY_SECTIONS = 8
#: 关键词统计用的停用字（只挡最常见的虚词，不求专业分词）
STOP_CHARS = set("的了是在我你他她它和与就都而及或有这那么呢吧啊呀也还把被让给对从到并且但"
                 "个这那些种样么什怎为什么可以能会要想说做看走来说着过很太更最之前后里中上下")
#: 让"有点内容"的句子加权：结构词、数字、引号（金句常见）
CONTENT_HINTS = ("因为", "所以", "但是", "其实", "关键", "本质", "原因", "第一", "第二",
                 "最后", "记住", "注意", "数据", "研究", "结果", "方法")


def _sentence_score(text: str) -> float:
    score = min(len(text), 60) / 60.0
    score += 0.6 * sum(1 for word in CONTENT_HINTS if word in text)
    score += 0.4 * len(re.findall(r"\d", text))
    score += 0.3 if ("“" in text or "「" in text or "《" in text) else 0.0
    return score


def keywords(text: str, *, limit: int = 10) -> list[dict]:
    """按字面统计提关键词（2–4 字），不依赖分词库。

    诚实标注：这是**字面共现**，不是语义关键词。它给读者一个"这片在讲什么"的把手，
    但不要当成主题模型的结果。
    """
    counts: dict[str, int] = {}
    for size in (4, 3, 2):
        for index in range(0, max(0, len(text) - size + 1)):
            piece = text[index:index + size]
            if any(char in STOP_CHARS for char in piece):
                continue
            if not all("\u4e00" <= char <= "\u9fff" for char in piece):
                continue
            counts[piece] = counts.get(piece, 0) + 1
    ranked = sorted(((word, count) for word, count in counts.items() if count >= 3),
                    key=lambda pair: (-pair[1], -len(pair[0])))[:limit * 4]
    picked: list[dict] = []
    for word, count in ranked:
        if any(word in kept["word"] or kept["word"] in word for kept in picked):
            continue
        picked.append({"word": word, "count": count})
        if len(picked) >= limit:
            break
    return picked


def summary(transcript: dict, *, duration_s: float = 0.0) -> dict:
    """**抽取式**摘要：分段要点 + 关键词 + 关键数字。不调用任何模型。

    为什么不做"模型总结"：一段 45 分钟的口播交给模型写摘要，读的人无法核对它有没有编；
    而"每个时间段里信息量最大的那句原文"是可以回原文核对的。需要更像人写的总结时，
    让 agent 拿着这份 transcript 在会话里写——那是另一层，不该混进这份"可核对"的报告。
    """
    if transcript.get("status") != status.MEASURED:
        reason = transcript.get("reason") or "没有口播文本"
        return status.skipped(f"摘要需要口播文本：{reason}", method="transcript-derived")
    entries = transcript.get("entries") or []
    if not entries:
        return status.skipped("口播为空", method="transcript-derived")

    total = max(float(entries[-1].get("to") or 0), float(duration_s or 0))
    span = total / SUMMARY_SECTIONS if total > 0 else 0.0
    buckets: list[list[dict]] = [[] for _ in range(SUMMARY_SECTIONS)] if span > 0 else [entries]
    for sentence in _sentences(entries):
        if span <= 0:
            break
        index = min(SUMMARY_SECTIONS - 1, max(0, int(float(sentence.get("from") or 0) / span)))
        buckets[index].append(sentence)

    outline = []
    for index, bucket in enumerate(buckets):
        if not bucket:
            continue
        best = max(bucket, key=lambda row: _sentence_score(row["text"]))
        outline.append({
            "index": index + 1,
            "from": round(float(best.get("from") or 0), 1),
            "to": round(float(best.get("to") or 0), 1),
            "point": best["text"][:160],
        })

    joined = "".join(str(entry.get("content") or "") for entry in entries)
    numbers = datapoints(transcript)
    return status.field(
        status.DERIVED, method="抽取式：分段时间窗 + 句子信息量打分", calibrated=False,
        params={"sections": SUMMARY_SECTIONS, "window_s": round(span, 1)},
        sentence_count=len(_sentences(entries)),
        chars=len(joined),
        duration_s=round(total, 1),
        lead=(_sentences(entries)[0]["text"][:120] if _sentences(entries) else ""),
        outline=outline,
        keywords=keywords(joined),
        key_numbers=(numbers.get("points") or [])[:8] if numbers.get("status") == status.DERIVED else [],
        note="抽取式（每段取信息量最高的原句）+ 字面关键词；**不是模型写的总结**",
    )


def build(transcript: dict) -> dict:
    """给报告用的 `copywriting` 段：骨架 + 分段 + 数据点。

    如果口播本身被判定为"疑似幻觉"（见 `asr._quality`），这里**不能**照样标 DERIVED：
    由可疑原文推出来的"钩子/句法统计"同样是可疑的。这种情况降级成 `ASSUMED` 并写明原因，
    这样它会出现在报告的 `unknowns[]` 里，而不是伪装成可信结论。
    """
    if transcript.get("status") != status.MEASURED:
        reason = transcript.get("reason") or "没有口播文本"
        return status.skipped(f"文案结构需要口播文本：{reason}", method="transcript-derived")
    structure = skeleton(transcript)
    if structure.get("status") != status.DERIVED:
        return structure
    quality = (transcript.get("quality_report") or {}).get("quality")
    suspect = quality == "suspect"
    item = {
        "hook": structure["hook"],
        "hook_chars": structure["hook_chars"],
        "sentences": structure["sentences"],
        "chars_per_min": structure.get("chars_per_min"),
        "question_ratio": structure.get("question_ratio"),
        "enumerations": structure.get("enumerations"),
        "cta": structure.get("cta"),
        "first_sentence": structure.get("first_sentence"),
        "sections": (sections(transcript).get("sections") or []),
        "datapoints": datapoints(transcript),
        "note": "确定性推导（非模型判断）：钩子=前 12 秒原文，分段=停顿切分，数据点=逐字正则",
    }
    if suspect:
        return status.field(
            status.ASSUMED, method="transcript 句法统计 + 停顿切节", calibrated=False,
            reason="口播被判定为疑似幻觉（覆盖低/重复高），据此推导的文案结构仅供参考",
            transcript_quality=quality, **item,
        )
    return status.field(status.DERIVED, method="transcript 句法统计 + 停顿切节",
                        calibrated=False, transcript_quality=quality, **item)
