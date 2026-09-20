function Test-OwVoicePathInsideTarget {
    param([string]$Path, [string]$TargetDirectory)
    if ([string]::IsNullOrWhiteSpace($Path)) { return $false }
    try {
        $candidate = [System.IO.Path]::GetFullPath($Path).TrimEnd("\")
        $target = [System.IO.Path]::GetFullPath($TargetDirectory).TrimEnd("\")
        return $candidate.Equals($target, [System.StringComparison]::OrdinalIgnoreCase) -or
            $candidate.StartsWith($target + "\", [System.StringComparison]::OrdinalIgnoreCase)
    } catch {
        return $false
    }
}

function Test-OwVoiceProcessBelongsToTarget {
    param([object]$ProcessInfo, [string]$TargetDirectory)
    $knownNames = @("OwVoice.exe", "python.exe", "pythonw.exe", "ffmpeg.exe")
    if ([string]$ProcessInfo.Name -notin $knownNames) { return $false }
    if (Test-OwVoicePathInsideTarget ([string]$ProcessInfo.ExecutablePath) $TargetDirectory) {
        return $true
    }
    $commandLine = ([string]$ProcessInfo.CommandLine).Replace("/", "\")
    if ([string]::IsNullOrWhiteSpace($commandLine)) { return $false }
    $targetPrefix = [System.IO.Path]::GetFullPath($TargetDirectory).TrimEnd("\") + "\"
    return $commandLine.IndexOf($targetPrefix, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
}

function Stop-OwVoiceOwnedProcesses {
    param(
        [Parameter(Mandatory=$true)][string]$TargetDirectory,
        [int[]]$ExcludeProcessIds = @()
    )
    $excluded = @($PID) + @($ExcludeProcessIds)
    try {
        $processes = @(Get-CimInstance Win32_Process -ErrorAction Stop)
    } catch {
        throw "无法读取 OwVoice 进程信息，已取消更新，避免误关其他程序。"
    }
    $owned = @($processes | Where-Object {
        ([int]$_.ProcessId -notin $excluded) -and
        (Test-OwVoiceProcessBelongsToTarget $_ $TargetDirectory)
    })
    # 只终止进程树根节点；/T 会同时处理其后端和语音引擎。
    # 如果随后再次对已被连带结束的子 PID 调用 taskkill，Windows PowerShell 5.1
    # 会把 taskkill 的 stderr 当成异常，导致更新被误判为失败。
    $ownedById = @{}
    foreach ($process in $owned) { $ownedById[[int]$process.ProcessId] = $true }
    $ordered = @($owned | Sort-Object @{ Expression = { if ($_.Name -eq "OwVoice.exe") { 0 } else { 1 } } }, ProcessId)
    $roots = @($ordered | Where-Object { -not $ownedById.ContainsKey([int]$_.ParentProcessId) })
    foreach ($process in $roots) {
        if ($null -eq (Get-Process -Id ([int]$process.ProcessId) -ErrorAction SilentlyContinue)) { continue }
        try {
            $taskkill = Start-Process -FilePath "taskkill.exe" -ArgumentList @(
                "/PID", [string]$process.ProcessId, "/T", "/F"
            ) -WindowStyle Hidden -Wait -PassThru -ErrorAction Stop
            if ($taskkill.ExitCode -ne 0) {
                Stop-Process -Id ([int]$process.ProcessId) -Force -ErrorAction SilentlyContinue
            }
        } catch {
            # 进程可能已被父节点的 /T 连带结束；最终存活检查负责确认结果。
            Stop-Process -Id ([int]$process.ProcessId) -Force -ErrorAction SilentlyContinue
        }
    }
    $deadline = [DateTime]::UtcNow.AddSeconds(15)
    do {
        $remaining = @($ordered | Where-Object {
            $null -ne (Get-Process -Id ([int]$_.ProcessId) -ErrorAction SilentlyContinue)
        })
        if ($remaining.Count -eq 0) { break }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    if ($remaining.Count -gt 0) {
        $ids = @($remaining | ForEach-Object { [string]$_.ProcessId }) -join ", "
        throw "OwVoice 相关进程未能安全关闭（PID：$ids），已取消更新。"
    }
    $statePath = Join-Path $TargetDirectory ".cache\runtime\processes.json"
    if (Test-Path -LiteralPath $statePath -PathType Leaf) {
        Remove-Item -LiteralPath $statePath -Force -ErrorAction SilentlyContinue
    }
    return @($owned | ForEach-Object { [int]$_.ProcessId })
}

function Get-OwVoiceErrorText {
    param([object]$ErrorRecord)
    if ($null -eq $ErrorRecord) { return "未返回可读错误，请查看日志。" }
    $candidates = @(
        [string]$ErrorRecord.Exception.Message,
        [string]$ErrorRecord.ErrorDetails.Message,
        [string]$ErrorRecord
    )
    foreach ($candidate in $candidates) {
        if (-not [string]::IsNullOrWhiteSpace($candidate)) { return $candidate.Trim() }
    }
    return "未返回可读错误，请查看日志。"
}
