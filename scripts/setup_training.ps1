param()

$ErrorActionPreference = "Stop"
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$projectDir = Split-Path -Parent $PSScriptRoot
Set-Location $projectDir
$pythonExe = Join-Path $projectDir ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
    throw "找不到 .venv\Scripts\python.exe。请先运行 scripts\setup.ps1 完成基础环境配置。"
}

Write-Host "正在安装本地训练依赖..." -ForegroundColor Cyan
& $pythonExe -m pip install --prefer-binary -r (Join-Path $projectDir "requirements-training.txt")
if ($LASTEXITCODE -ne 0) { throw "本地训练依赖安装失败。" }

Write-Host "正在下载本地训练所需预训练模型..." -ForegroundColor Cyan
& $pythonExe (Join-Path $projectDir "scripts\download_pretrained.py") --training
if ($LASTEXITCODE -ne 0) { throw "本地训练预训练模型下载失败。" }

Write-Host "正在验证本地训练环境..." -ForegroundColor Cyan
& $pythonExe (Join-Path $projectDir "scripts\verify_runtime.py") --training
if ($LASTEXITCODE -ne 0) { throw "本地训练环境验证未通过，请根据上方报告处理。" }

Write-Host "本地训练环境已就绪。" -ForegroundColor Green
