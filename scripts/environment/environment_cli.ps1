param(
    [ValidateSet("plan", "preflight", "migrate")][string]$Action = "plan",
    [string]$ProjectRoot = "",
    [ValidateSet("CPU", "GPU")][string]$Mode = "CPU",
    [switch]$WithTraining
)

$ErrorActionPreference = "Stop"
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) { $ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot) }
$ProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot)
$modulePath = Join-Path $PSScriptRoot "OwVoice.Environment.psm1"
Import-Module -Name $modulePath -Force

if ($Action -eq "plan") {
    $result = Get-OwVoiceEnvironmentPlan -ProjectRoot $ProjectRoot -Mode $Mode -WithTraining:$WithTraining
} elseif ($Action -eq "migrate") {
    $state = Get-OwVoiceEnvironmentState -ProjectRoot $ProjectRoot -MigrateLegacy
    if ($null -eq $state) { $result = [PSCustomObject]@{ ok=$false; code="ENV_LEGACY_STATE_NOT_FOUND"; message="没有可迁移的旧环境状态。" } } else { $result = [PSCustomObject]@{ ok=$true; code="OK"; state=$state } }
} else {
    $result = Test-OwVoiceEnvironmentPreflight -ProjectRoot $ProjectRoot -Mode $Mode -WithTraining:$WithTraining
}

$result | ConvertTo-Json -Depth 20 -Compress
if ($result.ok -eq $false) { exit 30 }
exit 0
