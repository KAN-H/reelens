"""视觉链的纯函数测试：不联网、不调模型。

重点覆盖"防静默降级"那几处：
1. 枚举外的值必须降级成"其他"，不能让脏值流进报告；
2. `_extract_json` 要能从围栏/前导说明里抠出 JSON；
3. 探针判定 `judge` 对四类答案的通过/失败边界；
4. `summarise` 的统计口径（没有帧时必须是 UNKNOWN，不是 0）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reelens import status, vision  # noqa: E402


def test_extract_json_tolerates_fences_and_prefix():
    text = '好的，结果如下：\n```json\n{"shot_size":"中景","confidence":0.8}\n```'
    assert vision._extract_json(text) == {"shot_size": "中景", "confidence": 0.8}
    assert vision._extract_json("no json here") is None


def test_coerce_clamps_and_degrades_unknown_enum():
    raw = {
        "shot_size": "超长镜头",             # 枚举外 → 其他
        "composition": ["三分法", "玄学构图"],  # 未知项被丢弃
        "subject_ratio": 7.5,                # 夹到 1.0
        "subtitle": {"present": True, "pos": "屏幕下方", "role": "口播字幕"},
        "on_screen_text": [" 标题 ", ""],
        "confidence": -1,
    }
    out = vision._coerce(raw)
    assert out["shot_size"] == "其他"
    assert out["composition"] == ["三分法"]
    assert out["subject_ratio"] == 1.0
    assert out["subtitle"]["pos"] == "无"      # 枚举外 → 无
    assert out["subtitle"]["role"] == "口播字幕"
    assert out["on_screen_text"] == ["标题"]
    assert out["confidence"] == 0.0


def test_judge_four_cases():
    assert vision.judge("position", "红色圆形", "red_circle")
    assert not vision.judge("position", "蓝色矩形", "red_circle")
    assert vision.judge("number", " 7 4 0 4 ", "7404")
    assert not vision.judge("number", "7405", "7404")
    assert vision.judge("color", "蓝色方块", "blue_rect")
    # 负样本：说"没有/NONE"才算过；说"有一个红圆"必须失败
    assert vision.judge("negative", "NONE", "none")
    assert vision.judge("negative", "图中没有文字", "none")
    assert not vision.judge("negative", "左上角有一个红色圆形", "none")


def test_chain_without_keys_is_empty(monkeypatch):
    for name in ("AMDGPU_API_KEY", "BREAKDOWN_AMDGPU_KEY", "BREAKDOWN_VISION_CHAIN",
                 "BREAKDOWN_LOCAL_VLM_BASE", "BREAKDOWN_OCP_CMD", "BREAKDOWN_WB_CMD",
                 "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert vision.chain(allow_cloud=True) == []


def test_chain_respects_explicit_config(monkeypatch):
    monkeypatch.setenv("BREAKDOWN_VISION_CHAIN",
                       '[{"name":"x","kind":"openai","base_url":"http://127.0.0.1:1/v1","model":"m"}]')
    items = vision.chain()
    assert len(items) == 1 and items[0]["name"] == "x"


def test_summarise_without_frames_is_unknown():
    assert vision.summarise({"status": status.MEASURED, "items": []})["status"] == status.UNKNOWN


def test_summarise_counts_subtitle_positions():
    visual = {
        "status": status.MEASURED, "provider": "p", "items": [
            {"shot_size": "中景", "motion_hint": "静止", "confidence": 0.9,
             "subtitle": {"present": True, "pos": "下", "role": "口播字幕"},
             "on_screen_text": ["A"]},
            {"shot_size": "中景", "motion_hint": "推", "confidence": 0.5,
             "subtitle": {"present": False, "pos": "无", "role": "无"},
             "on_screen_text": ["A", "B"]},
        ],
    }
    summary = vision.summarise(visual)
    assert summary["status"] == status.DERIVED
    assert summary["frames"] == 2
    assert summary["subtitle_ratio"] == 0.5
    assert summary["subtitle_pos"] == {"下": 1}
    assert summary["on_screen_text"] == ["A", "B"]
    assert summary["confidence_mean"] == 0.7
