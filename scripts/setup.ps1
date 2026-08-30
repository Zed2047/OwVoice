$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $PSScriptRoot
Set-Location $projectDir
$venvDir = Join-Path $projectDir ".venv"
$pythonExe = Join-Path $venvDir "Scripts\python.exe"
$configPath = Join-Path $projectDir "config\voices.local.json"
$examplePath = Join-Path $projectDir "config\voices.example.json"

function Write-Step([string]$message) { Write-Host ("`n[" + $message + "]") -ForegroundColor Cyan }

function Get-BasePython {
    if (-not [string]::IsNullOrWhiteSpace($env:OWVOICE_PYTHON)) {
        if (Test-Path -LiteralPath $env:OWVOICE_PYTHON -PathType Leaf) {
            $script:basePythonArgs = @()
            return [System.IO.Path]::GetFullPath($env:OWVOICE_PYTHON)
        }
        throw "OWVOICE_PYTHON does not point to a Python executable."
    }
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($null -ne $py) {
        foreach ($version in @("-3.10", "-3.9")) {
            & $py.Source $version -c "import sys; raise SystemExit(0 if sys.version_info[:2] in ((3,10),(3,9)) else 1)" 2>$null
            if ($LASTEXITCODE -eq 0) {
                $script:basePythonArgs = @($version)
                return $py.Source
            }
        }
    }
    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($null -ne $python) {
        & $python.Source -c "import sys; raise SystemExit(0 if sys.version_info[:2] in ((3,10),(3,9)) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) {
            $script:basePythonArgs = @()
            return $python.Source
        }
    }
    throw "Python 3.9 or 3.10 is required. Install it from python.org, then run this file again."
}

function Get-PythonVersion([string]$path) {
    return (& $path -c "import sys; print('%d.%d' % sys.version_info[:2])").Trim()
}

Write-Host "OwVoice first-time setup" -ForegroundColor Green
Write-Host ("Project: " + $projectDir)

Write-Step "Prepare Python 3.9/3.10 virtual environment"
$reuseVenv = $false
if (Test-Path -LiteralPath $pythonExe -PathType Leaf) {
    $reuseVenv = ((Get-PythonVersion $pythonExe) -in @("3.9", "3.10"))
}
if (-not $reuseVenv) {
    if (Test-Path -LiteralPath $venvDir) { Remove-Item -LiteralPath $venvDir -Recurse -Force }
    $basePython = Get-BasePython
    $baseArgs = @($script:basePythonArgs)
    & $basePython @baseArgs -m venv $venvDir
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $pythonExe)) { throw "Could not create the Python virtual environment." }
}

Write-Step "Install OwVoice and GPT-SoVITS dependencies with pip"
& $pythonExe -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed." }
$gsvRequirements = Join-Path $projectDir "GPT-SoVITS\requirements.txt"
if (-not (Test-Path -LiteralPath $gsvRequirements -PathType Leaf)) { throw "GPT-SoVITS source or requirements.txt is missing." }
& $pythonExe -m pip install -r (Join-Path $projectDir "requirements.txt") -r $gsvRequirements
if ($LASTEXITCODE -ne 0) { throw "OwVoice or GPT-SoVITS dependency installation failed." }

Write-Step "Install required NLTK data into the project environment"
& $pythonExe (Join-Path $projectDir "scripts\download_nltk_data.py")
if ($LASTEXITCODE -ne 0) { throw "NLTK data installation failed." }

Write-Step "Download required GPT-SoVITS pretrained assets"
& $pythonExe (Join-Path $projectDir "scripts\download_pretrained.py")
if ($LASTEXITCODE -ne 0) { throw "GPT-SoVITS pretrained asset download failed." }

Write-Step "Check voice configuration"
foreach ($directory in @("output", ".cache\synthesis", "logs")) { New-Item -ItemType Directory -Force -Path (Join-Path $projectDir $directory) | Out-Null }
if (-not (Test-Path -LiteralPath $configPath)) { Copy-Item -LiteralPath $examplePath -Destination $configPath }
try { $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json }
catch { throw "voices.local.json is invalid JSON: $($_.Exception.Message)" }
$voices = @($config.voices | Where-Object { $_.enabled -ne $false })
if ($voices.Count -ne 3) { throw "The first release must contain exactly three enabled voices." }
function Resolve-ProjectPath([string]$value) {
    if ([System.IO.Path]::IsPathRooted($value)) { return [System.IO.Path]::GetFullPath($value) }
    return [System.IO.Path]::GetFullPath((Join-Path $projectDir $value))
}
$missing = @()
foreach ($voice in $voices) {
    foreach ($field in @("gpt_model", "sovits_model", "reference_audio")) {
        $path = Resolve-ProjectPath ([string]$voice.$field)
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { $missing += "$($voice.id): $field -> $path" }
    }
}
if ($missing.Count -gt 0) { $missing | ForEach-Object { Write-Host $_ -ForegroundColor Yellow }; throw "A model or reference audio file is missing." }

Write-Step "Check GPT-SoVITS"
$gsvApi = Join-Path $projectDir "GPT-SoVITS\api.py"
if (-not (Test-Path -LiteralPath $gsvApi -PathType Leaf)) { throw "GPT-SoVITS api.py is missing." }
& $pythonExe -c "import torch; print('PyTorch', torch.__version__, 'CUDA', torch.version.cuda)"
if ($LASTEXITCODE -ne 0) { throw "PyTorch could not be imported. Check the NVIDIA/PyTorch installation." }
Write-Host "Setup complete. Run OwVoice.exe." -ForegroundColor Green
