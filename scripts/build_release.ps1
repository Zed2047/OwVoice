param(
    [string]$Version = "v0.1.0",
    [string]$OutputDirectory = "dist",
    [string]$RuntimeDirectory = "dist\runtime",
    [switch]$SkipArchive
)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
$releaseOutputPath = ".\$OutputDirectory"
$releaseStagingPath = ".\$OutputDirectory\release-staging"
$releaseName = "OwVoice-$Version"
$releasePackagePath = ".\$OutputDirectory\release-staging\$releaseName"
$releaseArchivePath = ".\$OutputDirectory\$releaseName.zip"

New-Item -ItemType Directory -Force -Path $releaseOutputPath | Out-Null
if (Test-Path -LiteralPath $releaseStagingPath) { Remove-Item -LiteralPath $releaseStagingPath -Recurse -Force }
if (Test-Path -LiteralPath $releaseArchivePath) { Remove-Item -LiteralPath $releaseArchivePath -Force }
New-Item -ItemType Directory -Force -Path $releasePackagePath | Out-Null

function Copy-EngineTree([string]$sourceDirectory, [string]$targetDirectory) {
    New-Item -ItemType Directory -Force -Path $targetDirectory | Out-Null
    foreach ($item in (Get-ChildItem -LiteralPath $sourceDirectory -Force)) {
        $skipDirectory = $item.PSIsContainer -and ($item.Name -in @("pretrained_models", "__pycache__", ".git", "G2PWModel", "ja_userdic"))
        $skipFile = (-not $item.PSIsContainer) -and ($item.Extension -in @(".bak", ".tmp", ".pyc", ".zip", ".pth", ".pt"))
        if ($skipDirectory -or $skipFile) { continue }
        $targetItem = Join-Path $targetDirectory $item.Name
        if ($item.PSIsContainer) {
            Copy-EngineTree $item.FullName $targetItem
        } else {
            Copy-Item -LiteralPath $item.FullName -Destination $targetItem -Force
        }
    }
}

foreach ($file in @("README.md", "LICENSE", "THIRD_PARTY_NOTICES.md", "requirements.txt", "requirements-runtime.txt")) {
    Copy-Item -LiteralPath ".\$file" -Destination $releasePackagePath
}
foreach ($directory in @("backend", "frontend", "assets", "config")) {
    $sourceDirectory = ".\$directory"
    $targetDirectory = ".\$OutputDirectory\release-staging\$releaseName\$directory"
    New-Item -ItemType Directory -Force -Path $targetDirectory | Out-Null
    Get-ChildItem -LiteralPath $sourceDirectory -Force | Copy-Item -Destination $targetDirectory -Recurse
}

# Installed launcher; build, setup, and diagnostic scripts stay in the source repository.
$releaseScriptsTargetPath = ".\$OutputDirectory\release-staging\$releaseName\scripts"
New-Item -ItemType Directory -Force -Path $releaseScriptsTargetPath | Out-Null
Copy-Item -LiteralPath ".\scripts\launch_frontend.py" -Destination $releaseScriptsTargetPath -Force

# Copy the modified GPT-SoVITS inference source, excluding its large model store.
$engineSource = ".\GPT-SoVITS"
$engineTargetPath = ".\$OutputDirectory\release-staging\$releaseName\GPT-SoVITS"
New-Item -ItemType Directory -Force -Path ".\$OutputDirectory\release-staging\$releaseName\GPT-SoVITS" | Out-Null
foreach ($file in @("api.py", "config.py", "extra-req.txt", "requirements.txt", "LICENSE")) {
    Copy-Item -LiteralPath (Join-Path $engineSource $file) -Destination ".\$OutputDirectory\release-staging\$releaseName\GPT-SoVITS" -Force
}
Copy-EngineTree (Join-Path $engineSource "GPT_SoVITS") ".\$OutputDirectory\release-staging\$releaseName\GPT-SoVITS\GPT_SoVITS"
$pretrainedSource = Join-Path $engineSource "GPT_SoVITS\pretrained_models"
$pretrainedTarget = ".\$OutputDirectory\release-staging\$releaseName\GPT-SoVITS\GPT_SoVITS\pretrained_models"
$requiredPretrainedFiles = @(
    "chinese-hubert-base\config.json",
    "chinese-hubert-base\preprocessor_config.json",
    "chinese-hubert-base\pytorch_model.bin",
    "chinese-roberta-wwm-ext-large\config.json",
    "chinese-roberta-wwm-ext-large\pytorch_model.bin",
    "chinese-roberta-wwm-ext-large\tokenizer.json"
)
foreach ($relativePath in $requiredPretrainedFiles) {
    $requiredPath = Join-Path $pretrainedSource $relativePath
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) { throw "Missing pretrained release asset: $requiredPath" }
}
foreach ($relativePath in @(
    "chinese-hubert-base",
    "chinese-roberta-wwm-ext-large"
)) {
    $sourcePath = Join-Path $pretrainedSource $relativePath
    if (-not (Test-Path -LiteralPath $sourcePath)) { throw "Missing pretrained release asset: $sourcePath" }
    $targetPath = Join-Path $pretrainedTarget $relativePath
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $targetPath) | Out-Null
    Copy-Item -LiteralPath $sourcePath -Destination $targetPath -Recurse -Force
}
$g2pwSource = Join-Path $engineSource "GPT_SoVITS\text\G2PWModel"
$g2pwTarget = ".\$OutputDirectory\release-staging\$releaseName\GPT-SoVITS\GPT_SoVITS\text\G2PWModel"
if (-not (Test-Path -LiteralPath (Join-Path $g2pwSource "g2pW.onnx") -PathType Leaf)) { throw "Missing G2PW release assets: $g2pwSource" }
Copy-Item -LiteralPath $g2pwSource -Destination $g2pwTarget -Recurse -Force
$toolsTargetPath = ".\$OutputDirectory\release-staging\$releaseName\GPT-SoVITS\tools"
New-Item -ItemType Directory -Force -Path ".\$OutputDirectory\release-staging\$releaseName\GPT-SoVITS\tools" | Out-Null
foreach ($file in @("__init__.py", "audio_sr.py", "assets.py", "my_utils.py")) {
    Copy-Item -LiteralPath (Join-Path $engineSource "tools\$file") -Destination ".\$OutputDirectory\release-staging\$releaseName\GPT-SoVITS\tools" -Force
}
Copy-EngineTree (Join-Path $engineSource "tools\i18n") ".\$OutputDirectory\release-staging\$releaseName\GPT-SoVITS\tools\i18n"
Copy-EngineTree (Join-Path $engineSource "tools\AP_BWE_main") ".\$OutputDirectory\release-staging\$releaseName\GPT-SoVITS\tools\AP_BWE_main"

