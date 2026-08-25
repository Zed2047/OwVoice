$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent $PSScriptRoot
$parentDir = Split-Path -Parent $projectDir

# 发布版可以通过 OWVOICE_GSV_ROOT 指向用户自己安装的 GPT-SoVITS。
$gsvRoot = $env:OWVOICE_GSV_ROOT
if ([string]::IsNullOrWhiteSpace($gsvRoot)) {
    $gsvRoot = Join-Path $parentDir "GPT-SoVITS"
}

$pythonExe = Join-Path $gsvRoot "runtime\python.exe"
$apiPy = Join-Path $gsvRoot "api.py"
$modelDir = Join-Path $parentDir "models"
$sovitsPath = Join-Path $modelDir "mambo_e8_s352.pth"
$gptPath = Join-Path $modelDir "mambo-e15.ckpt"
$refWav = Join-Path $modelDir "refer.wav"
$refText = "最近看大家都在讲自己的经历，球波也是忍不住了。"

foreach ($required in @($pythonExe, $apiPy, $sovitsPath, $gptPath, $refWav)) {
    if (-not (Test-Path $required)) {
        throw "找不到所需文件：$required。请设置 OWVOICE_GSV_ROOT 或准备本地模型。"
    }
}

Write-Host "正在启动 GPT-SoVITS API：$gsvRoot"
& $pythonExe $apiPy -a 127.0.0.1 -p 9880 -s $sovitsPath -g $gptPath -dr $refWav -dt $refText -dl zh

