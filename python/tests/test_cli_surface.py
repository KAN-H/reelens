"""CLI 表面契约：README / SKILL.md / CI 里承诺的每个 verb 与 flag 都必须真的存在。

为什么有这条（2026-11-23 真实事故）：
    第一版 CI 里写着 `reelens probe --json`，而 `probe` 从来没有 `--json` 参数
    （它本来就只输出 JSON）。本地 `pytest` 和 `node test-*.mjs` 全绿——因为没有一条
    测试碰过命令行表面；错误在第一次推送时才被 GitHub Actions 发现，四个 python job 全红。

这条测试把"命令行表面"变成可断言的对象：改参数名、删参数、或者文档里写了一个不存在的
flag，都会在**本地**先失败，而不是等 CI。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

VERBS = ("probe", "info", "list", "vision-probe", "run", "report", "frames", "clean")

#: 文档与 CI 里明确承诺过的 flag（改这些等于改公开契约，必须同步改文档与测试）
DOCUMENTED_FLAGS = {
    "run": ("--out", "--tier", "--items", "--max-seconds", "--no-asr", "--no-vision",
            "--allow-cloud", "--asr-engine", "--max-height", "--vision-budget",
            "--plan-id", "--frames", "--transcript-limit"),
    "report": ("--section", "--markdown", "--transcript-limit"),
    "list": ("--out", "--limit"),
    "vision-probe": ("--quick", "--force", "--allow-cloud"),
}

#: 明确**不应该**存在的 flag：把踩过的坑钉住，避免有人"顺手加回来"又改坏调用方
FORBIDDEN_FLAGS = {
    "probe": ("--json",),   # probe 只输出 JSON，没有开关
}


def _help(verb: str) -> str:
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    proc = subprocess.run(
        [sys.executable, "-m", "reelens.cli", verb, "--help"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
    )
    # argparse 在 Windows 上可能用 cp1252 输出，这里只要求退出码 0 与能读到文本
    assert proc.returncode == 0, f"`{verb} --help` 退出码 {proc.returncode}: {proc.stderr[:300]}"
    return proc.stdout + proc.stderr


@pytest.mark.parametrize("verb", VERBS)
def test_verb_exists(verb: str) -> None:
    output = _help(verb)
    assert "usage:" in output.lower(), f"{verb} 没有打印 usage"


@pytest.mark.parametrize("verb,flags", sorted(DOCUMENTED_FLAGS.items()))
def test_documented_flags_exist(verb: str, flags: tuple[str, ...]) -> None:
    output = _help(verb)
    missing = [flag for flag in flags if flag not in output]
    assert not missing, f"`reelens {verb}` 缺少文档里承诺的参数: {missing}"


@pytest.mark.parametrize("verb,flags", sorted(FORBIDDEN_FLAGS.items()))
def test_forbidden_flags_absent(verb: str, flags: tuple[str, ...]) -> None:
    output = _help(verb)
    present = [flag for flag in flags if flag in output]
    assert not present, (
        f"`reelens {verb}` 出现了不该有的参数 {present}——"
        f"这正是 2026-11-23 那次 CI 全红的原因（`probe --json` 不存在）。"
        f"要么去掉它，要么同步更新文档与 FORBIDDEN_FLAGS。"
    )


def test_top_level_lists_every_verb() -> None:
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    proc = subprocess.run(
        [sys.executable, "-m", "reelens.cli", "--help"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
    )
    assert proc.returncode == 0
    missing = [verb for verb in VERBS if verb not in proc.stdout]
    assert not missing, f"顶层 help 缺少子命令: {missing}"


#: 会写 `reelens <verb> --flag` 的文件——CI 与文档都算"承诺面"
_DOC_FILES = (
    ".github/workflows/ci.yml",
    ".github/workflows/publish.yml",
    "README.md",
    "SKILL.md",
    "python/README.md",
    "docs/DETAIL.md",
)

_INVOCATION = re.compile(r"\breelens\s+(?P<verb>[a-z][a-z-]*)(?P<rest>[^\n`|]*)")
_FLAG = re.compile(r"(?<![\w-])(--[a-z][a-z-]*)")


def _repo_root() -> Path:
    """测试在 `python/tests/` 下运行，仓库根在上两级。"""
    return Path(__file__).resolve().parents[2]


def test_ci_and_docs_only_use_real_flags() -> None:
    """CI 与文档里出现的 `reelens <verb> --flag` 必须真的被该 verb 接受。

    这条直接对应 2026-11-23 的事故：CI 写了 `reelens probe --json`，而 probe 没有该参数。
    """
    root = _repo_root()
    help_cache: dict[str, str] = {}
    problems: list[str] = []

    for relative in _DOC_FILES:
        path = root / relative
        if not path.exists():
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith(("#", "//")):
                continue
            for match in _INVOCATION.finditer(line):
                verb = match.group("verb")
                if verb not in VERBS:
                    continue
                if verb not in help_cache:
                    help_cache[verb] = _help(verb)
                accepted = help_cache[verb]
                for flag in _FLAG.findall(match.group("rest")):
                    if flag not in accepted:
                        problems.append(f"{relative}:{lineno}: `reelens {verb} {flag}` —— {flag} 不存在")

    assert not problems, "文档/CI 与 CLI 表面不一致：\n" + "\n".join(problems)

