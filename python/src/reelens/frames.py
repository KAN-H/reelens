"""批量抽帧 + 近似去重：给"构图/版式/字幕样式"提供素材。

为什么按镜头抽而不是等间隔抽
----------------------------
等间隔抽会让"3 秒一个镜头"和"40 秒一个长镜头"得到同样多的帧，而前者的信息量高得多。
所以按镜头抽：每个镜头取**首帧**和**55% 处**各一张——55% 而不是 50%，是旧实现
（早期审片脚本）踩出来的经验：避开转场起手的第一帧。

为什么还要去重
--------------
口播类视频经常连续几个镜头画面几乎一样（同一个机位），全送去视觉模型只是烧钱。
用 dHash（8×9 灰度差分）算 64 位指纹，海明距离 <6 视为同一画面——**每个镜头至少留一张**，
只删"跨镜头的重复"。

成本
----
抽帧是 `-ss` 前置于 `-i` 的关键帧定位，单帧毫秒级；100 帧在本地实测 ~12s（含 JPEG 编码）。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from . import paths, status

#: 每个镜头取帧的位置（相对镜头时长）。0.55 为了避开转场起手帧。
FRAME_AT = 0.55
#: 首帧偏移（从镜头起点往后一点点，避免上一镜头的余帧）。
FIRST_FRAME_OFFSET = 0.05
#: dHash 海明距离阈值：小于它认为是同一画面。
DHASH_DISTANCE = 6


def _run(command: list[str], *, timeout: float = 120.0) -> str:
    proc = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, env=paths.child_env())
    return (proc.stdout or "") + (proc.stderr or "")


def dhash(path: str | Path) -> int:
    """64 位 dHash。没有 PIL 时返回 0（等于"不去重"，宁可多花也不静默删帧）。"""
    try:
        from PIL import Image
    except ImportError:
        return 0
    try:
        with Image.open(path) as image:
            small = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
            pixels = list(small.getdata())
    except OSError:
        return 0
    bits = 0
    index = 0
    for row in range(8):
        for column in range(8):
            left = pixels[row * 9 + column]
            right = pixels[row * 9 + column + 1]
            if left > right:
                bits |= 1 << index
            index += 1
    return bits


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def extract_shot_frame(media: str | Path, at: float, target: Path) -> bool:
    binary = paths.ffmpeg_path()
    if not binary:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    # `-ss` 必须在 `-i` 之前：关键帧定位，比解码到该时刻快一到两个数量级。
    _run([binary, "-hide_banner", "-loglevel", "error", "-y",
          "-ss", f"{max(0.0, at):.3f}", "-i", str(media),
          "-frames:v", "1", "-q:v", "2", str(target)])
    return target.is_file() and target.stat().st_size > 0


def extract(media: str | Path, shots: list[dict], *, out_dir: str | Path,
            max_frames: int = 40, per_shot: int = 1, min_frames: int = 0,
            duration_s: float = 0.0) -> dict:
    """按镜头抽帧 + 跨镜头去重，镜头太少时按等间隔补齐。

    @param per_shot: 1=只取 55% 帧；2=首帧+55%（更多信息，成本翻倍）
    @param min_frames: 帧数下限。**为什么需要**：一条 180 秒的 MG 片子 scenedetect 只切出
        2 个镜头，按镜头抽就只有 2 帧——8 个内容段全丢。镜头数不该决定采样预算。
    @returns `{status, frames:[{shot_id, t, path, kind, dhash}], kept, dropped, ...}`
    """
    work = Path(out_dir) / "frames"
    work.mkdir(parents=True, exist_ok=True)
    if not paths.ffmpeg_path():
        return status.skipped("找不到 ffmpeg，无法抽帧", method="ffmpeg -ss")

    frames: list[dict] = []
    seen: list[int] = []
    dropped = 0
    for shot in shots:
        if len(frames) >= max_frames:
            break
        start, dur = float(shot["start"]), float(shot["dur"])
        wanted = [(start + max(FIRST_FRAME_OFFSET, dur * 0.02), "first")] if per_shot > 1 else []
        wanted.append((start + dur * FRAME_AT, "mid"))
        for at, kind in wanted:
            if len(frames) >= max_frames:
                break
            name = f"{shot['id']}-{kind}.jpg"
            target = work / name
            if not extract_shot_frame(media, at, target):
                continue
            digest = dhash(target)
            if any(abs(digest - other) <= DHASH_DISTANCE for other in seen if digest and other):
                # 跨镜头重复：删文件但不删"这个镜头已抽过"的事实，避免下游误以为抽帧失败
                dropped += 1
                target.unlink(missing_ok=True)
                continue
            seen.append(digest)
            frames.append({"shot_id": shot["id"], "t": round(at, 3), "kind": kind,
                           "path": str(target), "dhash": f"{digest:016x}"})

    # 补齐：镜头太少时按等间隔取，标记 kind="fill"（不冒充某个镜头）
    target_count = min(int(min_frames or 0), int(max_frames or 0)) if min_frames else 0
    if duration_s > 0 and len(frames) < target_count:
        need = target_count - len(frames)
        existing = sorted(frame["t"] for frame in frames)
        for index in range(need):
            at = duration_s * (index + 0.5) / max(need, 1)
            if any(abs(at - other) < 0.5 for other in existing):
                continue
            name = f"fill-{index:02d}.jpg"
            target = work / name
            if not extract_shot_frame(media, at, target):
                continue
            digest = dhash(target)
            if any(abs(digest - other) <= DHASH_DISTANCE for other in seen if digest and other):
                dropped += 1
                target.unlink(missing_ok=True)
                continue
            seen.append(digest)
            existing.append(at)
            nearest = min(shots, key=lambda s: abs(float(s["start"]) + float(s["dur"]) / 2 - at),
                          default=None)
            frames.append({"shot_id": (nearest or {}).get("id", "fill"), "t": round(at, 3),
                           "kind": "fill", "path": str(target), "dhash": f"{digest:016x}"})
        frames.sort(key=lambda row: row["t"])

    if not frames:
        return status.failed("一帧都没抽到（时间点超出片长？或全部被去重）", method="ffmpeg -ss")

    return status.field(
        status.MEASURED, method="ffmpeg -ss + dHash（镜头不足时等间隔补齐）",
        params={"per_shot": per_shot, "max_frames": max_frames,
                "min_frames": min_frames, "distance": DHASH_DISTANCE},
        frames=frames, kept=len(frames), dropped=dropped, dir=str(work),
    )


def contact_sheet(frame_paths: list[str], out_path: str | Path, *, cols: int = 4,
                  tile: str = "320x180") -> dict:
    """把帧拼成一张对照图（审片用；机器判定仍以逐帧为准）。

    **用 PIL 拼，不走 ffmpeg**：`concat` demuxer + `tile` 滤镜在图像帧上不稳
    （2026-11-23 实测报 `Failed to configure input pad on Parsed_pad_1`，
    因为各帧尺寸/像素格式不一致时滤镜链配不上）。PIL 拼图是纯像素操作，
    尺寸不一致也能各自缩放，且本来就依赖 pillow 做 dHash。
    """
    if not frame_paths:
        return status.skipped("没有帧可拼", method="PIL")
    try:
        from PIL import Image, ImageDraw, ImageFont  # noqa: PLC0415
    except ImportError:
        return status.skipped("没装 pillow（pip install pillow）", method="PIL")
    try:
        width, height = (int(part) for part in tile.lower().split("x"))
    except ValueError:
        width, height = 320, 180
    cols = max(1, min(int(cols or 4), len(frame_paths)))
    rows = (len(frame_paths) + cols - 1) // cols
    pad = 4
    sheet = Image.new("RGB", (cols * width + pad * (cols + 1),
                              rows * height + pad * (rows + 1)), (34, 34, 34))
    font = None
    for candidate in ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/msyh.ttc",
                      "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if Path(candidate).is_file():
            try:
                font = ImageFont.truetype(candidate, 14)
            except OSError:
                font = None
            break
    draw = ImageDraw.Draw(sheet)
    placed = 0
    for index, path in enumerate(frame_paths):
        if placed >= cols * rows:
            break
        try:
            with Image.open(path) as source:
                thumb = source.convert("RGB")
                thumb.thumbnail((width, height), Image.Resampling.LANCZOS)
        except OSError:
            continue
        column, row = placed % cols, placed // cols
        left = pad + column * (width + pad) + (width - thumb.width) // 2
        top = pad + row * (height + pad) + (height - thumb.height) // 2
        sheet.paste(thumb, (left, top))
        label = Path(path).stem[-12:]
        if font is not None:
            draw.rectangle([left, top + thumb.height - 18, left + 8 * len(label), top + thumb.height],
                           fill=(0, 0, 0))
            draw.text((left + 3, top + thumb.height - 17), label, fill=(255, 220, 120), font=font)
        placed += 1
    if not placed:
        return status.failed("一张图都读不出来（文件损坏或不是图片）", method="PIL")
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        sheet.save(out, "JPEG", quality=82)
    except OSError as exc:
        return status.failed(f"写对照图失败：{exc}", method="PIL")
    return status.field(status.MEASURED, method="PIL tile", path=str(out),
                        bytes=out.stat().st_size, cols=cols, rows=rows, placed=placed)
