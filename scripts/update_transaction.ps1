param(
    [string]$DownloadUrl = "",
    [string]$LocalArchivePath = "",
    [string]$Sha256 = "",
    [long]$ExpectedSize = 0,
    [Parameter(Mandatory=$true)][string]$TargetDirectory,
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
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$targetDir = [System.IO.Path]::GetFullPath($TargetDirectory)
. (Join-Path $PSScriptRoot "process_lifecycle.ps1")
$layoutPath = Join-Path $targetDir "release-layout.json"
$identityPath = Join-Path $targetDir "version.json"
$legacyLayoutPath = Join-Path $targetDir ".cache\updates\recovery\legacy-release-layout.json"
$targetExe = Join-Path $targetDir "OwVoice.exe"
if (-not $RecoverOnly) {
    if ($ExpectedSize -lt 1) { throw "更新包大小无效。" }
    if ($Sha256.Trim() -notmatch '^[0-9a-fA-F]{64}$') { throw "更新包 SHA256 格式无效。" }
    if ([string]::IsNullOrWhiteSpace($DownloadUrl) -and [string]::IsNullOrWhiteSpace($LocalArchivePath)) {
        throw "缺少更新包下载地址或本地文件。"
    }
}

$updateRoot = Join-Path $targetDir ".cache\updates"
$downloadDir = Join-Path $updateRoot "downloads"
$transactionsDir = Join-Path $updateRoot "transactions"
New-Item -ItemType Directory -Force -Path $downloadDir, $transactionsDir | Out-Null
$lockPath = Join-Path $updateRoot "update.lock"
$lockStream = $null
$archivePath = $null
$removeArchiveOnExit = $false
$extractDir = $null
$transactionDir = $null
$journalPath = $null
$journal = $null
$newProcess = $null
$waitedForApplication = $false
$updateLogDir = Join-Path $targetDir "logs"
New-Item -ItemType Directory -Force -Path $updateLogDir | Out-Null
foreach ($oldLog in @(Get-ChildItem -LiteralPath $updateLogDir -Filter "update-*.log" -File | Sort-Object LastWriteTime -Descending | Select-Object -Skip 9)) {
    Remove-Item -LiteralPath $oldLog.FullName -Force -ErrorAction SilentlyContinue
}
$updateLogPath = Join-Path $updateLogDir ("update-{0}.log" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
$updateTranscriptStarted = $false

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

function Test-IsCriticalMetadataPath([string]$Relative) {
    $normalized = $Relative.Replace("\", "/")
    return $normalized -eq "config/voices.local.json" -or
        $normalized -eq "data/models/installed-models.json" -or
        $normalized -match '^data/models/[^/]+/model\.json$' -or
        $normalized -match '^data/training/jobs/[^/]+/job\.json$'
}

function Get-CriticalMetadataPaths([string]$Root) {
    $paths = New-Object System.Collections.Generic.List[string]
    foreach ($relative in @("config/voices.local.json", "data/models/installed-models.json")) {
        if (Test-Path -LiteralPath (Join-Path $Root $relative) -PathType Leaf) { $paths.Add($relative) }
    }
    foreach ($entry in @(
        [PSCustomObject]@{ parent="data/models"; name="model.json" },
        [PSCustomObject]@{ parent="data/training/jobs"; name="job.json" }
    )) {
        $parent = Join-Path $Root $entry.parent
        if (-not (Test-Path -LiteralPath $parent -PathType Container)) { continue }
        foreach ($directory in @(Get-ChildItem -LiteralPath $parent -Directory -Force)) {
            if (($directory.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { continue }
            $path = Join-Path $directory.FullName $entry.name
            if (Test-Path -LiteralPath $path -PathType Leaf) {
                $paths.Add(($path.Substring($Root.Length).TrimStart("\", "/").Replace("\", "/")))
            }
        }
    }
    return @($paths | Sort-Object -Unique)
}

function Get-UserDirectorySummary([string]$Root, [string]$Relative) {
    $path = Join-Path $Root $Relative
    [long]$size = 0
    [long]$count = 0
    if (Test-Path -LiteralPath $path -PathType Container) {
        foreach ($file in @(Get-ChildItem -LiteralPath $path -Recurse -File -Force)) {
            if (($file.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { continue }
            $count++
            $size += [long]$file.Length
        }
    }
    return [ordered]@{ path=$Relative; file_count=$count; size_bytes=$size }
}

function Get-UserDataSnapshot([string]$Root) {
    $metadata = @(
        Get-CriticalMetadataPaths $Root | ForEach-Object {
            $path = Join-Path $Root $_
            [ordered]@{
                path=$_
                size_bytes=[long](Get-Item -LiteralPath $path).Length
                sha256=(Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
            }
        }
    )
    $directories = @(
        "data/models", "data/training", "assets/avatars", "output" | ForEach-Object {
            Get-UserDirectorySummary $Root $_
        }
    )
    return [ordered]@{ metadata=$metadata; directories=$directories }
}

function Backup-CriticalMetadata([string]$Root, [object]$Snapshot, [string]$BackupRoot) {
    New-Item -ItemType Directory -Force -Path $BackupRoot | Out-Null
    foreach ($item in @($Snapshot.metadata)) {
        $relative = ConvertTo-SafeRelativePath ([string]$item.path) "关键元数据"
        if (-not (Test-IsCriticalMetadataPath $relative)) { throw "关键元数据路径不在白名单：$relative" }
        $destination = Join-Path $BackupRoot $relative
        $parent = Split-Path -Parent $destination
        if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
        Copy-Item -LiteralPath (Join-Path $Root $relative) -Destination $destination -Force
    }
}

function Assert-UserDataSnapshot([string]$Root, [object]$Expected) {
    $current = Get-UserDataSnapshot $Root
    $metadata = @{}
    foreach ($item in @($current.metadata)) { $metadata[[string]$item.path] = $item }
    foreach ($item in @($Expected.metadata)) {
        $relative = [string]$item.path
        if (-not $metadata.ContainsKey($relative) -or
            [long]$metadata[$relative].size_bytes -ne [long]$item.size_bytes -or
            [string]$metadata[$relative].sha256 -ne [string]$item.sha256) {
            throw "更新过程中关键用户元数据发生变化：$relative"
        }
    }
    $summaries = @{}
    foreach ($item in @($current.directories)) { $summaries[[string]$item.path] = $item }
    foreach ($item in @($Expected.directories)) {
        $relative = [string]$item.path
        if (-not $summaries.ContainsKey($relative) -or
            [long]$summaries[$relative].file_count -lt [long]$item.file_count -or
            [long]$summaries[$relative].size_bytes -lt [long]$item.size_bytes) {
            throw "更新过程中用户目录内容无原因减少：$relative"
        }
    }
}

function Restore-CriticalMetadata([string]$Root, [object]$UserData, [string]$StatePath) {
    if ($null -eq $UserData -or [string]::IsNullOrWhiteSpace([string]$UserData.metadata_backup_dir)) { return }
    $transactionRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $StatePath))
    $transactionPrefix = $transactionRoot.TrimEnd("\") + "\"
    $backupRoot = [System.IO.Path]::GetFullPath([string]$UserData.metadata_backup_dir)
    if (-not $backupRoot.StartsWith($transactionPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "关键元数据备份路径越界，已保留现场：$backupRoot"
    }
    foreach ($item in @($UserData.snapshot.metadata)) {
        $relative = ConvertTo-SafeRelativePath ([string]$item.path) "关键元数据"
        if (-not (Test-IsCriticalMetadataPath $relative)) { throw "关键元数据路径不在白名单：$relative" }
        $source = Join-Path $backupRoot $relative
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw "关键元数据备份缺失：$relative" }
        $destination = Join-Path $Root $relative
        $parent = Split-Path -Parent $destination
        if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
        Copy-Item -LiteralPath $source -Destination $destination -Force
    }
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
    Write-Host "更新未完成，正在自动回滚应用和环境，请勿关闭窗口。" -ForegroundColor Yellow
    Restore-EnvironmentForTransaction $State
    if ($State.phase -ne "user_data_backed_up") {
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
    }
    Restore-CriticalMetadata $root $State.user_data $StatePath
    if ($null -ne $State.user_data) { Assert-UserDataSnapshot $root $State.user_data.snapshot }
    $State.phase = "rolled_back"
    $temporary = "$StatePath.tmp"
    [System.IO.File]::WriteAllText($temporary, ($State | ConvertTo-Json -Depth 8), $utf8NoBom)
    Move-Item -LiteralPath $temporary -Destination $StatePath -Force
    Remove-Item -LiteralPath (Split-Path -Parent $StatePath) -Recurse -Force
    Write-Host "自动回滚完成，旧版本已恢复。" -ForegroundColor Green
}

function Import-TargetEnvironmentCore {
    $modulePath = Join-Path $targetDir "scripts\environment\OwVoice.Environment.psm1"
    if (-not (Test-Path -LiteralPath $modulePath -PathType Leaf)) {
        throw "更新后的环境核心缺失：$modulePath"
    }
    Import-Module -Name $modulePath -Force
}

function Restore-EnvironmentForTransaction([object]$State) {
    $environment = $State.environment
    if ($null -eq $environment) { return }
    if ($environment.phase -eq "switched") {
        Import-TargetEnvironmentCore
        $result = Restore-OwVoiceEnvironment -ProjectRoot $targetDir -TransactionId ([string]$State.id)
        if ($result.ok -ne $true) { throw "环境回滚失败：$([string]$result.message)" }
    } elseif ($environment.phase -in @("candidate_created", "candidate_resources_prepared", "candidate_verified")) {
        $candidate = Join-Path $targetDir ".venv.next"
        if (Test-Path -LiteralPath $candidate -PathType Container) {
            Remove-Item -LiteralPath $candidate -Recurse -Force
        }
    }
}

function Complete-EnvironmentForTransaction([object]$State) {
    $environment = $State.environment
    if ($null -eq $environment -or $environment.phase -ne "switched") { return }
    Import-TargetEnvironmentCore
    $result = Complete-OwVoiceEnvironmentTransaction -ProjectRoot $targetDir -TransactionId ([string]$State.id)
    if ($result.ok -ne $true) { throw "环境提交清理失败：$([string]$result.message)" }
    $environment.phase = "committed"
    $State.environment = $environment
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
        } elseif ($state.phase -eq "health_verified") {
            Complete-EnvironmentForTransaction $state
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

function Format-OwVoiceMegabytes([long]$Bytes) {
    return ("{0:N1} MB" -f ([Math]::Max(0, $Bytes) / 1MB))
}

function Write-UpdateStage([int]$Step, [string]$Message) {
    Write-Host ("[{0}/9] {1}" -f $Step, $Message) -ForegroundColor Cyan
}

function Download-WithRetry([string]$Url, [string]$Target) {
    $partial = "$Target.part"
    $lastError = $null
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        $client = $null
        $response = $null
        $inputStream = $null
        $outputStream = $null
        try {
            Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
            Write-Host ("正在下载更新包（第 {0}/3 次）：0.0 / {1}" -f $attempt, (Format-OwVoiceMegabytes $ExpectedSize)) -ForegroundColor Cyan
            Add-Type -AssemblyName System.Net.Http
            $handler = New-Object System.Net.Http.HttpClientHandler
            $handler.AllowAutoRedirect = $true
            $client = New-Object System.Net.Http.HttpClient -ArgumentList $handler
            $client.Timeout = [TimeSpan]::FromMinutes(30)
            $client.DefaultRequestHeaders.UserAgent.ParseAdd("OwVoice-Updater/4")
            $response = $client.GetAsync($Url, [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead).GetAwaiter().GetResult()
            if (-not $response.IsSuccessStatusCode) { throw "下载源返回 HTTP $([int]$response.StatusCode)。" }
            $inputStream = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
            $outputStream = New-Object System.IO.FileStream($partial, [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
            $buffer = New-Object byte[] (1MB)
            $received = [long]0
            while (($read = $inputStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
                $outputStream.Write($buffer, 0, $read)
                $received += $read
                $percent = [Math]::Min(100, [int](($received * 100) / $ExpectedSize))
                Write-Progress -Activity "下载更新包" -Status ("{0} / {1} ({2}%)" -f (Format-OwVoiceMegabytes $received), (Format-OwVoiceMegabytes $ExpectedSize), $percent) -PercentComplete $percent
            }
            $outputStream.Flush()
            $outputStream.Dispose(); $outputStream = $null
            Write-Progress -Activity "下载更新包" -Completed
            $downloadedSize = (Get-Item -LiteralPath $partial).Length
            if ($downloadedSize -ne $ExpectedSize) { throw "下载大小不一致：应为 $(Format-OwVoiceMegabytes $ExpectedSize)，实际为 $(Format-OwVoiceMegabytes $downloadedSize)。" }
            $actualHash = (Get-FileHash -LiteralPath $partial -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($actualHash -ne $Sha256.Trim().ToLowerInvariant()) { throw "更新包 SHA256 校验失败。" }
            Move-Item -LiteralPath $partial -Destination $Target -Force
            Write-Host ("更新包下载完成：{0}；SHA256 校验通过。" -f (Format-OwVoiceMegabytes $downloadedSize)) -ForegroundColor Green
            return
        } catch {
            $lastError = Get-OwVoiceErrorText $_
            Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
            if ($attempt -lt 3) { Start-Sleep -Seconds ([math]::Min([math]::Pow(2, $attempt - 1), 8)) }
        } finally {
            Write-Progress -Activity "下载更新包" -Completed
            if ($null -ne $outputStream) { $outputStream.Dispose() }
            if ($null -ne $inputStream) { $inputStream.Dispose() }
            if ($null -ne $response) { $response.Dispose() }
            if ($null -ne $client) { $client.Dispose() }
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

function Get-EnvironmentTransactionInfo([string]$PackageRoot) {
    $statePath = Join-Path $targetDir ".runtime\environment-state.json"
    $currentState = $null
    if (Test-Path -LiteralPath $statePath -PathType Leaf) {
        try { $currentState = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json } catch { $currentState = $null }
    }
    $specPath = Join-Path $PackageRoot "environment-spec.json"
    $spec = $null
    if (Test-Path -LiteralPath $specPath -PathType Leaf) {
        try { $spec = Get-Content -LiteralPath $specPath -Raw -Encoding UTF8 | ConvertFrom-Json } catch { $spec = $null }
    }
    $desired = [ordered]@{}
    $specValid = $false
    if ($null -ne $spec -and $null -ne $spec.python -and $null -ne $spec.dependencies -and $null -ne $spec.resources) {
        $desired = [ordered]@{
            python_version = [string]$spec.python.version
            python_version_range = [string]$spec.python.version_range
            python_compatible_major_minor = [string]$spec.python.compatible_major_minor
            python_implementation = [string]$spec.python.implementation
            architecture = [string]$spec.python.architecture
            pyproject_sha256 = ([string]$spec.dependencies.pyproject_sha256).ToLowerInvariant()
            lock_sha256 = ([string]$spec.dependencies.lock_sha256).ToLowerInvariant()
            resource_schema = [int]$spec.resources.schema
        }
        $specValid = $desired.python_implementation -eq "CPython" -and
            $desired.python_version -match '^[0-9]+\.[0-9]+\.[0-9]+$' -and
            $desired.python_version_range -eq ">=3.10,<3.11" -and
            $desired.python_compatible_major_minor -eq "3.10" -and
            $desired.architecture -eq "x64" -and
            $desired.pyproject_sha256 -match '^[0-9a-f]{64}$' -and
            $desired.lock_sha256 -match '^[0-9a-f]{64}$' -and
            $desired.resource_schema -eq 2
    }
    $stateHasMode = $null -ne $currentState -and [string]$currentState.mode -in @("CPU", "GPU")
    $mode = if ($stateHasMode) { [string]$currentState.mode } elseif ($EnvironmentMode -in @("CPU", "GPU")) { $EnvironmentMode } else { "UNKNOWN" }
    $withTraining = if ($stateHasMode -and $null -ne $currentState.with_training) { [bool]$currentState.with_training } else { [bool]$EnvironmentWithTraining }
    $identityKnown = $null -ne $currentState -and $currentState.schema -eq 1 -and
        -not [string]::IsNullOrWhiteSpace([string]$currentState.python_version) -and
        -not [string]::IsNullOrWhiteSpace([string]$currentState.pyproject_sha256) -and
        -not [string]::IsNullOrWhiteSpace([string]$currentState.lock_sha256)
    $currentVersionParts = ([string]$currentState.python_version).Split(".")
    $currentMajorMinor = if ($currentVersionParts.Count -ge 2) { "$($currentVersionParts[0]).$($currentVersionParts[1])" } else { "" }
    $same = $identityKnown -and $desired.Count -gt 0 -and
        $currentMajorMinor -eq [string]$desired.python_compatible_major_minor -and
        [string]$currentState.architecture -eq [string]$desired.architecture -and
        [string]$currentState.pyproject_sha256 -eq [string]$desired.pyproject_sha256 -and
        [string]$currentState.lock_sha256 -eq [string]$desired.lock_sha256 -and
        [int]$currentState.resource_schema -eq [int]$desired.resource_schema
    [ordered]@{
        schema = 1
        required = (-not $same)
        reason = if ($same) { "environment_identity_matches" } elseif ($null -eq $currentState -and $mode -in @("CPU", "GPU")) { "environment_state_missing_user_selection" } elseif ($null -eq $currentState) { "environment_state_missing" } elseif ($desired.Count -eq 0) { "target_environment_spec_invalid" } else { "environment_identity_changed" }
        mode = $mode
        with_training = $withTraining
        spec_valid = $specValid
        eligible = ((-not $same) -and $specValid -and $mode -in @("CPU", "GPU"))
        state_path = $statePath
        current_path = (Join-Path $targetDir ".venv")
        candidate_path = (Join-Path $targetDir ".venv.next")
        old_path = (Join-Path $targetDir ".venv.old")
        desired = $desired
    }
}

function Invoke-EnvironmentMigration([object]$State) {
    $environment = $State.environment
    if ($null -eq $environment -or $environment.required -ne $true) { return }
    if ($environment.mode -in @("CPU", "GPU") -and $environment.spec_valid -ne $true) {
        throw "更新包的环境规范无效，已取消更新。"
    }
    if ($environment.eligible -ne $true) {
        $environment.phase = "deferred"
        $State.environment = $environment
        Write-Journal
        Write-Host "当前环境身份或模式未知，已保留现有环境；请在 OwVoice 中完成环境检查后再迁移。" -ForegroundColor Yellow
        return
    }
    if ($NoRestart) { throw "环境迁移需要启动健康检查，不支持与 -NoRestart 一起使用。" }
    if (-not $ApproveEnvironmentMigration) { throw "此更新需要同步项目 Python 环境。未获得明确确认，现有程序和环境均未修改。" }

    Import-TargetEnvironmentCore
    $requiredBytes = if ([string]$environment.mode -eq "GPU") { 24GB } else { 15GB }
    $preflight = Test-OwVoiceEnvironmentPreflight -ProjectRoot $targetDir -Mode ([string]$environment.mode) -WithTraining:([bool]$environment.with_training) -RequiredFreeBytes $requiredBytes
    if ($preflight.ok -ne $true) { throw "环境迁移前检查失败：$([string]$preflight.message)" }
    Write-UpdateStage 6 "正在准备兼容的 Python 环境并同步变化依赖"
    $candidate = New-OwVoiceCandidateEnvironment -ProjectRoot $targetDir -TransactionId ([string]$State.id) -CandidatePath (Join-Path $targetDir ".venv.next") -Mode ([string]$environment.mode) -WithTraining:([bool]$environment.with_training)
    if ($candidate.ok -ne $true) { throw "候选环境创建失败：$([string]$candidate.message)" }
    $environment.phase = "candidate_created"; $State.environment = $environment; Write-Journal
    Invoke-TestFailure "after_environment_prepare"
    Write-Host "正在校验候选环境中的 Python、Torch 和核心依赖。" -ForegroundColor Cyan
    $candidateCheck = Test-OwVoiceCandidateEnvironment -ProjectRoot $targetDir -TransactionId ([string]$State.id) -CandidatePath (Join-Path $targetDir ".venv.next") -Mode ([string]$environment.mode) -WithTraining:([bool]$environment.with_training)
    if ($candidateCheck.ok -ne $true) { throw "候选环境校验失败：$([string]$candidateCheck.message)" }
    Invoke-EnvironmentResourcePreparation -State $State
    $environment.phase = "candidate_resources_prepared"; $State.environment = $environment; Write-Journal
    Invoke-TestFailure "after_environment_resources"
    Write-Host "正在执行包含 FFmpeg、文本资源和预训练模型的完整校验。" -ForegroundColor Cyan
    $candidateCheck = Test-OwVoiceCandidateEnvironment -ProjectRoot $targetDir -TransactionId ([string]$State.id) -CandidatePath (Join-Path $targetDir ".venv.next") -Mode ([string]$environment.mode) -WithTraining:([bool]$environment.with_training) -VerifyProjectResources
    if ($candidateCheck.ok -ne $true) { throw "候选环境完整校验失败：$([string]$candidateCheck.message)" }
    $environment.phase = "candidate_verified"; $State.environment = $environment; Write-Journal
    $spec = Read-OwVoiceEnvironmentSpec -ProjectRoot $targetDir
    $candidateState = New-OwVoiceEnvironmentState -Spec $spec -ProjectRoot $targetDir -Mode ([string]$environment.mode) -WithTraining:([bool]$environment.with_training) -EnvironmentFingerprint ([string]$candidateCheck.state.environment_fingerprint) -Python $candidateCheck.state.python -Verified:$true -TransactionId ([string]$State.id)
    $switch = Switch-OwVoiceEnvironment -ProjectRoot $targetDir -TransactionId ([string]$State.id) -CandidatePath (Join-Path $targetDir ".venv.next") -State ([PSCustomObject]$candidateState)
    if ($switch.ok -ne $true) { throw "候选环境切换失败：$([string]$switch.message)" }
    $environment.phase = "switched"; $environment.state = $candidateState; $State.environment = $environment; Write-Journal
    Invoke-TestFailure "after_environment_switch"
}

function Invoke-EnvironmentResourcePreparation([object]$State) {
    $environment = $State.environment
    $python = Join-Path $targetDir ".venv.next\Scripts\python.exe"
    $nltkScript = Join-Path $targetDir "scripts\download_nltk_data.py"
    $pretrainedScript = Join-Path $targetDir "scripts\download_pretrained.py"
    foreach ($required in @($python, $nltkScript, $pretrainedScript)) {
        if (-not (Test-Path -LiteralPath $required -PathType Leaf)) { throw "资源准备入口缺失：$required" }
    }
    $processLogRoot = Join-Path $targetDir "logs\resource-processes"
    $savedPythonUtf8 = $env:PYTHONUTF8
    $savedPythonIoEncoding = $env:PYTHONIOENCODING
    $savedHfProgress = $env:HF_HUB_DISABLE_PROGRESS_BARS
    $savedHfWarning = $env:HF_HUB_DISABLE_SYMLINKS_WARNING
    try {
        $env:PYTHONUTF8 = "1"
        $env:PYTHONIOENCODING = "utf-8"
        $env:HF_HUB_DISABLE_PROGRESS_BARS = "1"
        $env:HF_HUB_DISABLE_SYMLINKS_WARNING = "1"
        Write-UpdateStage 7 "正在检查并补齐运行资源；已有且校验通过的文件会直接复用"
        $nltkExitCode = Invoke-OwVoiceTrackedProcess -FilePath $python -Arguments @($nltkScript) -Activity "正在准备 NLTK 文本资源" -LogRoot $processLogRoot -WorkingDirectory $targetDir
        if ($nltkExitCode -ne 0) { throw "NLTK 文本资源准备失败：$(Get-OwVoiceLastProcessFailureMessage)" }
        $arguments = @($pretrainedScript)
        if ([bool]$environment.with_training) { $arguments += "--training" }
        $pretrainedExitCode = Invoke-OwVoiceTrackedProcess -FilePath $python -Arguments $arguments -Activity "正在准备 GPT-SoVITS 运行资源" -LogRoot $processLogRoot -WorkingDirectory $targetDir
        if ($pretrainedExitCode -ne 0) { throw "GPT-SoVITS 运行资源准备失败：$(Get-OwVoiceLastProcessFailureMessage)" }
        Write-Host "运行资源准备完成。" -ForegroundColor Green
    } finally {
        $env:PYTHONUTF8 = $savedPythonUtf8
        $env:PYTHONIOENCODING = $savedPythonIoEncoding
        $env:HF_HUB_DISABLE_PROGRESS_BARS = $savedHfProgress
        $env:HF_HUB_DISABLE_SYMLINKS_WARNING = $savedHfWarning
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

function Get-UpdateHealthPayload {
    $request = [System.Net.HttpWebRequest]::Create("http://127.0.0.1:8765/api/health")
    $request.Timeout = 5000
    $request.ReadWriteTimeout = 5000
    $request.Proxy = $null
    $response = $null
    $reader = $null
    try {
        $response = $request.GetResponse()
        $reader = New-Object System.IO.StreamReader($response.GetResponseStream(), [System.Text.Encoding]::UTF8)
        return ($reader.ReadToEnd() | ConvertFrom-Json)
    } finally {
        if ($null -ne $reader) { $reader.Dispose() }
        if ($null -ne $response) { $response.Dispose() }
    }
}

function Get-UpdateHealthFailureSummary([object]$Health, [string]$ExpectedFingerprint="") {
    $issues = New-Object System.Collections.Generic.List[string]
    if ($null -eq $Health) { return "健康服务尚未响应" }
    if ($Health.owvoice -ne $true) { [void]$issues.Add("健康接口身份不是 OwVoice") }
    try { $healthRoot = [System.IO.Path]::GetFullPath([string]$Health.project_dir) }
    catch { $healthRoot = "" }
    if ([string]::IsNullOrWhiteSpace($healthRoot) -or -not $healthRoot.Equals($targetDir, [System.StringComparison]::OrdinalIgnoreCase)) {
        [void]$issues.Add("安装目录不匹配：$([string]$Health.project_dir)")
    }
    if ([string]$Health.version -ne [string]$packageManifest.version) {
        [void]$issues.Add("版本不匹配：实际 $([string]$Health.version)，应为 $([string]$packageManifest.version)")
    }
    $labels = [ordered]@{
        environment = "Python/Torch 环境"
        configuration = "本地配置"
        model_registry = "模型注册表"
        resources = "运行资源"
        dependencies = "后端依赖"
    }
    foreach ($property in $labels.GetEnumerator()) {
        $componentProperty = $Health.update_health.PSObject.Properties[[string]$property.Key]
        $component = if ($null -ne $componentProperty) { $componentProperty.Value } else { $null }
        if ($null -eq $component -or $component.ok -ne $true) {
            $detail = if ($null -ne $component -and -not [string]::IsNullOrWhiteSpace([string]$component.error)) { [string]$component.error } else { "未通过" }
            [void]$issues.Add("$($property.Value)：$detail")
        }
    }
    if (-not [string]::IsNullOrWhiteSpace($ExpectedFingerprint) -and
        [string]$Health.update_health.environment.fingerprint -ne $ExpectedFingerprint) {
        [void]$issues.Add("环境指纹不匹配")
    }
    if ($Health.ready_for_update_commit -ne $true -and $issues.Count -eq 0) {
        [void]$issues.Add("后端尚未允许提交更新")
    }
    if ($issues.Count -eq 0) { return "" }
    return ($issues -join "；")
}

function Stop-UpdateHealthProcess([object]$Process) {
    if ($null -eq $Process) { return }
    try { $Process.Refresh() } catch { }
    if ($Process.HasExited) { return }
    Start-Process -FilePath "taskkill.exe" -ArgumentList @("/PID", [string]$Process.Id, "/T", "/F") -WindowStyle Hidden -Wait -ErrorAction SilentlyContinue | Out-Null
}

try {
    Start-Transcript -LiteralPath $updateLogPath -Append | Out-Null
    $updateTranscriptStarted = $true
    Write-Host "更新会话：$([Guid]::NewGuid().ToString('N').Substring(0, 12))；目标目录：$targetDir"
    try { $lockStream = [System.IO.File]::Open($lockPath, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None) }
    catch { throw "另一个 OwVoice 更新任务正在运行。" }
    Recover-InterruptedTransactions

    # 必须先恢复上次中断事务，再检查这些可能在中断时已被移入 backup 的文件。
    $hasModelData = (Test-Path -LiteralPath (Join-Path $targetDir "data\models") -PathType Container) -or
        (Test-Path -LiteralPath (Join-Path $targetDir "models") -PathType Container)
    $effectiveLayoutPath = $layoutPath
    if ($AllowLegacyTargetWithoutIdentity -and -not (Test-Path -LiteralPath $effectiveLayoutPath -PathType Leaf) -and
        (Test-Path -LiteralPath $legacyLayoutPath -PathType Leaf)) {
        $effectiveLayoutPath = $legacyLayoutPath
    }
    if ((-not $AllowLegacyTargetWithoutIdentity -and -not (Test-Path -LiteralPath $identityPath -PathType Leaf)) -or
        -not (Test-Path -LiteralPath $effectiveLayoutPath -PathType Leaf) -or
        -not (Test-Path -LiteralPath $targetExe -PathType Leaf) -or
        -not (Test-Path -LiteralPath (Join-Path $targetDir "config") -PathType Container) -or
        -not $hasModelData) {
        throw "更新目标不是有效的 OwVoice 项目目录。"
    }

    if ($RecoverOnly) {
        Write-Host "已处理未完成的 OwVoice 更新事务。" -ForegroundColor Green
        return
    }

    $localLayout = Get-Content -LiteralPath $effectiveLayoutPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($localLayout.schema -ne 1) { throw "本地 release-layout.json schema 无效。" }
    $localItems = Get-NormalizedList @($localLayout.managedItems) "本地 managedItems"
    $localPreserve = Get-NormalizedList @($localLayout.preservePaths) "本地 preservePaths"
    Write-UpdateStage 1 "正在检查更新包和磁盘空间"
    Assert-FreeSpace ($ExpectedSize + 512MB) "准备更新包时"
    if (-not [string]::IsNullOrWhiteSpace($LocalArchivePath)) {
        $archivePath = [System.IO.Path]::GetFullPath($LocalArchivePath)
        if (-not (Test-Path -LiteralPath $archivePath -PathType Leaf)) { throw "本地更新包不存在：$archivePath" }
        $localSize = (Get-Item -LiteralPath $archivePath).Length
        if ($localSize -ne $ExpectedSize) { throw "本地更新包大小不一致：应为 $ExpectedSize，实际为 $localSize。" }
        $localHash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($localHash -ne $Sha256.ToLowerInvariant()) { throw "本地更新包 SHA256 校验失败。" }
        $updateRootPrefix = [System.IO.Path]::GetFullPath($updateRoot).TrimEnd("\", "/") + [System.IO.Path]::DirectorySeparatorChar
        $removeArchiveOnExit = Test-PathInside $archivePath $updateRootPrefix
    } else {
        $archiveFileName = [System.IO.Path]::GetFileName(([uri]$DownloadUrl).AbsolutePath)
        if ([string]::IsNullOrWhiteSpace($archiveFileName)) { throw "更新包文件名无效。" }
        $archivePath = Join-Path $downloadDir $archiveFileName
        $removeArchiveOnExit = $true
        Download-WithRetry $DownloadUrl $archivePath
    }

    Write-UpdateStage 2 "正在关闭当前安装目录中的 OwVoice 相关进程"
    if ($WaitPid -gt 0) {
        try { Wait-Process -Id $WaitPid -Timeout 120 -ErrorAction SilentlyContinue } catch { }
        if ($null -ne (Get-Process -Id $WaitPid -ErrorAction SilentlyContinue)) { throw "OwVoice 未在 120 秒内退出，已取消更新。" }
        $waitedForApplication = $true
    }
    $stoppedProcessIds = @(Stop-OwVoiceOwnedProcesses -TargetDirectory $targetDir -ExcludeProcessIds @($WaitPid))
    if ($stoppedProcessIds.Count -gt 0) {
        Write-Host ("已关闭当前 OwVoice 安装目录的残留进程：{0}" -f ($stoppedProcessIds -join ", ")) -ForegroundColor Yellow
    }
    if (-not $NoRestart) {
        $deadline = [DateTime]::UtcNow.AddSeconds(30)
        while ((Test-TcpPort 8765) -or (Test-TcpPort 9880)) {
            if ([DateTime]::UtcNow -ge $deadline) { throw "OwVoice 后端或语音引擎仍在运行，已取消更新。" }
            Start-Sleep -Milliseconds 500
        }
    }

    Write-UpdateStage 3 "正在安全解压并校验更新包"
    $extractDir = Join-Path $updateRoot ("extract-" + [guid]::NewGuid().ToString("N"))
    Expand-SafeArchive $archivePath $extractDir
    $topDirectories = @(Get-ChildItem -LiteralPath $extractDir -Directory -Force)
    if ($topDirectories.Count -ne 1 -or @(Get-ChildItem -LiteralPath $extractDir -File -Force).Count -ne 0) { throw "更新包顶层结构无效。" }
    $packageRoot = $topDirectories[0].FullName
    Assert-Package $packageRoot
    $environmentInfo = Get-EnvironmentTransactionInfo $packageRoot

    $transactionItems = @($localItems + $incomingItems | Sort-Object -Unique)
    $transactionPreserve = @($localPreserve + $incomingPreserve | Sort-Object -Unique)
    $transactionId = [guid]::NewGuid().ToString("N")
    $transactionDir = Join-Path $transactionsDir $transactionId
    $backupDir = Join-Path $transactionDir "backup"
    $metadataBackupDir = Join-Path $transactionDir "metadata-backup"
    $journalPath = Join-Path $transactionDir "journal.json"
    New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
    $journal = [ordered]@{
        schema = 1; id = $transactionId; phase = "prepared"; target_dir = $targetDir; backup_dir = $backupDir
        managed_items = $transactionItems; incoming_items = $incomingItems; preserve_paths = $transactionPreserve
        version = [string]$packageManifest.version; environment = $environmentInfo; created_at_utc = [DateTime]::UtcNow.ToString("o")
    }
    Write-Journal

    $userDataSnapshot = Get-UserDataSnapshot $targetDir
    Backup-CriticalMetadata $targetDir $userDataSnapshot $metadataBackupDir
    $journal.user_data = [ordered]@{ snapshot=$userDataSnapshot; metadata_backup_dir=$metadataBackupDir }
    $journal.phase = "user_data_backed_up"; Write-Journal
    Invoke-TestFailure "after_user_data_backup"

    Write-UpdateStage 4 "正在备份可回滚的程序文件和关键元数据"
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

    Write-UpdateStage 5 "正在安装新版本程序文件"
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
    Invoke-EnvironmentMigration $journal
    Assert-UserDataSnapshot $targetDir $journal.user_data.snapshot

    Write-UpdateStage 8 "正在后台启动新版本验证服务并执行健康检查"
    if (-not $NoRestart) {
        if ([string]::IsNullOrWhiteSpace($RestartPath)) { $RestartPath = $targetExe }
        $healthPython = Join-Path $targetDir ".venv\Scripts\python.exe"
        if (-not (Test-Path -LiteralPath $healthPython -PathType Leaf)) { throw "新环境缺少健康检查 Python：$healthPython" }
        Write-Host "更新验证期间不会显示主界面；正在核对版本、安装目录、Python/Torch、资源、配置和模型注册表。"
        Write-Host "全部通过并提交更新后，OwVoice 主界面才会自动打开。"
        $savedProjectDir = $env:OWVOICE_PROJECT_DIR
        $savedInitialVoice = $env:OWVOICE_INITIAL_VOICE_ID
        $savedPythonUtf8 = $env:PYTHONUTF8
        $savedPythonIoEncoding = $env:PYTHONIOENCODING
        $savedNltkData = $env:NLTK_DATA
        try {
            $env:OWVOICE_PROJECT_DIR = $targetDir
            $env:OWVOICE_INITIAL_VOICE_ID = ""
            $env:PYTHONUTF8 = "1"
            $env:PYTHONIOENCODING = "utf-8"
            $env:NLTK_DATA = Join-Path $targetDir "data\nltk_data"
            $newProcess = Start-Process -FilePath $healthPython -ArgumentList @("-m", "uvicorn", "backend.server:app", "--host", "127.0.0.1", "--port", "8765", "--log-level", "warning", "--no-access-log") -WorkingDirectory $targetDir -WindowStyle Hidden -PassThru
        } finally {
            $env:OWVOICE_PROJECT_DIR = $savedProjectDir
            $env:OWVOICE_INITIAL_VOICE_ID = $savedInitialVoice
            $env:PYTHONUTF8 = $savedPythonUtf8
            $env:PYTHONIOENCODING = $savedPythonIoEncoding
            $env:NLTK_DATA = $savedNltkData
        }
        $journal.phase = "pending_health"; $journal.started_pid = $newProcess.Id; Write-Journal
        $healthDeadline = [DateTime]::UtcNow.AddSeconds($HealthTimeoutSeconds)
        $healthy = $false
        $lastHealthIssue = "健康服务尚未响应"
        $healthReportPath = Join-Path $updateLogDir ("update-health-{0}.json" -f ([string]$journal.id))
        $healthWatch = [Diagnostics.Stopwatch]::StartNew()
        while ([DateTime]::UtcNow -lt $healthDeadline) {
            $newProcess.Refresh()
            if ($newProcess.HasExited) { $lastHealthIssue = "后台健康验证进程提前退出（退出码 $($newProcess.ExitCode)）"; break }
            Write-Progress -Activity "正在验证新版本" -Status ("已用时 {0:N0} 秒：{1}" -f $healthWatch.Elapsed.TotalSeconds, $lastHealthIssue)
            try {
                $health = Get-UpdateHealthPayload
                [System.IO.File]::WriteAllText($healthReportPath, ($health | ConvertTo-Json -Depth 12), $utf8NoBom)
                $expectedFingerprint = if ($journal.environment.phase -eq "switched") { [string]$journal.environment.state.environment_fingerprint } else { "" }
                $lastHealthIssue = Get-UpdateHealthFailureSummary -Health $health -ExpectedFingerprint $expectedFingerprint
                if ([string]::IsNullOrWhiteSpace($lastHealthIssue)) { $healthy = $true; break }
                throw "新版本健康响应已返回，但提交条件未通过：$lastHealthIssue"
            } catch {
                $message = Get-OwVoiceErrorText $_
                if ($message -like "新版本健康响应已返回，但提交条件未通过：*") { $lastHealthIssue = $message; break }
                $lastHealthIssue = "等待健康服务响应：$message"
            }
            Start-Sleep -Milliseconds 750
        }
        $healthWatch.Stop()
        Write-Progress -Activity "正在验证新版本" -Completed
        Stop-UpdateHealthProcess $newProcess
        $portCloseDeadline = [DateTime]::UtcNow.AddSeconds(10)
        while ((Test-TcpPort 8765) -and [DateTime]::UtcNow -lt $portCloseDeadline) { Start-Sleep -Milliseconds 200 }
        if (-not $healthy) {
            throw "新版本健康检查未通过：$lastHealthIssue。详细状态：$healthReportPath"
        }
        Write-Host ("新版本健康检查通过，已用时 {0:N0} 秒。" -f $healthWatch.Elapsed.TotalSeconds) -ForegroundColor Green
    }

    Assert-UserDataSnapshot $targetDir $journal.user_data.snapshot

    $journal.phase = "health_verified"; Write-Journal
    Write-UpdateStage 9 "正在提交更新并清理临时备份"
    Complete-EnvironmentForTransaction $journal
    $journal.phase = "committed"; $journal.committed_at_utc = [DateTime]::UtcNow.ToString("o"); Write-Journal
    Remove-Item -LiteralPath $transactionDir -Recurse -Force
    $transactionDir = $null
    Write-Host "OwVoice 更新完成。" -ForegroundColor Green
    if (-not $NoRestart) {
        try {
            Start-Process -FilePath ([System.IO.Path]::GetFullPath($RestartPath)) -WorkingDirectory $targetDir | Out-Null
            Write-Host "已提交更新，正在打开 OwVoice 主界面。" -ForegroundColor Green
        } catch {
            Write-Host "更新已成功提交，但未能自动打开 OwVoice；请手动双击 OwVoice.exe。原因：$(Get-OwVoiceErrorText $_)" -ForegroundColor Yellow
        }
    }
} catch {
    $originalError = Get-OwVoiceErrorText $_
    Stop-UpdateHealthProcess $newProcess
    $errorReference = "UPDATE-500 · " + [Guid]::NewGuid().ToString("N").Substring(0, 8)
    Write-Host "更新错误编号：$errorReference" -ForegroundColor Red
    if ($null -ne $journal -and $journal.phase -eq "health_verified") {
        throw "$originalError`n新版本已通过健康检查；环境清理将在下次更新恢复时继续。"
    }
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
    if ($updateTranscriptStarted) { Stop-Transcript | Out-Null }
    if ($null -ne $lockStream) { $lockStream.Dispose() }
    if ($null -ne $extractDir -and (Test-Path -LiteralPath $extractDir)) { Remove-Item -LiteralPath $extractDir -Recurse -Force -ErrorAction SilentlyContinue }
    if ($removeArchiveOnExit -and $null -ne $archivePath -and (Test-Path -LiteralPath $archivePath)) { Remove-Item -LiteralPath $archivePath -Force -ErrorAction SilentlyContinue }
}
