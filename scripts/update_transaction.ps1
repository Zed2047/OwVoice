param(
    [string]$DownloadUrl = "",
    [string]$Sha256 = "",
    [long]$ExpectedSize = 0,
    [Parameter(Mandatory=$true)][string]$TargetDirectory,
    [int]$WaitPid = 0,
    [string]$RestartPath = "",
    [int]$HealthTimeoutSeconds = 120,
    [switch]$NoRestart,
    [switch]$RecoverOnly,
    [ValidateSet("", "after_backup", "after_first_install", "after_install", "after_preserve", "after_verify")]
    [string]$TestFailurePoint = ""
)

$ErrorActionPreference = "Stop"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$targetDir = [System.IO.Path]::GetFullPath($TargetDirectory)
$layoutPath = Join-Path $targetDir "release-layout.json"
$identityPath = Join-Path $targetDir "version.json"
$targetExe = Join-Path $targetDir "OwVoice.exe"
if (-not $RecoverOnly) {
    if ($ExpectedSize -lt 1) { throw "更新包大小无效。" }
    if ($Sha256.Trim() -notmatch '^[0-9a-fA-F]{64}$') { throw "更新包 SHA256 格式无效。" }
}

$updateRoot = Join-Path $targetDir ".cache\updates"
$downloadDir = Join-Path $updateRoot "downloads"
$transactionsDir = Join-Path $updateRoot "transactions"
New-Item -ItemType Directory -Force -Path $downloadDir, $transactionsDir | Out-Null
$lockPath = Join-Path $updateRoot "update.lock"
$lockStream = $null
$archivePath = $null
$extractDir = $null
$transactionDir = $null
$journalPath = $null
$journal = $null
$newProcess = $null
$waitedForApplication = $false

function ConvertTo-SafeRelativePath([string]$Value, [string]$Label) {
    $path = $Value.Replace("\", "/").Trim()
    if ([string]::IsNullOrWhiteSpace($path) -or $path.StartsWith("/") -or $path -match '^[A-Za-z]:' -or $path.Contains(":")) {
        throw "$Label 包含非法路径：$Value"
    }
    $parts = @($path.Split("/") | Where-Object { $_ -ne "" })
    if ($parts.Count -eq 0 -or @($parts | Where-Object { $_ -in @(".", "..") }).Count -gt 0) {
        throw "$Label 包含非法路径：$Value"
    }
    return ($parts -join "/")
}

function Test-PathInside([string]$Path, [string]$RootPrefix) {
    return [System.IO.Path]::GetFullPath($Path).StartsWith($RootPrefix, [System.StringComparison]::OrdinalIgnoreCase)
}

function Test-IsSameOrChild([string]$Path, [string]$Parent) {
    return $Path.Equals($Parent, [System.StringComparison]::OrdinalIgnoreCase) -or
        $Path.StartsWith($Parent + "/", [System.StringComparison]::OrdinalIgnoreCase)
}

function Test-IsPreserved([string]$Path, [object[]]$PreservePaths) {
    return @($PreservePaths | Where-Object { Test-IsSameOrChild $Path ([string]$_) }).Count -gt 0
}

function Write-Journal {
    if ($null -eq $journal -or [string]::IsNullOrWhiteSpace($journalPath)) { return }
    $temporary = "$journalPath.tmp"
    [System.IO.File]::WriteAllText($temporary, ($journal | ConvertTo-Json -Depth 8), $utf8NoBom)
    Move-Item -LiteralPath $temporary -Destination $journalPath -Force
}

function Invoke-TestFailure([string]$Point) {
    if ($TestFailurePoint -eq $Point) { throw "测试故障注入：$Point" }
}

function Remove-ManagedTarget([string]$Root, [string]$Relative) {
    $path = Join-Path $Root $Relative
    if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Recurse -Force }
}

