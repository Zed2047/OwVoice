$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent $PSScriptRoot

# The project-local GPT-SoVITS directory is used by default.
$gsvRoot = $env:OWVOICE_GSV_ROOT
if ([string]::IsNullOrWhiteSpace($gsvRoot)) {
    $gsvRoot = Join-Path $projectDir "GPT-SoVITS"
}

$pythonExe = Join-Path $projectDir ".venv\Scripts\python.exe"
$apiPy = Join-Path $gsvRoot "api.py"
$configPath = Join-Path $projectDir "config\voices.local.json"

if (-not (Test-Path $configPath)) {
    throw "Voice configuration not found: $configPath"
}

$config = Get-Content $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
$voice = $config.voices | Where-Object { $_.enabled -ne $false } | Select-Object -First 1
if ($null -eq $voice) {
    throw "No enabled voice exists in voices.local.json."
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
        throw "Required file not found: $required"
    }
}

Write-Host "Starting GPT-SoVITS API: $gsvRoot"
Write-Host "Voice: $($voice.display_name)"
Set-Location $gsvRoot
$cutPunc = [string]::Concat([char]0xFF0C,[char]0x3002,[char]0xFF1F,[char]0xFF01,[char]0xFF1B,[char]0xFF1A,",.?!",[char]0x2026)
& $pythonExe $apiPy -a 127.0.0.1 -p 9880 -s $sovitsPath -g $gptPath -dr $refWav -dt $refText -dl zh -cp $cutPunc
