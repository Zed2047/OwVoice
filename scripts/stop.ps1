$ErrorActionPreference = "SilentlyContinue"

$projectDir = Split-Path -Parent $PSScriptRoot
$pidFile = Join-Path $projectDir "logs\service-pids.json"

function Get-DescendantProcessIds([int]$parentId) {
    $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $parentId")
    $result = @()
    foreach ($child in $children) {
        $result += $child.ProcessId
        $result += Get-DescendantProcessIds $child.ProcessId
    }
    return $result
}

function Stop-ProcessTree([int]$processId) {
    if ($processId -le 0) {
        return
    }
    $ids = @(Get-DescendantProcessIds $processId) + $processId
    foreach ($id in ($ids | Select-Object -Unique | Sort-Object -Descending)) {
        Stop-Process -Id $id -Force
    }
}

if (-not (Test-Path $pidFile)) {
    Write-Host "No OwVoice service record found; it may already be stopped."
    exit 0
}

try {
    $services = Get-Content $pidFile -Raw | ConvertFrom-Json
} catch {
    Remove-Item $pidFile -Force
    Write-Host "Service record was invalid and has been removed."
    exit 0
}

foreach ($service in @($services)) {
    if ($service.started_by_us -eq $true -and $service.pid) {
        Stop-ProcessTree ([int]$service.pid)
        Write-Host "Stopped $($service.name)"
    }
}

Remove-Item $pidFile -Force
Write-Host "OwVoice services stopped."
