param(
    [string]$Version = "v0.2.0",
    [string]$OutputDirectory = "dist"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$outputDir = [System.IO.Path]::GetFullPath((Join-Path $projectRoot $OutputDirectory))
$stagingDir = Join-Path $outputDir "update-bridge-staging"
$packageDir = Join-Path $stagingDir "UpdateBridge-$Version"
$scriptsDir = Join-Path $packageDir "scripts"
$archivePath = Join-Path $outputDir "UpdateBridge-$Version.zip"

New-Item -ItemType Directory -Force -Path $outputDir | Out-Null
if (Test-Path -LiteralPath $stagingDir) { Remove-Item -LiteralPath $stagingDir -Recurse -Force }
if (Test-Path -LiteralPath $archivePath) { Remove-Item -LiteralPath $archivePath -Force }
New-Item -ItemType Directory -Force -Path $scriptsDir | Out-Null
Copy-Item -LiteralPath (Join-Path $projectRoot "修复更新器.bat") -Destination $packageDir -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "repair_update_legacy.ps1") -Destination $scriptsDir -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "update_release.ps1") -Destination $scriptsDir -Force

# tar.exe 在部分中文 Windows 环境会以 OEM 编码写入中文条目名，固定使用
# Compress-Archive 以保证“修复更新器.bat”跨工具解压后名称正确。
Compress-Archive -Path (Join-Path $packageDir "*") -DestinationPath $archivePath -CompressionLevel Optimal
$hash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
Write-Host ("修复更新器已生成：{0}，SHA256：{1}" -f $archivePath, $hash) -ForegroundColor Green
