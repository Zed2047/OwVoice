$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$testRoot = Join-Path $projectRoot ".cache\update-release-test"
$expectedPrefix = [System.IO.Path]::GetFullPath((Join-Path $projectRoot ".cache")) + [System.IO.Path]::DirectorySeparatorChar
$resolvedTestRoot = [System.IO.Path]::GetFullPath($testRoot)
if (-not $resolvedTestRoot.StartsWith($expectedPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "测试目录超出项目缓存目录。"
}
if (Test-Path -LiteralPath $resolvedTestRoot) { Remove-Item -LiteralPath $resolvedTestRoot -Recurse -Force }

$target = Join-Path $resolvedTestRoot "target"
$serverRoot = Join-Path $resolvedTestRoot "server"
$package = Join-Path $serverRoot "OwVoice-v0.2.0"
$archive = Join-Path $serverRoot "OwVoice-v0.2.0.zip"
$brokenPackage = Join-Path $serverRoot "OwVoice-v0.2.1"
$brokenArchive = Join-Path $serverRoot "OwVoice-v0.2.1.zip"
$directories = @(
    "backend", "frontend", "assets\avatars", "GPT-SoVITS\GPT_SoVITS\pretrained_models",
    "scripts", "tools", "_internal", "config", "data\models\voice-001", "data\training\jobs\job-001", "output", ".venv", ".runtime"
)
foreach ($directory in $directories) { New-Item -ItemType Directory -Force -Path (Join-Path $target $directory) | Out-Null }
[System.IO.File]::WriteAllText((Join-Path $target ".venv\keep.txt"), "venv")
[System.IO.File]::WriteAllText((Join-Path $target ".runtime\keep.txt"), "runtime")
[System.IO.File]::WriteAllText((Join-Path $target "data\training\jobs\job-001\job.json"), "training")
[System.IO.File]::WriteAllText((Join-Path $target "data\models\installed-models.json"), "registry")
[System.IO.File]::WriteAllText((Join-Path $target "data\models\voice-001\model.json"), "metadata")
[System.IO.File]::WriteAllText((Join-Path $target "data\models\voice-001\weights.pth"), "weights")
[System.IO.File]::WriteAllText((Join-Path $target "output\result.wav"), "audio")
[System.IO.File]::WriteAllText((Join-Path $target "assets\avatars\keep.txt"), "avatar")
[System.IO.File]::WriteAllText((Join-Path $target "GPT-SoVITS\GPT_SoVITS\pretrained_models\keep.bin"), "model")
[System.IO.File]::WriteAllText((Join-Path $target "config\voices.local.json"), "local-config")
[System.IO.File]::WriteAllText((Join-Path $target "backend\old.txt"), "old")
[System.IO.File]::WriteAllText((Join-Path $target "assets\old-public.txt"), "stale")
[System.IO.File]::WriteAllText((Join-Path $target "GPT-SoVITS\old-code.py"), "stale")
[System.IO.File]::WriteAllText((Join-Path $target "OwVoice.exe"), "old-exe")
[System.IO.File]::WriteAllText((Join-Path $target "version.json"), '{"version":"0.1.2","channel":"stable","updateSchema":2}')
Copy-Item -LiteralPath (Join-Path $projectRoot "release-layout.json") -Destination $target
Copy-Item -LiteralPath (Join-Path $projectRoot "updater") -Destination $target -Recurse
Copy-Item -LiteralPath (Join-Path $projectRoot "scripts\update_transaction.ps1") -Destination (Join-Path $target "scripts") -Force
Copy-Item -LiteralPath (Join-Path $projectRoot "scripts\process_lifecycle.ps1") -Destination (Join-Path $target "scripts") -Force

foreach ($directory in @("backend", "frontend", "assets\avatars", "GPT-SoVITS\GPT_SoVITS\pretrained_models", "scripts", "tools\uv", "_internal", "config")) {
    New-Item -ItemType Directory -Force -Path (Join-Path $package $directory) | Out-Null
}
[System.IO.File]::WriteAllText((Join-Path $package "backend\new.txt"), "new")
[System.IO.File]::WriteAllText((Join-Path $package "assets\new.txt"), "new")
[System.IO.File]::WriteAllText((Join-Path $package "GPT-SoVITS\new.py"), "new")
[System.IO.File]::WriteAllText((Join-Path $package "tools\uv\uv.exe"), "new-uv")
[System.IO.File]::WriteAllText((Join-Path $package "config\voices.example.json"), "example")
foreach ($file in @("OwVoice.exe", "requirements-cpu.txt", "requirements-gpu.txt", "requirements-training.txt", "BUILD_INFO.json", "pyproject.toml", "uv.lock", "resource-lock.json", "environment-spec.json", "dependency-contract.json", "setup.bat", "README.md", "CHANGELOG.md", "MODEL_PACKAGE_SPEC.md", "LICENSE", "THIRD_PARTY_NOTICES.md")) {
    [System.IO.File]::WriteAllText((Join-Path $package $file), "new")
}
[System.IO.File]::WriteAllText((Join-Path $package "version.json"), '{"version":"0.2.0","channel":"stable","updateSchema":2}')
Copy-Item -LiteralPath (Join-Path $projectRoot "release-layout.json") -Destination $package
Copy-Item -LiteralPath (Join-Path $projectRoot "recover_update.bat") -Destination $package
Copy-Item -LiteralPath (Join-Path $projectRoot "updater") -Destination $package -Recurse
Copy-Item -LiteralPath (Join-Path $projectRoot "scripts\update_transaction.ps1") -Destination (Join-Path $package "scripts") -Force
Copy-Item -LiteralPath (Join-Path $projectRoot "scripts\process_lifecycle.ps1") -Destination (Join-Path $package "scripts") -Force
$layout = Get-Content -LiteralPath (Join-Path $package "release-layout.json") -Raw -Encoding UTF8 | ConvertFrom-Json
$managedItems = @($layout.managedItems | ForEach-Object { ([string]$_).Replace("\", "/").Trim("/") })
$preservePaths = @($layout.preservePaths | ForEach-Object { ([string]$_).Replace("\", "/").Trim("/") })
$manifestFiles = @(
    Get-ChildItem -LiteralPath $package -Recurse -File -Force | ForEach-Object {
        $relative = $_.FullName.Substring($package.Length).TrimStart("\", "/").Replace("\", "/")
        $isManaged = @($managedItems | Where-Object { $relative -eq $_ -or $relative.StartsWith($_ + "/", [StringComparison]::OrdinalIgnoreCase) }).Count -gt 0
        $isPreserved = @($preservePaths | Where-Object { $relative -eq $_ -or $relative.StartsWith($_ + "/", [StringComparison]::OrdinalIgnoreCase) }).Count -gt 0
        if ($isManaged -and -not $isPreserved) {
            [ordered]@{ path=$relative; size_bytes=$_.Length; sha256=(Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant() }
        }
    } | Sort-Object path
)
$internalManifest = [ordered]@{ schema=1; version="0.2.0"; tag="v0.2.0"; managed_items=$managedItems; preserve_paths=$preservePaths; files=$manifestFiles } | ConvertTo-Json -Depth 6
[System.IO.File]::WriteAllText((Join-Path $package "release-files-v1.json"), $internalManifest, (New-Object Text.UTF8Encoding($false)))
Compress-Archive -LiteralPath $package -DestinationPath $archive
$sha256 = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
$archiveSize = (Get-Item -LiteralPath $archive).Length
Copy-Item -LiteralPath $package -Destination $brokenPackage -Recurse
Remove-Item -LiteralPath (Join-Path $brokenPackage "README.md") -Force
Compress-Archive -LiteralPath $brokenPackage -DestinationPath $brokenArchive
$brokenSha256 = (Get-FileHash -LiteralPath $brokenArchive -Algorithm SHA256).Hash.ToLowerInvariant()
$brokenSize = (Get-Item -LiteralPath $brokenArchive).Length
$targetTemplate = Join-Path $resolvedTestRoot "target-template"
Copy-Item -LiteralPath $target -Destination $targetTemplate -Recurse

$listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
$listener.Start()
$port = ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
$listener.Stop()
$pythonExe = Join-Path $projectRoot ".venv\Scripts\python.exe"
$server = Start-Process -FilePath $pythonExe -ArgumentList @("-m", "http.server", "$port", "--bind", "127.0.0.1", "--directory", $serverRoot) -WindowStyle Hidden -PassThru
try {
    Start-Sleep -Milliseconds 750
    $localArchive = Join-Path $target ".cache\updates\downloads\OwVoice-v0.2.0.zip"
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $localArchive) | Out-Null
    Copy-Item -LiteralPath $archive -Destination $localArchive -Force
    & (Join-Path $projectRoot "scripts\update_release.ps1") -LocalArchivePath $localArchive -Sha256 $sha256 -ExpectedSize $archiveSize -TargetDirectory $target -NoRestart

    $checks = [ordered]@{
        venv = ((Get-Content -LiteralPath (Join-Path $target ".venv\keep.txt") -Raw) -eq "venv")
        runtime = ((Get-Content -LiteralPath (Join-Path $target ".runtime\keep.txt") -Raw) -eq "runtime")
        training = ((Get-Content -LiteralPath (Join-Path $target "data\training\jobs\job-001\job.json") -Raw) -eq "training")
        registry = ((Get-Content -LiteralPath (Join-Path $target "data\models\installed-models.json") -Raw) -eq "registry")
        modelMetadata = ((Get-Content -LiteralPath (Join-Path $target "data\models\voice-001\model.json") -Raw) -eq "metadata")
        modelWeights = ((Get-Content -LiteralPath (Join-Path $target "data\models\voice-001\weights.pth") -Raw) -eq "weights")
        output = ((Get-Content -LiteralPath (Join-Path $target "output\result.wav") -Raw) -eq "audio")
        avatar = ((Get-Content -LiteralPath (Join-Path $target "assets\avatars\keep.txt") -Raw) -eq "avatar")
        model = ((Get-Content -LiteralPath (Join-Path $target "GPT-SoVITS\GPT_SoVITS\pretrained_models\keep.bin") -Raw) -eq "model")
        localConfig = ((Get-Content -LiteralPath (Join-Path $target "config\voices.local.json") -Raw) -eq "local-config")
        newBackend = (Test-Path -LiteralPath (Join-Path $target "backend\new.txt") -PathType Leaf)
        oldBackendRemoved = (-not (Test-Path -LiteralPath (Join-Path $target "backend\old.txt")))
        staleAssetRemoved = (-not (Test-Path -LiteralPath (Join-Path $target "assets\old-public.txt")))
        staleEngineCodeRemoved = (-not (Test-Path -LiteralPath (Join-Path $target "GPT-SoVITS\old-code.py")))
        overlayCode = (Test-Path -LiteralPath (Join-Path $target "GPT-SoVITS\new.py") -PathType Leaf)
        uvTool = ((Get-Content -LiteralPath (Join-Path $target "tools\uv\uv.exe") -Raw) -eq "new-uv")
        lockFile = (Test-Path -LiteralPath (Join-Path $target "uv.lock") -PathType Leaf)
    }
    $failed = @($checks.GetEnumerator() | Where-Object { -not $_.Value } | ForEach-Object Key)
    if ($failed.Count -gt 0) { throw "更新保留测试失败：$($failed -join ', ')" }

    $expectedFailure = $false
    try {
        & (Join-Path $projectRoot "scripts\update_release.ps1") -DownloadUrl "http://127.0.0.1:$port/OwVoice-v0.2.1.zip" -Sha256 $brokenSha256 -ExpectedSize $brokenSize -TargetDirectory $target -NoRestart
    } catch {
        $expectedFailure = $true
    }
    if (-not $expectedFailure) { throw "缺少 README.md 的损坏更新包未被拒绝。" }
    if (-not (Test-Path -LiteralPath (Join-Path $target "backend\new.txt") -PathType Leaf)) {
        throw "更新失败回滚后原有 backend 文件丢失。"
    }
    if ((Get-Content -LiteralPath (Join-Path $target "README.md") -Raw) -ne "new") {
        throw "更新失败回滚后原有 README.md 未恢复。"
    }

    foreach ($failurePoint in @("after_user_data_backup", "after_backup", "after_first_install", "after_install", "after_preserve", "after_verify")) {
        $failureTarget = Join-Path $resolvedTestRoot ("failure-" + $failurePoint)
        Copy-Item -LiteralPath $targetTemplate -Destination $failureTarget -Recurse
        $failureObserved = $false
        try {
            & (Join-Path $projectRoot "scripts\update_release.ps1") `
                -DownloadUrl "http://127.0.0.1:$port/OwVoice-v0.2.0.zip" `
                -Sha256 $sha256 `
                -ExpectedSize $archiveSize `
                -TargetDirectory $failureTarget `
                -NoRestart `
                -TestFailurePoint $failurePoint
        } catch {
            $failureObserved = $true
        }
        if (-not $failureObserved) { throw "故障点 $failurePoint 未触发。" }
        if ((Get-Content -LiteralPath (Join-Path $failureTarget "backend\old.txt") -Raw) -ne "old" -or
            (Test-Path -LiteralPath (Join-Path $failureTarget "backend\new.txt")) -or
            (Get-Content -LiteralPath (Join-Path $failureTarget "assets\avatars\keep.txt") -Raw) -ne "avatar" -or
            (Get-Content -LiteralPath (Join-Path $failureTarget "config\voices.local.json") -Raw) -ne "local-config" -or
            (Get-Content -LiteralPath (Join-Path $failureTarget "data\models\installed-models.json") -Raw) -ne "registry" -or
            (Get-Content -LiteralPath (Join-Path $failureTarget "data\models\voice-001\model.json") -Raw) -ne "metadata" -or
            (Get-Content -LiteralPath (Join-Path $failureTarget "data\models\voice-001\weights.pth") -Raw) -ne "weights" -or
            (Get-Content -LiteralPath (Join-Path $failureTarget "output\result.wav") -Raw) -ne "audio") {
            throw "故障点 $failurePoint 回滚后不是完整旧版本。"
        }
        $pendingTransactions = @(Get-ChildItem -LiteralPath (Join-Path $failureTarget ".cache\updates\transactions") -Directory -ErrorAction SilentlyContinue)
        if ($pendingTransactions.Count -ne 0) { throw "故障点 $failurePoint 回滚后仍有未完成事务。" }
    }

    # 并发更新必须在下载和文件变更前被互斥锁拒绝。
    $lockPath = Join-Path $target ".cache\updates\update.lock"
    $lockHandle = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    try {
        $lockRejected = $false
        try {
            & (Join-Path $projectRoot "scripts\update_release.ps1") -DownloadUrl "http://127.0.0.1:$port/OwVoice-v0.2.0.zip" -Sha256 $sha256 -ExpectedSize $archiveSize -TargetDirectory $target -NoRestart
        } catch { $lockRejected = $true }
        if (-not $lockRejected) { throw "并发更新未被互斥锁拒绝。" }
    } finally { $lockHandle.Dispose() }

    # 恶意 ZIP 路径必须在解压写盘前被拒绝。
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $traversalArchive = Join-Path $serverRoot "OwVoice-path-traversal.zip"
    $zip = [IO.Compression.ZipFile]::Open($traversalArchive, [IO.Compression.ZipArchiveMode]::Create)
    try {
        $entry = $zip.CreateEntry("OwVoice-v0.2.0/../escape.txt")
        $writer = New-Object IO.StreamWriter($entry.Open())
        try { $writer.Write("escape") } finally { $writer.Dispose() }
    } finally { $zip.Dispose() }
    $traversalHash = (Get-FileHash -LiteralPath $traversalArchive -Algorithm SHA256).Hash.ToLowerInvariant()
    $traversalSize = (Get-Item -LiteralPath $traversalArchive).Length
    $securityTarget = Join-Path $resolvedTestRoot "security-target"
    Copy-Item -LiteralPath $targetTemplate -Destination $securityTarget -Recurse
    $traversalRejected = $false
    try {
        & (Join-Path $projectRoot "scripts\update_release.ps1") -DownloadUrl "http://127.0.0.1:$port/OwVoice-path-traversal.zip" -Sha256 $traversalHash -ExpectedSize $traversalSize -TargetDirectory $securityTarget -NoRestart
    } catch { $traversalRejected = $true }
    if (-not $traversalRejected -or -not (Test-Path -LiteralPath (Join-Path $securityTarget "backend\old.txt"))) {
        throw "ZIP 路径穿越防护失败。"
    }

    # 模拟上次进程在备份中途崩溃；下一次启动应先按 journal 恢复旧目录。
    $recoveryTarget = Join-Path $resolvedTestRoot "recovery-target"
    Copy-Item -LiteralPath $targetTemplate -Destination $recoveryTarget -Recurse
    $crashDir = Join-Path $recoveryTarget ".cache\updates\transactions\simulated-crash"
    $crashBackup = Join-Path $crashDir "backup"
    New-Item -ItemType Directory -Force -Path $crashBackup | Out-Null
    foreach ($crashedItem in @("backend", "OwVoice.exe", "version.json", "release-layout.json")) {
        $crashDestination = Join-Path $crashBackup $crashedItem
        $crashParent = Split-Path -Parent $crashDestination
        if (-not (Test-Path -LiteralPath $crashParent)) { New-Item -ItemType Directory -Force -Path $crashParent | Out-Null }
        Move-Item -LiteralPath (Join-Path $recoveryTarget $crashedItem) -Destination $crashDestination
    }
    $crashJournal = [ordered]@{
        schema=1; id="simulated-crash"; phase="backing_up"; target_dir=$recoveryTarget
        backup_dir=$crashBackup; managed_items=@("backend", "OwVoice.exe", "version.json", "release-layout.json"); incoming_items=@("backend"); preserve_paths=@()
    } | ConvertTo-Json -Depth 5
    [IO.File]::WriteAllText((Join-Path $crashDir "journal.json"), $crashJournal, (New-Object Text.UTF8Encoding($false)))
    try {
        & (Join-Path $projectRoot "scripts\update_release.ps1") -DownloadUrl "http://127.0.0.1:$port/OwVoice-path-traversal.zip" -Sha256 $traversalHash -ExpectedSize $traversalSize -TargetDirectory $recoveryTarget -NoRestart
    } catch { }
    if ((Get-Content -LiteralPath (Join-Path $recoveryTarget "backend\old.txt") -Raw) -ne "old" -or (Test-Path -LiteralPath $crashDir)) {
        throw "中断事务自动恢复失败。"
    }
    Write-Host "更新保留测试通过：.venv、私有 Python、模型、训练记录、头像和本地配置均保留，程序文件已更新。" -ForegroundColor Green
    Write-Host "更新失败回滚测试通过：损坏包在改动前被拒绝，6 个事务故障点均恢复为完整旧版本。" -ForegroundColor Green
    Write-Host "更新安全测试通过：并发锁、ZIP 路径穿越和中断 journal 恢复均符合预期。" -ForegroundColor Green
} finally {
    Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $resolvedTestRoot) { Remove-Item -LiteralPath $resolvedTestRoot -Recurse -Force }
}
