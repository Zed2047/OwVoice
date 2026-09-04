param(
    [string]$Version = "v0.1.2",
    [string]$OutputDirectory = "dist",
    [switch]$SkipArchive
)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
$projectRoot = (Get-Location).Path
$releaseOutputPath = [System.IO.Path]::GetFullPath((Join-Path $projectRoot $OutputDirectory))
$releaseStagingPath = Join-Path $releaseOutputPath "release-staging"
$releaseName = "OwVoice-$Version"
$releasePackagePath = Join-Path $releaseStagingPath $releaseName
$releaseArchivePath = Join-Path $releaseOutputPath "$releaseName.zip"

New-Item -ItemType Directory -Force -Path $releaseOutputPath | Out-Null
if (Test-Path -LiteralPath $releaseStagingPath) { Remove-Item -LiteralPath $releaseStagingPath -Recurse -Force }
if (Test-Path -LiteralPath $releaseArchivePath) { Remove-Item -LiteralPath $releaseArchivePath -Force }
New-Item -ItemType Directory -Force -Path $releasePackagePath | Out-Null

function Copy-EngineTree([string]$sourceDirectory, [string]$targetDirectory) {
    New-Item -ItemType Directory -Force -Path $targetDirectory | Out-Null
    foreach ($item in (Get-ChildItem -LiteralPath $sourceDirectory -Force)) {
        $skipDirectory = $item.PSIsContainer -and ($item.Name -in @("pretrained_models", "__pycache__", ".git", "G2PWModel", "uvr5_weights"))
        # 这些是本地运行后可自动生成的缓存/编译词典，不应占用发布包空间。
        $generatedFileNames = @("cmudict_cache.pickle", "engdict_cache.pickle", "namedict_cache.pickle", "user.dict")
        $skipFile = (-not $item.PSIsContainer) -and (
            $item.Extension -in @(".bak", ".tmp", ".pyc", ".zip", ".pth", ".pt") -or
            $item.Name -in $generatedFileNames
        )
        if ($skipDirectory -or $skipFile) { continue }
        $targetItem = Join-Path $targetDirectory $item.Name
        if ($item.PSIsContainer) {
            Copy-EngineTree $item.FullName $targetItem
        } else {
            Copy-Item -LiteralPath $item.FullName -Destination $targetItem -Force
        }
    }
}

foreach ($file in @("README.md", "MODEL_PACKAGE_SPEC.md", "LICENSE", "THIRD_PARTY_NOTICES.md", "requirements-cpu.txt", "requirements-gpu.txt", "requirements-training.txt")) {
    Copy-Item -LiteralPath (Join-Path $projectRoot $file) -Destination $releasePackagePath
}
# 发布 ZIP 使用 ASCII 文件名，避免 Windows 压缩工具处理中文文件名时产生乱码。
[System.IO.File]::Copy((Join-Path (Get-Location) "setup.bat"), (Join-Path $releasePackagePath "setup.bat"), $true)
foreach ($directory in @("backend", "frontend")) {
    $sourceDirectory = Join-Path $projectRoot $directory
    $targetDirectory = Join-Path $releasePackagePath $directory
    New-Item -ItemType Directory -Force -Path $targetDirectory | Out-Null
    Get-ChildItem -LiteralPath $sourceDirectory -Force | Copy-Item -Destination $targetDirectory -Recurse
}
New-Item -ItemType Directory -Force -Path (Join-Path $releasePackagePath "config") | Out-Null
# 公开包不携带角色头像等本地素材；用户模型和头像由本地模型库自行管理。
$releasePackagePath = Join-Path -Path $releaseStagingPath -ChildPath $releaseName
$assetsTargetPath = Join-Path -Path $releasePackagePath -ChildPath "assets"
New-Item -ItemType Directory -Force -Path $assetsTargetPath | Out-Null
Get-ChildItem -LiteralPath (Join-Path $projectRoot "assets") -Force | Where-Object { $_.Name -ne "avatars" } | Copy-Item -Destination $assetsTargetPath -Recurse -Force

# Release 只复制首次配置所需脚本，构建和调试脚本留在源码仓库。
$releaseScriptsTargetPath = Join-Path $releasePackagePath "scripts"
New-Item -ItemType Directory -Force -Path $releaseScriptsTargetPath | Out-Null
foreach ($scriptName in @("setup.ps1", "check_env.ps1", "download_pretrained.py", "verify_runtime.py", "setup_training.ps1", "download_nltk_data.py", "update_release.ps1", "start_training.ps1")) {
    Copy-Item -LiteralPath (Join-Path (Join-Path $projectRoot "scripts") $scriptName) -Destination $releaseScriptsTargetPath -Force
}

