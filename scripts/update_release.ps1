param(
    [Parameter(Mandatory=$true)][string]$DownloadUrl,
    [Parameter(Mandatory=$true)][string]$Sha256,
    [Parameter(Mandatory=$true)][string]$TargetDirectory,
    [int]$WaitPid = 0,
    [string]$RestartPath = "",
    [switch]$NoRestart
)

$ErrorActionPreference = "Stop"
$targetDir = [System.IO.Path]::GetFullPath($TargetDirectory)
$hasModelData = (Test-Path -LiteralPath (Join-Path $targetDir "data\models") -PathType Container) -or (Test-Path -LiteralPath (Join-Path $targetDir "models") -PathType Container)
if (-not (Test-Path -LiteralPath (Join-Path $targetDir "config") -PathType Container) -or -not $hasModelData) {
    throw "更新目标不是有效的 OwVoice 项目目录。"
}
$updateRoot = Join-Path $targetDir ".cache\updates"
$downloadDir = Join-Path $updateRoot "downloads"
$extractDir = Join-Path $updateRoot ("extract-" + [guid]::NewGuid().ToString("N"))
$backupDir = Join-Path $updateRoot ("backup-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $downloadDir | Out-Null
$archivePath = Join-Path $downloadDir ([System.IO.Path]::GetFileName(([uri]$DownloadUrl).AbsolutePath))
if ([string]::IsNullOrWhiteSpace([System.IO.Path]::GetFileName($archivePath))) { throw "更新包文件名无效。" }

function Download-WithRetry([string]$url, [string]$target) {
    $partial = "$target.part"
    $lastError = $null
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        try {
            Invoke-WebRequest -Uri $url -OutFile $partial -TimeoutSec 300
            Move-Item -LiteralPath $partial -Destination $target -Force
            return
        } catch {
            $lastError = $_.Exception
            Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
            if ($attempt -lt 3) { Start-Sleep -Seconds ([math]::Min([math]::Pow(2, $attempt - 1), 8)) }
        }
    }
    throw "更新包下载失败：$lastError"
}

Download-WithRetry $DownloadUrl $archivePath
$actualHash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualHash -ne $Sha256.Trim().ToLowerInvariant()) { throw "更新包 SHA256 校验失败，已停止更新。" }

if ($WaitPid -gt 0) {
    try { Wait-Process -Id $WaitPid -Timeout 120 -ErrorAction SilentlyContinue } catch { }
}
Expand-Archive -LiteralPath $archivePath -DestinationPath $extractDir
$packageRoot = Get-ChildItem -LiteralPath $extractDir -Directory | Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName "OwVoice.exe") } | Select-Object -First 1
if ($null -eq $packageRoot) { throw "更新包中找不到 OwVoice.exe。" }

