param(
    [string]$Version = "v0.2.0",
    [string]$Repository = "Zed2047/OwVoice"
)

$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent $PSScriptRoot
$archiveName = "OwVoice-$Version.zip"
$manifestName = "release-manifest-v2-$Version.json"
$apiUrl = "https://api.github.com/repos/$Repository/releases/tags/$Version"
$headers = @{ "User-Agent" = "OwVoice-Legacy-Repair-Updater" }
$manifestTemp = Join-Path ([System.IO.Path]::GetTempPath()) ("owvoice-manifest-" + [guid]::NewGuid().ToString("N") + ".json")

function Get-ReleaseAsset($assets, [string]$name) {
    return $assets | Where-Object { $_.name -eq $name } | Select-Object -First 1
}

try {
    Write-Host "正在获取 OwVoice $Version 更新信息..." -ForegroundColor Cyan
    $release = Invoke-RestMethod -Uri $apiUrl -Headers $headers -TimeoutSec 30
    $archiveAsset = Get-ReleaseAsset $release.assets $archiveName
    $manifestAsset = Get-ReleaseAsset $release.assets $manifestName
    if ($null -eq $archiveAsset -or $null -eq $manifestAsset) {
        throw "Release 缺少 $archiveName 或 $manifestName。"
    }

    Invoke-WebRequest -Uri $manifestAsset.browser_download_url -Headers $headers -OutFile $manifestTemp -TimeoutSec 60
    $manifest = Get-Content -LiteralPath $manifestTemp -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($manifest.schema -ne 2 -or $manifest.archive_name -ne $archiveName -or [string]::IsNullOrWhiteSpace($manifest.sha256)) {
        throw "更新清单格式或目标文件名无效。"
    }

    $running = Get-Process -Name "OwVoice" -ErrorAction SilentlyContinue
    if ($null -ne $running) {
        throw "OwVoice.exe 正在运行，请先关闭程序后再运行修复更新器。"
    }

    Write-Host "开始安全更新；现有 .venv、模型、头像和本地配置会保留。" -ForegroundColor Cyan
    & (Join-Path $PSScriptRoot "update_release.ps1") `
        -DownloadUrl $archiveAsset.browser_download_url `
        -Sha256 ([string]$manifest.sha256) `
        -TargetDirectory $projectDir `
        -RestartPath (Join-Path $projectDir "OwVoice.exe")
} finally {
    Remove-Item -LiteralPath $manifestTemp -Force -ErrorAction SilentlyContinue
}
