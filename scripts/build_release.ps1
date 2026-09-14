param(
    [string]$Version = "",
    [string]$OutputDirectory = "dist",
    [switch]$SkipArchive,
    [switch]$AllowUntaggedBuild
)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
$projectRoot = (Get-Location).Path
$compatibilityScript = Join-Path $PSScriptRoot "verify_text_compatibility.ps1"
$windowsPowerShell = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
& $windowsPowerShell -NoProfile -ExecutionPolicy Bypass -File $compatibilityScript -ProjectRoot $projectRoot
if ($LASTEXITCODE -ne 0) { throw "文本兼容性检查失败，已停止发布包构建。" }
. (Join-Path $PSScriptRoot "release_common.ps1")
$releaseIdentity = Get-OwVoiceReleaseIdentity -ProjectRoot $projectRoot -ValidateMirrors
$Version = Resolve-OwVoiceReleaseTag -Identity $releaseIdentity -RequestedVersion $Version
if (-not $AllowUntaggedBuild) { Assert-OwVoiceReleaseGitTag -Identity $releaseIdentity -ProjectRoot $projectRoot }
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

foreach ($file in @("README.md", "CHANGELOG.md", "MODEL_PACKAGE_SPEC.md", "LICENSE", "THIRD_PARTY_NOTICES.md", "version.json", "resource-lock.json", "release-layout.json", "pyproject.toml", "uv.lock", "requirements-cpu.txt", "requirements-gpu.txt", "requirements-training.txt")) {
    Copy-Item -LiteralPath (Join-Path $projectRoot $file) -Destination $releasePackagePath
}
# 发布 ZIP 使用 ASCII 文件名，避免 Windows 压缩工具处理中文文件名时产生乱码。
[System.IO.File]::Copy((Join-Path (Get-Location) "setup.bat"), (Join-Path $releasePackagePath "setup.bat"), $true)
Copy-Item -LiteralPath (Join-Path $projectRoot "recover_update.bat") -Destination $releasePackagePath -Force
Copy-Item -LiteralPath (Join-Path $projectRoot "updater") -Destination $releasePackagePath -Recurse -Force
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
foreach ($scriptName in @("setup_v2.ps1", "check_env.ps1", "download_pretrained.py", "verify_runtime.py", "setup_training.ps1", "download_nltk_data.py", "update_release.ps1", "update_transaction.ps1", "repair_update_legacy.ps1", "release_common.ps1", "start_training.ps1")) {
    Copy-Item -LiteralPath (Join-Path (Join-Path $projectRoot "scripts") $scriptName) -Destination $releaseScriptsTargetPath -Force
}

# 内置固定版本、带签名且经过哈希校验的 uv；用户无需预装 Python 或配置 PATH。
$uvSource = Join-Path $projectRoot "tools\uv\uv.exe"
if (-not (Test-Path -LiteralPath $uvSource -PathType Leaf)) { throw "缺少内置安装工具：tools\uv\uv.exe" }
$uvTarget = Join-Path $releasePackagePath "tools\uv"
New-Item -ItemType Directory -Force -Path $uvTarget | Out-Null
Copy-Item -LiteralPath $uvSource -Destination $uvTarget -Force

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
# 防御式清理：即使未来复制逻辑扩展，也绝不允许开发机的 data/models
# 或旧 installed-models.json 进入公开发布包。
$releaseDataPath = Join-Path $releasePackagePath "data"
if (Test-Path -LiteralPath $releaseDataPath) { Remove-Item -LiteralPath $releaseDataPath -Recurse -Force }
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

