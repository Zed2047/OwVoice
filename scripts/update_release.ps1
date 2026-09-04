param(
    [Parameter(Mandatory=$true)][string]$DownloadUrl,
    [Parameter(Mandatory=$true)][string]$Sha256,
    [Parameter(Mandatory=$true)][string]$TargetDirectory,
    [int]$WaitPid = 0,
    [string]$RestartPath = ""
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

Invoke-WebRequest -Uri $DownloadUrl -OutFile $archivePath
$actualHash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualHash -ne $Sha256.Trim().ToLowerInvariant()) { throw "更新包 SHA256 校验失败，已停止更新。" }

if ($WaitPid -gt 0) {
    try { Wait-Process -Id $WaitPid -Timeout 120 -ErrorAction SilentlyContinue } catch { }
}
Expand-Archive -LiteralPath $archivePath -DestinationPath $extractDir
$packageRoot = Get-ChildItem -LiteralPath $extractDir -Directory | Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName "OwVoice.exe") } | Select-Object -First 1
if ($null -eq $packageRoot) { throw "更新包中找不到 OwVoice.exe。" }

$items = @("backend", "frontend", "assets", "GPT-SoVITS", "scripts", "_internal", "OwVoice.exe")
New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
try {
    foreach ($item in $items) {
        $oldPath = Join-Path $targetDir $item
        if (Test-Path -LiteralPath $oldPath) {
            Move-Item -LiteralPath $oldPath -Destination (Join-Path $backupDir $item)
        }
    }
    foreach ($item in $items) {
        $newPath = Join-Path $packageRoot.FullName $item
        if (-not (Test-Path -LiteralPath $newPath)) { throw "更新包缺少：$item" }
        Move-Item -LiteralPath $newPath -Destination (Join-Path $targetDir $item)
    }
    $exampleConfig = Join-Path $packageRoot.FullName "config\voices.example.json"
    if (Test-Path -LiteralPath $exampleConfig) {
        Copy-Item -LiteralPath $exampleConfig -Destination (Join-Path $targetDir "config\voices.example.json") -Force
    }
    Remove-Item -LiteralPath $backupDir -Recurse -Force
} catch {
    foreach ($item in $items) {
        $newPath = Join-Path $targetDir $item
        $oldPath = Join-Path $backupDir $item
        if (Test-Path -LiteralPath $newPath) { Remove-Item -LiteralPath $newPath -Recurse -Force }
        if (Test-Path -LiteralPath $oldPath) { Move-Item -LiteralPath $oldPath -Destination (Join-Path $targetDir $item) }
    }
    throw
} finally {
    if (Test-Path -LiteralPath $extractDir) { Remove-Item -LiteralPath $extractDir -Recurse -Force -ErrorAction SilentlyContinue }
    if (Test-Path -LiteralPath $archivePath) { Remove-Item -LiteralPath $archivePath -Force -ErrorAction SilentlyContinue }
    if (Test-Path -LiteralPath $backupDir) { Remove-Item -LiteralPath $backupDir -Recurse -Force -ErrorAction SilentlyContinue }
}

if ([string]::IsNullOrWhiteSpace($RestartPath)) { $RestartPath = Join-Path $targetDir "OwVoice.exe" }
Start-Process -FilePath ([System.IO.Path]::GetFullPath($RestartPath)) -WorkingDirectory $targetDir
Write-Host "OwVoice 更新完成。"
