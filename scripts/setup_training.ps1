param()

$ErrorActionPreference = "Stop"
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$projectDir = Split-Path -Parent $PSScriptRoot
Set-Location $projectDir
$environmentModulePath = Join-Path $PSScriptRoot "environment\OwVoice.Environment.psm1"
if (-not (Test-Path -LiteralPath $environmentModulePath -PathType Leaf)) { throw "缺少公共环境核心：$environmentModulePath" }
Import-Module -Name $environmentModulePath -Force
$pythonExe = Join-Path $projectDir ".venv\Scripts\python.exe"
$uvExe = Join-Path $projectDir "tools\uv\uv.exe"
$cacheDir = Join-Path $projectDir ".cache"
$statePath = Join-Path $cacheDir "setup-state.json"

if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
    throw "找不到 .venv\Scripts\python.exe。请先运行 setup.bat 完成基础环境配置。"
}
if (-not (Test-Path -LiteralPath $uvExe -PathType Leaf)) { throw "缺少 tools\uv\uv.exe，请重新下载完整发布包。" }
$env:UV_CACHE_DIR = Join-Path $cacheDir "uv"
$env:UV_PYTHON_INSTALL_DIR = Join-Path $projectDir ".runtime\python"
$env:UV_PROJECT_ENVIRONMENT = Join-Path $projectDir ".venv"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:HF_HUB_DISABLE_PROGRESS_BARS = "1"
$env:HF_HUB_DISABLE_SYMLINKS_WARNING = "1"
$torchVersion = (& $pythonExe -c "import torch; print(torch.__version__)").Trim()
if ($LASTEXITCODE -ne 0) { throw "无法判断现有 CPU/GPU 环境，请先重新运行 setup.bat。" }
$mode = if ($torchVersion -match "\+cu") { "GPU" } else { "CPU" }
$plan = Get-OwVoiceEnvironmentPlan -ProjectRoot $projectDir -Mode $mode -WithTraining:$true
$requiredBytes = if ($mode -eq "GPU") { 24GB } else { 15GB }
$preflight = Test-OwVoiceEnvironmentPreflight -ProjectRoot $projectDir -Mode $mode -WithTraining:$true -RequiredFreeBytes $requiredBytes -UvPath $uvExe
if ($preflight.ok -ne $true) { throw [string]$preflight.message }
$script:EnvironmentTransactionId = ""
$script:EnvironmentSwitched = $false
$script:EnvironmentCandidateCreated = $false
trap {
    if ($script:EnvironmentSwitched -and -not [string]::IsNullOrWhiteSpace($script:EnvironmentTransactionId)) {
        $restore = Restore-OwVoiceEnvironment -ProjectRoot $projectDir -TransactionId $script:EnvironmentTransactionId
        if ($restore.ok -eq $true) { Write-Host "训练环境验证失败，旧环境已恢复。" -ForegroundColor Yellow }
    }
    if ($script:EnvironmentCandidateCreated -and (Test-Path -LiteralPath (Join-Path $projectDir ".venv.next"))) {
        Remove-Item -LiteralPath (Join-Path $projectDir ".venv.next") -Recurse -Force -ErrorAction SilentlyContinue
    }
    exit 1
}

if ($plan.action -eq "reuse") {
    Write-Host "现有环境已包含训练依赖，跳过环境重建。" -ForegroundColor Green
} else {
    $script:EnvironmentTransactionId = [Guid]::NewGuid().ToString()
    $candidate = Join-Path $projectDir ".venv.next"
    $candidateResult = New-OwVoiceCandidateEnvironment -ProjectRoot $projectDir -TransactionId $script:EnvironmentTransactionId -CandidatePath $candidate -Mode $mode -WithTraining:$true -UvPath $uvExe
    if ($candidateResult.ok -ne $true) { throw [string]$candidateResult.message }
    $script:EnvironmentCandidateCreated = $true
    $candidateCheck = Test-OwVoiceCandidateEnvironment -ProjectRoot $projectDir -TransactionId $script:EnvironmentTransactionId -CandidatePath $candidate -Mode $mode -WithTraining:$true
    if ($candidateCheck.ok -ne $true) { throw [string]$candidateCheck.message }
    $spec = Read-OwVoiceEnvironmentSpec -ProjectRoot $projectDir
    $candidateState = New-OwVoiceEnvironmentState -Spec $spec -ProjectRoot $projectDir -Mode $mode -WithTraining:$true -EnvironmentFingerprint ([string]$plan.environment_fingerprint) -Python $candidateCheck.state.python -Verified:$true -TransactionId $script:EnvironmentTransactionId
    $switchResult = Switch-OwVoiceEnvironment -ProjectRoot $projectDir -TransactionId $script:EnvironmentTransactionId -CandidatePath $candidate -State ([PSCustomObject]$candidateState)
    if ($switchResult.ok -ne $true) { throw [string]$switchResult.message }
    $script:EnvironmentSwitched = $true
    $pythonExe = Join-Path $projectDir ".venv\Scripts\python.exe"
}

Write-Host "正在下载本地训练所需预训练模型..." -ForegroundColor Cyan
$resourceProcessLogs = Join-Path $projectDir "logs\resource-processes"
$pretrainedExitCode = Invoke-OwVoiceTrackedProcess -FilePath $pythonExe -Arguments @((Join-Path $projectDir "scripts\download_pretrained.py"), "--training") -Activity "正在准备本地训练预训练模型" -LogRoot $resourceProcessLogs -WorkingDirectory $projectDir
if ($pretrainedExitCode -ne 0) { throw "本地训练预训练模型下载失败：$(Get-OwVoiceLastProcessFailureMessage)" }

Write-Host "正在验证本地训练环境..." -ForegroundColor Cyan
$verifyExitCode = Invoke-OwVoiceTrackedProcess -FilePath $pythonExe -Arguments @((Join-Path $projectDir "scripts\verify_runtime.py"), "--training") -Activity "正在验证本地训练环境" -LogRoot $resourceProcessLogs -WorkingDirectory $projectDir
if ($verifyExitCode -ne 0) { throw "本地训练环境验证未通过：$(Get-OwVoiceLastProcessFailureMessage)" }

$transactionId = if ($script:EnvironmentSwitched) { $script:EnvironmentTransactionId } else { [Guid]::NewGuid().ToString() }
$spec = Read-OwVoiceEnvironmentSpec -ProjectRoot $projectDir
$state = New-OwVoiceEnvironmentState -Spec $spec -ProjectRoot $projectDir -Mode $mode -WithTraining:$true -EnvironmentFingerprint ([string]$plan.environment_fingerprint) -Python (Get-OwVoicePythonInfo $pythonExe) -Verified:$true -TransactionId $transactionId
$complete = Complete-OwVoiceEnvironmentTransaction -ProjectRoot $projectDir -TransactionId $transactionId -State ([PSCustomObject]$state)
if ($complete.ok -ne $true) { throw [string]$complete.message }
$script:EnvironmentSwitched = $false

Write-Host "本地训练环境已就绪。" -ForegroundColor Green
