param(
    [ValidateSet("CPU", "GPU")]
    [string]$Mode = ""
)

$ErrorActionPreference = "Stop"
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:HF_HUB_DISABLE_PROGRESS_BARS = "1"
$env:HF_HUB_DISABLE_SYMLINKS_WARNING = "1"
$projectDir = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "release_common.ps1")
$environmentModulePath = Join-Path $PSScriptRoot "environment\OwVoice.Environment.psm1"
if (-not (Test-Path -LiteralPath $environmentModulePath -PathType Leaf)) { throw "缺少公共环境核心：$environmentModulePath" }
Import-Module -Name $environmentModulePath -Force
$releaseIdentity = Get-OwVoiceReleaseIdentity -ProjectRoot $projectDir
$appVersion = $releaseIdentity.tag
Set-Location $projectDir
$venvDir = Join-Path $projectDir ".venv"
$pythonExe = Join-Path $venvDir "Scripts\python.exe"
$uvExe = Join-Path $projectDir "tools\uv\uv.exe"
$uvSha256 = "efb9599543b26b3ea5adc1649bef69788633d9cc25c6cfd97b799e4dfa0c2cfb"
$recommendedPythonVersion = "3.10.10"
$cacheDir = Join-Path $projectDir ".cache"
$uvCacheDir = Join-Path $cacheDir "uv"
$managedPythonDir = Join-Path $projectDir ".runtime\python"
$setupStatePath = Join-Path $cacheDir "setup-state.json"
$logDir = Join-Path $projectDir "logs"
$logPath = Join-Path $logDir ("setup-{0}.log" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
$configPath = Join-Path $projectDir "config\voices.local.json"
$examplePath = Join-Path $projectDir "config\voices.example.json"
$script:EnvironmentTransactionId = ""
$script:EnvironmentSwitched = $false
$script:EnvironmentCandidateCreated = $false

function Write-Step([string]$message) { Write-Host ("`n[" + $message + "]") -ForegroundColor Cyan }

function Test-TcpPort([int]$port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $result = $client.BeginConnect("127.0.0.1", $port, $null, $null)
        if (-not $result.AsyncWaitHandle.WaitOne(300)) { return $false }
        $client.EndConnect($result)
        return $true
    } catch { return $false } finally { $client.Dispose() }
}

function Get-PythonInfo([string]$path) {
    $value = & $path -c "import platform, struct, sys; print('%d.%d|%d|%s' % (sys.version_info[0], sys.version_info[1], struct.calcsize('P') * 8, platform.python_implementation()))"
    if ($LASTEXITCODE -ne 0) { throw "无法运行 Python：$path" }
    return $value.Trim()
}

function Get-SetupState {
    if (-not (Test-Path -LiteralPath $setupStatePath -PathType Leaf)) { return $null }
    try { return Get-Content -LiteralPath $setupStatePath -Raw -Encoding UTF8 | ConvertFrom-Json } catch { return $null }
}

function Test-TrainingInstalled {
    if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) { return $false }
    & $pythonExe -c "import importlib.util; names=('tensorboard','gradio','funasr','modelscope'); raise SystemExit(0 if all(importlib.util.find_spec(n) for n in names) else 1)" 2>$null
    return $LASTEXITCODE -eq 0
}

function Select-InstallMode {
    if (-not [string]::IsNullOrWhiteSpace($Mode)) { return $Mode }
    Write-Host "`n请选择推理模式：" -ForegroundColor Yellow
    Write-Host "[1] CPU：无需独立显卡，兼容性最好；推理速度较慢。"
    Write-Host "[2] GPU：需要 NVIDIA 显卡和 570 或更高版本驱动；下载较大，速度更快。"
    do { $choice = (Read-Host "请输入 1 或 2").Trim() } while ($choice -notin @("1", "2"))
    if ($choice -eq "1") { return "CPU" }
    return "GPU"
}