function Restore-Transaction([object]$State, [string]$StatePath) {
    if ($null -eq $State.managed_items -or [string]::IsNullOrWhiteSpace([string]$State.backup_dir)) {
        throw "更新 journal 缺少回滚信息，已保留现场：$StatePath"
    }
    $root = [System.IO.Path]::GetFullPath([string]$State.target_dir)
    if (-not $root.Equals($targetDir, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "更新 journal 指向其他目录，已保留现场：$StatePath"
    }
    $backup = [System.IO.Path]::GetFullPath([string]$State.backup_dir)
    foreach ($relative in @($State.managed_items | Sort-Object { ([string]$_).Length } -Descending)) {
        $backupItem = Join-Path $backup ([string]$relative)
        if ($State.phase -ne "backing_up" -or (Test-Path -LiteralPath $backupItem)) {
            Remove-ManagedTarget $root ([string]$relative)
        }
    }
    foreach ($relative in @($State.managed_items | Sort-Object { ([string]$_).Length })) {
        $source = Join-Path $backup ([string]$relative)
        if (-not (Test-Path -LiteralPath $source)) { continue }
        $destination = Join-Path $root ([string]$relative)
        $parent = Split-Path -Parent $destination
        if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
        Move-Item -LiteralPath $source -Destination $destination -Force
    }
    $State.phase = "rolled_back"
    $temporary = "$StatePath.tmp"
    [System.IO.File]::WriteAllText($temporary, ($State | ConvertTo-Json -Depth 8), $utf8NoBom)
    Move-Item -LiteralPath $temporary -Destination $StatePath -Force
    Remove-Item -LiteralPath (Split-Path -Parent $StatePath) -Recurse -Force
}

function Recover-InterruptedTransactions {
    foreach ($directory in @(Get-ChildItem -LiteralPath $transactionsDir -Directory -Force)) {
        $statePath = Join-Path $directory.FullName "journal.json"
        if (-not (Test-Path -LiteralPath $statePath -PathType Leaf)) {
            throw "发现缺少 journal 的更新事务，已保留现场：$($directory.FullName)"
        }
        try { $state = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json }
        catch { throw "更新 journal 损坏，已保留现场：$statePath" }
        if ($state.schema -ne 1) { throw "更新 journal schema 无效，已保留现场：$statePath" }
        if ($state.phase -in @("committed", "rolled_back", "prepared")) {
            Remove-Item -LiteralPath $directory.FullName -Recurse -Force
        } else {
            Restore-Transaction $state $statePath
        }
    }
}

function Assert-FreeSpace([long]$RequiredBytes, [string]$Stage) {
    $drive = (Get-Item -LiteralPath $targetDir).PSDrive
    if ($null -ne $drive -and $null -ne $drive.Free -and [long]$drive.Free -lt $RequiredBytes) {
        $requiredGb = [Math]::Round($RequiredBytes / 1GB, 2)
        $freeGb = [Math]::Round([long]$drive.Free / 1GB, 2)
        throw "$Stage 磁盘空间不足：需要约 $requiredGb GB，当前可用 $freeGb GB。"
    }
}

function Download-WithRetry([string]$Url, [string]$Target) {
    $partial = "$Target.part"
    $lastError = $null
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        try {
            Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
            Invoke-WebRequest -Uri $Url -OutFile $partial -TimeoutSec 300
            $downloadedSize = (Get-Item -LiteralPath $partial).Length
            if ($downloadedSize -ne $ExpectedSize) { throw "下载大小不一致：应为 $ExpectedSize，实际为 $downloadedSize。" }
            $actualHash = (Get-FileHash -LiteralPath $partial -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($actualHash -ne $Sha256.Trim().ToLowerInvariant()) { throw "更新包 SHA256 校验失败。" }
            Move-Item -LiteralPath $partial -Destination $Target -Force
            return
        } catch {
            $lastError = $_.Exception.Message
            Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
            if ($attempt -lt 3) { Start-Sleep -Seconds ([math]::Min([math]::Pow(2, $attempt - 1), 8)) }
        }
    }
    throw "更新包下载或校验失败：$lastError"
}

function Expand-SafeArchive([string]$Archive, [string]$Destination) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [System.IO.Compression.ZipFile]::OpenRead($Archive)
    try {
        $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
        $topLevels = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
        $totalSize = [long]0
        foreach ($entry in $zip.Entries) {
            $relative = ConvertTo-SafeRelativePath $entry.FullName "ZIP"
            if (-not $seen.Add($relative)) { throw "ZIP 包含重复路径：$relative" }
            [void]$topLevels.Add($relative.Split("/")[0])
            $unixType = ([int64]$entry.ExternalAttributes -shr 16) -band 0xF000
            $windowsAttributes = [int64]$entry.ExternalAttributes -band 0xFFFF
            if ($unixType -eq 0xA000 -or ($windowsAttributes -band 0x400) -ne 0) { throw "ZIP 包含链接或重解析点：$relative" }
            $totalSize += [long]$entry.Length
        }
        if ($topLevels.Count -ne 1) { throw "更新包必须且只能包含一个顶层目录。" }
        Assert-FreeSpace ($totalSize + 512MB) "解压更新包时"
        New-Item -ItemType Directory -Force -Path $Destination | Out-Null
        $destinationPrefix = [System.IO.Path]::GetFullPath($Destination).TrimEnd("\", "/") + [System.IO.Path]::DirectorySeparatorChar
        foreach ($entry in $zip.Entries) {
            $relative = ConvertTo-SafeRelativePath $entry.FullName "ZIP"
            $target = Join-Path $Destination $relative
            if (-not (Test-PathInside $target $destinationPrefix)) { throw "ZIP 路径越界：$relative" }
            if ([string]::IsNullOrEmpty($entry.Name)) {
                New-Item -ItemType Directory -Force -Path $target | Out-Null
                continue
            }
            $parent = Split-Path -Parent $target
            if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
            $inputStream = $entry.Open()
            try {
                $outputStream = [System.IO.File]::Open($target, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
                try { $inputStream.CopyTo($outputStream) } finally { $outputStream.Dispose() }
            } finally { $inputStream.Dispose() }
        }
    } finally { $zip.Dispose() }
    $reparse = Get-ChildItem -LiteralPath $Destination -Recurse -Force | Where-Object { ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 } | Select-Object -First 1
    if ($null -ne $reparse) { throw "解压结果包含重解析点：$($reparse.FullName)" }
}

function Get-NormalizedList([object[]]$Values, [string]$Label) {
    $result = @($Values | ForEach-Object { ConvertTo-SafeRelativePath ([string]$_) $Label })
    if ($result.Count -eq 0) { throw "$Label 不能为空。" }
    $unique = @($result | Sort-Object -Unique)
    if ($unique.Count -ne $result.Count) { throw "$Label 包含重复路径。" }
    return $result
}

function Assert-ListsEqual([object[]]$Left, [object[]]$Right, [string]$Label) {
    $a = @($Left | Sort-Object)
    $b = @($Right | Sort-Object)
    if ($a.Count -ne $b.Count -or (Compare-Object $a $b).Count -ne 0) { throw "$Label 与发布布局不一致。" }
}

function Assert-Package([string]$PackageRoot) {
    $manifestPath = Join-Path $PackageRoot "release-files-v1.json"
    $packageLayoutPath = Join-Path $PackageRoot "release-layout.json"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf) -or -not (Test-Path -LiteralPath $packageLayoutPath -PathType Leaf)) {
        throw "更新包缺少内部文件清单或发布布局。"
    }
    try {
        $script:packageManifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $script:incomingLayout = Get-Content -LiteralPath $packageLayoutPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $packageIdentity = Get-Content -LiteralPath (Join-Path $PackageRoot "version.json") -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch { throw "更新包身份文件不是有效 JSON：$($_.Exception.Message)" }
    if ($script:packageManifest.schema -ne 1 -or $script:incomingLayout.schema -ne 1) { throw "更新包内部 schema 无效。" }
    foreach ($stableEntry in @("updater\update_release.ps1", "recover_update.bat")) {
        if (-not (Test-Path -LiteralPath (Join-Path $PackageRoot $stableEntry) -PathType Leaf)) { throw "更新包缺少独立恢复入口：$stableEntry" }
    }
    if ([string]$script:packageManifest.version -ne [string]$packageIdentity.version -or
        [string]$script:packageManifest.tag -ne ("v" + [string]$packageIdentity.version)) { throw "更新包版本身份不一致。" }
    $script:incomingItems = Get-NormalizedList @($script:incomingLayout.managedItems) "managedItems"
    $script:incomingPreserve = Get-NormalizedList @($script:incomingLayout.preservePaths) "preservePaths"
    $script:incomingUserPaths = Get-NormalizedList @($script:incomingLayout.userPaths) "userPaths"
    $manifestItems = Get-NormalizedList @($script:packageManifest.managed_items) "manifest managed_items"
    $manifestPreserve = Get-NormalizedList @($script:packageManifest.preserve_paths) "manifest preserve_paths"
    Assert-ListsEqual $script:incomingItems $manifestItems "manifest managed_items"
    Assert-ListsEqual $script:incomingPreserve $manifestPreserve "manifest preserve_paths"
    foreach ($item in $script:incomingItems) {
        foreach ($userPath in $script:incomingUserPaths) {
            if ((Test-IsSameOrChild $item $userPath) -or (Test-IsSameOrChild $userPath $item)) { throw "受管理项与用户路径冲突：$item / $userPath" }
        }
        if (-not (Test-Path -LiteralPath (Join-Path $PackageRoot $item))) { throw "更新包缺少受管理项：$item" }
    }
    $expected = @{}
    foreach ($file in @($script:packageManifest.files)) {
        $relative = ConvertTo-SafeRelativePath ([string]$file.path) "manifest files"
        if ($expected.ContainsKey($relative.ToLowerInvariant())) { throw "内部清单包含重复文件：$relative" }
        if ([string]$file.sha256 -notmatch '^[0-9a-fA-F]{64}$' -or [long]$file.size_bytes -lt 0) { throw "内部清单文件属性无效：$relative" }
        $path = Join-Path $PackageRoot $relative
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "更新包缺少清单文件：$relative" }
        if ((Get-Item -LiteralPath $path).Length -ne [long]$file.size_bytes) { throw "更新包文件大小不一致：$relative" }
        if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -ne [string]$file.sha256) { throw "更新包文件哈希不一致：$relative" }
        $expected[$relative.ToLowerInvariant()] = $relative
    }
    $actual = @{}
    foreach ($item in $script:incomingItems) {
        $itemPath = Join-Path $PackageRoot $item
        $files = if (Test-Path -LiteralPath $itemPath -PathType Leaf) { @(Get-Item -LiteralPath $itemPath) } else { @(Get-ChildItem -LiteralPath $itemPath -Recurse -File -Force) }
        foreach ($file in $files) {
            $relative = $file.FullName.Substring($PackageRoot.Length).TrimStart("\", "/").Replace("\", "/")
            if (Test-IsPreserved $relative $script:incomingPreserve) { throw "更新包不得携带本地保留文件：$relative" }
            if ($relative -eq "release-files-v1.json") { continue }
            $actual[$relative.ToLowerInvariant()] = $relative
        }
    }
    $missing = @($expected.Keys | Where-Object { -not $actual.ContainsKey($_) })
    $extra = @($actual.Keys | Where-Object { -not $expected.ContainsKey($_) })
    if ($missing.Count -gt 0 -or $extra.Count -gt 0) { throw "更新包文件集合与内部清单不一致。缺少：$($missing -join ', ')；多出：$($extra -join ', ')" }
}

