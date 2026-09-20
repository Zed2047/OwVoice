param(
    [string]$OutputDirectory = "dist\exe"
)

$ErrorActionPreference = "Stop"
$env:PYTHONNOUSERSITE = "1"
$projectDir = Split-Path -Parent $PSScriptRoot
$compatibilityScript = Join-Path $PSScriptRoot "verify_text_compatibility.ps1"
$windowsPowerShell = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
& $windowsPowerShell -NoProfile -ExecutionPolicy Bypass -File $compatibilityScript -ProjectRoot $projectDir
if ($LASTEXITCODE -ne 0) { throw "文本兼容性检查失败，已停止 EXE 构建。" }
. (Join-Path $PSScriptRoot "release_common.ps1")
$releaseIdentity = Get-OwVoiceReleaseIdentity -ProjectRoot $projectDir -ValidateMirrors
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
    "--hidden-import", "backend.app_version",
    "--hidden-import", "backend.model_catalog",
    "--hidden-import", "backend.model_manager",
    "--hidden-import", "backend.training_errors",
    "--hidden-import", "backend.update_manager",
    "--hidden-import", "backend.process_lifecycle",
    "--hidden-import", "frontend.update_ui",
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
Copy-Item -LiteralPath (Join-Path $projectDir "version.json") -Destination (Split-Path -Parent $exePath) -Force

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
$syncRoot = Join-Path $projectDir (".cache\build-exe-sync\" + [Guid]::NewGuid().ToString("N"))
$incomingInternalPath = Join-Path $syncRoot "_internal"
$backupInternalPath = Join-Path $syncRoot "_internal-backup"
$backupExePath = Join-Path $syncRoot "OwVoice.exe.backup"
New-Item -ItemType Directory -Force -Path $syncRoot | Out-Null
$rootInternalMoved = $false
$rootExeMoved = $false
try {
    if (-not (Test-Path -LiteralPath $builtInternalPath -PathType Container)) {
        throw "PyInstaller 未生成 _internal：$builtInternalPath"
    }
    # 先复制到独立 incoming 目录，避免根目录出现半棵运行库树。
    Copy-Item -LiteralPath $builtInternalPath -Destination $incomingInternalPath -Recurse -Force
    if (Test-Path -LiteralPath $rootInternalPath) {
        Move-Item -LiteralPath $rootInternalPath -Destination $backupInternalPath -Force
        $rootInternalMoved = $true
    }
    if (Test-Path -LiteralPath $rootExePath) {
        Move-Item -LiteralPath $rootExePath -Destination $backupExePath -Force
        $rootExeMoved = $true
    }
    Move-Item -LiteralPath $incomingInternalPath -Destination $rootInternalPath -Force
    Copy-Item -LiteralPath $exePath -Destination $rootExePath -Force

    $builtExeHash = (Get-FileHash -LiteralPath $exePath -Algorithm SHA256).Hash.ToLowerInvariant()
    $rootExeHash = (Get-FileHash -LiteralPath $rootExePath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($builtExeHash -ne $rootExeHash) { throw "根目录 EXE 与构建产物 SHA256 不一致。" }
    $builtFiles = @(Get-ChildItem -LiteralPath $builtInternalPath -Recurse -File -Force)
    $rootFiles = @(Get-ChildItem -LiteralPath $rootInternalPath -Recurse -File -Force)
    if ($builtFiles.Count -ne $rootFiles.Count) {
        throw "根目录 _internal 文件数量与构建产物不一致：$($rootFiles.Count) / $($builtFiles.Count)"
    }
    foreach ($builtFile in $builtFiles) {
        $relative = $builtFile.FullName.Substring($builtInternalPath.Length).TrimStart("\", "/")
        $rootFile = Join-Path $rootInternalPath $relative
        if (-not (Test-Path -LiteralPath $rootFile -PathType Leaf)) { throw "根目录 _internal 缺少文件：$relative" }
        if ((Get-Item -LiteralPath $rootFile).Length -ne $builtFile.Length) { throw "根目录 _internal 文件大小不一致：$relative" }
        $builtHash = (Get-FileHash -LiteralPath $builtFile.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        $rootHash = (Get-FileHash -LiteralPath $rootFile -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($builtHash -ne $rootHash) { throw "根目录 _internal 文件哈希不一致：$relative" }
    }
    Remove-Item -LiteralPath $backupInternalPath -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $backupExePath -Force -ErrorAction SilentlyContinue
} catch {
    Remove-Item -LiteralPath $rootInternalPath -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $rootExePath -Force -ErrorAction SilentlyContinue
    if ($rootInternalMoved -and (Test-Path -LiteralPath $backupInternalPath)) {
        Move-Item -LiteralPath $backupInternalPath -Destination $rootInternalPath -Force
    }
    if ($rootExeMoved -and (Test-Path -LiteralPath $backupExePath)) {
        Move-Item -LiteralPath $backupExePath -Destination $rootExePath -Force
    }
    throw
} finally {
    if (Test-Path -LiteralPath $syncRoot) { Remove-Item -LiteralPath $syncRoot -Recurse -Force -ErrorAction SilentlyContinue }
}
Write-Host "OwVoice.exe and _internal synchronized to: $projectDir" -ForegroundColor Green
