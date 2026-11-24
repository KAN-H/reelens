"""路径与外部工具的解析——所有"本机细节"都收敛在这一处。

为什么单独一个模块
------------------
这个包要开源。开源的第一条纪律是**任何地方都不许出现写死的本机绝对路径**。
所以 ffmpeg / ffprobe / whisper.cpp / 模型目录 / 工作目录 / 临时目录全部走
"环境变量 → 已知安装点 → PATH" 的解析顺序，并且解析失败时给**可执行的建议**，
而不是抛一个 FileNotFoundError 让人去猜。

三个实测出来的坑（2026-11-22，Windows 11）
------------------------------------------
1. **`TEMP` 可能指向一个子进程写不进去的目录**（Windows 上常见：系统把 TEMP 指到别的盘，
   而进程只对工作区可写）。子进程（venv python / ffmpeg）写它会被拒（WinError 5），
   于是 pip 与模型下载会以"找不到可用临时目录"这种完全误导人的方式失败。
   → `ensure_tmp()` 把 `/tmp` 落到**工作区内**。
2. **代理环境变量会毒死国内站点**：本机的 `HTTP_PROXY`/`HTTPS_PROXY`（例如指向本地代理端口）会
   代理没起时 `api.bilibili.com` 直接 `WinError 10061`。→ `child_env()` 默认
   **剥掉代理变量**（拆解只访问国内站点与本地文件），需要代理时显式传 `proxy=True`。
3. **`ffmpeg -vf` 的 `file=` 参数不能吃 Windows 绝对路径**（见 `shots.py` 注释）。
"""
from __future__ import annotations

import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Optional

#: 本包根目录（仓库内 `python/src/reelens/paths.py` → 上溯到仓库根）
PACKAGE_DIR = Path(__file__).resolve().parent
REPO_DIR = PACKAGE_DIR.parents[2]


def _env(name: str) -> str:
    return str(os.environ.get(name) or "").strip()


def _first_file(*candidates: str) -> str:
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return ""


def _node_platform_dir() -> str:
    """@ffmpeg-installer / @ffprobe-installer 的平台目录名。"""
    system = {"win32": "win32", "darwin": "darwin"}.get(sys.platform, "linux")
    machine = platform.machine().lower()
    arch = {
        "amd64": "x64", "x86_64": "x64",
        "arm64": "arm64", "aarch64": "arm64",
        "i386": "ia32", "i686": "ia32", "x86": "ia32",
        "armv7l": "arm",
    }.get(machine, machine)
    return f"{system}-{arch}"


def _bundled_npm_binary(installer: str, executable: str) -> str:
    """DSH 插件 npm 依赖自带的可执行文件；纯 pip 安装时目录不存在，直接跳过。"""
    suffix = ".exe" if sys.platform == "win32" else ""
    candidate = (REPO_DIR / "node_modules" / installer / _node_platform_dir()
                 / f"{executable}{suffix}")
    return str(candidate) if candidate.is_file() else ""


def ffmpeg_path() -> str:
    """ffmpeg。顺序：显式环境变量 → 插件自带 npm 二进制 → PATH。"""
    return _first_file(_env("REELENS_FFMPEG"), _env("BREAKDOWN_FFMPEG")) \
        or _bundled_npm_binary("@ffmpeg-installer", "ffmpeg") \
        or shutil.which("ffmpeg") or ""


def ffprobe_path() -> str:
    """ffprobe。顺序：显式环境变量 → 插件自带 / 与 ffmpeg 同源布局 → PATH。

    `@ffprobe-installer` 与 `@ffmpeg-installer` 是两个 npm 包，ffprobe **不在**
    ffmpeg 那个目录里；只按 PATH 找会拿不到。
    """
    explicit = _first_file(_env("REELENS_FFPROBE"), _env("BREAKDOWN_FFPROBE"))
    if explicit:
        return explicit
    bundled = _bundled_npm_binary("@ffprobe-installer", "ffprobe")
    if bundled:
        return bundled
    ffmpeg = Path(ffmpeg_path()) if ffmpeg_path() else None
    if ffmpeg is not None:
        sibling = (ffmpeg.parent.parent.parent / "@ffprobe-installer"
                   / _node_platform_dir() / f"ffprobe{'.exe' if sys.platform == 'win32' else ''}")
        if sibling.is_file():
            return str(sibling)
        for parent in ffmpeg.parents:
            for candidate in parent.glob("@ffprobe-installer/*/ffprobe*"):
                if candidate.is_file():
                    return str(candidate)
    return shutil.which("ffprobe") or ""