# 记录发布产物的可追溯身份，便于定位“同版本、不同构建”问题。
$pythonExe = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) { throw "缺少构建环境 Python：$pythonExe" }
$gitCommand = Get-Command git.exe -ErrorAction SilentlyContinue
if ($null -eq $gitCommand) { throw "无法生成 BUILD_INFO.json：未找到 git.exe。" }
$gitCommit = ((& $gitCommand.Source -C $projectRoot rev-parse HEAD 2>&1) | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $gitCommit -notmatch '^[0-9a-fA-F]{40}$') { throw "无法读取构建提交哈希。" }
$pythonVersion = ((& $pythonExe --version 2>&1) | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { throw "无法读取 Python 版本。" }
$pyinstallerVersion = ((& $pythonExe -m PyInstaller --version 2>&1) | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { throw "无法读取 PyInstaller 版本。" }
$uvVersion = ((& $uvSource --version 2>&1) | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { throw "无法读取 uv 版本。" }
$buildArchitecture = if ([Environment]::Is64BitOperatingSystem) { "x64" } else { "x86" }
$buildInfo = [ordered]@{
    schema = 1
    version = $releaseIdentity.version
    tag = $releaseIdentity.tag
    channel = $releaseIdentity.channel
    update_schema = $releaseIdentity.updateSchema
    git_commit = $gitCommit.ToLowerInvariant()
    built_at_utc = [DateTime]::UtcNow.ToString("o")
    python = $pythonVersion
    pyinstaller = $pyinstallerVersion
    uv = $uvVersion
    architecture = $buildArchitecture
    os = [Environment]::OSVersion.VersionString
} | ConvertTo-Json
[System.IO.File]::WriteAllText((Join-Path $releasePackagePath "BUILD_INFO.json"), $buildInfo, $utf8NoBom)

# 包内清单描述所有受发布管理的文件。更新器以它验证完整性并拒绝混版。
$releaseLayout = Get-Content -LiteralPath (Join-Path $projectRoot "release-layout.json") -Raw -Encoding UTF8 | ConvertFrom-Json
if ($releaseLayout.schema -ne 1) { throw "release-layout.json schema 无效。" }
$managedItems = @($releaseLayout.managedItems | ForEach-Object { ([string]$_).Replace("\", "/").Trim("/") })
$preservePaths = @($releaseLayout.preservePaths | ForEach-Object { ([string]$_).Replace("\", "/").Trim("/") })
foreach ($item in $managedItems) {
    if ($item -ne "release-files-v1.json" -and -not (Test-Path -LiteralPath (Join-Path $releasePackagePath $item))) { throw "发布包缺少受管理项：$item" }
}
$releaseFiles = @(
    Get-ChildItem -LiteralPath $releasePackagePath -Recurse -File -Force | ForEach-Object {
        $relative = $_.FullName.Substring($releasePackagePath.Length).TrimStart("\", "/").Replace("\", "/")
        $isManaged = @($managedItems | Where-Object { $relative -eq $_ -or $relative.StartsWith($_ + "/", [StringComparison]::OrdinalIgnoreCase) }).Count -gt 0
        $isPreserved = @($preservePaths | Where-Object { $relative -eq $_ -or $relative.StartsWith($_ + "/", [StringComparison]::OrdinalIgnoreCase) }).Count -gt 0
        if ($isManaged -and -not $isPreserved -and $relative -ne "release-files-v1.json") {
            [ordered]@{
                path = $relative
                size_bytes = $_.Length
                sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            }
        }
    } | Sort-Object path
)
$packageManifest = [ordered]@{
    schema = 1
    version = $releaseIdentity.version
    tag = $releaseIdentity.tag
    managed_items = $managedItems
    preserve_paths = $preservePaths
    files = $releaseFiles
} | ConvertTo-Json -Depth 6
[System.IO.File]::WriteAllText((Join-Path $releasePackagePath "release-files-v1.json"), $packageManifest, $utf8NoBom)

# 发布前硬性检查用户数据隔离。以后即使复制规则被修改，也不能静默把开发机
# 的模型、训练记录、虚拟环境或缓存带入公开包。
$registryCheckPath = Join-Path $releasePackagePath "data\models\installed-models.json"
$releaseDataFiles = @(Get-ChildItem -LiteralPath (Join-Path $releasePackagePath "data") -Recurse -File -Force)
if ($releaseDataFiles.Count -ne 1 -or $releaseDataFiles[0].FullName -ne $registryCheckPath) {
    throw "发布包 data 目录包含意外文件：$($releaseDataFiles.FullName -join ', ')"
}
$registryCheck = Get-Content -LiteralPath $registryCheckPath -Raw -Encoding UTF8 | ConvertFrom-Json
if (@($registryCheck.models).Count -ne 0) { throw "发布包模型注册表不是空的。" }
$configCheck = Get-Content -LiteralPath (Join-Path $releasePackagePath "config\voices.local.json") -Raw -Encoding UTF8 | ConvertFrom-Json
if (@($configCheck.voices).Count -ne 0) { throw "发布包语音配置不是空的。" }
foreach ($relativePath in @(".venv", ".runtime", ".cache", "logs", "output", "data\training", "assets\avatars")) {
    if (Test-Path -LiteralPath (Join-Path $releasePackagePath $relativePath)) {
        throw "发布包包含禁止的本地数据目录：$relativePath"
    }
}
$modelArtifacts = @(Get-ChildItem -LiteralPath $releasePackagePath -Recurse -File -Force | Where-Object {
    $_.Extension.ToLowerInvariant() -in @(".ckpt", ".pth", ".pt", ".onnx", ".safetensors", ".wav", ".mp3", ".flac")
})
if ($modelArtifacts.Count -gt 0) {
    throw "发布包包含模型或音频文件：$($modelArtifacts.FullName -join ', ')"
}

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
$manifestPath = Join-Path $releaseOutputPath "release-manifest-v2-$Version.json"
$manifestJson = [PSCustomObject]@{ schema=2; version=$Version; archive_name=(Split-Path -Leaf $releaseArchivePath); sha256=$hash; size_bytes=$size } | ConvertTo-Json
[System.IO.File]::WriteAllText($manifestPath, $manifestJson, (New-Object System.Text.UTF8Encoding($false)))
Write-Host ("Release ZIP complete: {0} MB, SHA256: {1}" -f [Math]::Round($size / 1MB, 1), $hash) -ForegroundColor Green