function Assert-Preflight([string]$installMode) {
    Write-Step "安装前检查"
    if (-not [Environment]::Is64BitOperatingSystem) { throw "OwVoice $appVersion 仅支持 64 位 Windows。" }
    if ($projectDir.Length -gt 180) { throw "安装路径过长（$($projectDir.Length) 个字符）。请解压到较短路径，例如 D:\OwVoice。" }
    if ($projectDir.Length -gt 120) { Write-Host "警告：当前路径较长，建议移动到 D:\OwVoice。" -ForegroundColor Yellow }
    if ($projectDir -match "(?i)\\Program Files( \(x86\))?\\") { throw "请勿放在 Program Files 中安装；请移动到 D:\OwVoice 等有写权限的目录。" }

    New-Item -ItemType Directory -Force -Path $cacheDir, $logDir | Out-Null
    $writeProbe = Join-Path $cacheDir ("write-test-" + [Guid]::NewGuid().ToString("N") + ".tmp")
    try { [System.IO.File]::WriteAllText($writeProbe, "ok", $utf8) } finally { Remove-Item -LiteralPath $writeProbe -Force -ErrorAction SilentlyContinue }

    $drive = (Get-Item -LiteralPath $projectDir).PSDrive
    if ($null -ne $drive -and $null -ne $drive.Free) {
        $requiredGb = if ($installMode -eq "GPU") { 24 } else { 15 }
        $freeGb = [Math]::Round($drive.Free / 1GB, 1)
        if ($freeGb -lt $requiredGb) { throw "磁盘可用空间仅 $freeGb GB；$installMode 首次安装至少需要 $requiredGb GB。" }
        Write-Host "磁盘可用空间：$freeGb GB"
    }

    foreach ($process in @(Get-Process -Name "OwVoice" -ErrorAction SilentlyContinue)) {
        throw "检测到 OwVoice.exe 正在运行（PID $($process.Id)）。请先关闭程序再安装或更新环境。"
    }
    foreach ($port in @(8765, 9880)) {
        if (Test-TcpPort $port) { Write-Host "警告：端口 $port 已被占用，OwVoice 启动时可能冲突。" -ForegroundColor Yellow }
    }

    $runtimeFiles = @("$env:WINDIR\System32\vcruntime140.dll", "$env:WINDIR\System32\msvcp140.dll")
    if (@($runtimeFiles | Where-Object { -not (Test-Path -LiteralPath $_ -PathType Leaf) }).Count -gt 0) {
        throw "缺少 Microsoft Visual C++ 2015-2022 x64 运行库（不是编译工具）。请安装 https://aka.ms/vs/17/release/vc_redist.x64.exe 后重试。"
    }

    if ($installMode -eq "GPU") {
        $nvidiaSmi = Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue
        if ($null -eq $nvidiaSmi) { throw "未检测到 NVIDIA 驱动。请选择 CPU，或先安装/更新 NVIDIA 驱动。" }
        $gpuInfo = & $nvidiaSmi.Source --query-gpu=name,driver_version,memory.total --format=csv,noheader,nounits 2>$null | Select-Object -First 1
        if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($gpuInfo)) { throw "nvidia-smi 无法读取显卡状态。请修复驱动或选择 CPU。" }
        $parts = @($gpuInfo -split "," | ForEach-Object { $_.Trim() })
        $driverMajor = 0
        [void][int]::TryParse(($parts[1] -split "\.")[0], [ref]$driverMajor)
        if ($driverMajor -lt 570) { throw "当前 NVIDIA 驱动为 $($parts[1])；CUDA 12.8 需要 570 或更高版本驱动。请更新驱动或选择 CPU。" }
        Write-Host ("显卡：{0}，驱动：{1}，显存：{2} MB" -f $parts[0], $parts[1], $parts[2])
    }
}

function Assert-Uv {
    if (-not (Test-Path -LiteralPath $uvExe -PathType Leaf)) { throw "安装工具缺失：tools\uv\uv.exe。请重新下载完整发布包。" }
    $actual = (Get-FileHash -LiteralPath $uvExe -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $uvSha256) { throw "tools\uv\uv.exe 校验失败。请重新下载发布包。" }
    $signature = Get-AuthenticodeSignature -LiteralPath $uvExe
    if ($signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid) { throw "安装工具数字签名无效（$($signature.Status)）。" }
}

