param(
    [string]$DownloadUrl = "",
    [string]$LocalArchivePath = "",
    [string]$Sha256 = "",
    [long]$ExpectedSize = 0,
    [string]$TargetDirectory = "",
    [int]$WaitPid = 0,
    [string]$RestartPath = "",
    [int]$HealthTimeoutSeconds = 120,
    [switch]$NoRestart,
    [switch]$RecoverOnly,
    [switch]$ApproveEnvironmentMigration,
    [switch]$AllowLegacyTargetWithoutIdentity,
    [ValidateSet("", "CPU", "GPU")][string]$EnvironmentMode = "",
    [switch]$EnvironmentWithTraining,
    [ValidateSet("", "after_user_data_backup", "after_backup", "after_first_install", "after_install", "after_preserve", "after_verify", "after_environment_prepare", "after_environment_resources", "after_environment_switch")]
    [string]$TestFailurePoint = ""
)

$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($TargetDirectory)) { $TargetDirectory = Split-Path -Parent $PSScriptRoot }
$TargetDirectory = [System.IO.Path]::GetFullPath($TargetDirectory)
$recoveryDir = Join-Path $TargetDirectory ".cache\updates\recovery"
$cachedTransaction = Join-Path $recoveryDir "update_transaction.ps1"
$cachedLifecycle = Join-Path $recoveryDir "process_lifecycle.ps1"
$cachedLegacyLayout = Join-Path $recoveryDir "legacy-release-layout.json"
$sourceTransaction = Join-Path $TargetDirectory "scripts\update_transaction.ps1"
$sourceLifecycle = Join-Path $TargetDirectory "scripts\process_lifecycle.ps1"
New-Item -ItemType Directory -Force -Path $recoveryDir | Out-Null

# 每次正常更新优先缓存当前事务实现；缓存位于用户运行时目录，不参与程序目录替换。
if (-not $RecoverOnly -and (Test-Path -LiteralPath $sourceTransaction -PathType Leaf)) {
    $temporary = "$cachedTransaction.tmp"
    Copy-Item -LiteralPath $sourceTransaction -Destination $temporary -Force
    Move-Item -LiteralPath $temporary -Destination $cachedTransaction -Force
    if (-not (Test-Path -LiteralPath $sourceLifecycle -PathType Leaf)) {
        throw "缺少 OwVoice 进程生命周期模块，无法安全更新。"
    }
    $lifecycleTemporary = "$cachedLifecycle.tmp"
    Copy-Item -LiteralPath $sourceLifecycle -Destination $lifecycleTemporary -Force
    Move-Item -LiteralPath $lifecycleTemporary -Destination $cachedLifecycle -Force
    if ($AllowLegacyTargetWithoutIdentity) {
        $sourceLegacyLayout = Join-Path $TargetDirectory "release-layout.json"
        if (-not (Test-Path -LiteralPath $sourceLegacyLayout -PathType Leaf)) {
            $sourceLegacyLayout = Join-Path $TargetDirectory "scripts\legacy-release-layout-v0.1.2.json"
        }
        if (-not (Test-Path -LiteralPath $sourceLegacyLayout -PathType Leaf)) {
            throw "UpdateBridge 缺少 v0.1.2 旧版布局，无法安全识别旧版本文件。"
        }
        $layoutTemporary = "$cachedLegacyLayout.tmp"
        Copy-Item -LiteralPath $sourceLegacyLayout -Destination $layoutTemporary -Force
        Move-Item -LiteralPath $layoutTemporary -Destination $cachedLegacyLayout -Force
    }
}
if (-not (Test-Path -LiteralPath $cachedTransaction -PathType Leaf) -or
    -not (Test-Path -LiteralPath $cachedLifecycle -PathType Leaf)) {
    throw "缺少事务更新器缓存。请先运行完整安装包中的更新入口。"
}

$arguments = @{ TargetDirectory = $TargetDirectory }
if ($RecoverOnly) {
    $arguments["RecoverOnly"] = $true
} else {
    if (([string]::IsNullOrWhiteSpace($DownloadUrl) -and [string]::IsNullOrWhiteSpace($LocalArchivePath)) -or
        [string]::IsNullOrWhiteSpace($Sha256) -or $ExpectedSize -lt 1) {
        throw "更新参数不完整。"
    }
    if (-not [string]::IsNullOrWhiteSpace($DownloadUrl)) { $arguments["DownloadUrl"] = $DownloadUrl }
    if (-not [string]::IsNullOrWhiteSpace($LocalArchivePath)) { $arguments["LocalArchivePath"] = $LocalArchivePath }
    $arguments["Sha256"] = $Sha256
    $arguments["ExpectedSize"] = $ExpectedSize
    $arguments["WaitPid"] = $WaitPid
    $arguments["HealthTimeoutSeconds"] = $HealthTimeoutSeconds
    if (-not [string]::IsNullOrWhiteSpace($RestartPath)) { $arguments["RestartPath"] = $RestartPath }
    if ($NoRestart) { $arguments["NoRestart"] = $true }
    if ($ApproveEnvironmentMigration) { $arguments["ApproveEnvironmentMigration"] = $true }
    if ($AllowLegacyTargetWithoutIdentity) { $arguments["AllowLegacyTargetWithoutIdentity"] = $true }
    if ($EnvironmentMode -in @("CPU", "GPU")) { $arguments["EnvironmentMode"] = $EnvironmentMode }
    if ($EnvironmentWithTraining) { $arguments["EnvironmentWithTraining"] = $true }
    if (-not [string]::IsNullOrWhiteSpace($TestFailurePoint)) { $arguments["TestFailurePoint"] = $TestFailurePoint }
}
& $cachedTransaction @arguments
