#!/usr/bin/env python3
"""G5 转场标定：造一条"硬切 + 叠化"的已知样片，量出 ffmpeg 的 scene 分数到底长什么样。

为什么必须标定
--------------
`select=gt(scene,0.30)` 是**单帧差分**。硬切是一帧的事，分数会冲到 0.4–0.9；
而叠化/淡入淡出是**多帧小差分累积**，每一帧都可能只有 0.03–0.15——于是
阈值 0.30 一帧都选不中。报告里如果只写"检测到 N 个镜头"，用户会以为"没有转场"，
而真相是"渐变被判成了同一个镜头"。这是**看起来正确、实际反向**的失败。

标定目标
--------
1. 硬切在 0.30 / 0.10 两档下的分数分布（确认 0.30 不会漏硬切）；
2. 叠化在 0.10 档下会不会形成**连续成片**的选中帧（这是"渐变"的可判据）；
3. 给出一组可用的聚类参数：`gradual_min_run`（连续帧数）与 `gradual_max_score`。

样片（ffmpeg 现造，不依赖任何外部素材）
----------------------------------------
    0–3s 红　3–6s 绿　6–9s 蓝　（三处硬切）
    9–12s 红→蓝 线性混合（叠化）
    12–12.5s 白（硬切，用来验证渐变段之后还能恢复识别）
    12.5–15s 黑（淡出到黑，用 fade 造）
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "python" / "src"))

from reelens import paths  # noqa: E402

FIXTURE = REPO / ".tmp" / "fixtures" / "cuts-dissolve.mp4"
OUT = REPO / ".dev-notes" / "gates" / "g5" / "g5-report.json"

#: 已知答案（秒）
HARD_CUTS = [3.0, 6.0, 9.0, 12.0]
DISSOLVE = [9.0, 12.0]
FADE_OUT = [12.5, 15.0]

_PTS = re.compile(r"pts_time:([0-9.]+)")
_SCORE = re.compile(r"scene_score=([0-9.]+)")


def build_fixture() -> Path:
    """红/绿/蓝 + 一段 red→blue 的 blend 叠化 + 白 + 淡出到黑。"""
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = paths.ffmpeg_path()
    if not ffmpeg:
        raise SystemExit("找不到 ffmpeg")
    head = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=red:s=320x240:d=3:r=30",
        "-f", "lavfi", "-i", "color=c=green:s=320x240:d=3:r=30",
        "-f", "lavfi", "-i", "color=c=blue:s=320x240:d=3:r=30",
        "-f", "lavfi", "-i", "color=c=red:s=320x240:d=3:r=30",
        "-f", "lavfi", "-i", "color=c=blue:s=320x240:d=3:r=30",
        "-f", "lavfi", "-i", "color=c=white:s=320x240:d=0.5:r=30",
        "-f", "lavfi", "-i", "color=c=white:s=320x240:d=2.5:r=30",
    ]
    graph = (
        "[3:v][4:v]blend=all_expr='A*(1-(T/3))+B*(T/3)'[mix];"
        "[5:v][6:v]concat=n=2:v=1:a=0[white];"
        "[white]fade=t=out:st=0:d=2.5[blk];"
        "[0:v][1:v][2:v][mix][blk]concat=n=5:v=1:a=0[out]"
    )
    command = head + ["-filter_complex", graph, "-map", "[out]",
                      "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30", str(FIXTURE)]
    proc = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=paths.child_env())
    if proc.returncode != 0 or not FIXTURE.is_file():
        raise SystemExit("造样片失败：\n" + (proc.stderr or "")[-800:])
    return FIXTURE


def scan(media: Path, threshold: float) -> list[dict]:
    ffmpeg = paths.ffmpeg_path()
    command = [ffmpeg, "-hide_banner", "-nostats", "-i", str(media),
               "-vf", f"select='gt(scene,{threshold})',metadata=print",
               "-an", "-f", "null", "-"]
    proc = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=paths.child_env())
    text = (proc.stdout or "") + (proc.stderr or "")
    rows: list[dict] = []
    last = None
    for line in text.splitlines():
        pts = _PTS.search(line)
        if pts:
            last = float(pts.group(1))
            continue
        score = _SCORE.search(line)
        if score and last is not None:
            rows.append({"t": round(last, 3), "score": round(float(score.group(1)), 3)})
            last = None
    return rows


def runs(rows: list[dict], *, gap_s: float = 0.5) -> list[list[dict]]:
    """把时间上相邻的检测点聚成"段"——渐变会形成一段，硬切只有孤立一个点。"""
    clusters: list[list[dict]] = []
    for row in rows:
        if clusters and row["t"] - clusters[-1][-1]["t"] <= gap_s:
            clusters[-1].append(row)
        else:
            clusters.append([row])
    return clusters


def main() -> int:
    started = time.time()
    media = build_fixture()
    fine = scan(media, 0.10)
    coarse = scan(media, 0.30)
    clusters = runs(fine)

    classified = []
    for cluster in clusters:
        scores = [row["score"] for row in cluster]
        span = cluster[-1]["t"] - cluster[0]["t"]
        kind = "hard_cut" if (len(cluster) <= 2 and span <= 0.12) else "gradual"
        classified.append({"t0": cluster[0]["t"], "t1": cluster[-1]["t"],
                           "samples": len(cluster), "span_s": round(span, 3),
                           "max_score": max(scores), "min_score": min(scores),
                           "kind": kind})

    def near(value: float, targets, tol: float = 0.6) -> bool:
        return any(abs(value - target) <= tol for target in targets)

    hard_found = [c for c in classified if c["kind"] == "hard_cut"]
    gradual_found = [c for c in classified if c["kind"] == "gradual"]

    report = {
        "schema": "video-breakdown.gate.g5.v1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "fixture": str(media),
        "ground_truth": {"hard_cuts": HARD_CUTS, "dissolve": DISSOLVE, "fade_out": FADE_OUT},
        "threshold_030": {"detections": coarse, "count": len(coarse)},
        "threshold_010": {"detections": len(fine),
                          "score_min": min((r["score"] for r in fine), default=None),
                          "score_max": max((r["score"] for r in fine), default=None)},
        "clusters": classified,
        "hard_cut_recall": {
            "expected": len(HARD_CUTS),
            "matched": [c["t0"] for c in hard_found if near(c["t0"], HARD_CUTS)],
            "missed": [t for t in HARD_CUTS if not any(near(c["t0"], [t]) for c in hard_found)],
        },
        "gradual_detected": [{"t0": c["t0"], "t1": c["t1"], "span_s": c["span_s"],
                              "samples": c["samples"], "max_score": c["max_score"]}
                             for c in gradual_found],
        "elapsed_s": round(time.time() - started, 1),
    }
    # 通过判据：三处纯色硬切全中；叠化段至少形成 1 个"连续成片"的渐变簇
    report["ok"] = (not report["hard_cut_recall"]["missed"]) and bool(gradual_found)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
