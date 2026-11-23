"""视觉链：把"这一帧画面是什么"变成结构化 JSON，并保证**不会静默降级**。

为什么要有"探针"
----------------
纯文本模型被喂图片时通常**不会报错**：有的后端直接忽略图、有的返回一段格式合法的
胡话。本机实测（2026-11-22）llama.cpp 是很诚实的（HTTP 500 `image input is not
supported`），但不能假设所有后端都这样——尤其 `amdgpu` 网关上的模型**都没声明**
`input_modalities`。

所以每个 provider 必须先用**四张已知答案图**验证：位置（左上红圆）、数字（随机 nonce）、
颜色（右下蓝矩形）、**负样本**（纯白必须回 NONE）。数字带随机 nonce，缓存的旧答案
无法复用。**没过探针的 provider 不许进链**；探针结果 24h 内复用，超过就重探。

链条（顺序可配，默认按"免费 → 本地 → 付费"）
---------------------------------------------
1. `amdgpu`（DSH profile 里那个免费额度；模型按 G1 实测顺序）
2. `cli:opencode`（命令模板）
3. `cli:workbuddy`（命令模板）
4. `local`（llama.cpp OpenAI 兼容端点；必须确认 loaded 模型能吃图）
5. `cloud`（显式 `--allow-cloud` 才启用，按次计费 + 外发）

两条纪律
--------
- **失败不重试同 tier**，直接降级并把原因记进 `unknowns`。
- **外发要披露**：调用方（宿主/CLI）必须在 plan 里说明"会把 N 帧发往 provider/model"，
  `evidence` 里记录实际调用；`allow_cloud=False` 时第 5 档根本不进链。
"""
from __future__ import annotations

import base64
import json
import os
import shlex
import random
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import paths, status

PROBE_TTL_S = 24 * 3600
#: 单帧超时（秒）。视觉模型看一张图不该超过这个数；超了就换下一档。
FRAME_TIMEOUT_S = 45.0
#: 默认帧预算（与档位无关的上限，防止误传 500 帧账单）。
DEFAULT_BUDGET = 100
#: 并发。免费额度通常吃不住更高并发。
DEFAULT_WORKERS = 4

#: G1 实测顺序：最快/最稳的排前面（MiniCPM-V-35B 0.42s，Gemma 1.5s，Qwen 4s+）。
AMdGPU_MODELS = ["MiniCPM-V-4.7-35B-A3B", "Gemma-4-31B-IT",
                 "Qwen3.8-27B", "MiniCPM-V-4.7-1B"]

PROMPT = """你是拉片分析师。只根据这一张画面输出，不要推测前后文，不要解释。
返回严格 JSON（不要 markdown 围栏、不要自然语言说明）：
{"shot_size":"大远景|远景|全景|中景|近景|特写|微距|其他",
 "composition":["三分法","中心","对称","引导线","框架","其他"],
 "subject_ratio":0.0,
 "layout_zones":["左文右图","全屏口播","上下分区","四周角标","其他"],
 "motion_hint":"静止|推|拉|摇|移|手持抖|其他",
 "subtitle":{"present":true,"pos":"上|中|下|角|无","role":"口播字幕|花字|标题|角标|无",
             "style":{"weight":"bold|regular","stroke":true,"background":false}},
 "on_screen_text":["图中出现的文字，逐条列出；没有就给空数组"],
 "confidence":0.0}
约束：subject_ratio 与 confidence 是 0–1 的小数；枚举值只能用上面列出的那些；
看不清的字段给 null，不要编。"""

_ENUMS = {
    "shot_size": {"大远景", "远景", "全景", "中景", "近景", "特写", "微距", "其他"},
    "motion_hint": {"静止", "推", "拉", "摇", "移", "手持抖", "其他"},
}
_COMPOSITION = {"三分法", "中心", "对称", "引导线", "框架", "其他"}
_LAYOUT = {"左文右图", "全屏口播", "上下分区", "四周角标", "其他"}
_SUB_POS = {"上", "中", "下", "角", "无"}
_SUB_ROLE = {"口播字幕", "花字", "标题", "角标", "无"}


# ---------------------------------------------------------------------------
# provider 配置
# ---------------------------------------------------------------------------

def _env_json(name: str) -> list | None:
    raw = str(os.environ.get(name) or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, list) else None