function Ensure-PythonEnvironment {
    Write-Step "检查现有 CPython 3.10.x x64（推荐 $recommendedPythonVersion）"
    if (Test-Path -LiteralPath $pythonExe -PathType Leaf) {
        $info = Get-PythonInfo $pythonExe
        if ($info -notlike "3.10|64|CPython") { throw ".venv 中的 Python 不是 64 位 CPython 3.10。请手动重命名 .venv 后重试。" }
        Write-Host "复用现有环境：$info" -ForegroundColor Green
        return
    }
    if (Test-Path -LiteralPath $venvDir) { throw ".venv 已存在但不完整。请将其重命名为 .venv-broken 后重试。" }
    $existingPython = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($null -eq $existingPython -or -not (Test-Path -LiteralPath $existingPython.Source -PathType Leaf)) { throw "未找到现有 CPython 3.10.x x64。新安装推荐 $recommendedPythonVersion；请先安装并配置 python.exe 后重试。" }
    $info = Get-PythonInfo $existingPython.Source
    if ($info -notlike "3.10|64|CPython") { throw "当前 python.exe 不是兼容的 CPython 3.10.x x64。新安装推荐 $recommendedPythonVersion；请先修复 PATH 后重试。" }
    New-Item -ItemType Directory -Force -Path $uvCacheDir | Out-Null
    & $uvExe venv $venvDir --python $existingPython.Source
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) { throw "无法创建项目私有 Python 环境。" }
}

function Sync-Dependencies([string]$installMode) {
    Write-Step "安装锁定的二进制依赖"
    $lockPath = Join-Path $projectDir "uv.lock"
    $projectPath = Join-Path $projectDir "pyproject.toml"
    if (-not (Test-Path -LiteralPath $lockPath -PathType Leaf) -or -not (Test-Path -LiteralPath $projectPath -PathType Leaf)) { throw "pyproject.toml 或 uv.lock 缺失。" }
    $environmentHash = ((Get-FileHash $lockPath -Algorithm SHA256).Hash + (Get-FileHash $projectPath -Algorithm SHA256).Hash).ToLowerInvariant()
    $oldState = Get-SetupState
    $withTraining = (($null -ne $oldState -and $oldState.trainingReady -eq $true) -or (Test-TrainingInstalled))
    # jieba 仅发布纯 Python sdist，uv 会在隔离环境中打包它；其余原生依赖均使用锁定 wheel，用户无需编译工具。
    $uvArgs = @("sync", "--frozen", "--extra", $installMode.ToLowerInvariant(), "--no-dev", "--no-install-project")
    if ($withTraining) { $uvArgs += @("--extra", "training") }
    $stateMatches = $null -ne $oldState -and $oldState.dependenciesReady -eq $true -and $oldState.mode -eq $installMode -and $oldState.environmentSha256 -eq $environmentHash
    if ($stateMatches) {
        & $uvExe @uvArgs --check
        if ($LASTEXITCODE -eq 0) { Write-Host "环境与锁文件一致，跳过重复安装。" -ForegroundColor Green; return }
    }
    & $uvExe @uvArgs
    if ($LASTEXITCODE -ne 0) { throw "Python 依赖安装失败。用户无需安装 C/C++ 编译工具；请查看日志中的下载错误后重试。" }
    & $uvExe pip check --python $pythonExe
    if ($LASTEXITCODE -ne 0) { throw "依赖已安装，但完整性检查失败。" }
    $state = [ordered]@{ schema=2; dependenciesReady=$true; trainingReady=$withTraining; mode=$installMode; environmentSha256=$environmentHash; python=(Get-PythonInfo $pythonExe); updatedAt=[DateTime]::UtcNow.ToString("o") } | ConvertTo-Json
    [System.IO.File]::WriteAllText($setupStatePath, $state, $utf8)
}

