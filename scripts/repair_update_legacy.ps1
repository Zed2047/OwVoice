param(
    [string]$Version = "v0.2.0",
    [string]$Repository = "Zed2047/OwVoice"
)

$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "process_lifecycle.ps1")
if ($Version -notmatch '^v0\.2\.0$') { throw "此 UpdateBridge 仅用于修复并升级到 v0.2.0。" }
$archiveName = "OwVoice-$Version.zip"
$manifestName = "update.json"
$releaseDownloadBase = "https://github.com/$Repository/releases/download/$Version"
$manifestUrl = "$releaseDownloadBase/$manifestName"
$archiveUrl = "$releaseDownloadBase/$archiveName"
$headers = @{ "User-Agent" = "OwVoice-Legacy-Repair-Updater" }
$manifestTemp = Join-Path ([System.IO.Path]::GetTempPath()) ("owvoice-manifest-" + [guid]::NewGuid().ToString("N") + ".json")
$bridgeUpdaterVersion = 4

function Format-ByteEstimate([long]$bytes) {
    if ($bytes -le 0) { return "发布清单未提供" }
    if ($bytes -ge 1GB) { return ("约 {0:N1} GB" -f ($bytes / 1GB)) }
    return ("约 {0:N0} MB" -f ($bytes / 1MB))
}

function Get-LegacyEnvironmentSelection {
    $mode = ""
    $withTraining = $false
    $trainingKnown = $false
    foreach ($statePath in @(
        (Join-Path $projectDir ".runtime\environment-state.json"),
        (Join-Path $projectDir ".cache\setup-state.json")
    )) {
        if (-not (Test-Path -LiteralPath $statePath -PathType Leaf)) { continue }
        try { $state = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json } catch { continue }
        if ([string]$state.mode -in @("CPU", "GPU")) {
            $mode = ([string]$state.mode).ToUpperInvariant()
            if ($null -ne $state.with_training) { $withTraining = [bool]$state.with_training; $trainingKnown = $true }
            elseif ($null -ne $state.trainingReady) { $withTraining = [bool]$state.trainingReady; $trainingKnown = $true }
            break
        }
    }
    if ([string]::IsNullOrWhiteSpace($mode)) {
        $python = Join-Path $projectDir ".venv\Scripts\python.exe"
        if (Test-Path -LiteralPath $python -PathType Leaf) {
            $probeCode = "import importlib.util,json; import torch; names=('tensorboard','gradio','funasr','modelscope','av'); print(json.dumps({'mode':'GPU' if '+cu' in str(torch.__version__) else 'CPU','with_training':all(importlib.util.find_spec(n) for n in names)}))"
            try {
                $probeOutput = & $python -c $probeCode 2>$null
                if ($LASTEXITCODE -eq 0) {
                    $probe = ($probeOutput -join "") | ConvertFrom-Json
                    if ([string]$probe.mode -in @("CPU", "GPU")) { $mode = ([string]$probe.mode).ToUpperInvariant() }
                    $withTraining = [bool]$probe.with_training; $trainingKnown = $true
                }
            } catch { }
        }
    }
    if ([string]::IsNullOrWhiteSpace($mode)) {
        Write-Host "无法可靠识别旧环境的 CPU/GPU 模式。" -ForegroundColor Yellow
        do { $choice = (Read-Host "请选择新环境模式：[1] CPU  [2] GPU（默认取消）").Trim() } while ($choice -notin @("", "1", "2"))
        if ([string]::IsNullOrWhiteSpace($choice)) { return $null }
        $mode = if ($choice -eq "1") { "CPU" } else { "GPU" }
    }
    if (-not $trainingKnown) {
        $trainingChoice = (Read-Host "是否在新环境中安装本地训练组件？输入 Y 确认，其他输入表示否").Trim().ToUpperInvariant()
        $withTraining = $trainingChoice -eq "Y"
    }
    return [PSCustomObject]@{ mode=$mode; with_training=$withTraining }
}