def chain(*, allow_cloud: bool = False, allow_cli: bool = True) -> list[dict]:
    """返回按顺序排列的 provider 列表。

    配置方式：`BREAKDOWN_VISION_CHAIN` 给一个 JSON 数组就能完全接管顺序，例如
    `[{"name":"amdgpu","kind":"openai","base_url":"https://.../v1","model":"MiniCPM-V-4.7-35B-A3B","api_key_env":"AMDGPU_API_KEY"}]`n（`api_key_env` 只是给人看的说明；实现里按 `BREAKDOWN_*` 优先、再读 `AMDGPU_API_KEY` 等固定名字解析，不会执行任意变量名。）`
    """
    explicit = _env_json("BREAKDOWN_VISION_CHAIN")
    if explicit:
        return [item for item in explicit if isinstance(item, dict) and item.get("kind")]

    items: list[dict] = []
    amd_key = str(os.environ.get("BREAKDOWN_AMDGPU_KEY") or os.environ.get("AMDGPU_API_KEY") or "")
    if amd_key:
        base = str(os.environ.get("BREAKDOWN_AMDGPU_BASE") or "https://developer.amd.com.cn/radeon/api/v1")
        for model in AMdGPU_MODELS:
            items.append({"name": f"amdgpu/{model}", "kind": "openai", "base_url": base,
                          "model": model, "api_key": amd_key, "tier": "free"})
    if allow_cli:
        # CLI 档**默认关闭**：它是"本机恰好装了某个 agent CLI"时的补充路径，
        # 开源默认不假设用户有它。设 BREAKDOWN_OCP_CMD / BREAKDOWN_WB_CMD 才进链。
        for name, template in (("cli:opencode", os.environ.get("BREAKDOWN_OCP_CMD") or ""),
                               ("cli:workbuddy", os.environ.get("BREAKDOWN_WB_CMD") or "")):
            if template:
                items.append({"name": name, "kind": "cli", "command": template, "tier": "free"})
    local_base = str(os.environ.get("BREAKDOWN_LOCAL_VLM_BASE") or "")
    local_model = str(os.environ.get("BREAKDOWN_LOCAL_VLM_MODEL") or "")
    if local_base and local_model:
        items.append({"name": f"local/{local_model}", "kind": "openai", "base_url": local_base,
                      "model": local_model, "tier": "local",
                      "api_key": str(os.environ.get("BREAKDOWN_LOCAL_VLM_KEY") or "local"),
                      "require_loaded_match": True})
    if allow_cloud:
        cloud_key = str(os.environ.get("DEEPSEEK_API_KEY") or "")
        if cloud_key:
            items.append({"name": "cloud/deepseek", "kind": "openai", "tier": "paid",
                          "base_url": str(os.environ.get("BREAKDOWN_CLOUD_BASE")
                                          or "https://api.deepseek.com/v1"),
                          "model": str(os.environ.get("BREAKDOWN_CLOUD_MODEL") or "deepseek-v4-flash-vision"),
                          "api_key": cloud_key})
    return items


# ---------------------------------------------------------------------------
# 探针（四张已知答案图；数字用随机 nonce）
# ---------------------------------------------------------------------------

def _probe_dir() -> Path:
    target = paths.ensure_tmp() / "vision-probe"
    target.mkdir(parents=True, exist_ok=True)
    return target


