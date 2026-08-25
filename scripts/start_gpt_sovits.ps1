$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent $PSScriptRoot

# 发布版可以通过 OWVOICE_GSV_ROOT 指向用户自己安装的 GPT-SoVITS。
$gsvRoot = $env:OWVOICE_GSV_ROOT
if ([string]::IsNullOrWhiteSpace($gsvRoot)) {
    $gsvRoot = Join-Path $projectDir "GPT-SoVITS"
}

$pythonExe = Join-Path $gsvRoot "runtime\python.exe"
$apiPy = Join-Path $gsvRoot "api.py"
$configPath = Join-Path $projectDir "config\voices.local.json"

if (-not (Test-Path $configPath)) {
    throw "找不到人物配置：$configPath。请先复制 voices.example.json 为 voices.local.json。"
}

$config = Get-Content $configPath -Raw | ConvertFrom-Json
$voice = $config.voices | Where-Object { $_.enabled -ne $false } | Select-Object -First 1
if ($null -eq $voice) {
    throw "voices.local.json 中没有启用的人物。"
}

function Resolve-VoicePath([string]$value) {
    if ([string]::IsNullOrWhiteSpace($value)) {
        return $null
    }
    if ([System.IO.Path]::IsPathRooted($value)) {
        return [System.IO.Path]::GetFullPath($value)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $projectDir $value))
}

$sovitsPath = Resolve-VoicePath $voice.sovits_model
$gptPath = Resolve-VoicePath $voice.gpt_model
$refWav = Resolve-VoicePath $voice.reference_audio
$refText = $voice.prompt_text

foreach ($required in @($pythonExe, $apiPy, $sovitsPath, $gptPath, $refWav)) {
    if (-not (Test-Path $required)) {
        throw "找不到所需文件：$required。请设置 OWVOICE_GSV_ROOT 或准备本地模型。"
    }
}

Write-Host "正在启动 GPT-SoVITS API：$gsvRoot"
Write-Host "启动角色：$($voice.display_name)"
& $pythonExe $apiPy -a 127.0.0.1 -p 9880 -s $sovitsPath -g $gptPath -dr $refWav -dt $refText -dl zh
