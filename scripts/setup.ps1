# video-breakdown setup (Windows / PowerShell)
# 幂等：重复跑只会补缺。不写系统设置、不装全局包。

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "[1/5] Node 依赖（插件自带 ffmpeg/ffprobe）"
$npm = Get-Command npm -ErrorAction SilentlyContinue
if (-not $npm) { throw '找不到 npm；DSH 插件需要 Node.js/npm 来安装自带 ffmpeg/ffprobe' }
& $npm.Source install --omit=dev --ignore-scripts --no-audit --no-fund
if ($LASTEXITCODE -ne 0) { throw 'npm 依赖安装失败；未取得自带 ffmpeg/ffprobe' }

Write-Host "[2/5] Python 解释器"
$python = $env:BREAKDOWN_PYTHON
if (-not $python) {
  foreach ($candidate in @('python', 'python3')) {
    $found = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($found) { $python = $found.Source; break }
  }
}
if (-not $python) { throw '找不到 python，请先安装 Python 3.10+ 或设置 BREAKDOWN_PYTHON' }
Write-Host "      $python"

Write-Host "[3/5] 虚拟环境 .venv-breakdown"
$venv = Join-Path $root '.venv-breakdown'
if (-not (Test-Path (Join-Path $venv 'Scripts\python.exe'))) {
  & $python -m venv $venv
}
$venvPython = Join-Path $venv 'Scripts\python.exe'

Write-Host "[4/5] 安装核心包（可选依赖装不上只提示，不中断）"
& $venvPython -m pip install --upgrade pip wheel setuptools --quiet
& $venvPython -m pip install -e "python[all]" --quiet
if ($LASTEXITCODE -ne 0) {
  Write-Warning '可选依赖没装全（scenes/asr/audio/download 之一失败）。核心功能仍可用，相关段落会标 SKIPPED。'
  & $venvPython -m pip install -e "python" --quiet
}

Write-Host "[5/5] 自检"
& $venvPython -m reelens.cli probe
if ($LASTEXITCODE -ne 0) {
  Write-Warning '自检未全绿：上面缺什么就装什么（ffmpeg 需在 PATH，或设 BREAKDOWN_FFMPEG）。'
}
Write-Host ''
Write-Host '跑一条试试：'
Write-Host ('  ' + $venvPython + ' -m reelens.cli run <媒体文件或URL> --out outputs\demo --tier fast')
Write-Host '视觉 provider 自检（会消耗少量额度）：'
Write-Host ('  ' + $venvPython + ' -m reelens.cli vision-probe')
