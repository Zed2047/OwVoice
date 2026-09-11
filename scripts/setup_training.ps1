param()

$ErrorActionPreference = "Stop"
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$projectDir = Split-Path -Parent $PSScriptRoot
Set-Location $projectDir
$pythonExe = Join-Path $projectDir ".venv\Scripts\python.exe"
$uvExe = Join-Path $projectDir "tools\uv\uv.exe"
$cacheDir = Join-Path $projectDir ".cache"
$statePath = Join-Path $cacheDir "setup-state.json"

if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
    throw "找不到 .venv\Scripts\python.exe。请先运行 setup.bat 完成基础环境配置。"
}
if (-not (Test-Path -LiteralPath $uvExe -PathType Leaf)) { throw "缺少 tools\uv\uv.exe，请重新下载完整发布包。" }
$env:UV_CACHE_DIR = Join-Path $cacheDir "uv"
$env:UV_PYTHON_INSTALL_DIR = Join-Path $projectDir ".runtime\python"
$env:UV_PROJECT_ENVIRONMENT = Join-Path $projectDir ".venv"
$torchVersion = (& $pythonExe -c "import torch; print(torch.__version__)").Trim()
if ($LASTEXITCODE -ne 0) { throw "无法判断现有 CPU/GPU 环境，请先重新运行 setup.bat。" }
$mode = if ($torchVersion -match "\+cu") { "gpu" } else { "cpu" }

Write-Host "正在安装本地训练依赖..." -ForegroundColor Cyan
& $uvExe sync --frozen --extra $mode --extra training --no-dev --no-install-project
if ($LASTEXITCODE -ne 0) { throw "本地训练依赖安装失败。" }

Write-Host "正在下载本地训练所需预训练模型..." -ForegroundColor Cyan
& $pythonExe (Join-Path $projectDir "scripts\download_pretrained.py") --training
if ($LASTEXITCODE -ne 0) { throw "本地训练预训练模型下载失败。" }

Write-Host "正在验证本地训练环境..." -ForegroundColor Cyan
& $pythonExe (Join-Path $projectDir "scripts\verify_runtime.py") --training
if ($LASTEXITCODE -ne 0) { throw "本地训练环境验证未通过，请根据上方报告处理。" }

$environmentHash = ((Get-FileHash (Join-Path $projectDir "uv.lock") -Algorithm SHA256).Hash + (Get-FileHash (Join-Path $projectDir "pyproject.toml") -Algorithm SHA256).Hash).ToLowerInvariant()
$state = [ordered]@{ schema=2; dependenciesReady=$true; trainingReady=$true; mode=$mode.ToUpperInvariant(); environmentSha256=$environmentHash; python=$torchVersion; updatedAt=[DateTime]::UtcNow.ToString("o") } | ConvertTo-Json
New-Item -ItemType Directory -Force -Path $cacheDir | Out-Null
[System.IO.File]::WriteAllText($statePath, $state, $utf8)

Write-Host "本地训练环境已就绪。" -ForegroundColor Green
