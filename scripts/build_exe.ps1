param(
    [string]$OutputDirectory = "dist\exe"
)

$ErrorActionPreference = "Stop"
$env:PYTHONNOUSERSITE = "1"
$projectDir = Split-Path -Parent $PSScriptRoot
$env:PYTHONUSERBASE = Join-Path $projectDir ".pyinstaller-user"
Set-Location $projectDir

$pythonExe = Join-Path $projectDir ".venv\Scripts\python.exe"
$entryPoint = Join-Path $projectDir "scripts\launch_frontend.py"
$iconPath = Join-Path $projectDir "assets\OwVoice.ico"
$distPath = Join-Path $projectDir $OutputDirectory
$workPath = Join-Path $projectDir "dist\build\pyinstaller"
$specPath = Join-Path $projectDir "dist\build\pyinstaller"

if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw "OwVoice Python environment not found: $pythonExe"
}

$basePrefix = (& $pythonExe -c "import sys; print(sys.base_prefix)").Trim()
$pyinstallerArgs = @(
    "--noconfirm",
    "--clean",
    "--windowed",
    "--onedir",
    "--name", "OwVoice",
    "--distpath", $distPath,
    "--workpath", $workPath,
    "--specpath", $specPath,
    "--paths", $projectDir,
    "--icon", $iconPath,
    "--hidden-import", "frontend.app",
    "--hidden-import", "frontend.startup",
    "--hidden-import", "PySide6.QtMultimedia"
)

foreach ($dllName in @("ffi.dll", "libmpdec-4.dll")) {
    $dllPath = Join-Path $basePrefix "Library\bin\$dllName"
    if (Test-Path -LiteralPath $dllPath) {
        $pyinstallerArgs += @("--add-binary", "$dllPath;.")
    }
}

$pyinstallerArgs += $entryPoint
& $pythonExe -m PyInstaller @pyinstallerArgs

if ($LASTEXITCODE -ne 0) {
    throw "OwVoice.exe build failed."
}

$exePath = Join-Path $distPath "OwVoice\OwVoice.exe"
if (-not (Test-Path -LiteralPath $exePath)) {
    throw "OwVoice.exe was not generated: $exePath"
}

Write-Host "OwVoice.exe build complete: $exePath" -ForegroundColor Green
