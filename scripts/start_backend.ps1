$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent $PSScriptRoot
Set-Location $projectDir

$venvPython = Join-Path $projectDir ".venv\Scripts\python.exe"
$launcher = Join-Path $projectDir "scripts\launch_backend.py"

if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host "OwVoice Python environment not found. Run first-time setup first."
    exit 1
}
$pythonExe = $venvPython

if (-not (Test-Path -LiteralPath "config\voices.local.json")) {
    throw "config\voices.local.json not found. Run first-time setup first."
}

& $pythonExe $launcher
