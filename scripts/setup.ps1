param(
    [ValidateSet("CPU", "GPU")]
    [string]$Mode = ""
)

$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $PSScriptRoot
Set-Location $projectDir
$venvDir = Join-Path $projectDir ".venv"
$pythonExe = Join-Path $venvDir "Scripts\python.exe"
$configPath = Join-Path $projectDir "config\voices.local.json"
$examplePath = Join-Path $projectDir "config\voices.example.json"
$cacheDir = Join-Path $projectDir ".cache"
$pipCacheDir = Join-Path $cacheDir "pip"
$setupStatePath = Join-Path $cacheDir "setup-state.json"

function Write-Step([string]$message) { Write-Host ("`n[" + $message + "]") -ForegroundColor Cyan }

function Get-BasePython {
    if (-not [string]::IsNullOrWhiteSpace($env:OWVOICE_PYTHON)) {
        if (Test-Path -LiteralPath $env:OWVOICE_PYTHON -PathType Leaf) {
            $script:basePythonArgs = @()
            return [System.IO.Path]::GetFullPath($env:OWVOICE_PYTHON)
        }
        throw "OWVOICE_PYTHON 指向的文件不存在。"
    }

    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($null -ne $py) {
        & $py.Source -3.10 -c "import struct, sys; raise SystemExit(0 if sys.version_info[:2] == (3,10) and struct.calcsize('P') * 8 == 64 else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) {
            $script:basePythonArgs = @("-3.10")
            return $py.Source
        }
    }

    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($null -ne $python) {
        & $python.Source -c "import struct, sys; raise SystemExit(0 if sys.version_info[:2] == (3,10) and struct.calcsize('P') * 8 == 64 else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) {
            $script:basePythonArgs = @()
            return $python.Source
        }
    }

    throw "需要 64 位 Python 3.10。请先安装 Python 3.10 x64，再重新运行 setup.ps1。"
}

function Get-PythonInfo([string]$path) {
    return (& $path -c "import platform, struct, sys; print('%d.%d|%d|%s' % (sys.version_info[0], sys.version_info[1], struct.calcsize('P') * 8, platform.python_implementation()))").Trim()
}

function Test-DependencyState([string]$requirementsHash) {
    if (-not (Test-Path -LiteralPath $setupStatePath -PathType Leaf)) { return $false }
    try {
        $state = Get-Content -LiteralPath $setupStatePath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($state.dependenciesReady -ne $true -or $state.mode -ne $installMode -or $state.requirementsSha256 -ne $requirementsHash) {
            return $false
        }
        & $pythonExe -m pip check | Out-Host
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

function Save-DependencyState([string]$requirementsHash) {
    New-Item -ItemType Directory -Force -Path $cacheDir | Out-Null
    $state = [ordered]@{
        schema = 1
        dependenciesReady = $true
        mode = $installMode
        requirementsFile = $requirementsName
        requirementsSha256 = $requirementsHash
        python = Get-PythonInfo $pythonExe
        updatedAt = [DateTime]::UtcNow.ToString("o")
    } | ConvertTo-Json
    [System.IO.File]::WriteAllText($setupStatePath, $state, (New-Object System.Text.UTF8Encoding($false)))
}

function Select-InstallMode {
    if (-not [string]::IsNullOrWhiteSpace($Mode)) { return $Mode }
    Write-Host "`n请选择推理模式：" -ForegroundColor Yellow
    Write-Host "[1] CPU：下载和配置较少，不需要 NVIDIA 显卡；但推理较慢。"
    Write-Host "[2] GPU：需要 NVIDIA 显卡和兼容的 CUDA 驱动/运行组件；下载和配置较多，但推理效率高。"
    do { $choice = (Read-Host "请输入 1 或 2").Trim() } while ($choice -notin @("1", "2"))
    if ($choice -eq "1") { return "CPU" }
    return "GPU"
}

$installMode = Select-InstallMode
$requirementsName = if ($installMode -eq "GPU") { "requirements-gpu.txt" } else { "requirements-cpu.txt" }
$requirementsPath = Join-Path $projectDir $requirementsName

Write-Host "OwVoice 首次配置" -ForegroundColor Green
Write-Host ("项目目录: " + $projectDir)
Write-Host ("安装模式: " + $installMode)

if ($installMode -eq "GPU" -and $null -eq (Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue)) {
    throw "未检测到 nvidia-smi.exe。GPU 模式需要 NVIDIA 显卡及正常安装的 NVIDIA 驱动；可重新运行并选择 CPU。"
}
if (-not (Test-Path -LiteralPath $requirementsPath -PathType Leaf)) { throw "$requirementsName 不存在。" }

Write-Step "准备 Python 3.10 x64 虚拟环境"
$reuseVenv = $false
if (Test-Path -LiteralPath $pythonExe -PathType Leaf) {
    $venvInfo = Get-PythonInfo $pythonExe
    $reuseVenv = ($venvInfo -like "3.10|64|*")
    if (-not $reuseVenv) { throw ".venv 中的 Python 不是 3.10 x64。请先备份需要的内容，再删除 $venvDir 后重试。" }
}
if (-not $reuseVenv) {
    $basePython = Get-BasePython
    $baseArgs = @($script:basePythonArgs)
    & $basePython @baseArgs -m venv $venvDir
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $pythonExe)) { throw "无法创建 Python 虚拟环境。" }
}