def build_probe_images(nonce: int) -> dict[str, tuple[Path, str, str]]:
    """造 4 张图 → `{case: (path, question, expectation_code)}`。

    没有 PIL 就返回空 dict（调用方把探针标 SKIPPED，而不是假装通过）。
    """
    try:
        from PIL import Image, ImageDraw, ImageFont  # noqa: PLC0415
    except ImportError:
        return {}
    folder = _probe_dir()
    size = (640, 480)
    out: dict[str, tuple[Path, str, str]] = {}

    image = Image.new("RGB", size, "white")
    ImageDraw.Draw(image).ellipse((40, 40, 120, 120), fill=(220, 30, 30))
    out["position"] = (folder / "p1.png", "这张图的左上角有什么？只回答「颜色+形状」两项。", "red_circle")
    image.save(out["position"][0])

    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    font = None
    for candidate in ("C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf",
                      "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        if Path(candidate).is_file():
            font = ImageFont.truetype(candidate, 160)
            break
    text = f"{nonce:04d}"
    if font is not None:
        box = draw.textbbox((0, 0), text, font=font)
        draw.text(((size[0] - (box[2] - box[0])) / 2, (size[1] - (box[3] - box[1])) / 2 - box[1]),
                  text, fill="black", font=font)
    else:
        for rect in [(200, 190, 240, 290), (250, 190, 290, 290), (300, 190, 340, 290), (350, 190, 390, 290)]:
            draw.rectangle(rect, fill="black")
    out["number"] = (folder / "p2.png", "图中间的数字是什么？只输出数字本身。", text)
    image.save(out["number"][0])

    image = Image.new("RGB", size, "white")
    ImageDraw.Draw(image).rectangle((440, 320, 600, 440), fill=(20, 60, 200))
    out["color"] = (folder / "p3.png", "这张图的右下角是什么颜色、什么形状？只回答两项。", "blue_rect")
    image.save(out["color"][0])

    image = Image.new("RGB", size, "white")
    out["negative"] = (folder / "p4.png", "图中是否有任何文字或图形？什么都没有就只输出 NONE。", "none")
    image.save(out["negative"][0])
    return out


def judge(case: str, answer: str, expectation: str) -> bool:
    low = (answer or "").lower()
    if case == "position":
        return ("红" in answer or "red" in low) and any(k in answer for k in ("圆", "点")) or "circle" in low
    if case == "number":
        return expectation in re.sub(r"[\s,]", "", answer or "")
    if case == "color":
        return ("蓝" in answer or "blue" in low) and any(k in answer for k in ("矩", "方", "块"))
    if case == "negative":
        return any(k in low for k in ("none", "无", "没有", "空", "blank", "nothing"))
    return False


def _cache_path() -> Path:
    return paths.home() / "vision-probe.json"


def load_probe_cache() -> dict:
    path = _cache_path()
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_probe_cache(cache: dict) -> None:
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass  # 缓存写不进去不影响本次结果


def _cache_key(provider: dict) -> str:
    return f"{provider.get('kind')}|{provider.get('base_url') or provider.get('command')}|{provider.get('model')}"


# ---------------------------------------------------------------------------
# 单次调用
# ---------------------------------------------------------------------------

def _post_openai(provider: dict, image_b64: str, prompt: str, *, timeout: float) -> dict:
    payload = {
        "model": provider.get("model") or "",
        "temperature": 0,
        "max_tokens": 400,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + image_b64}},
        ]}],
    }
    request = urllib.request.Request(
        str(provider["base_url"]).rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + str(provider.get("api_key") or "local")},
        method="POST")
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8", errors="replace"))
        choice = (body.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(str(part.get("text") or "") for part in content
                               if isinstance(part, dict))
        return {"ok": True, "text": str(content or "").strip(),
                "elapsed_s": round(time.time() - started, 2),
                "model": str(body.get("model") or provider.get("model") or "")}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        return {"ok": False, "status": exc.code, "error": f"HTTP {exc.code}: {detail}",
                # 429 是**限流**，不是"这个模型看不了图"——两者必须分开，
                # 否则一次限流会被写进 24h 探针缓存，等于把该模型封一天。
                "rate_limited": exc.code == 429,
                "elapsed_s": round(time.time() - started, 2)}
    except Exception as exc:  # noqa: BLE001 - 网络问题要可读
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "elapsed_s": round(time.time() - started, 2)}


def _run_cli(provider: dict, image_path: Path, prompt: str, *, timeout: float) -> dict:
    """把图交给本地 CLI 代理（如 opencode / workbuddy）。命令模板里 `{image}` `{prompt}` 会被替换。

    **不用 `shell=True`**（2026-11-23 评审阻断项）：模板来自环境变量，替换进去的是文件路径，
    走 shell 就等于把路径当代码解释——路径里一个 `&` 或 `"` 就能追加命令。
    这里先按 shell 规则**切分成 argv**，再在**单个 argv 元素内部**做占位符替换，
    于是路径再怎么古怪都只是一个参数，不会被解释。
    """
    template = str(provider.get("command") or "")
    if not template:
        return {"ok": False, "error": "命令模板为空"}
    try:
        argv = shlex.split(template, posix=os.name != "nt")
    except ValueError as exc:
        return {"ok": False, "error": f"命令模板无法解析：{exc}"}
    if not argv:
        return {"ok": False, "error": "命令模板为空"}
    argv = [part.replace("{image}", str(image_path)).replace("{prompt}", prompt) for part in argv]
    started = time.time()
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, env=paths.child_env())
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "elapsed_s": round(time.time() - started, 2)}
    text = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    return {"ok": proc.returncode == 0 and bool(text), "text": text[-4000:],
            "error": "" if proc.returncode == 0 else f"exit {proc.returncode}",
            "elapsed_s": round(time.time() - started, 2)}


