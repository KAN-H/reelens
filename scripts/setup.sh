#!/usr/bin/env sh
# video-breakdown setup (Linux / macOS)
# Idempotent: re-running only fills gaps. No system-wide changes.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"

echo "[1/4] Python"
PYTHON=${BREAKDOWN_PYTHON:-}
if [ -z "$PYTHON" ]; then
  if command -v python3 >/dev/null 2>&1; then PYTHON=python3; else PYTHON=python; fi
fi
"$PYTHON" -c 'import sys; assert sys.version_info >= (3, 10), "需要 Python 3.10+"'
echo "      $PYTHON"

echo "[2/4] venv .venv-breakdown"
if [ ! -x .venv-breakdown/bin/python ]; then "$PYTHON" -m venv .venv-breakdown; fi

echo "[3/4] install"
.venv-breakdown/bin/python -m pip install --upgrade pip wheel setuptools --quiet
if ! .venv-breakdown/bin/python -m pip install -e "python[all]" --quiet; then
  echo "      warning: 可选依赖没装全（scenes/asr/audio/download 之一失败），相关段落会标 SKIPPED" >&2
  .venv-breakdown/bin/python -m pip install -e "python" --quiet
fi

echo "[4/4] self-check"
.venv-breakdown/bin/python -m reelens.cli probe || {
  echo "      warning: 自检未全绿（ffmpeg 需在 PATH，或设 BREAKDOWN_FFMPEG）" >&2
}

cat <<'EOF'

跑一条试试：
  .venv-breakdown/bin/python -m reelens.cli run <媒体文件或URL> --out outputs/demo --tier fast

视觉 provider 自检（会消耗少量额度）：
  .venv-breakdown/bin/python -m reelens.cli vision-probe
EOF