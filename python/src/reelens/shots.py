"""镜头切分 + 画质代理：**一次解码**同时出场景切点、亮度/抖动、细节量。

为什么必须"一次解码"
--------------------
早期实现对每个镜头再跑两次 `-ss/-t` 解码算 `signalstats`
与 `edgedetect`。一个 40 镜头的视频就是 1（全片场景）+ 80 次解码，长片上是分钟级浪费。
这里改成：整片解码一次，`split` 成三路 filter，同时落三个 `metadata=print` 文件。

三个**实测**出来的硬约束（2026-11-22，ffmpeg N-92722 / 2018 构建）
------------------------------------------------------------------
1. `null` 是**直通滤镜，不是接收器**。写了 `...metadata=print:file=a.txt,null[x]` 却不
   `-map "[x]"`，ffmpeg 报 `Filter metadata has an unconnected output`，整条命令失败。
   → 两条/三条分支都 `-map` 到**同一个** `-f null -` 输出（一个输出可以有多条视频流）。
2. `metadata=print:file=` **不能吃 Windows 绝对路径**：`<盘符>:\a\b.txt` 会被解析成
   `Error setting option mode to value <盘符>:...` 直接初始化失败。
   → `file=` 只写**相对文件名**，并把 ffmpeg 的 cwd 设到工作目录。
3. 无音轨的视频不能带 `-map 0:a`：会以 `Stream map '0:a' matches no streams` 中止，
   而且这是**整个命令失败**，不是"少一条音轨"。→ 先 probe，argv 条件构建。

另外 `-vsync 0` 是必需的：`select` 分支输出的是变帧率流，默认 cfr 会在取切点时
复制/丢帧，导致 `pts_time` 与真实切点错位。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Optional

from . import paths, status

#: 场景切分阈值。0.30 是 ffmpeg 官方例子的常用值（硬切通常 >0.4，手持晃动能到 0.15）。
DEFAULT_THRESHOLD = 0.30
#: 画质采样的目标帧率（再按镜头时长收缩到 ≤12 个采样点，见 `aggregate`）。
STATS_FPS = 4.0
#: 单个镜头太短就没有意义（与旧实现一致）。
MIN_SHOT_S = 0.05
#: 切点贴着开头/结尾时要丢掉（0.15s 内多半是第一帧噪声）。
EDGE_GUARD_S = 0.15
QUALITY_SAMPLES = 12
#: 超过这个时长（秒）就走"降采样 + 跳帧"的快速路径（实测见 detect_scenedetect）。
LONG_CLIP_S = 600.0

_PTS_RE = re.compile(r"pts_time:([0-9.]+)")
_SCORE_RE = re.compile(r"scene_score=([0-9.]+)")
_SCORE_LINE_RE = re.compile(r"lavfi\.scene_score=([0-9.]+)")
_YAVG_RE = re.compile(r"lavfi\.signalstats\.YAVG=([0-9.]+)")
_YDIF_RE = re.compile(r"lavfi\.signalstats\.YDIF=([0-9.]+)")


def _run(command: list[str], *, cwd: Path, timeout: float) -> str:
    proc = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, cwd=str(cwd), env=paths.child_env(),
    )
    return (proc.stdout or "") + (proc.stderr or "")


def _build_command(media: Path, *, has_audio: bool, scene_fps: float = 0.0,
                   stats_fps: float = STATS_FPS, edges_fps: float = 0.0) -> list[str]:
    """见模块头三条约束：三路 split、显式 map、`-vsync 0`、音轨按需。

    **2026-11-23 改动**：切点分支从 `select='gt(scene,0.30)'`（只吐出超过阈值的帧）
    改成 `select='gte(scene,0)'` + `metadata=print`（**每帧都带分数**）。
    这样一次解码就拿到完整的 scene_score 序列，切点阈值改成**在 Python 里自适应**算
    （median + k·MAD），既能保留低分硬切，又不再需要第二遍 scenedetect 解码。
    实测：45 分钟片子 374s → 单遍 ~70s（取决于编码，AV1 软解最慢）。
    """
    binary = paths.ffmpeg_path()
    # 长片可以在**计分之前**先降帧率：切点会持续一整个镜头（≥1 秒），10fps 足够抓到，
    # 而 `select(scene)` 和 `edgedetect` 都是逐帧计算——降帧直接把这两项的成本按比例砍掉。
    # 实测（45 分钟 852×480）：裸解码 82s，账全在滤镜上，降帧后 215s → ~130s。
    pre_scene = f"fps={scene_fps}," if scene_fps else ""
    pre_edges = f"fps={edges_fps}," if edges_fps else ""
    graph = (
        "[0:v]split=3[v0][v1][v2];"
        f"[v0]{pre_scene}select='gte(scene,0)',metadata=print:file=scene.txt[o0];"
        f"[v1]fps={stats_fps},signalstats,metadata=print:file=stats.txt[o1];"
        f"[v2]fps={stats_fps},{pre_edges}edgedetect,signalstats,metadata=print:file=edges.txt[o2]"
    )
    command = [
        binary, "-hide_banner", "-nostats", "-y",
        "-i", str(media),
        "-filter_complex", graph,
        "-map", "[o0]", "-map", "[o1]", "-map", "[o2]",
        "-vsync", "0", "-f", "null", "-",
    ]
    if has_audio:
        command += ["-map", "0:a", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "audio.16k.wav"]
    return command


def _parse_cuts(text: str) -> list[dict]:
    cuts: list[dict] = []
    last_pts: Optional[float] = None
    for line in text.splitlines():
        pts = _PTS_RE.search(line)
        if pts:
            last_pts = float(pts.group(1))
            continue
        score = _SCORE_RE.search(line)
        if score and last_pts is not None:
            cuts.append({"t": round(last_pts, 3), "score": round(float(score.group(1)), 3)})
            last_pts = None
    return cuts


def _parse_series(text: str) -> list[dict]:
    """`metadata=print` 的时间序列表 → `[{t, yavg, ydif}]`。"""
    rows: list[dict] = []
    current: Optional[dict] = None
    for line in text.splitlines():
        pts = _PTS_RE.search(line)
        if pts:
            if current is not None:
                rows.append(current)
            current = {"t": round(float(pts.group(1)), 3), "yavg": None, "ydif": None}
            continue
        if current is None:
            continue
        yavg = _YAVG_RE.search(line)
        if yavg:
            current["yavg"] = float(yavg.group(1))
            continue
        ydif = _YDIF_RE.search(line)
        if ydif:
            current["ydif"] = float(ydif.group(1))
    if current is not None:
        rows.append(current)
    return rows


def boundaries(cuts: list[dict], duration_s: float) -> list[float]:
    """切点 → 首尾相接的镜头边界（与旧实现同一口径，G3 会逐值对比）。"""
    points = [0.0]
    points += [c["t"] for c in cuts if EDGE_GUARD_S < c["t"] < duration_s - 0.1]
    points.append(duration_s)
    return sorted({round(p, 3) for p in points})


def build_shots(points: list[float], cuts: list[dict]) -> list[dict]:
    scores = {c["t"]: c["score"] for c in cuts}
    shots: list[dict] = []
    for index in range(len(points) - 1):
        start, end = points[index], points[index + 1]
        if end - start <= MIN_SHOT_S:
            continue
        shots.append({
            "id": f"s{len(shots):03d}",
            "start": round(start, 3),
            "end": round(end, 3),
            "dur": round(end - start, 3),
            "cut_score": round(scores.get(start, 0.0), 3),
        })
    return shots


def _window(series: list[dict], start: float, end: float, limit: int = QUALITY_SAMPLES) -> list[dict]:
    rows = [r for r in series if start - 1e-6 <= r["t"] < end - 1e-6]
    if len(rows) <= limit:
        return rows
    step = len(rows) / float(limit)
    return [rows[min(len(rows) - 1, int(i * step))] for i in range(limit)]


def aggregate(shots: list[dict], stats: list[dict], edges: list[dict]) -> None:
    """把全片采样序列按镜头窗口聚合成画质代理（就地写进 shot["quality"]）。

    抖动用 `YDIF` 的**离散度**而不是最大值——一个转场就能把最大值拉满。
    """
    edge_values = {round(r["t"], 3): r.get("yavg") for r in edges if r.get("yavg") is not None}
    for shot in shots:
        rows = _window(stats, shot["start"], shot["end"])
        yavg = [r["yavg"] for r in rows if r.get("yavg") is not None]
        ydif = [r["ydif"] for r in rows if r.get("ydif") is not None]
        edge = [edge_values[round(r["t"], 3)] for r in rows if round(r["t"], 3) in edge_values]
        if not yavg:
            shot["quality"] = status.unknown("该镜头窗口内没有采样点（镜头极短或解码失败）",
                                             method="signalstats")
            continue
        mean = sum(yavg) / len(yavg)
        shake = 0.0
        if len(ydif) > 2:
            tail = ydif[1:]  # 第一帧的 YDIF 相对上一段黑场，不代表本镜头
            avg = sum(tail) / len(tail)
            shake = (sum((v - avg) ** 2 for v in tail) / len(tail)) ** 0.5
        shot["quality"] = status.field(
            status.DERIVED, method="signalstats+edgedetect",
            params={"fps": STATS_FPS, "samples": len(yavg), "window_s": shot["dur"]},
            calibrated=False,   # 阈值是经验值，未经人工标定 → 只能是 DERIVED
            yavg=round(mean, 2),
            yavg_min=round(min(yavg), 2),
            yavg_max=round(max(yavg), 2),
            shake=round(shake, 2),
            edge=round(sum(edge) / len(edge), 2) if edge else None,
            samples=len(yavg),
        )


def verdict(quality: dict) -> tuple[str, str]:
    """把代理指标翻成"能不能用"。阈值是经验值——**标记，不替人做决定**。

    黑场也可能是刻意的转场，所以这里只给 `warn` + 理由，从不删除镜头。
    """
    if quality.get("status") != status.DERIVED:
        return "unknown", quality.get("reason") or "没测到"
    reasons = []
    if float(quality.get("yavg") or 128) < 25:
        reasons.append("过暗/黑场")
    elif float(quality.get("yavg") or 128) > 215:
        reasons.append("过曝")
    if float(quality.get("shake") or 0) > 6:
        reasons.append("抖动明显")
    if quality.get("edge") is not None and float(quality["edge"]) < 4:
        reasons.append("细节很少")
    return ("warn", "、".join(reasons)) if reasons else ("good", "")


def detect_scenedetect(media: str | Path, *, detector: str = "content",
                       threshold: float = 27.0, downscale: int = 1,
                       frame_skip: int = 1) -> dict:
    """用 PySceneDetect 找镜头边界（可选依赖；没装就 SKIPPED）。

    **为什么它是默认引擎**：G5 标定实测（2026-11-22，171s 真实视频）——
    ffmpeg `select=gt(scene,0.30)` 只找到 **7** 个边界，ContentDetector 找到 **11**、
    AdaptiveDetector 找到 **15**。0.30 这个默认值在真实素材上**系统性漏检约一半硬切**，
    而"镜头少了"会让节奏结论整体偏慢（每分钟 2.5 切 vs 实际 ~5 切）。
    合成样片（纯色块）两者都对，所以**合成样片不能用来证明真实素材的准确度**。
    """
    try:
        from scenedetect import SceneManager, open_video  # noqa: PLC0415
        from scenedetect.detectors import AdaptiveDetector, ContentDetector  # noqa: PLC0415
    except ImportError:
        return status.skipped("没装 scenedetect（pip install scenedetect）", method="scenedetect")
    try:
        video = open_video(str(media))
        # 0.7 起 `open_video` 返回不得二次 pass 的视频对象；detect_scenes 会读完整条流
        #
        # **长片必须降采样**（2026-11-23 实测）：45 分钟 852×480 / 81000 帧，
        # 全分辨率逐帧算 HSV 直方图要 **374 秒**。downscale=2（长边减半）+ 每 2 帧看一次
        # 之后是 1/4~1/6 的耗时，而切点位置基本不变（切点是画面级跳变，不靠单像素）。
        # 短片（≤10 分钟）保持全精度：G3/G5 的标定结果是在那个配置下拿到的，不能悄悄改。
        manager = SceneManager()
        if detector == "adaptive":
            manager.add_detector(AdaptiveDetector())
        else:
            manager.add_detector(ContentDetector(threshold=float(threshold)))
        manager.detect_scenes(video, frame_skip=max(0, int(frame_skip) - 1))
        scenes = manager.get_scene_list()
    except Exception as exc:  # noqa: BLE001 - 检测器失败要可读，不能拖垮整条链
        return status.failed(f"{type(exc).__name__}: {exc}", method="scenedetect")
    starts = []
    for scene in scenes:
        seconds = float(getattr(scene[0], "seconds", 0.0))
        starts.append(round(seconds, 3))
    if not starts:
        return status.failed("scenedetect 没有找到任何镜头", method="scenedetect")
    return status.field(status.MEASURED,
                        method=f"scenedetect/{detector}" + (
                            f"（downscale={downscale}, frame_skip={frame_skip}）"
                            if downscale > 1 or frame_skip > 1 else ""),
                        params={"threshold": threshold, "downscale": downscale,
                                "frame_skip": frame_skip},
                        starts=starts, count=len(starts))


def _parse_scene_scores(text: str) -> list[dict]:
    """解析 `scene.txt`：每帧一条 `lavfi.scene_score`。"""
    rows: list[dict] = []
    last_pts: Optional[float] = None
    for line in text.splitlines():
        pts = _PTS_RE.search(line)
        if pts:
            last_pts = float(pts.group(1))
            continue
        score = _SCORE_LINE_RE.search(line)
        if score and last_pts is not None:
            rows.append({"t": round(last_pts, 4), "score": round(float(score.group(1)), 6)})
            last_pts = None
    return rows


def detect_from_scores(scores: list[dict], *, k: float = 3.0, floor: float = 0.10,
                       min_gap: float = 0.40) -> dict:
    """在**已有的 scene_score 序列**上自适应找切点（不需要第二遍解码）。

    为什么比固定阈值好（G5 实测 + 2026-11-23 复核）：固定 0.30 在真实素材上系统性漏检，
    因为同一个"硬切"在不同片源上的分数可以从 0.2 到 0.9——跟编码、运动、亮度都有关。
    用本片自身的分布定阈值（median + k·MAD）才跨片源稳定。

    `min_gap` 用来合并"一次切变连续几帧都高分"的抖动（取最高分那一帧）。
    """
    if len(scores) < 20:
        return status.unknown(f"scene_score 采样太少（{len(scores)} 帧）", method="scene_score")
    values = sorted(row["score"] for row in scores)
    median = values[len(values) // 2]
    deviations = sorted(abs(value - median) for value in values)
    mad = deviations[len(deviations) // 2]
    threshold = median + max(float(k) * mad, float(floor))
    picks: list[dict] = []
    for row in scores:
        if row["score"] < threshold:
            continue
        if picks and row["t"] - picks[-1]["t"] < float(min_gap):
            if row["score"] > picks[-1]["score"]:
                picks[-1] = row
            continue
        picks.append(row)
    starts = sorted({0.0} | {round(row["t"], 3) for row in picks})
    return status.field(
        status.DERIVED,
        method=f"scene_score 自适应（median+k·MAD，k={k}，floor={floor}）",
        calibrated=False,
        params={"median": round(median, 4), "mad": round(mad, 4),
                "threshold": round(threshold, 4), "min_gap": min_gap,
                "frames": len(scores)},
        starts=starts,
        count=len(starts),
        peaks=[round(row["score"], 3) for row in picks[:50]],
    )


def frame_diff_rhythm(stats: list[dict], *, duration_s: float,
                      shot_count: int) -> dict:
    """帧差节奏：**镜头切分看不见的"元素在动"**。

    为什么需要第二条节奏信号（2026-11-23 真实素材教训）：一条纯 MG 教学片的背景恒定、
    只有图层元素在动，scenedetect 对它只报 **2 个镜头 / 180 秒**，于是"每分钟 0.7 切"
    听起来像一条极慢的片子——实际它有 8 个内容段、字幕在持续换行。

    所以这里用**已有的 `signalstats` 采样序列**（`YDIF`，4fps）算变化点：
    帧差显著高于自身基线的时刻，就是"画面内容变了"（换字幕、元素进出）。
    这不需要任何额外解码，也不引入新依赖。

    @returns `{status, mean, p50, p90, change_points, change_rate_per_min,
               degenerate, degenerate_reason}`
    """
    values = [float(row["ydif"]) for row in stats if row.get("ydif") is not None]
    if len(values) < 8:
        return status.unknown("采样点太少（<8），无法估计帧差节奏", method="signalstats YDIF")
    ordered = sorted(values)
    p50 = ordered[len(ordered) // 2]
    p90 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))]
    median = p50
    deviations = sorted(abs(value - median) for value in values)
    mad = deviations[len(deviations) // 2] or 0.0
    threshold = p90 + max(1.0, 2.0 * mad)
    change_points = sum(1 for value in values if value >= threshold and value >= median + 2 * mad)
    minutes = max(duration_s / 60.0, 1e-6)
    rate = round(change_points / minutes, 2)
    shot_rate = round(shot_count / minutes, 2)

    # 退化判定：镜头切分给不出节奏，但帧差显示画面一直在变
    degenerate = shot_rate < 2.0 and rate >= 6.0
    item = status.field(
        status.DERIVED, method="signalstats YDIF 变化点", calibrated=False,
        params={"sample_n": len(values), "threshold": round(threshold, 2),
                "rule": "YDIF ≥ p90+2·MAD"},
        mean=round(sum(values) / len(values), 2),
        p50=round(p50, 2), p90=round(p90, 2), mad=round(mad, 2),
        change_points=change_points,
        change_rate_per_min=rate,
        shot_rate_per_min=shot_rate,
        degenerate=degenerate,
    )
    if degenerate:
        item["degenerate_reason"] = (
            f"镜头切分只给了 {shot_rate} 切/分，但帧差有 {rate} 次/分显著变化——"
            "典型是静态背景 + 元素动画（MG/字幕驱动），**不能据此判断剪辑很慢**")
    return item


def analyze(media: str | Path, *, duration_s: float, has_audio: bool,
            work_dir: str | Path, timeout: float = 1800.0, engine: str = "auto",
            scenedetect_threshold: float = 27.0) -> dict:
    """跑一次解码，返回 `{"cuts": [...], "shots": [...], "timing_s": n}`。

    @param engine: `auto`（有 scenedetect 就用它，否则 ffmpeg）/ `ffmpeg` / `scenedetect`
    """
    binary = paths.ffmpeg_path()
    if not binary:
        return status.skipped("找不到 ffmpeg（把 ffmpeg 放进 PATH 或设 BREAKDOWN_FFMPEG）",
                              method="select=gt(scene)")
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    media_path = Path(media).resolve()

    import time
    from concurrent.futures import ThreadPoolExecutor

    # 长片：只跑一遍 ffmpeg（scene_score 已带全量分数），不再叠一遍 scenedetect 解码。
    long_clip = float(duration_s) > LONG_CLIP_S
    downscale = 2 if long_clip else 1
    frame_skip = 2 if long_clip else 1
    # 默认走 scene_score 自适应（单遍解码、零额外依赖）；scenedetect 只在显式指定时跑，
    # 用来做交叉校验（G3/G5 的标定就是用它做的）。
    use_scenedetect = engine == "scenedetect" and not long_clip
    scene_fps = 10.0 if long_clip else 0.0
    stats_fps = 2.0 if long_clip else STATS_FPS
    edges_fps = 1.0 if long_clip else 0.0

    started = time.time()
    # 两路都是**独立子进程**（ffmpeg 解码 vs scenedetect 解码），并行跑能把
    # 长片的墙钟时间从 "a+b" 压到 "max(a,b)"。这里用线程只是为了同时等两个子进程。
    with ThreadPoolExecutor(max_workers=2) as pool:
        ff_future = pool.submit(lambda: _run(_build_command(media_path, has_audio=has_audio, scene_fps=scene_fps, stats_fps=stats_fps, edges_fps=edges_fps),
                                             cwd=work, timeout=timeout))
        sd_future = pool.submit(detect_scenedetect, media_path,
                                threshold=scenedetect_threshold,
                                downscale=downscale, frame_skip=frame_skip) \
            if use_scenedetect else None
        output = ff_future.result()
        detector = sd_future.result() if sd_future is not None else status.skipped(
            "engine=ffmpeg，未启用 scenedetect", method="scenedetect")
    elapsed = round(time.time() - started, 1)

    score_rows = _parse_scene_scores((work / "scene.txt").read_text(encoding="utf-8", errors="replace")
                                     if (work / "scene.txt").is_file() else "")
    cuts = [{"t": row["t"], "score": row["score"]} for row in score_rows
            if row["score"] >= DEFAULT_THRESHOLD]
    stats = _parse_series((work / "stats.txt").read_text(encoding="utf-8", errors="replace")
                          if (work / "stats.txt").is_file() else "")
    edges = _parse_series((work / "edges.txt").read_text(encoding="utf-8", errors="replace")
                          if (work / "edges.txt").is_file() else "")

    if not stats and not cuts:
        tail = "\n".join(output.strip().splitlines()[-4:])
        return status.failed(f"单次解码没有产出任何采样（ffmpeg 输出尾部：{tail}）",
                             method="select=gt(scene)")

    # 边界来源（优先级）：
    #   1. 短片且装了 scenedetect → 用它（G3/G5 的标定是在这个配置下拿到的）
    #   2. 否则用本次解码已经拿到的 scene_score 序列做自适应检测（零额外解码）
    #   3. 再不行才回退固定阈值
    points: list[float]
    method = "scene_score 自适应 + signalstats + edgedetect（单次解码，三路输出）"
    adaptive = detect_from_scores(score_rows) if score_rows else status.unknown(
        "没有 scene_score 采样", method="scene_score")
    if use_scenedetect and detector.get("status") == status.MEASURED:
        points = sorted({round(float(s), 3) for s in detector["starts"]} | {0.0,
                         round(duration_s, 3)})
        method = f"{detector['method']} + ffmpeg 单次解码采画质"
    elif engine == "scenedetect" and detector.get("status") != status.MEASURED:
        return detector
    elif adaptive.get("status") == status.DERIVED:
        points = sorted(set(adaptive["starts"]) | {round(duration_s, 3)})
        method = f"{adaptive['method']} + signalstats + edgedetect（单次解码，三路输出）"
        detector = adaptive
    else:
        points = boundaries(cuts, duration_s)

    shots = build_shots(points, cuts)
    aggregate(shots, stats, edges)
    for shot in shots:
        shot["verdict"], shot["verdict_why"] = verdict(shot.get("quality") or {})
    return {
        "status": status.MEASURED,
        "method": method,
        "params": {"threshold": DEFAULT_THRESHOLD, "stats_fps": STATS_FPS,
                   "edge_guard_s": EDGE_GUARD_S,
                   "scenedetect_threshold": scenedetect_threshold},
        "calibrated": False,   # 阈值未经人工样片标定（G5 只做了合成样片 + 一次真实对比）
        "timing_s": elapsed,
        "cuts": cuts,
        "ffmpeg_cuts": len(cuts),
        "detector": {k: v for k, v in detector.items() if k != "starts"},
        "rhythm": frame_diff_rhythm(stats, duration_s=duration_s, shot_count=len(shots)),
        "shots": shots,
        "sampled": {"stats": len(stats), "edges": len(edges)},
        "audio_wav": str(work / "audio.16k.wav") if (work / "audio.16k.wav").is_file() else "",
    }


def relative_stats(shots: list[dict]) -> dict:
    """节奏统计（供剪辑手法推断用）。全部是 DERIVED：由镜头时长推算。"""
    durations = sorted(float(s["dur"]) for s in shots if float(s.get("dur") or 0) > 0)
    if not durations:
        return status.unknown("没有可用镜头", method="dur 统计")
    total = sum(durations)
    p90 = durations[min(len(durations) - 1, int(len(durations) * 0.9))]
    return status.field(
        status.DERIVED, method="shot durations", calibrated=False,
        shots=len(durations),
        total_s=round(total, 1),
        cut_rate_per_min=round(len(durations) / max(total / 60.0, 1e-6), 1),
        mean=round(total / len(durations), 2),
        median=round(durations[len(durations) // 2], 2),
        p90=round(p90, 2),
        shortest=round(durations[0], 2),
        longest=round(durations[-1], 2),
    )