def ask(provider: dict, image_path: str | Path, prompt: str,
        *, timeout: float = FRAME_TIMEOUT_S) -> dict:
    path = Path(image_path)
    if not path.is_file():
        return {"ok": False, "error": f"图不存在：{path}"}
    if provider.get("kind") == "cli":
        return _run_cli(provider, path, prompt, timeout=timeout)
    try:
        payload = base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError as exc:
        return {"ok": False, "error": f"读图失败：{exc}"}
    return _post_openai(provider, payload, prompt, timeout=timeout)


#: 送图前的最长边（像素）。852×480 的原始帧实测单帧要 45 秒；缩到 768 后通常 2–4 秒，
#: 而这个分辨率对"景别/构图/字幕位置"的判断已经足够。
MAX_EDGE = 768
#: 重编码质量（PIL 口径，82 ≈ 视觉无损）。
JPEG_QUALITY = 82


def prepare_image(path: str | Path) -> Path:
    """把帧压到 `MAX_EDGE` 以内再送模型；失败就退回原图（不阻断）。

    缓存键含源文件 mtime+size，源变了会重压。
    """
    source = Path(path)
    try:
        from PIL import Image  # noqa: PLC0415
    except ImportError:
        return source
    try:
        stat = source.stat()
    except OSError:
        return source
    target = paths.ensure_tmp() / "vision-upload"
    target.mkdir(parents=True, exist_ok=True)
    cached = target / f"{source.stem}-{int(stat.st_mtime)}-{stat.st_size}.jpg"
    if cached.is_file():
        return cached
    try:
        with Image.open(source) as image:
            picture = image.convert("RGB")
            if max(picture.size) > MAX_EDGE:
                ratio = MAX_EDGE / float(max(picture.size))
                picture = picture.resize((max(1, int(picture.width * ratio)),
                                          max(1, int(picture.height * ratio))),
                                         Image.Resampling.LANCZOS)
            picture.save(cached, "JPEG", quality=JPEG_QUALITY)
        return cached
    except Exception:  # noqa: BLE001 - 压缩失败不是错误，退回原图
        return source


# ---------------------------------------------------------------------------
# provider 验证（探针 + 每次 run 的快检）
# ---------------------------------------------------------------------------

def verify(provider: dict, *, force: bool = False, ttl_s: int = PROBE_TTL_S,
           quick: bool = False) -> dict:
    """验证一个 provider 是否"真的看得见"。

    @param quick: 只跑 2 张（数字 + 负样本），用于每次 run 前的复检
    @returns `{ok, cases, reason, cached, elapsed_s}`
    """
    key = _cache_key(provider)
    cache = load_probe_cache()
    entry = cache.get(key) or {}
    fresh = (time.time() - float(entry.get("verified_at") or 0)) < ttl_s
    if fresh and not force and not quick:
        return {"ok": bool(entry.get("ok")), "cached": True,
                "verified_at": entry.get("verified_at"), "cases": entry.get("cases") or [],
                "reason": "" if entry.get("ok") else (entry.get("reason") or "上次未通过")}

    nonce = random.randint(1000, 9999)
    images = build_probe_images(nonce)
    if not images:
        return {"ok": False, "cached": False, "cases": [],
                "reason": "没有 PIL，无法造探针图（pip install pillow）"}

    cases = ["number", "negative"] if quick else ["position", "number", "color", "negative"]
    results = []
    rate_limited = False
    started = time.time()
    for case in cases:
        path, question, expectation = images[case]
        outcome = ask(provider, prepare_image(path), question)
        if outcome.get("rate_limited"):
            rate_limited = True
        passed = bool(outcome.get("ok")) and judge(case, outcome.get("text", ""), expectation)
        results.append({"case": case, "pass": passed, "expect": expectation,
                        "answer": (outcome.get("text") or outcome.get("error") or "")[:120]})
        if not passed:
            break
    ok = all(item["pass"] for item in results) and len(results) == len(cases)
    verdict = {"ok": ok, "cached": False, "cases": results, "nonce": nonce,
               "rate_limited": rate_limited,
               "elapsed_s": round(time.time() - started, 2),
               "reason": "" if ok else ("探针被限流（429），本次跳过该 provider，结论不写缓存"
                                        if rate_limited else "探针未通过（该 provider 不进链）")}
    # 限流不代表"看不了图"：不写缓存，否则一次 429 会把这个模型封掉一整天
    if not quick and not rate_limited:
        cache[key] = {"verified_at": time.time(), "ok": ok, "cases": results,
                      "reason": verdict["reason"]}
        save_probe_cache(cache)
    return verdict


