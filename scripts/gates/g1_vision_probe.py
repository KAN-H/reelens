#!/usr/bin/env python3
"""G1 视觉基线：用"4 张已知答案图"探测一个视觉 provider/model 是否真的看得见。

为什么要 4 张而不是 1 张
------------------------
纯文本模型被喂图片时，通常**不会报错**，而是照样编出一段格式合法的回答。
单图 + "返回 JSON" 这种探针无法区分"看见"与"编造"。所以这里用四张图分别考四个
互不相同的能力：位置、数字、颜色、以及**负样本**（空白图必须回答 NONE）。
负样本是关键：只会说"有字/有图形"的模型在 p4 上必然翻车。

安全
----
密钥只从凭据文件读进内存，**任何情况下都不打印**；输出里只保留模型名、耗时、
判定结果与模型原文回答。

用法
----
    python scripts/gates/g1_vision_probe.py                 # 探测默认 amdgpu 白名单
    python scripts/gates/g1_vision_probe.py --model X --limit 4
    python scripts/gates/g1_vision_probe.py --base-url http://127.0.0.1:8090 --model "G4 Gemma4-E4B-131K-IMG"
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

GATE_DIR = Path(__file__).resolve().parents[2] / ".dev-notes" / "gates" / "g1"
IMAGE_DIR = GATE_DIR / "images"
REPORT = GATE_DIR / "g1-report.json"

DEFAULT_BASE_URL = "https://developer.amd.com.cn/radeon/api/v1"
DEFAULT_MODELS = [
    "DeepSeek-V4-Flash-Vision-Exp",
    "MiniCPM-V-4.7-35B-A3B",
    "Qwen3.8-27B",
    "Gemma-4-31B-IT",
]

#: 每张图：文件名 / 画什么 / 问什么 / 判定函数名
CASES = [
    {
        "id": "p1_position",
        "ask": "这张图的左上角有什么？只回答「颜色+形状」两项，例如「红色圆形」。不要解释。",
        "expect": "红 + 圆（red + circle）",
    },
    {
        "id": "p2_number",
        "ask": "图中间的数字是什么？只输出数字本身，不要解释。",
        "expect": "4827",
    },
    {
        "id": "p3_color",
        "ask": "这张图的右下角是什么颜色、什么形状？只回答两项，例如「蓝色矩形」。不要解释。",
        "expect": "蓝 + 矩形（blue + rectangle）",
    },
    {
        "id": "p4_negative",
        "ask": "图中是否有任何文字或图形？如果什么都没有，只输出 NONE。不要解释。",
        "expect": "NONE（负样本：空白图）",
    },
]


# ---------------------------------------------------------------------------
# 造图（PIL；答案是我们自己画的，不依赖任何外部素材）
# ---------------------------------------------------------------------------


def build_images() -> dict[str, Path]:
    from PIL import Image, ImageDraw, ImageFont

    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    size = (640, 480)
    paths: dict[str, Path] = {}

    # p1：左上红圆
    img = Image.new("RGB", size, "white")
    ImageDraw.Draw(img).ellipse((40, 40, 120, 120), fill=(220, 30, 30))
    paths["p1_position"] = IMAGE_DIR / "p1_position.png"
    img.save(paths["p1_position"])

    # p2：中间数字 4827
    img = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(img)
    font = None
    for candidate in ("C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf",
                      "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        if Path(candidate).is_file():
            font = ImageFont.truetype(candidate, 160)
            break
    text = "4827"
    if font is not None:
        box = draw.textbbox((0, 0), text, font=font)
        draw.text(((size[0] - (box[2] - box[0])) / 2, (size[1] - (box[3] - box[1])) / 2 - box[1]),
                  text, fill="black", font=font)
    else:  # 没字体就画四个粗黑块，至少不是空白图
        for index, rect in enumerate([(200, 190, 240, 290), (250, 190, 290, 290),
                                      (300, 190, 340, 290), (350, 190, 390, 290)]):
            draw.rectangle(rect, fill="black")
    paths["p2_number"] = IMAGE_DIR / "p2_number.png"
    img.save(paths["p2_number"])

    # p3：右下蓝矩形
    img = Image.new("RGB", size, "white")
    ImageDraw.Draw(img).rectangle((440, 320, 600, 440), fill=(20, 60, 200))
    paths["p3_color"] = IMAGE_DIR / "p3_color.png"
    img.save(paths["p3_color"])

    # p4：纯白负样本
    img = Image.new("RGB", size, "white")
    paths["p4_negative"] = IMAGE_DIR / "p4_negative.png"
    img.save(paths["p4_negative"])

    return paths


# ---------------------------------------------------------------------------
# 判定（按内容，不按 schema）
# ---------------------------------------------------------------------------


def _has(text: str, *needles: str) -> bool:
    low = text.lower()
    return any(n.lower() in low for n in needles)


def judge(case_id: str, answer: str) -> bool:
    if case_id == "p1_position":
        return _has(answer, "红", "red") and _has(answer, "圆", "circle", "round", "点")
    if case_id == "p2_number":
        return "4827" in re.sub(r"[\s,]", "", answer)
    if case_id == "p3_color":
        return _has(answer, "蓝", "blue") and _has(answer, "矩", "方", "rectangle", "square", "块")
    if case_id == "p4_negative":
        return _has(answer, "none", "无", "没有", "空", "blank", "nothing", "纯白")
    return False


# ---------------------------------------------------------------------------
# 凭据与 HTTP
# ---------------------------------------------------------------------------


def read_credential(name: str, path: Path | None = None) -> str:
    """从 ~/.dsh/.credentials.yaml 读一个 ref 的字面值。**只返回，不打印。**"""
    cred_path = path or (Path.home() / ".dsh" / ".credentials.yaml")
    if not cred_path.is_file():
        return ""
    in_refs = False
    for raw in cred_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if re.match(r"^refs:\s*$", raw):
            in_refs = True
            continue
        if in_refs and re.match(r"^\S", raw):  # 回到顶层键
            in_refs = False
        match = re.match(r"^\s+" + re.escape(name) + r":\s*(\S.*?)\s*$", raw)
        if match and in_refs:
            value = match.group(1).strip().strip('"').strip("'")
            return value
    return ""


def ask(base_url: str, api_key: str, model: str, image: Path, prompt: str,
        *, timeout: float = 90.0) -> dict:
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 300,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": {"url": "data:image/png;base64,"
                               + base64.b64encode(image.read_bytes()).decode("ascii")}},
            ],
        }],
    }
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_key},
        method="POST",
    )
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8", errors="replace"))
        choice = (body.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = message.get("content")
        if isinstance(content, list):  # 少数实现返回 part 数组
            content = " ".join(str(p.get("text") or "") for p in content if isinstance(p, dict))
        return {"ok": True, "answer": str(content or "").strip(),
                "elapsed_s": round(time.time() - started, 2),
                "reported_model": str(body.get("model") or "")}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        return {"ok": False, "status": exc.code, "error": detail,
                "elapsed_s": round(time.time() - started, 2)}
    except Exception as exc:  # noqa: BLE001 - 探针要能报告任何失败
        return {"ok": False, "status": "exception",
                "error": f"{type(exc).__name__}: {exc}",
                "elapsed_s": round(time.time() - started, 2)}


def probe_model(base_url: str, api_key: str, model: str, images: dict[str, Path]) -> dict:
    results = []
    for case in CASES:
        outcome = ask(base_url, api_key, model, images[case["id"]], case["ask"])
        passed = bool(outcome.get("ok")) and judge(case["id"], outcome.get("answer", ""))
        results.append({
            "case": case["id"], "expect": case["expect"], "pass": passed, **outcome,
        })
    passed_n = sum(1 for r in results if r["pass"])
    return {
        "model": model,
        "base_url": base_url,
        "verdict": "USABLE" if passed_n == len(CASES) else ("PARTIAL" if passed_n else "UNUSABLE"),
        "passed": passed_n,
        "total": len(CASES),
        "cases": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="G1：用已知答案图探测视觉模型是否真的看得见")
    parser.add_argument("--base-url", default=os.environ.get("G1_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--model", action="append", default=None,
                        help="可重复；缺省用 amdgpu 白名单")
    parser.add_argument("--key-env", default="AMDGPU_API_KEY",
                        help="凭据文件里的 ref 名（默认 AMDGPU_API_KEY）")
    parser.add_argument("--api-key", default="", help="直接给 key（不推荐；会出现在进程参数里）")
    parser.add_argument("--out", default=str(REPORT))
    args = parser.parse_args(argv)

    models = args.model or DEFAULT_MODELS
    api_key = args.api_key or read_credential(args.key_env)
    if not api_key:
        print(json.dumps({"ok": False, "error": f"读不到凭据 {args.key_env}（~/.dsh/.credentials.yaml）"},
                         ensure_ascii=False))
        return 2

    try:
        images = build_images()
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"造图失败：{type(exc).__name__}: {exc}"},
                         ensure_ascii=False))
        return 2

    report = {
        "schema": "video-breakdown.gate.g1.v1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "base_url": args.base_url,
        "images": {k: str(v.relative_to(GATE_DIR)) for k, v in images.items()},
        "results": [],
    }
    for model in models:
        item = probe_model(args.base_url, api_key, model, images)
        report["results"].append(item)
        print(f"[{item['verdict']:<8}] {model} — {item['passed']}/{item['total']}")
        for case in item["cases"]:
            flag = "OK " if case["pass"] else "!! "
            answer = (case.get("answer") or case.get("error") or "")[:80].replace("\n", " ")
            print(f"   {flag}{case['case']:<12} {case.get('elapsed_s', '?')}s  {answer}")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    usable = [r["model"] for r in report["results"] if r["verdict"] == "USABLE"]
    print()
    print(json.dumps({"ok": bool(usable), "usable": usable, "report": str(out_path)},
                     ensure_ascii=False))
    return 0 if usable else 1


if __name__ == "__main__":
    raise SystemExit(main())
