$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent $PSScriptRoot
Set-Location $projectDir

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "未找到虚拟环境，请先安装依赖："
    Write-Host "py -m venv .venv"
    Write-Host ".\.venv\Scripts\pip.exe install -r requirements.txt"
    exit 1
}

if (-not (Test-Path "config\voices.local.json")) {
    Copy-Item "config\voices.example.json" "config\voices.local.json"
    Write-Host "已创建 config\voices.local.json，请先填写模型和参考音频路径。"
    exit 1
}

& ".venv\Scripts\python.exe" -m uvicorn backend.server:app --host 127.0.0.1 --port 8765