$requirementsHash = (Get-FileHash -LiteralPath $requirementsPath -Algorithm SHA256).Hash.ToLowerInvariant()
if (Test-DependencyState $requirementsHash) {
    Write-Step "复用已安装的 Python 依赖"
    Write-Host "依赖清单未变化且 pip check 通过，跳过重复安装。" -ForegroundColor Green
} else {
    Write-Step "安装固定版本依赖"
    New-Item -ItemType Directory -Force -Path $pipCacheDir | Out-Null
    & $pythonExe -m pip install --cache-dir $pipCacheDir --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "pip 升级失败。" }
    & $pythonExe -m pip install --cache-dir $pipCacheDir --prefer-binary -r $requirementsPath
    if ($LASTEXITCODE -ne 0) { throw "$requirementsName 安装失败。" }
    & $pythonExe -m pip check
    if ($LASTEXITCODE -ne 0) { throw "$requirementsName 已安装，但依赖完整性检查失败。" }
    Save-DependencyState $requirementsHash
}

Write-Step "安装 NLTK 数据"
& $pythonExe (Join-Path $projectDir "scripts\download_nltk_data.py")
if ($LASTEXITCODE -ne 0) { throw "NLTK 数据安装失败。" }

Write-Step "下载 GPT-SoVITS 必需资源"
& $pythonExe (Join-Path $projectDir "scripts\download_pretrained.py")
if ($LASTEXITCODE -ne 0) { throw "GPT-SoVITS 预训练资源下载失败。" }

Write-Step "检查语音配置"
foreach ($directory in @("output", ".cache\synthesis", "logs")) { New-Item -ItemType Directory -Force -Path (Join-Path $projectDir $directory) | Out-Null }
if (-not (Test-Path -LiteralPath $configPath)) { Copy-Item -LiteralPath $examplePath -Destination $configPath }
try { $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json }
catch { throw "voices.local.json 不是有效 JSON：$($_.Exception.Message)" }
$voices = @($config.voices | Where-Object { $_.enabled -ne $false })
if ($voices.Count -lt 1) {
    Write-Host "没有配置本地语音模型，继续完成环境配置。" -ForegroundColor Yellow
} else {
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
    if ($missing.Count -gt 0) { $missing | ForEach-Object { Write-Host $_ -ForegroundColor Yellow }; throw "模型或参考音频文件缺失。" }
}

Write-Step "检查 PyTorch"
$torchCheck = "import torch; print('PyTorch', torch.__version__, 'CUDA', torch.version.cuda, '可用', torch.cuda.is_available()); raise SystemExit(0 if '$installMode' != 'GPU' or torch.cuda.is_available() else 2)"
& $pythonExe -c $torchCheck
if ($LASTEXITCODE -ne 0) { throw "GPU 模式下 PyTorch 未检测到可用 CUDA。请检查 NVIDIA 驱动，或重新选择 CPU 模式。" }

Write-Host "`n配置完成。" -ForegroundColor Green
$releaseExe = Join-Path $projectDir "OwVoice.exe"
$sourceRunScript = Join-Path $projectDir "scripts\run.ps1"
if ((Test-Path -LiteralPath $releaseExe -PathType Leaf) -and -not (Test-Path -LiteralPath $sourceRunScript -PathType Leaf)) {
    Write-Host "普通用户请双击 OwVoice.exe 启动。" -ForegroundColor Green
} elseif (Test-Path -LiteralPath $sourceRunScript -PathType Leaf) {
    Write-Host "源码运行请执行：.\scripts\run.ps1" -ForegroundColor Green
} else {
    Write-Host "请运行 OwVoice.exe 启动程序。" -ForegroundColor Green
}
