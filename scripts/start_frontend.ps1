$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent $PSScriptRoot
Set-Location $projectDir

$venvPython = Join-Path $projectDir ".venv\Scripts\python.exe"
$launcher = Join-Path $projectDir "scripts\launch_frontend.py"

if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    throw "OwVoice Python environment not found. Run first-time setup first."
}


& $venvPython $launcher