$items = @(
    "backend", "frontend", "assets", "GPT-SoVITS", "scripts", "tools", "_internal", "OwVoice.exe",
    "requirements-cpu.txt", "requirements-gpu.txt", "requirements-training.txt", "setup.bat",
    "pyproject.toml", "uv.lock", "README.md", "CHANGELOG.md", "MODEL_PACKAGE_SPEC.md", "LICENSE", "THIRD_PARTY_NOTICES.md"
)
$overlayItems = @("assets", "GPT-SoVITS")
$overlayExclusions = @(
    "assets\avatars",
    "GPT-SoVITS\GPT_SoVITS\pretrained_models",
    "GPT-SoVITS\GPT_SoVITS\text\G2PWModel",
    "GPT-SoVITS\tools\uvr5\uvr5_weights",
    "GPT-SoVITS\ffmpeg.exe",
    "GPT-SoVITS\ffprobe.exe",
    "GPT-SoVITS\weight.json"
)
function Is-ExcludedOverlayPath([string]$relativePath) {
    foreach ($excluded in $overlayExclusions) {
        if ($relativePath -eq $excluded -or $relativePath.StartsWith($excluded + "\", [System.StringComparison]::OrdinalIgnoreCase)) { return $true }
    }
    return $false
}
function Copy-DirectoryOverlay([string]$sourceDirectory, [string]$targetDirectory, [string]$relativeRoot) {
    if (-not (Test-Path -LiteralPath $sourceDirectory -PathType Container)) { return }
    New-Item -ItemType Directory -Force -Path $targetDirectory | Out-Null
    foreach ($sourceItem in Get-ChildItem -LiteralPath $sourceDirectory -Force) {
        $relativePath = Join-Path $relativeRoot $sourceItem.Name
        if (Is-ExcludedOverlayPath $relativePath) { continue }
        $targetItem = Join-Path $targetDirectory $sourceItem.Name
        if ($sourceItem.PSIsContainer) {
            Copy-DirectoryOverlay $sourceItem.FullName $targetItem $relativePath
        } else {
            Copy-Item -LiteralPath $sourceItem.FullName -Destination $targetItem -Force
        }
    }
}
New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
$backedUpItems = @()
$installedItems = @()
try {
    foreach ($item in $items) {
        if ($overlayItems -contains $item) { continue }
        $oldPath = Join-Path $targetDir $item
        if (Test-Path -LiteralPath $oldPath) {
            Move-Item -LiteralPath $oldPath -Destination (Join-Path $backupDir $item) -Force
            $backedUpItems += $item
        }
    }
    foreach ($item in $items) {
        if ($overlayItems -contains $item) { continue }
        $newPath = Join-Path $packageRoot.FullName $item
        if (-not (Test-Path -LiteralPath $newPath)) { throw "更新包缺少：$item" }
        Move-Item -LiteralPath $newPath -Destination (Join-Path $targetDir $item) -Force
        $installedItems += $item
    }
    foreach ($item in $overlayItems) {
        $newPath = Join-Path $packageRoot.FullName $item
        if (-not (Test-Path -LiteralPath $newPath -PathType Container)) { throw "更新包缺少：$item" }
        $oldPath = Join-Path $targetDir $item
        $overlayBackupPath = Join-Path $backupDir (Join-Path "overlay" $item)
        Copy-DirectoryOverlay $oldPath $overlayBackupPath $item
        Copy-DirectoryOverlay $newPath $oldPath $item
    }
    $exampleConfig = Join-Path $packageRoot.FullName "config\voices.example.json"
    if (Test-Path -LiteralPath $exampleConfig) {
        Copy-Item -LiteralPath $exampleConfig -Destination (Join-Path $targetDir "config\voices.example.json") -Force
    }
    Remove-Item -LiteralPath $backupDir -Recurse -Force
} catch {
    foreach ($item in $installedItems) {
        $newPath = Join-Path $targetDir $item
        if (Test-Path -LiteralPath $newPath) { Remove-Item -LiteralPath $newPath -Recurse -Force -ErrorAction SilentlyContinue }
    }
    foreach ($item in $backedUpItems) {
        $oldPath = Join-Path $backupDir $item
        if (Test-Path -LiteralPath $oldPath) { Move-Item -LiteralPath $oldPath -Destination (Join-Path $targetDir $item) -Force }
    }
    foreach ($item in $overlayItems) {
        $overlayBackupPath = Join-Path $backupDir (Join-Path "overlay" $item)
        $targetPath = Join-Path $targetDir $item
        Copy-DirectoryOverlay $overlayBackupPath $targetPath $item
    }
    throw
} finally {
    if (Test-Path -LiteralPath $extractDir) { Remove-Item -LiteralPath $extractDir -Recurse -Force -ErrorAction SilentlyContinue }
    if (Test-Path -LiteralPath $archivePath) { Remove-Item -LiteralPath $archivePath -Force -ErrorAction SilentlyContinue }
    if (Test-Path -LiteralPath $backupDir) { Remove-Item -LiteralPath $backupDir -Recurse -Force -ErrorAction SilentlyContinue }
}

if (-not $NoRestart) {
    if ([string]::IsNullOrWhiteSpace($RestartPath)) { $RestartPath = Join-Path $targetDir "OwVoice.exe" }
    Start-Process -FilePath ([System.IO.Path]::GetFullPath($RestartPath)) -WorkingDirectory $targetDir
}
Write-Host "OwVoice 更新完成。"
