"""核心纯函数测试：不需要网络、不需要 ffmpeg、不需要真实视频。

覆盖三条最容易退化的契约：
1. `status`——没测到的项必须带原因，且必须能被 `collect_unknowns` 汇总出来；
2. `shots`——切点 → 边界的规则（与早期实现同口径，端到端对比在 scripts/gates，这里是快测）；
3. `report`——`ok` 必须只由 required 项决定，"缺 transcript"不许被算成通过。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reelens import report, shots, status  # noqa: E402


def test_unknown_requires_reason_and_is_collected():
    payload = {
        "probe": status.field(status.MEASURED, method="ffprobe", width=1920),
        "nested": {"deep": status.unknown("没有音轨")},
        "list": [status.field(status.MEASURED, method="x"), status.skipped("没装 librosa")],
    }
    found = status.collect_unknowns(payload)
    fields = {item["field"] for item in found}
    assert "nested.deep" in fields
    assert any(name.startswith("list") for name in fields)
    assert all(item["reason"] for item in found)


def test_field_rejects_unknown_status():
    try:
        status.field("SOMETHING_ELSE")
    except ValueError as exc:
        assert "未知 status" in str(exc)
    else:  # pragma: no cover - 正常不会走到
        raise AssertionError("非法 status 应该被拒绝")


def test_boundaries_drop_edges_and_keep_ends():
    cuts = [{"t": 0.05, "score": 0.5}, {"t": 3.0, "score": 0.4},
            {"t": 9.999, "score": 0.5}, {"t": 10.05, "score": 0.5}]
    points = shots.boundaries(cuts, duration_s=10.0)
    # 0.05 太靠头、10.05 超出片长、9.999 太贴尾 → 都丢掉
    assert points == [0.0, 3.0, 10.0]


def test_build_shots_skips_micro_shots_and_ids_are_sequential():
    rows = shots.build_shots([0.0, 1.0, 1.02, 4.0], [])
    assert [row["id"] for row in rows] == ["s000", "s001"]
    assert round(rows[0]["dur"], 3) == 1.0
    # 1.0→1.02 只有 0.02s（<=MIN_SHOT_S）被跳过，所以第二镜从 1.02 起、长 2.98s
    assert round(rows[1]["dur"], 3) == 2.98


def test_verdict_marks_but_never_deletes():
    dark = status.field(status.DERIVED, method="signalstats", calibrated=False,
                        yavg=10.0, shake=0.0, edge=20.0)
    label, why = shots.verdict(dark)
    assert label == "warn" and "过暗" in why
    missing = status.unknown("没测到")
    assert shots.verdict(missing)[0] == "unknown"


def test_report_ok_gate_requires_transcript():
    measured = status.field(status.MEASURED, method="x")
    payload = report.assemble(
        source={"kind": "local"}, probe=measured,
        shots_item={"status": status.MEASURED, "shots": [{"id": "s000", "start": 0.0,
                                                          "end": 1.0, "dur": 1.0}]},
        frames_item=measured,
        transcript=status.skipped("调用方关掉了转写"),
        audio={}, editing={}, visual=status.skipped("无"), tier="fast", plan_id="p1",
        timing={}, evidence={})
    assert payload["ok"] is False
    assert payload["missing_required"] == ["transcript"]
    assert any(item["field"] == "transcript" for item in payload["unknowns"])

    payload["transcript"] = measured
    payload["unknowns"] = status.collect_unknowns(payload)
    assert report._missing_required(payload) == []


def test_no_speech_is_a_conclusion_not_a_missing_section():
    """纯音乐/纯 MG 的片子很常见：'没有人声'是结论，不该让整份报告判不完整。"""
    measured = status.field(status.MEASURED, method="x")
    no_speech = status.skipped("no_speech_detected：VAD 全片与最响 15 秒都没有语音",
                               method="faster-whisper/silero-vad")
    no_speech["speech_present"] = False
    payload = report.assemble(
        source={"kind": "url"}, probe=measured,
        shots_item={"status": status.MEASURED, "shots": [{"id": "s000", "start": 0.0,
                                                          "end": 1.0, "dur": 1.0}]},
        frames_item=measured, transcript=no_speech, audio={}, editing={},
        visual=measured, tier="fast", plan_id="p1", timing={}, evidence={})
    assert payload["narration"] == "absent"
    assert payload["ok"] is True, payload.get("missing_required")
    # 仍然要进 unknowns：读者需要知道"这一项为什么是空的"
    assert any(item["field"] == "transcript" for item in payload["unknowns"])


def test_transcript_failure_still_fails_the_gate():
    """真正的失败（引擎坏了）不能被当成"没人声"放过。"""
    measured = status.field(status.MEASURED, method="x")
    failed = status.failed("所有 ASR 引擎都不可用")
    payload = report.assemble(
        source={}, probe=measured,
        shots_item={"status": status.MEASURED, "shots": [{"id": "s000", "start": 0, "end": 1, "dur": 1}]},
        frames_item=measured, transcript=failed, audio={}, editing={}, visual={},
        tier="fast", plan_id="p", timing={}, evidence={})
    assert payload["ok"] is False
    assert payload["narration"] == "unknown"


def test_conclusion_identifies_static_background_mg():
    """纯 MG：无人声 + 镜头稀 + 帧差密集 → 结论必须点出"检测退化"。"""
    probe = status.field(status.MEASURED, method="ffprobe", duration_s=180.0, portrait=False)
    transcript = status.skipped("no_speech_detected：VAD 两处都没有语音")
    transcript["speech_present"] = False
    editing = {"frame_diff": {"status": status.DERIVED, "shot_rate_per_min": 0.67,
                              "change_rate_per_min": 15.0, "degenerate": True}}
    visual = {"status": status.MEASURED,
              "summary": {"subtitle_ratio": 1.0, "on_screen_text": ["a", "b", "c"]}}
    item = report.conclusion({"probe": probe, "transcript": transcript,
                              "editing": editing, "visual": visual})
    assert item["status"] == status.DERIVED
    assert "MG" in item["type"]
    assert "检测退化" in item["one_liner"]
    assert item["signals"]["shot_detection_degenerate"] is True


def test_conclusion_flags_talking_head():
    probe = status.field(status.MEASURED, method="ffprobe", duration_s=60.0, portrait=True)
    transcript = status.field(status.MEASURED, method="asr", entries=[{"from": 0, "to": 1, "content": "x"}])
    editing = {"frame_diff": {"status": status.DERIVED, "shot_rate_per_min": 12.0,
                              "change_rate_per_min": 20.0, "degenerate": False}}
    item = report.conclusion({"probe": probe, "transcript": transcript,
                              "editing": editing, "visual": {}})
    assert "口播" in item["type"]
    assert item["mode"] == "口播驱动"
    assert item["orientation"] == "竖"
