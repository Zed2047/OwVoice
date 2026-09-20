param(
    [string]$DownloadUrl = "",
    [string]$LocalArchivePath = "",
    [Parameter(Mandatory=$true)][string]$Sha256,
    [Parameter(Mandatory=$true)][long]$ExpectedSize,
    [Parameter(Mandatory=$true)][string]$TargetDirectory,
    [int]$WaitPid = 0,
    [string]$RestartPath = "",
    [int]$HealthTimeoutSeconds = 120,
    [switch]$NoRestart,
    [switch]$ApproveEnvironmentMigration,
    [switch]$AllowLegacyTargetWithoutIdentity,
    [ValidateSet("", "CPU", "GPU")][string]$EnvironmentMode = "",
    [switch]$EnvironmentWithTraining,
    [ValidateSet("", "after_user_data_backup", "after_backup", "after_first_install", "after_install", "after_preserve", "after_verify", "after_environment_prepare", "after_environment_resources", "after_environment_switch")]
    [string]$TestFailurePoint = ""
)

# 保持源码入口兼容；正常情况使用独立 updater，失败回滚后可直接复用恢复缓存。
$projectRoot = Split-Path -Parent $PSScriptRoot
$stableUpdater = Join-Path $projectRoot "updater\update_release.ps1"
if (Test-Path -LiteralPath $stableUpdater -PathType Leaf) {
    & $stableUpdater @PSBoundParameters
    return
}
$cachedTransaction = Join-Path $projectRoot ".cache\updates\recovery\update_transaction.ps1"
if (-not (Test-Path -LiteralPath $cachedTransaction -PathType Leaf)) {
    throw "缺少独立更新器和恢复缓存，请重新解压 UpdateBridge 后再试。"
}
& $cachedTransaction @PSBoundParameters
return