# Copy the modified GPT-SoVITS inference source, excluding its large model store.
$engineSource = Join-Path $projectRoot "GPT-SoVITS"
$engineTargetPath = Join-Path $releasePackagePath "GPT-SoVITS"
New-Item -ItemType Directory -Force -Path $engineTargetPath | Out-Null
foreach ($file in @("api.py", "config.py", "extra-req.txt", "requirements.txt", "webui.py", "LICENSE")) {
    Copy-Item -LiteralPath (Join-Path $engineSource $file) -Destination $engineTargetPath -Force
}
Copy-EngineTree (Join-Path $engineSource "GPT_SoVITS") (Join-Path $engineTargetPath "GPT_SoVITS")
# lid.176.bin 由 setup.ps1 -> download_pretrained.py 下载，发布包不重复携带约 125 MB 文件。
# 训练界面需要 tools/asr、tools/uvr5 等通用源码；Copy-EngineTree 会排除缓存和权重文件。
$engineTargetPath = Join-Path -Path $releasePackagePath -ChildPath "GPT-SoVITS"
$toolsTargetPath = Join-Path -Path $engineTargetPath -ChildPath "tools"
Copy-EngineTree (Join-Path $engineSource "tools") $toolsTargetPath
Get-ChildItem -LiteralPath $releasePackagePath -Recurse -File | Where-Object { $_.Extension -in @('.bak', '.tmp', '.pyc') } | Remove-Item -Force
Get-ChildItem -LiteralPath $releasePackagePath -Recurse -Directory | Where-Object { $_.Name -in @('__pycache__', '.pytest_cache') } | Remove-Item -Recurse -Force
Remove-Item -LiteralPath (Join-Path $releasePackagePath "config\voices.local.json") -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path (Join-Path $releasePackagePath "config") | Out-Null
Copy-Item -LiteralPath ".\config\voices.example.json" -Destination (Join-Path $releasePackagePath "config") -Force
Copy-Item -LiteralPath ".\config\voices.example.json" -Destination (Join-Path $releasePackagePath "config\voices.local.json") -Force

# 公开发布包不包含任何角色模型权重；本地模型由用户自行导入到独立数据目录。
$releasePackagePath = Join-Path -Path $releaseStagingPath -ChildPath $releaseName
$releaseModelsPath = Join-Path $releasePackagePath "data\models"
New-Item -ItemType Directory -Force -Path $releaseModelsPath | Out-Null
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$emptyRegistry = @{ schema = 1; models = @() } | ConvertTo-Json
[System.IO.File]::WriteAllText((Join-Path $releaseModelsPath "installed-models.json"), $emptyRegistry, $utf8NoBom)
$emptyConfig = @{ version = 1; voices = @() } | ConvertTo-Json
[System.IO.File]::WriteAllText((Join-Path $releasePackagePath "config\voices.example.json"), $emptyConfig, $utf8NoBom)
[System.IO.File]::WriteAllText((Join-Path $releasePackagePath "config\voices.local.json"), $emptyConfig, $utf8NoBom)
$exeSource = Join-Path $projectRoot "dist\exe\OwVoice"
if (-not (Test-Path -LiteralPath (Join-Path $exeSource "OwVoice.exe") -PathType Leaf)) {
    throw "OwVoice.exe is missing. Run scripts\build_exe.ps1 before building the release package."
}
Copy-Item -LiteralPath (Join-Path $exeSource "OwVoice.exe") -Destination $releasePackagePath -Force
Copy-Item -LiteralPath (Join-Path $exeSource "_internal") -Destination $releasePackagePath -Recurse -Force

$stagingOnlyPath = Join-Path $releaseStagingPath $releaseName
if ($SkipArchive) {
    Write-Host ("Release staging complete (no ZIP created): {0}" -f $stagingOnlyPath) -ForegroundColor Green
    return
}

$tar = Get-Command tar.exe -ErrorAction SilentlyContinue
if ($null -ne $tar) {
    & $tar.Source -a -cf $releaseArchivePath -C $releaseStagingPath $releaseName
    if ($LASTEXITCODE -ne 0) { throw "Release ZIP creation failed." }
} else {
    Compress-Archive -LiteralPath $releasePackagePath -DestinationPath $releaseArchivePath -CompressionLevel Optimal
}
$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $releaseArchivePath).Hash.ToLowerInvariant()
$size = (Get-Item -LiteralPath $releaseArchivePath).Length
[PSCustomObject]@{ version=$Version; archive_name=(Split-Path -Leaf $releaseArchivePath); sha256=$hash; size_bytes=$size } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $releaseOutputPath "release-manifest-$Version.json") -Encoding UTF8
Write-Host ("Release ZIP complete: {0} MB, SHA256: {1}" -f [Math]::Round($size / 1MB, 1), $hash) -ForegroundColor Green
