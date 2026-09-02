param(
    [string]$OutputDirectory = "dist\runtime",
    [string]$PythonVersion = "3.10.11"
)

$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent $PSScriptRoot
Set-Location $projectDir

$buildPython = Join-Path $projectDir ".venv\Scripts\python.exe"
$nltkSource = Join-Path $projectDir ".venv\nltk_data"
if (-not (Test-Path -LiteralPath $buildPython -PathType Leaf)) {
    throw "Build environment not found: $buildPython"
}
if (-not (Test-Path -LiteralPath $nltkSource -PathType Container)) {
    throw "NLTK data not found: $nltkSource"
}
$buildPythonInfo = (& $buildPython -c "import struct, sys; print('%d.%d|%d' % (sys.version_info[0], sys.version_info[1], struct.calcsize('P') * 8))").Trim()
if ($buildPythonInfo -ne "3.10|64") {
    throw "Portable runtime builds require Python 3.10 x64; found $buildPythonInfo"
}

$outputPath = [System.IO.Path]::GetFullPath((Join-Path $projectDir $OutputDirectory))
$distRoot = [System.IO.Path]::GetFullPath((Join-Path $projectDir "dist"))
if (-not $outputPath.StartsWith($distRoot + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Runtime output must stay under $distRoot"
}

$cacheDir = Join-Path $projectDir ".cache\build"
$archive = Join-Path $cacheDir "python-$PythonVersion-embed-amd64.zip"
$archiveUrl = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"
$expectedSha256 = "608619f8619075629c9c69f361352a0da6ed7e62f83a0e19c63e0ea32eb7629d"

New-Item -ItemType Directory -Force -Path $cacheDir | Out-Null
if (-not (Test-Path -LiteralPath $archive -PathType Leaf)) {
    Invoke-WebRequest -Uri $archiveUrl -OutFile $archive
}
$actualSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $archive).Hash.ToLowerInvariant()
if ($actualSha256 -ne $expectedSha256) {
    throw "Embedded Python archive SHA256 mismatch: $actualSha256"
}

