param(
    [string]$DownloadUrl = "",
    [string]$Sha256 = "",
    [long]$ExpectedSize = 0,
    [string]$TargetDirectory = "",
    [int]$WaitPid = 0,
    [string]$RestartPath = "",
    [int]$HealthTimeoutSeconds = 120,
    [switch]$NoRestart,
    [switch]$RecoverOnly,
    [ValidateSet("", "after_backup", "after_first_install", "after_install", "after_preserve", "after_verify")]
    [string]$TestFailurePoint = ""
)

$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($TargetDirectory)) { $TargetDirectory = Split-Path -Parent $PSScriptRoot }
$TargetDirectory = [System.IO.Path]::GetFullPath($TargetDirectory)
$recoveryDir = Join-Path $TargetDirectory ".cache\updates\recovery"
$cachedTransaction = Join-Path $recoveryDir "update_transaction.ps1"
$sourceTransaction = Join-Path $TargetDirectory "scripts\update_transaction.ps1"
New-Item -ItemType Directory -Force -Path $recoveryDir | Out-Null

# 每次正常更新优先缓存当前事务实现；缓存位于用户运行时目录，不参与程序目录替换。
if (-not $RecoverOnly -and (Test-Path -LiteralPath $sourceTransaction -PathType Leaf)) {
    $temporary = "$cachedTransaction.tmp"
    Copy-Item -LiteralPath $sourceTransaction -Destination $temporary -Force
    Move-Item -LiteralPath $temporary -Destination $cachedTransaction -Force
}
if (-not (Test-Path -LiteralPath $cachedTransaction -PathType Leaf)) {
    throw "缺少事务更新器缓存。请先运行完整安装包中的更新入口。"
}

$arguments = @{ TargetDirectory = $TargetDirectory }
if ($RecoverOnly) {
    $arguments["RecoverOnly"] = $true
} else {
    if ([string]::IsNullOrWhiteSpace($DownloadUrl) -or [string]::IsNullOrWhiteSpace($Sha256) -or $ExpectedSize -lt 1) {
        throw "更新参数不完整。"
    }
    $arguments["DownloadUrl"] = $DownloadUrl
    $arguments["Sha256"] = $Sha256
    $arguments["ExpectedSize"] = $ExpectedSize
    $arguments["WaitPid"] = $WaitPid
    $arguments["HealthTimeoutSeconds"] = $HealthTimeoutSeconds
    if (-not [string]::IsNullOrWhiteSpace($RestartPath)) { $arguments["RestartPath"] = $RestartPath }
    if ($NoRestart) { $arguments["NoRestart"] = $true }
    if (-not [string]::IsNullOrWhiteSpace($TestFailurePoint)) { $arguments["TestFailurePoint"] = $TestFailurePoint }
}
& $cachedTransaction @arguments