$runtimeSource = [System.IO.Path]::GetFullPath((Join-Path (Get-Location).Path $RuntimeDirectory))
if (-not (Test-Path -LiteralPath (Join-Path $runtimeSource "pythonw.exe") -PathType Leaf)) {
    throw "Portable runtime is missing. Run scripts\build_runtime.ps1 first."
}
Copy-Item -LiteralPath $runtimeSource -Destination ".\$OutputDirectory\release-staging\$releaseName\runtime" -Recurse -Force

$releaseRoot = [System.IO.Path]::GetFullPath(".\$OutputDirectory\release-staging\$releaseName")
$runtimeTarget = Join-Path $releaseRoot "runtime"
Get-ChildItem -LiteralPath $releaseRoot -Recurse -File |
    Where-Object { -not $_.FullName.StartsWith($runtimeTarget, [System.StringComparison]::OrdinalIgnoreCase) -and $_.Extension -in @('.bak', '.tmp', '.pyc') } |
    Remove-Item -Force
Get-ChildItem -LiteralPath $releaseRoot -Recurse -Directory |
    Where-Object { -not $_.FullName.StartsWith($runtimeTarget, [System.StringComparison]::OrdinalIgnoreCase) -and $_.Name -in @('__pycache__', '.pytest_cache') } |
    Remove-Item -Recurse -Force
Remove-Item -LiteralPath ".\$OutputDirectory\release-staging\$releaseName\config\voices.local.json" -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path ".\$OutputDirectory\release-staging\$releaseName\config" | Out-Null
Copy-Item -LiteralPath ".\config\voices.example.json" -Destination ".\$OutputDirectory\release-staging\$releaseName\config" -Force
Copy-Item -LiteralPath ".\config\voices.example.json" -Destination ".\$OutputDirectory\release-staging\$releaseName\config\voices.local.json" -Force

$voiceFiles = @(
    @{ dir = "monk"; files = @("monk-gpt-expanded-v3-e10.ckpt", "monk-sovits-emotion-focus-e9.pth", "reference.wav") },
    @{ dir = "ana"; files = @("ana-gpt-curated-v2-e8.ckpt", "ana-sovits-emotion-focus-e7.pth", "reference.wav") },
    @{ dir = "doomfist"; files = @("doomfist-gpt-expanded-v3-e10.ckpt", "doomfist-sovits-curated-v4-e7.pth", "reference.wav") }
)
foreach ($voice in $voiceFiles) {
    $targetPath = ".\$OutputDirectory\release-staging\$releaseName\models\$($voice.dir)"
    New-Item -ItemType Directory -Force -Path ".\$OutputDirectory\release-staging\$releaseName\models\$($voice.dir)" | Out-Null
    foreach ($file in $voice.files) {
        $sourceFile = Join-Path (Get-Location).Path (Join-Path (Join-Path "models" $voice.dir) $file)
        if (-not (Test-Path -LiteralPath $sourceFile -PathType Leaf)) { throw "Missing release file: $sourceFile" }
        Copy-Item -LiteralPath $sourceFile -Destination ".\$OutputDirectory\release-staging\$releaseName\models\$($voice.dir)"
    }
}

if ($SkipArchive) {
    Write-Host "Release staging complete: $releasePackagePath" -ForegroundColor Green
    return
}

$tar = Get-Command tar.exe -ErrorAction SilentlyContinue
if ($null -ne $tar) {
    & $tar.Source -a -cf $releaseArchivePath -C $releaseStagingPath $releaseName
    if ($LASTEXITCODE -ne 0) { throw "Release ZIP creation failed." }
} else {
    Compress-Archive -LiteralPath ".\$OutputDirectory\release-staging\$releaseName" -DestinationPath $releaseArchivePath -CompressionLevel Optimal
}
$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $releaseArchivePath).Hash.ToLowerInvariant()
$size = (Get-Item -LiteralPath $releaseArchivePath).Length
[PSCustomObject]@{ version=$Version; archive_name=(Split-Path -Leaf $releaseArchivePath); sha256=$hash; size_bytes=$size } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $releaseOutputPath "release-manifest-$Version.json") -Encoding UTF8
Write-Host ("Release ZIP complete: {0} MB, SHA256: {1}" -f [Math]::Round($size / 1MB, 1), $hash) -ForegroundColor Green