def filter_chain(items: list[dict], *, quick: bool = True) -> tuple[list[dict], list[dict]]:
    """按顺序验证，返回 `(可用 provider, 被淘汰的证据)`。"""
    usable: list[dict] = []
    rejected: list[dict] = []
    for provider in items:
        verdict = verify(provider, quick=quick)
        if verdict.get("ok"):
            usable.append({**provider, "probe": {"ok": True, "cases": verdict.get("cases")}})
        else:
            rejected.append({"provider": provider.get("name"), "reason": verdict.get("reason"),
                             "cases": verdict.get("cases")})
        if usable:
            break  # 第一档可用就够；后面按需降级
    return usable, rejected


# ---------------------------------------------------------------------------
# 逐帧描述
# ---------------------------------------------------------------------------

def _extract_json(text: str) -> dict | None:
    raw = str(text or "").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    decoder = json.JSONDecoder()
    for index, char in enumerate(raw):
        if char != "{":
            continue
        try:
            value, _end = decoder.raw_decode(raw[index:])
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _coerce(payload: dict) -> dict:
    """把模型输出规整成契约形状：枚举外的值降级成"其他"，数值夹到 0–1。"""
    def one_of(value, allowed, default="其他"):
        text = str(value or "").strip()
        return text if text in allowed else default

    def ratio(value):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return round(min(1.0, max(0.0, number)), 3)

    subtitle = payload.get("subtitle") if isinstance(payload.get("subtitle"), dict) else {}
    style = subtitle.get("style") if isinstance(subtitle.get("style"), dict) else {}
    composition = [one_of(item, _COMPOSITION) for item in (payload.get("composition") or [])
                   if isinstance(item, (str, int))]
    layout = [one_of(item, _LAYOUT) for item in (payload.get("layout_zones") or [])
              if isinstance(item, (str, int))]
    texts = [str(item).strip() for item in (payload.get("on_screen_text") or [])
             if str(item or "").strip()]
    return {
        "shot_size": one_of(payload.get("shot_size"), _ENUMS["shot_size"], "其他"),
        "composition": [c for c in composition if c != "其他"] or [],
        "subject_ratio": ratio(payload.get("subject_ratio")),
        "layout_zones": [z for z in layout if z != "其他"] or [],
        "motion_hint": one_of(payload.get("motion_hint"), _ENUMS["motion_hint"], "其他"),
        "subtitle": {
            "present": bool(subtitle.get("present")),
            "pos": one_of(subtitle.get("pos"), _SUB_POS, "无"),
            "role": one_of(subtitle.get("role"), _SUB_ROLE, "无"),
            "style": {"weight": one_of(style.get("weight"), {"bold", "regular"}, "regular"),
                      "stroke": bool(style.get("stroke")),
                      "background": bool(style.get("background"))},
        },
        "on_screen_text": texts[:12],
        "confidence": ratio(payload.get("confidence")),
    }


def describe_frame(provider: dict, frame: dict, *, timeout: float = FRAME_TIMEOUT_S) -> dict:
    outcome = ask(provider, prepare_image(frame["path"]), PROMPT, timeout=timeout)
    if not outcome.get("ok"):
        return status.failed(outcome.get("error") or "调用失败",
                             method=f"{provider.get('name')}")
    payload = _extract_json(outcome.get("text") or "")
    if payload is None:
        return status.failed("模型没有返回 JSON", method=f"{provider.get('name')}")
    visual = _coerce(payload)
    missing = [key for key in ("shot_size", "subtitle", "on_screen_text")
               if key not in payload]
    item = status.field(status.MEASURED, method=f"vlm/{provider.get('name')}",
                        params={"prompt": "v1", "timeout_s": timeout},
                        provider=provider.get("name"), model=outcome.get("model") or provider.get("model"),
                        elapsed_s=outcome.get("elapsed_s"), **visual)
    if missing:
        item["partial"] = missing
    item["evidence"] = [Path(frame["path"]).name]
    item["shot_id"] = frame.get("shot_id")
    item["t"] = frame.get("t")
    return item


