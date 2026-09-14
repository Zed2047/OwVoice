param(
    [Parameter(Mandatory=$true)][string]$DownloadUrl,
    [Parameter(Mandatory=$true)][string]$Sha256,
    [Parameter(Mandatory=$true)][long]$ExpectedSize,
    [Parameter(Mandatory=$true)][string]$TargetDirectory,
    [int]$WaitPid = 0,
    [string]$RestartPath = "",
    [int]$HealthTimeoutSeconds = 120,
    [switch]$NoRestart,
    [ValidateSet("", "after_backup", "after_first_install", "after_install", "after_preserve", "after_verify")]
    [string]$TestFailurePoint = ""
)

# 保持源码入口兼容；正式程序使用独立 updater 目录中的稳定入口。
& (Join-Path (Split-Path -Parent $PSScriptRoot) "updater\update_release.ps1") @PSBoundParameters
return
