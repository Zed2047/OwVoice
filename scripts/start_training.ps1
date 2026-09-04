$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent $PSScriptRoot
$gsvRoot = $env:OWVOICE_GSV_ROOT
if ([string]::IsNullOrWhiteSpace($gsvRoot)) { $gsvRoot = Join-Path $projectDir "GPT-SoVITS" }
$venvPython = Join-Path $projectDir ".venv\Scripts\python.exe"
$pythonExe = $venvPython
$webui = Join-Path $gsvRoot "webui.py"
if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) { throw "未找到 Python：$pythonExe。请先完成首次环境配置。" }
if (-not (Test-Path -LiteralPath $webui -PathType Leaf)) { throw "未找到 GPT-SoVITS 训练界面：$webui" }
& $pythonExe -c "import gradio" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "缺少训练依赖 gradio。请执行：" -ForegroundColor Yellow
    Write-Host ".\.venv\Scripts\python.exe -m pip install -r requirements-training.txt" -ForegroundColor Cyan
    Read-Host "按 Enter 键退出"
    exit 2
}$env:GRADIO_ANALYTICS_ENABLED = "False"
$env:GRADIO_SERVER_NAME = "127.0.0.1"
Set-Location $gsvRoot
Write-Host "正在启动本地 GPT-SoVITS 训练界面..."
Write-Host "训练完成后请在 OwVoice 中导入模型。"
& $pythonExe $webui
if ($LASTEXITCODE -ne 0) { throw "训练界面退出，退出码：$LASTEXITCODE" }
