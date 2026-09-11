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
    "scripts", "tools", "_internal", "config", "data\models", "data\training\jobs\job-001", ".venv", ".runtime"
)
foreach ($directory in $directories) { New-Item -ItemType Directory -Force -Path (Join-Path $target $directory) | Out-Null }
[System.IO.File]::WriteAllText((Join-Path $target ".venv\keep.txt"), "venv")
[System.IO.File]::WriteAllText((Join-Path $target ".runtime\keep.txt"), "runtime")
[System.IO.File]::WriteAllText((Join-Path $target "data\training\jobs\job-001\job.json"), "training")
[System.IO.File]::WriteAllText((Join-Path $target "assets\avatars\keep.txt"), "avatar")
[System.IO.File]::WriteAllText((Join-Path $target "GPT-SoVITS\GPT_SoVITS\pretrained_models\keep.bin"), "model")
[System.IO.File]::WriteAllText((Join-Path $target "config\voices.local.json"), "local-config")
[System.IO.File]::WriteAllText((Join-Path $target "backend\old.txt"), "old")

foreach ($directory in @("backend", "frontend", "assets\avatars", "GPT-SoVITS\GPT_SoVITS\pretrained_models", "scripts", "tools\uv", "_internal", "config")) {
    New-Item -ItemType Directory -Force -Path (Join-Path $package $directory) | Out-Null
}
[System.IO.File]::WriteAllText((Join-Path $package "backend\new.txt"), "new")
[System.IO.File]::WriteAllText((Join-Path $package "assets\new.txt"), "new")
[System.IO.File]::WriteAllText((Join-Path $package "GPT-SoVITS\new.py"), "new")
[System.IO.File]::WriteAllText((Join-Path $package "assets\avatars\keep.txt"), "overwrite-attempt")
[System.IO.File]::WriteAllText((Join-Path $package "GPT-SoVITS\GPT_SoVITS\pretrained_models\keep.bin"), "overwrite-attempt")
[System.IO.File]::WriteAllText((Join-Path $package "tools\uv\uv.exe"), "new-uv")
[System.IO.File]::WriteAllText((Join-Path $package "config\voices.example.json"), "example")
foreach ($file in @("OwVoice.exe", "requirements-cpu.txt", "requirements-gpu.txt", "requirements-training.txt", "pyproject.toml", "uv.lock", "setup.bat", "README.md", "CHANGELOG.md", "MODEL_PACKAGE_SPEC.md", "LICENSE", "THIRD_PARTY_NOTICES.md")) {
    [System.IO.File]::WriteAllText((Join-Path $package $file), "new")
}
Compress-Archive -LiteralPath $package -DestinationPath $archive
$sha256 = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
Copy-Item -LiteralPath $package -Destination $brokenPackage -Recurse
Remove-Item -LiteralPath (Join-Path $brokenPackage "README.md") -Force
Compress-Archive -LiteralPath $brokenPackage -DestinationPath $brokenArchive
$brokenSha256 = (Get-FileHash -LiteralPath $brokenArchive -Algorithm SHA256).Hash.ToLowerInvariant()

$listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
$listener.Start()
$port = ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
$listener.Stop()
$pythonExe = Join-Path $projectRoot ".venv\Scripts\python.exe"
$server = Start-Process -FilePath $pythonExe -ArgumentList @("-m", "http.server", "$port", "--bind", "127.0.0.1", "--directory", $serverRoot) -WindowStyle Hidden -PassThru
try {
    Start-Sleep -Milliseconds 750
    & (Join-Path $projectRoot "scripts\update_release.ps1") -DownloadUrl "http://127.0.0.1:$port/OwVoice-v0.2.0.zip" -Sha256 $sha256 -TargetDirectory $target -NoRestart

    $checks = [ordered]@{
        venv = ((Get-Content -LiteralPath (Join-Path $target ".venv\keep.txt") -Raw) -eq "venv")
        runtime = ((Get-Content -LiteralPath (Join-Path $target ".runtime\keep.txt") -Raw) -eq "runtime")
        training = ((Get-Content -LiteralPath (Join-Path $target "data\training\jobs\job-001\job.json") -Raw) -eq "training")
        avatar = ((Get-Content -LiteralPath (Join-Path $target "assets\avatars\keep.txt") -Raw) -eq "avatar")
        model = ((Get-Content -LiteralPath (Join-Path $target "GPT-SoVITS\GPT_SoVITS\pretrained_models\keep.bin") -Raw) -eq "model")
        localConfig = ((Get-Content -LiteralPath (Join-Path $target "config\voices.local.json") -Raw) -eq "local-config")
        newBackend = (Test-Path -LiteralPath (Join-Path $target "backend\new.txt") -PathType Leaf)
        oldBackendRemoved = (-not (Test-Path -LiteralPath (Join-Path $target "backend\old.txt")))
        overlayCode = (Test-Path -LiteralPath (Join-Path $target "GPT-SoVITS\new.py") -PathType Leaf)
        uvTool = ((Get-Content -LiteralPath (Join-Path $target "tools\uv\uv.exe") -Raw) -eq "new-uv")
        lockFile = (Test-Path -LiteralPath (Join-Path $target "uv.lock") -PathType Leaf)
    }
    $failed = @($checks.GetEnumerator() | Where-Object { -not $_.Value } | ForEach-Object Key)
    if ($failed.Count -gt 0) { throw "更新保留测试失败：$($failed -join ', ')" }

    $expectedFailure = $false
    try {
        & (Join-Path $projectRoot "scripts\update_release.ps1") -DownloadUrl "http://127.0.0.1:$port/OwVoice-v0.2.1.zip" -Sha256 $brokenSha256 -TargetDirectory $target -NoRestart
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
    Write-Host "更新保留测试通过：.venv、私有 Python、模型、训练记录、头像和本地配置均保留，程序文件已更新。" -ForegroundColor Green
    Write-Host "更新失败回滚测试通过：损坏包被拒绝，原程序文件已恢复。" -ForegroundColor Green
} finally {
    Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $resolvedTestRoot) { Remove-Item -LiteralPath $resolvedTestRoot -Recurse -Force }
}