if (Test-Path -LiteralPath $outputPath) {
    Remove-Item -LiteralPath $outputPath -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $outputPath | Out-Null
Expand-Archive -LiteralPath $archive -DestinationPath $outputPath -Force
Copy-Item -LiteralPath (Join-Path $projectDir "scripts\runtime_sitecustomize.py") `
    -Destination (Join-Path $outputPath "sitecustomize.py") -Force

$sitePackages = Join-Path $outputPath "Lib\site-packages"
New-Item -ItemType Directory -Force -Path $sitePackages | Out-Null
$env:PYTHONNOUSERSITE = "1"
$env:PIP_DISABLE_PIP_VERSION_CHECK = "1"
& $buildPython -m pip install --no-compile --upgrade --target $sitePackages `
    -r (Join-Path $projectDir "requirements-runtime.txt")
if ($LASTEXITCODE -ne 0) {
    throw "Portable runtime dependency installation failed."
}

$pthFile = Get-ChildItem -LiteralPath $outputPath -Filter "python*._pth" -File | Select-Object -First 1
if ($null -eq $pthFile) {
    throw "Embedded Python ._pth file was not found."
}
@(
    "python310.zip"
    "."
    "Lib"
    "Lib\site-packages"
    ".."
    "import site"
) | Set-Content -LiteralPath $pthFile.FullName -Encoding ASCII

Copy-Item -LiteralPath $nltkSource -Destination (Join-Path $outputPath "nltk_data") -Recurse -Force

# Wheels contain build-time files and optional Qt modules that OwVoice never imports.
Get-ChildItem -LiteralPath $sitePackages -Recurse -File -Filter "*.lib" | Remove-Item -Force
foreach ($unusedPath in @(
    (Join-Path $sitePackages "bin")
    (Join-Path $sitePackages "torch\include")
    (Join-Path $sitePackages "PySide6\doc")
    (Join-Path $sitePackages "PySide6\glue")
    (Join-Path $sitePackages "PySide6\include")
    (Join-Path $sitePackages "PySide6\metatypes")
    (Join-Path $sitePackages "PySide6\qml")
    (Join-Path $sitePackages "PySide6\resources")
    (Join-Path $sitePackages "PySide6\translations")
    (Join-Path $sitePackages "PySide6\typesystems")
)) {
    if (Test-Path -LiteralPath $unusedPath) {
        Remove-Item -LiteralPath $unusedPath -Recurse -Force
    }
}
$pysidePath = Join-Path $sitePackages "PySide6"
Get-ChildItem -LiteralPath $pysidePath -File |
    Where-Object { $_.Name -like "Qt*WebEngine*" -or $_.Name -eq "QtWebEngineProcess.exe" } |
    Remove-Item -Force

$cacheDirectories = @(Get-ChildItem -LiteralPath $outputPath -Recurse -Directory -Force |
    Where-Object { $_.Name -in @("__pycache__", ".pytest_cache") } |
    Sort-Object FullName -Descending)
foreach ($cacheDirectory in $cacheDirectories) {
    if (Test-Path -LiteralPath $cacheDirectory.FullName) {
        Remove-Item -LiteralPath $cacheDirectory.FullName -Recurse -Force
    }
}
Get-ChildItem -LiteralPath $outputPath -Recurse -File -Force |
    Where-Object { $_.Extension -in @(".pyc", ".pyo") } |
    Remove-Item -Force

$runtimePython = Join-Path $outputPath "python.exe"
& $runtimePython -m compileall -q (Join-Path $sitePackages "jieba")
if ($LASTEXITCODE -ne 0) {
    throw "Jieba bytecode compilation failed."
}
$env:PYTHONDONTWRITEBYTECODE = "1"
& $runtimePython -c "import fastapi, librosa, numpy, onnxruntime, peft, PySide6, pytorch_lightning, requests, sitecustomize, torch, torchaudio, transformers, uvicorn; print('portable runtime imports OK')"
if ($LASTEXITCODE -ne 0) {
    throw "Portable runtime import check failed."
}
$env:QT_QPA_PLATFORM = "offscreen"
& $runtimePython -c "from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer; from PySide6.QtWidgets import QApplication, QWidget; app=QApplication([]); widget=QWidget(); player=QMediaPlayer(); audio=QAudioOutput(); player.setAudioOutput(audio); print('Qt widgets and multimedia OK')"
if ($LASTEXITCODE -ne 0) {
    throw "Qt runtime check failed."
}
Remove-Item Env:QT_QPA_PLATFORM -ErrorAction SilentlyContinue
$env:OWVOICE_VALIDATION_PROJECT_DIR = $projectDir
& $runtimePython -c "import os, sys; root=os.environ['OWVOICE_VALIDATION_PROJECT_DIR']; sys.path[:0]=[root + r'\GPT-SoVITS', root + r'\GPT-SoVITS\GPT_SoVITS']; from text.LangSegmenter import LangSegmenter; assert LangSegmenter.getTexts('你好 OpenAI'); print('language segmentation OK')"
if ($LASTEXITCODE -ne 0) {
    throw "Language segmentation check failed."
}
& $runtimePython -c "import os, sys; root=os.environ['OWVOICE_VALIDATION_PROJECT_DIR']; gsv=root + r'\GPT-SoVITS'; os.chdir(gsv); sys.path[:0]=[gsv, gsv + r'\GPT_SoVITS']; from text.cleaner import clean_text; phones, word2ph, text = clean_text('你好 OpenAI', 'zh', 'v2'); assert phones and word2ph; print('G2PW text cleaning OK')"
if ($LASTEXITCODE -ne 0) {
    throw "G2PW text cleaning check failed."
}
Remove-Item Env:OWVOICE_VALIDATION_PROJECT_DIR -ErrorAction SilentlyContinue
Remove-Item Env:PYTHONDONTWRITEBYTECODE -ErrorAction SilentlyContinue

$size = (Get-ChildItem -LiteralPath $outputPath -Recurse -File | Measure-Object -Property Length -Sum).Sum
Write-Host ("Portable runtime complete: {0} GB" -f [Math]::Round($size / 1GB, 2)) -ForegroundColor Green
