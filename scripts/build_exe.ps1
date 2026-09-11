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
$specFile = Join-Path $specPath "OwVoice.spec"

if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw "OwVoice Python environment not found: $pythonExe"
}

# PyInstaller 会复用同名 spec 文件；如果不删除旧 spec，新增的 hidden-import
# 和 exclude-module 参数可能不会写入本次构建，导致训练依赖意外进入 EXE。
if (Test-Path -LiteralPath $specFile) {
    Remove-Item -LiteralPath $specFile -Force
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
    "--add-data", "$projectDir\assets;assets",
    "--icon", $iconPath,
    "--hidden-import", "frontend.app",
    "--hidden-import", "frontend.startup",
    # startup.py 在运行时动态导入 backend.server，必须显式告知 PyInstaller。
    "--hidden-import", "backend.server",
    "--hidden-import", "backend.model_catalog",
    "--hidden-import", "backend.model_manager",
    "--hidden-import", "backend.training_errors",
    "--hidden-import", "backend.update_manager",
    # 训练依赖由用户点击本地训练后在项目 .venv 中按需加载，不能进入主 EXE。
    # 训练服务仍由项目 .venv 启动，故这些模块不能作为桌面端的打包依赖。
    "--exclude-module", "backend.training",
    "--exclude-module", "torch",
    "--exclude-module", "torchaudio",
    "--exclude-module", "transformers",
    "--exclude-module", "funasr",
    "--exclude-module", "modelscope",
    "--exclude-module", "tensorboard",
    "--exclude-module", "gradio",
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

# onedir 构建的 EXE 依赖同目录 _internal；只复制 EXE 会导致运行库版本不一致。
$rootExePath = Join-Path $projectDir "OwVoice.exe"
$running = @(Get-Process -Name "OwVoice" -ErrorAction SilentlyContinue | Where-Object {
    $processPath = $null
    try { $processPath = $_.Path } catch { return $false }
    if ([string]::IsNullOrWhiteSpace($processPath)) { return $false }
    try { [System.IO.Path]::GetFullPath($processPath) -eq [System.IO.Path]::GetFullPath($rootExePath) }
    catch { $false }
})
if ($running.Count -gt 0) {
    throw "主目录 OwVoice.exe 正在运行，请关闭后重新构建并同步：$rootExePath"
}

$builtInternalPath = Join-Path (Split-Path -Parent $exePath) "_internal"
$rootInternalPath = Join-Path $projectDir "_internal"
Copy-Item -LiteralPath $exePath -Destination $rootExePath -Force
if (Test-Path -LiteralPath $builtInternalPath) {
    if (-not (Test-Path -LiteralPath $rootInternalPath)) {
        New-Item -ItemType Directory -Path $rootInternalPath | Out-Null
    }
    Get-ChildItem -LiteralPath $builtInternalPath -Force | Copy-Item -Destination $rootInternalPath -Recurse -Force
}
Write-Host "OwVoice.exe and _internal synchronized to: $projectDir" -ForegroundColor Green