function Assert-InstalledFiles([string]$Root) {
    foreach ($file in @($packageManifest.files)) {
        $relative = ConvertTo-SafeRelativePath ([string]$file.path) "manifest files"
        $path = Join-Path $Root $relative
        if (-not (Test-Path -LiteralPath $path -PathType Leaf) -or
            (Get-Item -LiteralPath $path).Length -ne [long]$file.size_bytes -or
            (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -ne [string]$file.sha256) {
            throw "安装结果校验失败：$relative"
        }
    }
    $expected = @($packageManifest.files | ForEach-Object { ([string]$_.path).ToLowerInvariant() })
    $actual = @()
    foreach ($item in $incomingItems) {
        $itemPath = Join-Path $Root $item
        if (-not (Test-Path -LiteralPath $itemPath)) { throw "安装结果缺少受管理项：$item" }
        $files = if (Test-Path -LiteralPath $itemPath -PathType Leaf) { @(Get-Item -LiteralPath $itemPath) } else { @(Get-ChildItem -LiteralPath $itemPath -Recurse -File -Force) }
        foreach ($file in $files) {
            $relative = $file.FullName.Substring($Root.Length).TrimStart("\", "/").Replace("\", "/")
            if ($relative -eq "release-files-v1.json" -or (Test-IsPreserved $relative $transactionPreserve)) { continue }
            $actual += $relative.ToLowerInvariant()
        }
    }
    if ((Compare-Object @($expected | Sort-Object) @($actual | Sort-Object)).Count -ne 0) {
        throw "安装后的受管理文件集合不精确，可能存在旧文件残留。"
    }
}

function Test-TcpPort([int]$Port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $result = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        if (-not $result.AsyncWaitHandle.WaitOne(250)) { return $false }
        $client.EndConnect($result)
        return $true
    } catch { return $false } finally { $client.Dispose() }
}

try {
    try { $lockStream = [System.IO.File]::Open($lockPath, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None) }
    catch { throw "另一个 OwVoice 更新任务正在运行。" }
    Recover-InterruptedTransactions

    # 必须先恢复上次中断事务，再检查这些可能在中断时已被移入 backup 的文件。
    $hasModelData = (Test-Path -LiteralPath (Join-Path $targetDir "data\models") -PathType Container) -or
        (Test-Path -LiteralPath (Join-Path $targetDir "models") -PathType Container)
    if (-not (Test-Path -LiteralPath $identityPath -PathType Leaf) -or
        -not (Test-Path -LiteralPath $layoutPath -PathType Leaf) -or
        -not (Test-Path -LiteralPath $targetExe -PathType Leaf) -or
        -not (Test-Path -LiteralPath (Join-Path $targetDir "config") -PathType Container) -or
        -not $hasModelData) {
        throw "更新目标不是有效的 OwVoice 项目目录。"
    }

    if ($RecoverOnly) {
        Write-Host "已处理未完成的 OwVoice 更新事务。" -ForegroundColor Green
        return
    }

    $localLayout = Get-Content -LiteralPath $layoutPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($localLayout.schema -ne 1) { throw "本地 release-layout.json schema 无效。" }
    $localItems = Get-NormalizedList @($localLayout.managedItems) "本地 managedItems"
    $localPreserve = Get-NormalizedList @($localLayout.preservePaths) "本地 preservePaths"
    Assert-FreeSpace ($ExpectedSize + 512MB) "下载更新包时"
    $archiveFileName = [System.IO.Path]::GetFileName(([uri]$DownloadUrl).AbsolutePath)
    if ([string]::IsNullOrWhiteSpace($archiveFileName)) { throw "更新包文件名无效。" }
    $archivePath = Join-Path $downloadDir $archiveFileName
    Download-WithRetry $DownloadUrl $archivePath

    if ($WaitPid -gt 0) {
        try { Wait-Process -Id $WaitPid -Timeout 120 -ErrorAction SilentlyContinue } catch { }
        if ($null -ne (Get-Process -Id $WaitPid -ErrorAction SilentlyContinue)) { throw "OwVoice 未在 120 秒内退出，已取消更新。" }
        $waitedForApplication = $true
    }
    if (-not $NoRestart) {
        $deadline = [DateTime]::UtcNow.AddSeconds(30)
        while ((Test-TcpPort 8765) -or (Test-TcpPort 9880)) {
            if ([DateTime]::UtcNow -ge $deadline) { throw "OwVoice 后端或语音引擎仍在运行，已取消更新。" }
            Start-Sleep -Milliseconds 500
        }
    }

    $extractDir = Join-Path $updateRoot ("extract-" + [guid]::NewGuid().ToString("N"))
    Expand-SafeArchive $archivePath $extractDir
    $topDirectories = @(Get-ChildItem -LiteralPath $extractDir -Directory -Force)
    if ($topDirectories.Count -ne 1 -or @(Get-ChildItem -LiteralPath $extractDir -File -Force).Count -ne 0) { throw "更新包顶层结构无效。" }
    $packageRoot = $topDirectories[0].FullName
    Assert-Package $packageRoot

    $transactionItems = @($localItems + $incomingItems | Sort-Object -Unique)
    $transactionPreserve = @($localPreserve + $incomingPreserve | Sort-Object -Unique)
    $transactionId = [guid]::NewGuid().ToString("N")
    $transactionDir = Join-Path $transactionsDir $transactionId
    $backupDir = Join-Path $transactionDir "backup"
    $journalPath = Join-Path $transactionDir "journal.json"
    New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
    $journal = [ordered]@{
        schema = 1; id = $transactionId; phase = "prepared"; target_dir = $targetDir; backup_dir = $backupDir
        managed_items = $transactionItems; incoming_items = $incomingItems; preserve_paths = $transactionPreserve
        version = [string]$packageManifest.version; created_at_utc = [DateTime]::UtcNow.ToString("o")
    }
    Write-Journal

    $journal.phase = "backing_up"; Write-Journal
    foreach ($item in $transactionItems) {
        $source = Join-Path $targetDir $item
        if (-not (Test-Path -LiteralPath $source)) { continue }
        $destination = Join-Path $backupDir $item
        $parent = Split-Path -Parent $destination
        if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
        Move-Item -LiteralPath $source -Destination $destination -Force
    }
    $journal.phase = "backed_up"; Write-Journal
    Invoke-TestFailure "after_backup"

    $journal.phase = "installing"; Write-Journal
    $installIndex = 0
    foreach ($item in $incomingItems) {
        $source = Join-Path $packageRoot $item
        $destination = Join-Path $targetDir $item
        $parent = Split-Path -Parent $destination
        if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
        Move-Item -LiteralPath $source -Destination $destination -Force
        $installIndex++
        if ($installIndex -eq 1) { Invoke-TestFailure "after_first_install" }
    }
    $journal.phase = "installed"; Write-Journal
    Invoke-TestFailure "after_install"

    foreach ($relative in $transactionPreserve) {
        $destination = Join-Path $targetDir $relative
        if (Test-Path -LiteralPath $destination) { Remove-Item -LiteralPath $destination -Recurse -Force }
        $source = Join-Path $backupDir $relative
        if (-not (Test-Path -LiteralPath $source)) { continue }
        $parent = Split-Path -Parent $destination
        if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
        Copy-Item -LiteralPath $source -Destination $destination -Recurse -Force
    }
    $journal.phase = "preserved"; Write-Journal
    Invoke-TestFailure "after_preserve"

    Assert-InstalledFiles $targetDir
    $journal.phase = "verified"; Write-Journal
    Invoke-TestFailure "after_verify"

    if (-not $NoRestart) {
        if ([string]::IsNullOrWhiteSpace($RestartPath)) { $RestartPath = $targetExe }
        $newProcess = Start-Process -FilePath ([System.IO.Path]::GetFullPath($RestartPath)) -WorkingDirectory $targetDir -PassThru
        $journal.phase = "pending_health"; $journal.started_pid = $newProcess.Id; Write-Journal
        $healthDeadline = [DateTime]::UtcNow.AddSeconds($HealthTimeoutSeconds)
        $healthy = $false
        while ([DateTime]::UtcNow -lt $healthDeadline) {
            if ($newProcess.HasExited) { break }
            try {
                $health = Invoke-RestMethod -Uri "http://127.0.0.1:8765/api/health" -TimeoutSec 5
                $healthRoot = [System.IO.Path]::GetFullPath([string]$health.project_dir)
                if ($health.owvoice -eq $true -and $healthRoot.Equals($targetDir, [System.StringComparison]::OrdinalIgnoreCase) -and
                    [string]$health.version -eq [string]$packageManifest.version) { $healthy = $true; break }
            } catch { }
            Start-Sleep -Milliseconds 750
        }
        if (-not $healthy) {
            if ($null -ne $newProcess -and -not $newProcess.HasExited) { & taskkill.exe /PID $newProcess.Id /T /F 2>$null | Out-Null }
            throw "新版本启动健康检查失败，将恢复旧版本。"
        }
    }

    $journal.phase = "committed"; $journal.committed_at_utc = [DateTime]::UtcNow.ToString("o"); Write-Journal
    Remove-Item -LiteralPath $transactionDir -Recurse -Force
    $transactionDir = $null
    Write-Host "OwVoice 更新完成。" -ForegroundColor Green
} catch {
    $originalError = $_.Exception.Message
    if ($null -ne $journal -and $journal.phase -notin @("prepared", "committed", "rolled_back")) {
        try { Restore-Transaction $journal $journalPath; $transactionDir = $null }
        catch { throw "$originalError`n自动回滚失败，备份已保留：$transactionDir`n$($_.Exception.Message)" }
        if (-not $NoRestart -and $waitedForApplication -and (Test-Path -LiteralPath $targetExe -PathType Leaf)) {
            Start-Process -FilePath $targetExe -WorkingDirectory $targetDir | Out-Null
        }
        throw "$originalError`n旧版本已自动恢复。"
    }
    throw
} finally {
    if ($null -ne $lockStream) { $lockStream.Dispose() }
    if ($null -ne $extractDir -and (Test-Path -LiteralPath $extractDir)) { Remove-Item -LiteralPath $extractDir -Recurse -Force -ErrorAction SilentlyContinue }
    if ($null -ne $archivePath -and (Test-Path -LiteralPath $archivePath)) { Remove-Item -LiteralPath $archivePath -Force -ErrorAction SilentlyContinue }
}