function Invoke-Setup {
    $installMode = Select-InstallMode
    Write-Host "OwVoice $appVersion 首次配置/环境修复" -ForegroundColor Green
    Write-Host "项目目录：$projectDir"
    Write-Host "安装模式：$installMode"
    $withTraining = Test-TrainingInstalled
    $script:EnvironmentWithTraining = $withTraining
    $plan = Get-OwVoiceEnvironmentPlan -ProjectRoot $projectDir -Mode $installMode -WithTraining:$withTraining
    if ((Test-Path -LiteralPath $venvDir -PathType Container) -and $plan.action -ne "reuse") {
        $answer = (Read-Host "当前环境需要安全同步（$($plan.reason)）。程序会优先复制并复用兼容环境，只安装发生变化的依赖。是否继续？请输入 Y/N").Trim().ToUpperInvariant()
        if ($answer -ne "Y") { throw "用户取消环境同步，未修改现有环境。" }
        $plan = Get-OwVoiceEnvironmentPlan -ProjectRoot $projectDir -Mode $installMode -WithTraining:$withTraining -MigrateLegacy
    }
    $requiredBytes = if ($installMode -eq "GPU") { 24GB } else { 15GB }
    $preflight = Test-OwVoiceEnvironmentPreflight -ProjectRoot $projectDir -Mode $installMode -WithTraining:$withTraining -RequiredFreeBytes $requiredBytes -UvPath $uvExe
    if ($preflight.ok -ne $true) { throw [string]$preflight.message }
    if ($plan.action -eq "reuse") {
        Write-Host "现有项目环境与目标规范一致，跳过环境同步。" -ForegroundColor Green
    } else {
        $script:EnvironmentTransactionId = [Guid]::NewGuid().ToString()
        $candidateResult = New-OwVoiceCandidateEnvironment -ProjectRoot $projectDir -TransactionId $script:EnvironmentTransactionId -CandidatePath (Join-Path $projectDir ".venv.next") -Mode $installMode -WithTraining:$withTraining -UvPath $uvExe
        if ($candidateResult.ok -ne $true) { throw [string]$candidateResult.message }
        $script:EnvironmentCandidateCreated = $true
        $candidateCheck = Test-OwVoiceCandidateEnvironment -ProjectRoot $projectDir -TransactionId $script:EnvironmentTransactionId -CandidatePath (Join-Path $projectDir ".venv.next") -Mode $installMode -WithTraining:$withTraining
        if ($candidateCheck.ok -ne $true) { throw ([string]$candidateCheck.message) }
        $spec = Read-OwVoiceEnvironmentSpec -ProjectRoot $projectDir
        $candidateState = New-OwVoiceEnvironmentState -Spec $spec -ProjectRoot $projectDir -Mode $installMode -WithTraining:$withTraining -EnvironmentFingerprint ([string]$plan.environment_fingerprint) -Python $candidateCheck.state.python -Verified:$true -TransactionId $script:EnvironmentTransactionId
        $switchResult = Switch-OwVoiceEnvironment -ProjectRoot $projectDir -TransactionId $script:EnvironmentTransactionId -CandidatePath (Join-Path $projectDir ".venv.next") -State ([PSCustomObject]$candidateState)
        if ($switchResult.ok -ne $true) { throw [string]$switchResult.message }
        $script:EnvironmentSwitched = $true
        Write-Host "候选环境已验证并切换为正式环境。" -ForegroundColor Green
    }

    Write-Step "安装 NLTK 数据"
    $resourceProcessLogs = Join-Path $logDir "resource-processes"
    $nltkExitCode = Invoke-OwVoiceTrackedProcess -FilePath $pythonExe -Arguments @((Join-Path $projectDir "scripts\download_nltk_data.py")) -Activity "正在准备 NLTK 文本资源" -LogRoot $resourceProcessLogs -WorkingDirectory $projectDir
    if ($nltkExitCode -ne 0) { throw "NLTK 数据安装失败：$(Get-OwVoiceLastProcessFailureMessage)" }
    Write-Step "下载并校验 GPT-SoVITS 必需资源"
    $pretrainedExitCode = Invoke-OwVoiceTrackedProcess -FilePath $pythonExe -Arguments @((Join-Path $projectDir "scripts\download_pretrained.py")) -Activity "正在准备 GPT-SoVITS 运行资源" -LogRoot $resourceProcessLogs -WorkingDirectory $projectDir
    if ($pretrainedExitCode -ne 0) { throw "GPT-SoVITS 预训练资源下载失败：$(Get-OwVoiceLastProcessFailureMessage)" }

    Write-Step "检查语音配置"
    foreach ($directory in @("output", ".cache\synthesis", "logs")) { New-Item -ItemType Directory -Force -Path (Join-Path $projectDir $directory) | Out-Null }
    if (-not (Test-Path -LiteralPath $configPath)) { Copy-Item -LiteralPath $examplePath -Destination $configPath }
    try { $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json } catch { throw "voices.local.json 不是有效 JSON：$($_.Exception.Message)" }
    $voices = @($config.voices | Where-Object { $_.enabled -ne $false })
    if ($voices.Count -lt 1) { Write-Host "尚未导入角色模型；环境安装会正常完成，之后可在 OwVoice 中导入。" -ForegroundColor Yellow }

    Write-Step "最终运行检查"
    $verifyExitCode = Invoke-OwVoiceTrackedProcess -FilePath $pythonExe -Arguments @((Join-Path $projectDir "scripts\verify_runtime.py")) -Activity "正在验证 OwVoice 运行环境" -LogRoot $resourceProcessLogs -WorkingDirectory $projectDir
    if ($verifyExitCode -ne 0) { throw "运行环境验证未通过：$(Get-OwVoiceLastProcessFailureMessage)" }
    $torchCheck = "import torch; print('PyTorch', torch.__version__, 'CUDA', torch.version.cuda, '可用', torch.cuda.is_available()); raise SystemExit(0 if '$installMode' != 'GPU' or torch.cuda.is_available() else 2)"
    $torchExitCode = Invoke-OwVoiceTrackedProcess -FilePath $pythonExe -Arguments @("-c", $torchCheck) -Activity "正在检查 PyTorch 和 CUDA" -LogRoot $resourceProcessLogs -WorkingDirectory $projectDir
    if ($torchExitCode -ne 0) { throw "GPU 模式下 PyTorch 未检测到可用 CUDA。请更新 NVIDIA 驱动或选择 CPU。详情：$(Get-OwVoiceLastProcessFailureMessage)" }
    $spec = Read-OwVoiceEnvironmentSpec -ProjectRoot $projectDir
    $finalState = New-OwVoiceEnvironmentState -Spec $spec -ProjectRoot $projectDir -Mode $installMode -WithTraining:$withTraining -EnvironmentFingerprint ([string]$plan.environment_fingerprint) -Python (Get-OwVoicePythonInfo $pythonExe) -Verified:$true -TransactionId $script:EnvironmentTransactionId
    $complete = Complete-OwVoiceEnvironmentTransaction -ProjectRoot $projectDir -TransactionId $(if ($script:EnvironmentSwitched) { $script:EnvironmentTransactionId } else { [Guid]::NewGuid().ToString() }) -State ([PSCustomObject]$finalState)
    if ($complete.ok -ne $true) { throw [string]$complete.message }
    $script:EnvironmentSwitched = $false
    Write-Host "`n配置完成。普通用户请双击 OwVoice.exe 启动。" -ForegroundColor Green
}