try {
    Write-Host "正在获取 OwVoice $Version 更新信息..." -ForegroundColor Cyan
    try {
        Invoke-WebRequest -Uri $manifestUrl -Headers $headers -OutFile $manifestTemp -TimeoutSec 60 -UseBasicParsing
    } catch {
        throw "无法下载更新清单 $manifestName。请确认 v0.2.0 Release 中三个资产已全部替换，并检查网络后重试。原始错误：$(Get-OwVoiceErrorText $_)"
    }

    $manifest = Get-Content -LiteralPath $manifestTemp -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($manifest.schema -ne 3 -or $manifest.version -ne $Version -or
        $manifest.package.name -ne $archiveName -or [string]$manifest.package.sha256 -notmatch '^[0-9a-fA-F]{64}$' -or
        [long]$manifest.package.size_bytes -lt 1 -or $manifest.environment.schema -ne 1 -or
        [string]$manifest.environment.python_version -ne "3.10.10" -or
        [string]$manifest.environment.python_version_range -ne ">=3.10,<3.11" -or
        [string]$manifest.environment.python_compatible_major_minor -ne "3.10" -or
        @($manifest.environment.python_tested_versions).Count -lt 2 -or
        @($manifest.environment.python_tested_versions) -notcontains "3.10.10" -or
        @($manifest.environment.python_tested_versions) -notcontains "3.10.21" -or
        [string]$manifest.environment.architecture -ne "x64" -or
        [int]$manifest.minimum_updater_version -gt $bridgeUpdaterVersion -or
        [long]$manifest.environment.estimated_download_bytes.cpu -le 0 -or
        [long]$manifest.environment.estimated_download_bytes.gpu -le 0 -or
        [long]$manifest.environment.estimated_download_bytes.training_extra -le 0 -or
        [long]$manifest.environment.minimum_temporary_space_bytes.cpu -le 0 -or
        [long]$manifest.environment.minimum_temporary_space_bytes.gpu -le 0 -or
        $archiveUrl -notmatch '^https://' -or
        $manifest.environment.migration_required -isnot [bool]) {
        throw "更新清单格式或目标文件名无效。"
    }

    $selection = $null
    if ($manifest.environment.migration_required -eq $true) {
        $selection = Get-LegacyEnvironmentSelection
        if ($null -eq $selection) { Write-Host "已取消更新；当前程序和环境均未修改。" -ForegroundColor Yellow; return }
        $downloadMap = $manifest.environment.estimated_download_bytes
        $spaceMap = $manifest.environment.minimum_temporary_space_bytes
        $modeKey = ([string]$selection.mode).ToLowerInvariant()
        $downloadBytes = [long]$downloadMap.$modeKey
        if ($selection.with_training) { $downloadBytes += [long]$downloadMap.training_extra }
        $temporaryBytes = [long]$spaceMap.$modeKey
        $trainingText = if ($selection.with_training) { "（包含训练组件）" } else { "" }
        Write-Host "`n本次更新需要同时同步 OwVoice 运行环境" -ForegroundColor Yellow
        Write-Host "当前选择：$($selection.mode)$trainingText"
        Write-Host "最坏情况下载：$(Format-ByteEstimate $downloadBytes)"
        Write-Host "更新期间临时需要：$(Format-ByteEstimate $temporaryBytes) 可用空间"
        Write-Host "兼容的 Python 3.10.x 和相同版本 Torch 会从旧环境复用，只同步变化项。"
        Write-Host "健康检查通过后才会删除旧环境；模型、配置、头像、训练记录和输出不会删除。"
        $confirmation = (Read-Host "输入 Y 同意更新应用和环境；其他输入取消").Trim().ToUpperInvariant()
        if ($confirmation -ne "Y") { Write-Host "已取消更新；当前程序和环境均未修改。" -ForegroundColor Yellow; return }
    }

    Write-Host "开始安全更新；当前安装目录的 OwVoice 进程会自动关闭。" -ForegroundColor Cyan
    $updateEntry = Join-Path $projectDir "updater\update_release.ps1"
    if (-not (Test-Path -LiteralPath $updateEntry -PathType Leaf)) { $updateEntry = Join-Path $PSScriptRoot "update_release.ps1" }
    $updateArguments = @{
        DownloadUrl = $archiveUrl
        Sha256 = [string]$manifest.package.sha256
        ExpectedSize = [long]$manifest.package.size_bytes
        TargetDirectory = $projectDir
        RestartPath = (Join-Path $projectDir "OwVoice.exe")
        AllowLegacyTargetWithoutIdentity = $true
    }
    if ($null -ne $selection) {
        $updateArguments["ApproveEnvironmentMigration"] = $true
        $updateArguments["EnvironmentMode"] = [string]$selection.mode
        if ($selection.with_training) { $updateArguments["EnvironmentWithTraining"] = $true }
    }
    & $updateEntry @updateArguments
} catch {
    Write-Host ""
    Write-Host "更新未能完成，更新器已尝试恢复旧版本。" -ForegroundColor Red
    Write-Host "原因：$(Get-OwVoiceErrorText $_)" -ForegroundColor Red
    Write-Host "详细过程保存在当前安装目录的 logs\update-*.log。" -ForegroundColor Yellow
    exit 1
} finally {
    Remove-Item -LiteralPath $manifestTemp -Force -ErrorAction SilentlyContinue
}