def describe_frames(frames: list[dict], *, allow_cloud: bool = False, allow_cli: bool = True,
                    budget: int = DEFAULT_BUDGET, workers: int = DEFAULT_WORKERS) -> dict:
    """对一批帧做视觉描述。返回 `{status, provider, items, failures, chain}`。"""
    selected = frames[: max(0, int(budget))]
    if not selected:
        return status.skipped("没有可描述的帧", method="vlm")
    candidates = chain(allow_cloud=allow_cloud, allow_cli=allow_cli)
    if not candidates:
        return status.skipped("没有可用的视觉 provider（设 AMDGPU_API_KEY / 本地 VLM / 云端）",
                              method="vlm")
    usable, rejected = filter_chain(candidates, quick=True)
    if not usable:
        return status.failed("所有视觉 provider 都没过探针：" + json.dumps(rejected, ensure_ascii=False)[:400],
                             method="vlm")
    provider = usable[0]
    items: list[dict] = []
    failures: list[dict] = []
    started = time.time()
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        futures = {pool.submit(describe_frame, provider, frame): frame for frame in selected}
        for future in as_completed(futures):
            frame = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001
                result = status.failed(f"{type(exc).__name__}: {exc}", method="vlm")
            if result.get("status") == status.MEASURED:
                items.append(result)
            else:
                failures.append({"shot_id": frame.get("shot_id"), "reason": result.get("reason")})
    items.sort(key=lambda row: (row.get("t") or 0))
    if not items:
        return status.failed("所有帧都失败了：" + json.dumps(failures[:3], ensure_ascii=False),
                             method="vlm")
    elapsed = round(time.time() - started, 1)
    out = status.field(
        status.MEASURED, method=f"vlm/{provider.get('name')}",
        params={"budget": len(selected), "workers": workers},
        provider=provider.get("name"), model=provider.get("model") or "",
        frames=len(items), failed=len(failures),
        elapsed_s=elapsed,
        per_frame_s=round(elapsed / max(len(items), 1), 2),
        items=items, failures=failures, rejected=rejected,
    )
    if failures:
        out["partial"] = True
    return out


def summarise(visual: dict) -> dict:
    """把逐帧描述聚成"这条片子的画面语言"（全部 DERIVED：模型判断 + 计数）。"""
    if visual.get("status") != status.MEASURED:
        return visual
    items = visual.get("items") or []
    if not items:
        return status.unknown("没有可汇总的帧", method="vlm-summary")

    def tally(key: str) -> dict:
        counts: dict[str, int] = {}
        for item in items:
            value = item.get(key)
            if isinstance(value, str) and value:
                counts[value] = counts.get(value, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True))

    subtitle_frames = [item for item in items if (item.get("subtitle") or {}).get("present")]
    positions: dict[str, int] = {}
    roles: dict[str, int] = {}
    for item in subtitle_frames:
        subtitle = item.get("subtitle") or {}
        pos = str(subtitle.get("pos") or "无")
        role = str(subtitle.get("role") or "无")
        positions[pos] = positions.get(pos, 0) + 1
        roles[role] = roles.get(role, 0) + 1
    texts: list[str] = []
    for item in items:
        for text in item.get("on_screen_text") or []:
            if text not in texts:
                texts.append(text)
    return status.field(
        status.DERIVED, method="vlm items 计数", calibrated=False,
        provider=visual.get("provider"),
        frames=len(items),
        shot_size=tally("shot_size"),
        motion=tally("motion_hint"),
        subtitle_ratio=round(len(subtitle_frames) / len(items), 3),
        subtitle_pos=dict(sorted(positions.items(), key=lambda kv: kv[1], reverse=True)),
        subtitle_role=dict(sorted(roles.items(), key=lambda kv: kv[1], reverse=True)),
        on_screen_text=texts[:40],
        confidence_mean=round(
            sum(float(item.get("confidence") or 0) for item in items) / len(items), 3),
    )