New-Item -ItemType Directory -Force -Path $logDir | Out-Null
foreach ($oldLog in @(Get-ChildItem -LiteralPath $logDir -Filter "setup-*.log" -File | Sort-Object LastWriteTime -Descending | Select-Object -Skip 9)) {
    Remove-Item -LiteralPath $oldLog.FullName -Force -ErrorAction SilentlyContinue
}
$transcriptStarted = $false
$exitCode = 0
try {
    Start-Transcript -LiteralPath $logPath -Append | Out-Null
    $transcriptStarted = $true
    Invoke-Setup
} catch {
    $exitCode = 1
    $errorReference = "SETUP-500 · " + [Guid]::NewGuid().ToString("N").Substring(0, 8)
    if ($script:EnvironmentSwitched -and -not [string]::IsNullOrWhiteSpace($script:EnvironmentTransactionId)) {
        $restore = Restore-OwVoiceEnvironment -ProjectRoot $projectDir -TransactionId $script:EnvironmentTransactionId
        if ($restore.ok -eq $true) { Write-Host "环境验证失败，旧环境已恢复。" -ForegroundColor Yellow } else { Write-Host "环境验证失败，自动恢复也失败：$($restore.message)" -ForegroundColor Red }
    }
    if ($script:EnvironmentCandidateCreated -and (Test-Path -LiteralPath (Join-Path $projectDir ".venv.next"))) {
        Remove-Item -LiteralPath (Join-Path $projectDir ".venv.next") -Recurse -Force -ErrorAction SilentlyContinue
    }
    $script:EnvironmentCandidateCreated = $false
    Write-Host "`n安装未完成：$($_.Exception.Message)" -ForegroundColor Red
    Write-Host "错误编号：$errorReference" -ForegroundColor Red
    Write-Host "可以直接重新运行 setup.bat；下载缓存和已完成步骤会自动复用。" -ForegroundColor Yellow
    Write-Host "排错日志：$logPath" -ForegroundColor Yellow
} finally {
    if ($transcriptStarted) { Stop-Transcript | Out-Null }
}
exit $exitCode