def whisper_cpp() -> str:
    return _first_file(_env("REELENS_WHISPER_CPP"), _env("BREAKDOWN_WHISPER_CPP")) \
        or shutil.which("whisper-cli") or shutil.which("whisper") or ""


def whisper_model() -> str:
    return _first_file(_env("REELENS_WHISPER_MODEL"), _env("BREAKDOWN_WHISPER_MODEL"))


def whisper_no_gpu() -> bool:
    flag = (_env("REELENS_WHISPER_NO_GPU") or _env("BREAKDOWN_WHISPER_NO_GPU")).lower()
    return flag in {"1", "true", "yes", "on"}


def _writable(target: Path) -> bool:
    """能不能真的写进去（只 mkdir 不够：本机沙箱会让子进程对某些路径 WinError 5）。"""
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def home() -> Path:
    """配置与缓存根。**开源默认值在前，本机兜底在后**。

    为什么要有兜底：本机（Windows + 沙箱）里子进程对 `%APPDATA%` 是**拒绝写**的
    （WinError 5），只对工作区内可写。直接崩掉不如退到仓库内 `.cache`（已 gitignore）。
    """
    explicit = _env("BREAKDOWN_HOME")
    if explicit:
        return Path(explicit)
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home())
        primary = Path(base) / "reelens"
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        primary = Path(base) / "reelens"
    for candidate in (primary, REPO_DIR / ".cache"):
        if _writable(candidate):
            return candidate
    return Path(_env("BREAKDOWN_TMPDIR") or (REPO_DIR / ".tmp"))


def hf_cache_dir() -> Path:
    """ASR 模型缓存目录——**不能靠继承 `HF_HOME`**。

    2026-11-23 真实事故：系统级 `HF_HOME` 指向一个空目录，而模型实际下在
    插件仓库的 `.models/hf`。命令行里手动设了 `HF_HOME` 所以跑得很好，但宿主插件起的子进程
    继承系统变量 → faster-whisper 找不到模型 → 静默回落到 whisper.cpp → 产出幻觉。
    **"在我机器上能跑"就是这么来的。**

    解析顺序：`BREAKDOWN_HF_HOME` → 仓库内 `.models/hf`（开发态已有模型）→ 可写数据目录。
    """
    explicit = _env("BREAKDOWN_HF_HOME")
    if explicit:
        target = Path(explicit)
        try:
            target.mkdir(parents=True, exist_ok=True)
            return target
        except OSError:
            pass
    repo_cache = REPO_DIR / ".models" / "hf"
    if (repo_cache / "hub").is_dir():
        return repo_cache
    target = home() / "models" / "hf"
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return target


def ensure_tmp() -> Path:
    """为子进程准备一个**确定可写**的临时目录（见模块头第 1 条坑）。

    只改本进程的 `TMP/TEMP`（子进程继承），不改系统设置。
    """
    target = Path(_env("BREAKDOWN_TMPDIR") or (home() / "tmp"))
    if not _writable(target):
        target = REPO_DIR / ".tmp"
        target.mkdir(parents=True, exist_ok=True)
    os.environ["TMP"] = str(target)
    os.environ["TEMP"] = str(target)
    return target


def child_env(*, proxy: bool = False, extra: Optional[dict] = None) -> dict:
    """子进程环境：默认**剥代理**（见模块头第 2 条坑），并固定 UTF-8 输出。

    `proxy=True` 只在明确需要（例如用户配了海外镜像）时才保留代理变量。
    """
    env = dict(os.environ)
    if not proxy:
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
            env.pop(name, None)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env


def tool_report() -> dict:
    """一次性给出"这台机器能做什么"——`breakdown probe` 与宿主自检共用。"""
    ffmpeg = ffmpeg_path()
    ffprobe = ffprobe_path()
    return {
        "ffmpeg": {"path": ffmpeg, "ok": bool(ffmpeg),
                   "hint": "" if ffmpeg else "把 ffmpeg 放进 PATH，或设 BREAKDOWN_FFMPEG 指向可执行文件"},
        "ffprobe": {"path": ffprobe, "ok": bool(ffprobe),
                    "hint": "" if ffprobe else "把 ffprobe 放进 PATH，或设 BREAKDOWN_FFPROBE"},
        "whisper_cpp": {"path": whisper_cpp(), "model": whisper_model(),
                        "ok": bool(whisper_cpp() and whisper_model()),
                        "hint": "" if (whisper_cpp() and whisper_model())
                        else "设 BREAKDOWN_WHISPER_CPP 与 BREAKDOWN_WHISPER_MODEL（可选，装不了就用 faster-whisper）"},
        "tmp": str(ensure_tmp()),
        "python": sys.version.split()[0],
    }
