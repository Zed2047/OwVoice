param(
    [string]$Version = "v0.1.1",
    [switch]$SkipPayloadBuild
)

$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent $PSScriptRoot
Set-Location $projectDir

$appVersion = $Version.TrimStart("v")
$releaseVersion = "v$appVersion"

if (-not $SkipPayloadBuild) {
    & (Join-Path $PSScriptRoot "build_runtime.ps1")
    if ($LASTEXITCODE -ne 0) { throw "Portable runtime build failed." }

    & (Join-Path $PSScriptRoot "build_release.ps1") -Version $releaseVersion -SkipArchive
    if ($LASTEXITCODE -ne 0) { throw "Installer payload build failed." }
}

$payloadDir = [System.IO.Path]::GetFullPath((Join-Path $projectDir "dist\release-staging\OwVoice-$releaseVersion"))
$installerOutputDir = [System.IO.Path]::GetFullPath((Join-Path $projectDir "dist\installer"))
New-Item -ItemType Directory -Force -Path $installerOutputDir | Out-Null

$isccCandidates = @(
    (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
    (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 7\ISCC.exe"),
    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    "C:\Program Files\Inno Setup 7\ISCC.exe"
)
$iscc = $isccCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
if ([string]::IsNullOrWhiteSpace($iscc)) {
    throw "Inno Setup compiler (ISCC.exe) was not found."
}

$issFile = Join-Path $projectDir "installer\OwVoice.iss"
$installerPath = Join-Path $installerOutputDir "OwVoice-Setup-v$appVersion-Universal.exe"
$tempRoot = [System.IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA "OwVoice\installer-build"))
$tempOutputDir = [System.IO.Path]::GetFullPath((Join-Path $tempRoot ([Guid]::NewGuid().ToString("N"))))
if (-not $tempOutputDir.StartsWith($tempRoot + [System.IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Unsafe temporary installer path: $tempOutputDir"
}
$tempBaseName = "OwVoice-$([Guid]::NewGuid().ToString('N'))"
$tempInstallerPath = Join-Path $tempOutputDir "$tempBaseName.exe"
$publishPath = Join-Path $installerOutputDir ".$tempBaseName.partial"
$published = $false

New-Item -ItemType Directory -Force -Path $tempOutputDir | Out-Null
[System.IO.File]::SetAttributes(
    $tempOutputDir,
    [System.IO.File]::GetAttributes($tempOutputDir) -bor [System.IO.FileAttributes]::NotContentIndexed
)

try {
    & $iscc "/Qp" "/O$tempOutputDir" "/F$tempBaseName" "/DAppVersion=$appVersion" "/DBuildFlavor=Universal" "/DPayloadDir=$payloadDir" "/DInstallerOutputDir=$tempOutputDir" $issFile
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup compilation failed." }
    if (-not (Test-Path -LiteralPath $tempInstallerPath -PathType Leaf)) {
        throw "Installer was not generated: $tempInstallerPath"
    }

    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $tempInstallerPath).Hash.ToLowerInvariant()
    $size = (Get-Item -LiteralPath $tempInstallerPath).Length
    Copy-Item -LiteralPath $tempInstallerPath -Destination $publishPath
    $copiedHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $publishPath).Hash.ToLowerInvariant()
    if ($copiedHash -ne $hash) { throw "Copied installer hash mismatch." }

    for ($attempt = 1; $attempt -le 5; $attempt++) {
        try {
            [System.IO.File]::Move($publishPath, $installerPath, $true)
            break
        } catch {
            if ($attempt -eq 5) { throw }
            Start-Sleep -Seconds 1
        }
    }
    $finalHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $installerPath).Hash.ToLowerInvariant()
    if ($finalHash -ne $hash) { throw "Published installer hash mismatch." }
    $published = $true
} finally {
    if ($published) {
        Remove-Item -LiteralPath $tempOutputDir -Recurse -Force -ErrorAction SilentlyContinue
    } elseif (Test-Path -LiteralPath $tempInstallerPath -PathType Leaf) {
        Write-Warning "Installer build output preserved for retry: $tempInstallerPath"
    } else {
        Remove-Item -LiteralPath $tempOutputDir -Recurse -Force -ErrorAction SilentlyContinue
        Write-Warning "Inno Setup removed its failed temporary output; compilation must be retried."
    }
}

[PSCustomObject]@{
    version = $releaseVersion
    installer_name = Split-Path -Leaf $installerPath
    sha256 = $hash
    size_bytes = $size
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $installerOutputDir "installer-manifest-$releaseVersion.json") -Encoding UTF8

Write-Host ("Installer complete: {0} GB, SHA256: {1}" -f [Math]::Round($size / 1GB, 2), $hash) -ForegroundColor Green
