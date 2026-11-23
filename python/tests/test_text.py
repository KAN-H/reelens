"""文案结构推导的测试：全确定性，不需要模型、不需要网络。

重点：
1. 钩子只取前 12 秒的原文（不是"模型总结"）；
2. 分段按停顿阈值切，不是按字数硬切；
3. 数据点必须逐字来自原文，且 <2 个就明确 SKIPPED（不许凑）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reelens import status, text  # noqa: E402


def transcript(entries):
    return status.field(status.MEASURED, method="test", entries=entries,
                        quality_report={"quality": "ok"})


def test_skeleton_takes_hook_from_first_12_seconds():
    rows = [
        {"from": 0.0, "to": 4.0, "content": "房间不打扫，就会落满灰。"},
        {"from": 4.0, "to": 9.5, "content": "城市没有人，就会退回森林。"},
        {"from": 30.0, "to": 34.0, "content": "所以这段不该进钩子。"},
    ]
    out = text.skeleton(transcript(rows))
    assert out["status"] == status.DERIVED
    assert out["hook"].startswith("房间不打扫")
    assert "所以这段不该进钩子" not in out["hook"]
    assert out["sentences"] == 3
    assert out["question_ratio"] == 0.0


def test_skeleton_counts_questions_and_cta():
    rows = [
        {"from": 0.0, "to": 3.0, "content": "你知道熵增吗？"},
        {"from": 3.0, "to": 6.0, "content": "记得点赞关注。"},
    ]
    out = text.skeleton(transcript(rows))
    assert out["question_ratio"] > 0
    assert "关注" in out["cta"]


def test_sections_split_on_pause():
    rows = [
        {"from": 0.0, "to": 2.0, "content": "第一段。"},
        {"from": 2.0, "to": 4.0, "content": "还连着。"},
        {"from": 8.0, "to": 10.0, "content": "隔了四秒，另起一节。"},
    ]
    out = text.sections(transcript(rows))
    assert out["status"] == status.DERIVED
    assert out["count"] == 2
    assert out["sections"][0]["lead"].startswith("第一段")


def test_datapoints_require_verbatim_and_two_items():
    one = text.datapoints(transcript([{"from": 0, "to": 2, "content": "大约 180 亿。"}]))
    assert one["status"] == status.SKIPPED

    many = text.datapoints(transcript([
        {"from": 0, "to": 2, "content": "规模 180亿，增长 32%，是去年的 3倍。"},
    ]))
    assert many["status"] == status.DERIVED
    assert many["count"] >= 3
    literals = [item["literal"] for item in many["points"]]
    assert any("180" in item for item in literals)
    assert set(many["groups"]) >= {"亿", "%", "倍"}


def test_datapoints_skip_vague_number_without_unit():
    out = text.datapoints(transcript([
        {"from": 0, "to": 2, "content": "约300人参加，另有500人参展。"},
    ]))
    # "约300" 无单位 → 丢掉；"500人" 保留 → 只有 1 个 → SKIPPED
    assert out["status"] == status.SKIPPED
    assert "1 个" in out["reason"]


def test_summary_is_extractive_and_marked():
    """摘要是**抽取式**：每段要点必须是原文里的句子，不能是改写。"""
    rows = [
        {"from": 0, "to": 9, "content": "朋友脸色不对，你问他怎么了，他说没什么。"},
        {"from": 9, "to": 20, "content": "人不说真话，是因为说出来的风险大于收益。"},
        {"from": 20, "to": 31, "content": "你先交出一个无伤大雅的弱点，风险就变成两个人一起扛。"},
        {"from": 31, "to": 45, "content": "心理学把这叫互惠式自我暴露：你先开门，他才敢进来。"},
        {"from": 45, "to": 60, "content": "第二层是切口降维，都行这两个字最假。"},
        {"from": 60, "to": 72, "content": "一句话漏出两个信息：吃腻了，还在控制饮食。"},
    ]
    item = status.field(status.MEASURED, method="asr", entries=rows,
                        quality_report={"quality": "ok"})
    out = text.summary(item, duration_s=72)
    assert out["status"] == status.DERIVED
    assert "不是模型" in out["note"]
    assert out["calibrated"] is False
    originals = "".join(row["content"] for row in rows)
    assert out["outline"], "至少要有一段要点"
    for point in out["outline"]:
        # 每条要点都必须能在原文里逐字找到（抽取式的核心约束）
        assert point["point"][:20] in originals
    assert out["sentence_count"] == len(rows)


def test_summary_without_transcript_is_skipped():
    out = text.summary(status.skipped("no_speech_detected"))
    assert out["status"] == status.SKIPPED
    assert "口播" in out["reason"]
    out = text.build(status.skipped("没有音轨，无法转写"))
    assert out["status"] == status.SKIPPED
    assert "没有音轨" in out["reason"]


def test_build_marks_deterministic_method():
    rows = [{"from": 0, "to": 3, "content": "开头这句很重要。后面还有一句。"}]
    out = text.build(transcript(rows))
    assert out["status"] == status.DERIVED
    assert out["calibrated"] is False
    assert "确定性" in out["note"]
    assert "sections" in out and "datapoints" in out


def test_build_downgrades_when_transcript_is_suspect():
    """口播疑似幻觉时，据它推出的文案结构不能标 DERIVED——必须降级并写明原因。"""
    rows = [{"from": 0, "to": 3, "content": "字幕志愿者 杨茜茜"}]
    item = status.field(status.MEASURED, method="asr", entries=rows,
                        quality_report={"quality": "suspect", "unique_ratio": 0.17})
    out = text.build(item)
    assert out["status"] == status.ASSUMED
    assert "疑似幻觉" in out["reason"]
    assert out["transcript_quality"] == "suspect"
    # 进 unknowns 汇总（ASSUMED 也算非结论项）
    found = status.collect_unknowns({"copywriting": out})
    assert any(entry["code"] == "ASSUMED" for entry in found)
