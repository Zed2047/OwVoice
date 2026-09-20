$script:OwVoiceEnvironmentSchema = 1
$script:OwVoiceEnvironmentUtf8 = New-Object System.Text.UTF8Encoding($false)

function Get-OwVoiceEnvironmentPath {
    param([Parameter(Mandatory=$true)][string]$ProjectRoot)
    $root = [System.IO.Path]::GetFullPath($ProjectRoot)
    return [PSCustomObject]@{
        Root = $root
        Spec = Join-Path $root "environment-spec.json"
        Runtime = Join-Path $root ".runtime"
        State = Join-Path $root ".runtime\environment-state.json"
        Lock = Join-Path $root ".runtime\environment.lock"
        JournalRoot = Join-Path $root ".runtime\environment-transactions"
        Venv = Join-Path $root ".venv"
        Candidate = Join-Path $root ".venv.next"
        OldVenv = Join-Path $root ".venv.old"
    }
}

function Write-OwVoiceEnvironmentJsonAtomic {
    param([Parameter(Mandatory=$true)][string]$Path, [Parameter(Mandatory=$true)]$Value)
    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    $temporary = "$Path.$([Guid]::NewGuid().ToString('N')).tmp"
    try {
        $json = $Value | ConvertTo-Json -Depth 20
        [System.IO.File]::WriteAllText($temporary, $json, $script:OwVoiceEnvironmentUtf8)
        Move-Item -LiteralPath $temporary -Destination $Path -Force
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue }
    }
}

function Enter-OwVoiceEnvironmentLock {
    param([Parameter(Mandatory=$true)][string]$Path)
    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    try {
        return New-Object System.IO.FileStream($Path, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
    } catch {
        throw "环境操作正在由另一个 OwVoice 任务执行：$Path"
    }
}

function Test-OwVoiceSha256([object]$Value) { return ([string]$Value -match '^[0-9a-fA-F]{64}$') }

function Get-OwVoiceFileSha256 {
    param([Parameter(Mandatory=$true)][string]$Path)
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    $stream = $null
    try {
        $stream = [System.IO.File]::OpenRead($Path)
        return ([BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace("-", "").ToLowerInvariant()
    } finally {
        if ($null -ne $stream) { $stream.Dispose() }
        $algorithm.Dispose()
    }
}

function Get-OwVoiceAvailableFreeBytes {
    param([Parameter(Mandatory=$true)][string]$ProjectRoot)
    try {
        $root = [System.IO.Path]::GetPathRoot([System.IO.Path]::GetFullPath($ProjectRoot))
        if ([string]::IsNullOrWhiteSpace($root)) { return $null }
        $drive = New-Object System.IO.DriveInfo -ArgumentList $root
        if (-not $drive.IsReady) { return $null }
        return [long]$drive.AvailableFreeSpace
    } catch {
        # 空间信息不可读时交给上层继续执行其他门禁，不把权限异常误报成空间不足。
        return $null
    }
}

function Test-OwVoicePythonCompatible {
    param([object]$Python, [object]$PythonSpec)
    if ($null -eq $Python -or $null -eq $PythonSpec) { return $false }
    $parts = ([string]$Python.version).Split(".")
    $majorMinor = if ($parts.Count -ge 2) { "$($parts[0]).$($parts[1])" } else { "" }
    return [string]$Python.implementation -eq [string]$PythonSpec.implementation -and
        [string]$Python.architecture -eq [string]$PythonSpec.architecture -and
        $majorMinor -eq [string]$PythonSpec.compatible_major_minor
}

function Get-OwVoiceExistingPythonPath {
    param([Parameter(Mandatory=$true)]$PythonSpec)
    $command = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($null -eq $command -or -not (Test-Path -LiteralPath $command.Source -PathType Leaf)) { return $null }
    try {
        $info = Get-OwVoicePythonInfo -PythonPath $command.Source
        if (Test-OwVoicePythonCompatible $info $PythonSpec) { return $command.Source }
    } catch { return $null }
    return $null
}

function Read-OwVoiceEnvironmentSpec {
    param([Parameter(Mandatory=$true)][string]$ProjectRoot)
    $paths = Get-OwVoiceEnvironmentPath $ProjectRoot
    if (-not (Test-Path -LiteralPath $paths.Spec -PathType Leaf)) { throw "缺少环境规范：$($paths.Spec)" }
    try { $spec = Get-Content -LiteralPath $paths.Spec -Raw -Encoding UTF8 | ConvertFrom-Json } catch { throw "environment-spec.json 不是有效 JSON：$($_.Exception.Message)" }
    if ($null -eq $spec -or $spec.schema -ne $script:OwVoiceEnvironmentSchema) { throw "environment-spec.json schema 无效。" }
    $pythonSpec = $spec.python
    if ($null -eq $pythonSpec -or ([string]$pythonSpec.implementation) -ne "CPython" -or
        ([string]$pythonSpec.version) -notmatch '^[0-9]+\.[0-9]+\.[0-9]+$' -or
        ([string]$pythonSpec.version_range) -ne ">=3.10,<3.11" -or
        ([string]$pythonSpec.compatible_major_minor) -ne "3.10" -or
        @($pythonSpec.tested_versions).Count -lt 2 -or
        @($pythonSpec.tested_versions) -notcontains "3.10.10" -or
        @($pythonSpec.tested_versions) -notcontains "3.10.21" -or
        ([string]$pythonSpec.architecture) -ne "x64" -or ([string]$pythonSpec.source) -ne "existing-system") { throw "environment-spec.json 的 Python 规范无效。" }
    $dependencySpec = $spec.dependencies
    if ($null -eq $dependencySpec -or -not (Test-OwVoiceSha256 $dependencySpec.pyproject_sha256) -or -not (Test-OwVoiceSha256 $dependencySpec.lock_sha256) -or @($dependencySpec.modes).Count -lt 2 -or @($dependencySpec.modes) -notcontains "CPU" -or @($dependencySpec.modes) -notcontains "GPU" -or [string]::IsNullOrWhiteSpace([string]$dependencySpec.training_extra)) { throw "environment-spec.json 的依赖规范无效。" }
    $resourceSpec = $spec.resources
    if ($null -eq $resourceSpec -or $resourceSpec.schema -ne 2 -or ([string]$resourceSpec.lock_file) -ne "resource-lock.json") { throw "environment-spec.json 的资源规范无效。" }
    $toolSpec = $spec.tools
    if ($null -eq $toolSpec -or [string]::IsNullOrWhiteSpace([string]$toolSpec.uv_version) -or -not (Test-OwVoiceSha256 $toolSpec.uv_sha256)) { throw "environment-spec.json 的工具规范无效。" }
    $resourceLockPath = Join-Path $paths.Root ([string]$resourceSpec.lock_file)
    if (-not (Test-Path -LiteralPath $resourceLockPath -PathType Leaf)) { throw "环境规范引用的资源锁不存在：$($resourceSpec.lock_file)" }
    $projectPath = Join-Path $paths.Root "pyproject.toml"
    $lockPath = Join-Path $paths.Root "uv.lock"
    $uvPath = Join-Path $paths.Root "tools\uv\uv.exe"
    if (-not (Test-Path -LiteralPath $projectPath -PathType Leaf) -or -not (Test-Path -LiteralPath $lockPath -PathType Leaf) -or -not (Test-Path -LiteralPath $uvPath -PathType Leaf)) { throw "环境规范依赖的 pyproject.toml、uv.lock 或 uv.exe 缺失。" }
    $actualProjectHash = Get-OwVoiceFileSha256 $projectPath
    $actualLockHash = Get-OwVoiceFileSha256 $lockPath
    $actualUvHash = Get-OwVoiceFileSha256 $uvPath
    if ($actualProjectHash -ne ([string]$dependencySpec.pyproject_sha256).ToLowerInvariant() -or $actualLockHash -ne ([string]$dependencySpec.lock_sha256).ToLowerInvariant() -or $actualUvHash -ne ([string]$toolSpec.uv_sha256).ToLowerInvariant()) { throw "环境规范哈希与实际安装输入不一致。" }
    return $spec
}

function Get-OwVoiceEnvironmentFingerprint {
    param([Parameter(Mandatory=$true)]$Spec, [Parameter(Mandatory=$true)][ValidateSet("CPU", "GPU")][string]$Mode, [Parameter(Mandatory=$true)][bool]$WithTraining)
    $input = "{0}|{1}|{2}|{3}|{4}" -f $Spec.python.version_range, $Spec.dependencies.pyproject_sha256, $Spec.dependencies.lock_sha256, $Mode.ToUpperInvariant(), $WithTraining.ToString().ToLowerInvariant()
    $sha = New-Object System.Security.Cryptography.SHA256Managed
    try { return ([BitConverter]::ToString($sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($input)))).Replace("-", "").ToLowerInvariant() } finally { $sha.Dispose() }
}

function New-OwVoiceEnvironmentState {
    param(
        [Parameter(Mandatory=$true)]$Spec,
        [Parameter(Mandatory=$true)][ValidateSet("CPU", "GPU")][string]$Mode,
        [bool]$WithTraining=$false,
        [Parameter(Mandatory=$true)][string]$EnvironmentFingerprint,
        $Python=$null,
        [bool]$Verified=$true,
        [string]$ProjectRoot="",
        [string]$TransactionId=""
    )
    return [PSCustomObject][ordered]@{
        schema = $script:OwVoiceEnvironmentSchema
        project_root = $ProjectRoot
        mode = $Mode.ToUpperInvariant()
        with_training = $WithTraining
        environment_fingerprint = $EnvironmentFingerprint.ToLowerInvariant()
        python = $Python
        python_version = if ($null -ne $Python -and -not [string]::IsNullOrWhiteSpace([string]$Python.version)) { [string]$Python.version } else { [string]$Spec.python.version }
        python_version_range = [string]$Spec.python.version_range
        python_implementation = [string]$Spec.python.implementation
        architecture = [string]$Spec.python.architecture
        pyproject_sha256 = ([string]$Spec.dependencies.pyproject_sha256).ToLowerInvariant()
        lock_sha256 = ([string]$Spec.dependencies.lock_sha256).ToLowerInvariant()
        resource_schema = [int]$Spec.resources.schema
        verified = $Verified
        transaction_id = $TransactionId
        updated_at = [DateTime]::UtcNow.ToString("o")
    }
}

function Get-OwVoicePythonInfo {
    param([Parameter(Mandatory=$true)][string]$PythonPath)
    if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) { return $null }
    $code = "import json,platform,struct,sys; print(json.dumps({'implementation':platform.python_implementation(),'version':'.'.join(str(x) for x in sys.version_info[:3]),'architecture':'x64' if struct.calcsize('P') * 8 == 64 else 'x86'}, ensure_ascii=False))"
    try {
        $output = & $PythonPath -c $code 2>$null
        if ($LASTEXITCODE -ne 0) { return $null }
        return (($output -join "") | ConvertFrom-Json)
    } catch { return $null }
}

function Get-OwVoiceEnvironmentState {
    param([Parameter(Mandatory=$true)][string]$ProjectRoot, [switch]$MigrateLegacy)
    $paths = Get-OwVoiceEnvironmentPath $ProjectRoot
    if (Test-Path -LiteralPath $paths.State -PathType Leaf) {
        try { $state = Get-Content -LiteralPath $paths.State -Raw -Encoding UTF8 | ConvertFrom-Json } catch { return $null }
        if ($null -eq $state -or $state.schema -ne $script:OwVoiceEnvironmentSchema) { return $null }
        return $state
    }
    if (-not $MigrateLegacy) { return $null }
    $legacyPath = Join-Path $paths.Root ".cache\setup-state.json"
    if (-not (Test-Path -LiteralPath $legacyPath -PathType Leaf)) { return $null }
    try { $legacy = Get-Content -LiteralPath $legacyPath -Raw -Encoding UTF8 | ConvertFrom-Json } catch { return $null }
    if ($null -eq $legacy -or $legacy.schema -ne 2 -or $legacy.dependenciesReady -ne $true -or [string]$legacy.mode -notin @("CPU", "GPU")) { return $null }
    $spec = Read-OwVoiceEnvironmentSpec $paths.Root
    $mode = ([string]$legacy.mode).ToUpperInvariant()
    $withTraining = ($legacy.trainingReady -eq $true)
    $fingerprint = Get-OwVoiceEnvironmentFingerprint $spec $mode $withTraining
    $pythonInfo = Get-OwVoicePythonInfo (Join-Path $paths.Venv "Scripts\python.exe")
    $migrated = [ordered]@{
        schema=$script:OwVoiceEnvironmentSchema; project_root=$paths.Root; mode=$mode; with_training=$withTraining
        environment_fingerprint=$fingerprint; python=[string]$legacy.python
        python_version=if ($null -ne $pythonInfo) { [string]$pythonInfo.version } else { [string]$spec.python.version }; python_version_range=[string]$spec.python.version_range; python_implementation=[string]$spec.python.implementation
        architecture=[string]$spec.python.architecture; pyproject_sha256=([string]$spec.dependencies.pyproject_sha256).ToLowerInvariant()
        lock_sha256=([string]$spec.dependencies.lock_sha256).ToLowerInvariant(); resource_schema=[int]$spec.resources.schema
        verified=$true; legacy=$true; legacy_fingerprint=[string]$legacy.environmentSha256; migrated_at=[DateTime]::UtcNow.ToString("o")
    }
    Write-OwVoiceEnvironmentJsonAtomic -Path $paths.State -Value $migrated
    return ([PSCustomObject]$migrated)
}

function Get-OwVoiceEnvironmentPlan {
    param([Parameter(Mandatory=$true)][string]$ProjectRoot, [Parameter(Mandatory=$true)][ValidateSet("CPU", "GPU")][string]$Mode, [bool]$WithTraining=$false, [switch]$MigrateLegacy)
    $paths = Get-OwVoiceEnvironmentPath $ProjectRoot
    $spec = Read-OwVoiceEnvironmentSpec $paths.Root
    $mode = $Mode.ToUpperInvariant()
    $fingerprint = Get-OwVoiceEnvironmentFingerprint $spec $mode $WithTraining
    $pythonPath = Join-Path $paths.Venv "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $paths.Venv -PathType Container)) { return [PSCustomObject]@{ schema=1; action="create"; reason="missing_environment"; mode=$mode; with_training=$WithTraining; environment_fingerprint=$fingerprint } }
    if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) { return [PSCustomObject]@{ schema=1; action="repair"; reason="python_missing"; mode=$mode; with_training=$WithTraining; environment_fingerprint=$fingerprint } }
    $state = Get-OwVoiceEnvironmentState -ProjectRoot $paths.Root -MigrateLegacy:$MigrateLegacy
    if ($null -eq $state) { return [PSCustomObject]@{ schema=1; action="repair"; reason="state_missing_or_invalid"; mode=$mode; with_training=$WithTraining; environment_fingerprint=$fingerprint } }
    $python = Get-OwVoicePythonInfo $pythonPath
    if (-not (Test-OwVoicePythonCompatible $python $spec.python)) { return [PSCustomObject]@{ schema=1; action="repair"; reason="python_incompatible"; mode=$mode; with_training=$WithTraining; environment_fingerprint=$fingerprint } }
    if ([string]$state.mode -ne $mode -or ($state.with_training -eq $true) -ne $WithTraining) { return [PSCustomObject]@{ schema=1; action="migrate"; reason="mode_or_training_changed"; mode=$mode; with_training=$WithTraining; environment_fingerprint=$fingerprint } }
    if ([string]$state.environment_fingerprint -ne $fingerprint -or $state.legacy -eq $true) { return [PSCustomObject]@{ schema=1; action="migrate"; reason="spec_changed_or_legacy_state"; mode=$mode; with_training=$WithTraining; environment_fingerprint=$fingerprint } }
    return [PSCustomObject]@{ schema=1; action="reuse"; reason="environment_matches_spec"; mode=$mode; with_training=$WithTraining; environment_fingerprint=$fingerprint }
}

function Test-OwVoiceEnvironmentPreflight {
    param([Parameter(Mandatory=$true)][string]$ProjectRoot, [Parameter(Mandatory=$true)][ValidateSet("CPU", "GPU")][string]$Mode, [bool]$WithTraining=$false, [long]$RequiredFreeBytes=0, [string]$UvPath="")
    $paths = Get-OwVoiceEnvironmentPath $ProjectRoot
    try { $spec = Read-OwVoiceEnvironmentSpec $paths.Root } catch { return [PSCustomObject]@{ ok=$false; code="ENV_SPEC_INVALID"; message=$_.Exception.Message } }
    if (-not [Environment]::Is64BitOperatingSystem) { return [PSCustomObject]@{ ok=$false; code="ENV_OS_UNSUPPORTED"; message="仅支持 64 位 Windows。" } }
    if ($paths.Root.Length -gt 180) { return [PSCustomObject]@{ ok=$false; code="ENV_PATH_TOO_LONG"; message="项目路径过长。" } }
    if ([string]::IsNullOrWhiteSpace($UvPath)) { $UvPath = Join-Path $paths.Root "tools\uv\uv.exe" }
    if (-not (Test-Path -LiteralPath $UvPath -PathType Leaf)) { return [PSCustomObject]@{ ok=$false; code="ENV_UV_MISSING"; message="缺少固定版本 uv：$UvPath" } }
    if ((Get-OwVoiceFileSha256 $UvPath) -ne ([string]$spec.tools.uv_sha256).ToLowerInvariant()) { return [PSCustomObject]@{ ok=$false; code="ENV_UV_HASH_MISMATCH"; message="固定版本 uv 校验失败。" } }
    if ($RequiredFreeBytes -gt 0) { $freeBytes = Get-OwVoiceAvailableFreeBytes -ProjectRoot $paths.Root; if ($null -eq $freeBytes -or [long]$freeBytes -lt $RequiredFreeBytes) { return [PSCustomObject]@{ ok=$false; code="ENV_DISK_SPACE_INSUFFICIENT"; message="磁盘可用空间不足或无法读取。" } } }
    return [PSCustomObject]@{ ok=$true; code="OK"; mode=$Mode.ToUpperInvariant(); with_training=$WithTraining; python_version=[string]$spec.python.version; python_version_range=[string]$spec.python.version_range; uv_path=[System.IO.Path]::GetFullPath($UvPath) }
}

function ConvertTo-OwVoiceProcessArgument {
    param([Parameter(Mandatory=$true)][string]$Value)
    return '"' + $Value.Replace('"', '\"') + '"'
}

function Invoke-OwVoiceTrackedProcess {
    param(
        [Parameter(Mandatory=$true)][string]$FilePath,
        [Parameter(Mandatory=$true)][string[]]$Arguments,
        [Parameter(Mandatory=$true)][string]$Activity,
        [Parameter(Mandatory=$true)][string]$LogRoot,
        [string]$WorkingDirectory="",
        [int[]]$AcceptExitCodes=@(0)
    )
    New-Item -ItemType Directory -Force -Path $LogRoot | Out-Null
    if ([string]::IsNullOrWhiteSpace($WorkingDirectory)) { $WorkingDirectory = $LogRoot }
    $token = [Guid]::NewGuid().ToString("N")
    $stdoutPath = Join-Path $LogRoot ("process-" + $token + ".out.log")
    $stderrPath = Join-Path $LogRoot ("process-" + $token + ".err.log")
    $argumentLine = (@($Arguments) | ForEach-Object { ConvertTo-OwVoiceProcessArgument ([string]$_) }) -join " "
    $watch = [Diagnostics.Stopwatch]::StartNew()
    $script:OwVoiceLastProcessFailureDetail = ""
    $script:OwVoiceLastProcessFailureLogs = ""
    $process = $null
    $accepted = $false
    try {
        $startInfo = New-Object System.Diagnostics.ProcessStartInfo
        $startInfo.FileName = $FilePath
        $startInfo.Arguments = $argumentLine
        $startInfo.WorkingDirectory = $WorkingDirectory
        $startInfo.UseShellExecute = $false
        $startInfo.CreateNoWindow = $true
        $startInfo.RedirectStandardOutput = $true
        $startInfo.RedirectStandardError = $true
        $startInfo.StandardOutputEncoding = [System.Text.Encoding]::UTF8
        $startInfo.StandardErrorEncoding = [System.Text.Encoding]::UTF8
        $process = New-Object System.Diagnostics.Process
        $process.StartInfo = $startInfo
        if (-not $process.Start()) { throw "无法启动子进程：$FilePath" }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        while (-not $process.HasExited) {
            Write-Progress -Activity $Activity -Status ("已用时 {0:N0} 秒，请保持窗口运行" -f $watch.Elapsed.TotalSeconds)
            Start-Sleep -Seconds 1
            $process.Refresh()
        }
        $process.WaitForExit()
        Write-Progress -Activity $Activity -Completed
        $stdoutText = [string]$stdoutTask.GetAwaiter().GetResult()
        $stderrText = [string]$stderrTask.GetAwaiter().GetResult()
        [System.IO.File]::WriteAllText($stdoutPath, $stdoutText, $script:OwVoiceEnvironmentUtf8)
        [System.IO.File]::WriteAllText($stderrPath, $stderrText, $script:OwVoiceEnvironmentUtf8)
        $stdoutLines = @($stdoutText -split "`r?`n")
        $stderrLines = @($stderrText -split "`r?`n")
        foreach ($outputPath in @($stdoutPath, $stderrPath)) {
            if (Test-Path -LiteralPath $outputPath -PathType Leaf) {
                Get-Content -LiteralPath $outputPath -Encoding UTF8 | ForEach-Object { Write-Host $_ }
            }
        }
        $accepted = [int]$process.ExitCode -in @($AcceptExitCodes)
        if (-not $accepted) {
            $detailLines = @($stderrLines + $stdoutLines | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) } | Select-Object -Last 20)
            $script:OwVoiceLastProcessFailureDetail = ($detailLines -join " | ").Trim()
            $script:OwVoiceLastProcessFailureLogs = "$stdoutPath；$stderrPath"
        }
        return [int]$process.ExitCode
    } catch {
        if ([string]::IsNullOrWhiteSpace($script:OwVoiceLastProcessFailureDetail)) {
            $script:OwVoiceLastProcessFailureDetail = ([string]$_.Exception.Message).Trim()
        }
        $script:OwVoiceLastProcessFailureLogs = "$stdoutPath；$stderrPath"
        throw
    } finally {
        $watch.Stop()
        Write-Progress -Activity $Activity -Completed
        if ($null -ne $process) { $process.Dispose() }
        if ($accepted) { Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue }
    }
}

function Get-OwVoiceLastProcessFailureMessage {
    if (-not [string]::IsNullOrWhiteSpace($script:OwVoiceLastProcessFailureDetail)) {
        return "$($script:OwVoiceLastProcessFailureDetail)（子进程日志：$($script:OwVoiceLastProcessFailureLogs)）"
    }
    if (-not [string]::IsNullOrWhiteSpace($script:OwVoiceLastProcessFailureLogs)) {
        return "子进程未返回可读错误；日志：$($script:OwVoiceLastProcessFailureLogs)"
    }
    return ""
}

function New-OwVoiceCandidateEnvironment {
    param([Parameter(Mandatory=$true)][string]$ProjectRoot, [Parameter(Mandatory=$true)][string]$TransactionId, [Parameter(Mandatory=$true)][string]$CandidatePath, [Parameter(Mandatory=$true)][ValidateSet("CPU", "GPU")][string]$Mode, [bool]$WithTraining=$false, [string]$UvPath="")
    $paths = Get-OwVoiceEnvironmentPath $ProjectRoot
    try { $candidate = [System.IO.Path]::GetFullPath($CandidatePath) } catch { return [PSCustomObject]@{ ok=$false; code="ENV_CANDIDATE_PATH_INVALID"; message="候选环境路径无效。" } }
    if (-not $candidate.Equals([System.IO.Path]::GetFullPath($paths.Candidate), [System.StringComparison]::OrdinalIgnoreCase)) { return [PSCustomObject]@{ ok=$false; code="ENV_CANDIDATE_PATH_INVALID"; message="候选环境必须是项目目录下的 .venv.next。" } }
    $parsed = [Guid]::Empty
    if (-not [Guid]::TryParse($TransactionId, [ref]$parsed)) { return [PSCustomObject]@{ ok=$false; code="ENV_TRANSACTION_ID_INVALID"; message="事务 ID 无效。" } }
    if (Test-Path -LiteralPath $candidate) { return [PSCustomObject]@{ ok=$false; code="ENV_CANDIDATE_EXISTS"; message="候选环境已存在，拒绝自动删除。" } }
    if ([string]::IsNullOrWhiteSpace($UvPath)) { $UvPath = Join-Path $paths.Root "tools\uv\uv.exe" }
    if (-not (Test-Path -LiteralPath $UvPath -PathType Leaf)) { return [PSCustomObject]@{ ok=$false; code="ENV_UV_MISSING"; message="缺少固定版本 uv。" } }
    $spec = Read-OwVoiceEnvironmentSpec $paths.Root
    $lock = $null; $created = $false
    $savedProjectEnvironment = $env:UV_PROJECT_ENVIRONMENT; $savedPythonInstallDir = $env:UV_PYTHON_INSTALL_DIR; $savedCacheDir = $env:UV_CACHE_DIR
    try {
        $lock = Enter-OwVoiceEnvironmentLock $paths.Lock
        $runtimePython = Join-Path $paths.Root ".runtime\python"; $cache = Join-Path $paths.Root ".cache\uv"
        New-Item -ItemType Directory -Force -Path $cache | Out-Null
        $env:UV_PROJECT_ENVIRONMENT = $candidate; $env:UV_CACHE_DIR = $cache
        $source = "fresh"
        $currentPython = Join-Path $paths.Venv "Scripts\python.exe"
        $currentInfo = Get-OwVoicePythonInfo $currentPython
        if ((Test-Path -LiteralPath $paths.Venv -PathType Container) -and (Test-OwVoicePythonCompatible $currentInfo $spec.python)) {
            Write-Host "正在复制并复用旧环境；文件较多时需要几分钟，请勿关闭窗口。" -ForegroundColor Cyan
            $copyWatch = [Diagnostics.Stopwatch]::StartNew()
            $copyExitCode = Invoke-OwVoiceTrackedProcess -FilePath "robocopy.exe" -Arguments @($paths.Venv, $candidate, "/E", "/COPY:DAT", "/DCOPY:DAT", "/R:2", "/W:1", "/NFL", "/NDL", "/NJH", "/NJS", "/NP") -Activity "正在复制并复用旧环境" -LogRoot $paths.Runtime -WorkingDirectory $paths.Root -AcceptExitCodes @(0, 1, 2, 3, 4, 5, 6, 7)
            $copyWatch.Stop()
            if ($copyExitCode -gt 7) { throw "旧环境复制失败，robocopy 退出码：$copyExitCode" }
            Write-Host ("旧环境复制完成，已用时 {0:N0} 秒。" -f $copyWatch.Elapsed.TotalSeconds) -ForegroundColor Green
            $source = "cloned"
        } else {
            $existingPython = Get-OwVoiceExistingPythonPath -PythonSpec $spec.python
            if ([string]::IsNullOrWhiteSpace($existingPython)) { throw "未找到兼容的 CPython 3.10.x x64。新安装推荐使用 $($spec.python.version)。" }
            Write-Host "正在创建候选 Python 3.10.x 环境。" -ForegroundColor Cyan
            $venvExitCode = Invoke-OwVoiceTrackedProcess -FilePath $UvPath -Arguments @("venv", $candidate, "--python", $existingPython) -Activity "正在创建候选 Python 环境" -LogRoot $paths.Runtime
            if ($venvExitCode -ne 0) { throw "候选 Python 环境创建失败。" }
        }
        if (-not (Test-Path -LiteralPath $candidate -PathType Container)) { throw "候选 Python 环境创建失败。" }
        $created = $true
        $arguments = @("sync", "--project", $paths.Root, "--frozen", "--extra", $Mode.ToLowerInvariant(), "--no-dev", "--no-install-project")
        if ($WithTraining) { $arguments += @("--extra", [string]$spec.dependencies.training_extra) }
        Write-Host "正在按锁文件同步依赖；出现 Uninstalled 表示移除旧环境中不再需要的包，不是在卸载 OwVoice。" -ForegroundColor Cyan
        $syncWatch = [Diagnostics.Stopwatch]::StartNew()
        $syncExitCode = Invoke-OwVoiceTrackedProcess -FilePath $UvPath -Arguments $arguments -Activity "正在按锁文件同步依赖" -LogRoot $paths.Runtime
        $syncWatch.Stop()
        if ($syncExitCode -ne 0) { throw "候选环境依赖安装失败。" }
        Write-Host ("依赖同步完成，已用时 {0:N0} 秒。" -f $syncWatch.Elapsed.TotalSeconds) -ForegroundColor Green
        return [PSCustomObject]@{ ok=$true; code="OK"; phase="candidate_created"; transaction_id=$TransactionId; candidate_path=$candidate; mode=$Mode.ToUpperInvariant(); with_training=$WithTraining; source=$source }
    } catch {
        if ($created -and (Test-Path -LiteralPath $candidate)) { Remove-Item -LiteralPath $candidate -Recurse -Force -ErrorAction SilentlyContinue }
        return [PSCustomObject]@{ ok=$false; code="ENV_CANDIDATE_CREATE_FAILED"; message=$_.Exception.Message; transaction_id=$TransactionId }
    } finally {
        if ($null -ne $lock) { $lock.Dispose() }
        if ($null -eq $savedProjectEnvironment) { Remove-Item Env:UV_PROJECT_ENVIRONMENT -ErrorAction SilentlyContinue } else { $env:UV_PROJECT_ENVIRONMENT = $savedProjectEnvironment }
        if ($null -eq $savedPythonInstallDir) { Remove-Item Env:UV_PYTHON_INSTALL_DIR -ErrorAction SilentlyContinue } else { $env:UV_PYTHON_INSTALL_DIR = $savedPythonInstallDir }
        if ($null -eq $savedCacheDir) { Remove-Item Env:UV_CACHE_DIR -ErrorAction SilentlyContinue } else { $env:UV_CACHE_DIR = $savedCacheDir }
    }
}

function Test-OwVoiceCandidateEnvironment {
    param([Parameter(Mandatory=$true)][string]$ProjectRoot, [Parameter(Mandatory=$true)][string]$TransactionId, [Parameter(Mandatory=$true)][string]$CandidatePath, [Parameter(Mandatory=$true)][ValidateSet("CPU", "GPU")][string]$Mode, [bool]$WithTraining=$false, [switch]$VerifyProjectResources)
    $paths = Get-OwVoiceEnvironmentPath $ProjectRoot; $candidate = [System.IO.Path]::GetFullPath($CandidatePath)
    if (-not $candidate.Equals([System.IO.Path]::GetFullPath($paths.Candidate), [System.StringComparison]::OrdinalIgnoreCase)) { return [PSCustomObject]@{ ok=$false; code="ENV_CANDIDATE_PATH_INVALID"; message="候选环境路径无效。" } }
    $python = Join-Path $candidate "Scripts\python.exe"; $verifier = Join-Path $paths.Root "scripts\environment\verify_environment.py"; $runtimeVerifier = Join-Path $paths.Root "scripts\verify_runtime.py"
    if (-not (Test-Path -LiteralPath $python -PathType Leaf) -or -not (Test-Path -LiteralPath $verifier -PathType Leaf) -or -not (Test-Path -LiteralPath $runtimeVerifier -PathType Leaf)) { return [PSCustomObject]@{ ok=$false; code="ENV_CANDIDATE_INCOMPLETE"; message="候选环境缺少 Python 或验证器。" } }
    $outputPath = Join-Path $paths.Runtime ("environment-candidate-" + $TransactionId + ".json")
    try {
        New-Item -ItemType Directory -Force -Path $paths.Runtime | Out-Null
        $verifyArgs = @($verifier, "--project-root", $paths.Root, "--mode", $Mode.ToLowerInvariant(), "--output", $outputPath)
        if ($WithTraining) { $verifyArgs += "--with-training" }
        $verifyExitCode = Invoke-OwVoiceTrackedProcess -FilePath $python -Arguments $verifyArgs -Activity "正在校验候选环境依赖" -LogRoot $paths.Runtime -WorkingDirectory $paths.Root
        if ($verifyExitCode -ne 0 -or -not (Test-Path -LiteralPath $outputPath -PathType Leaf)) {
            $failure = Get-OwVoiceLastProcessFailureMessage
            if ([string]::IsNullOrWhiteSpace($failure)) { $failure = "候选环境校验器未生成结果。" }
            throw $failure
        }
        $result = Get-Content -LiteralPath $outputPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($result.ok -ne $true) { return [PSCustomObject]@{ ok=$false; code="ENV_CANDIDATE_VERIFY_FAILED"; result=$result } }
        if ($VerifyProjectResources) {
            $runtimeArgs = @($runtimeVerifier, "--project-dir", $paths.Root)
            if ($WithTraining) { $runtimeArgs += "--training" }
            $runtimeExitCode = Invoke-OwVoiceTrackedProcess -FilePath $python -Arguments $runtimeArgs -Activity "正在校验候选环境运行资源" -LogRoot $paths.Runtime -WorkingDirectory $paths.Root
            if ($runtimeExitCode -ne 0) { return [PSCustomObject]@{ ok=$false; code="ENV_CANDIDATE_RUNTIME_VERIFY_FAILED"; message=(Get-OwVoiceLastProcessFailureMessage); result=$result } }
        }
        return [PSCustomObject]@{ ok=$true; code="OK"; phase="candidate_verified"; transaction_id=$TransactionId; candidate_path=$candidate; state=$result }
    } catch { return [PSCustomObject]@{ ok=$false; code="ENV_CANDIDATE_VERIFY_FAILED"; message=$_.Exception.Message; transaction_id=$TransactionId } }
}

function Get-OwVoiceEnvironmentJournalPath { param($Paths, [string]$TransactionId) return Join-Path $Paths.JournalRoot ($TransactionId + ".json") }

function Switch-OwVoiceEnvironment {
    param([Parameter(Mandatory=$true)][string]$ProjectRoot, [Parameter(Mandatory=$true)][string]$TransactionId, [Parameter(Mandatory=$true)][string]$CandidatePath, [Parameter(Mandatory=$true)]$State)
    $paths = Get-OwVoiceEnvironmentPath $ProjectRoot; $candidate = [System.IO.Path]::GetFullPath($CandidatePath)
    if (-not $candidate.Equals([System.IO.Path]::GetFullPath($paths.Candidate), [System.StringComparison]::OrdinalIgnoreCase) -or -not (Test-Path -LiteralPath $candidate -PathType Container)) { return [PSCustomObject]@{ ok=$false; code="ENV_CANDIDATE_PATH_INVALID"; message="候选环境不存在或路径无效。" } }
    if (Test-Path -LiteralPath $paths.OldVenv) { return [PSCustomObject]@{ ok=$false; code="ENV_OLD_ENVIRONMENT_EXISTS"; message="已有未完成环境事务，拒绝覆盖 .venv.old。" } }
    $parsed = [Guid]::Empty; if (-not [Guid]::TryParse($TransactionId, [ref]$parsed)) { return [PSCustomObject]@{ ok=$false; code="ENV_TRANSACTION_ID_INVALID"; message="事务 ID 无效。" } }
    $journalPath = Get-OwVoiceEnvironmentJournalPath $paths $TransactionId; if (Test-Path -LiteralPath $journalPath) { return [PSCustomObject]@{ ok=$false; code="ENV_TRANSACTION_EXISTS"; message="环境事务已存在。" } }
    $lock = $null; $movedOld = $false; $movedCandidate = $false; $stateBackup = Join-Path $paths.Runtime ("environment-state-" + $TransactionId + ".bak")
    try {
        $lock = Enter-OwVoiceEnvironmentLock $paths.Lock
        $journal = [ordered]@{ schema=1; transaction_id=$TransactionId; phase="prepared"; project_root=$paths.Root; candidate_path=$candidate; current_path=$paths.Venv; old_path=$paths.OldVenv; state_path=$paths.State; state_backup_path=$stateBackup; previous_state_exists=(Test-Path -LiteralPath $paths.State -PathType Leaf) }
        Write-OwVoiceEnvironmentJsonAtomic -Path $journalPath -Value $journal
        if (Test-Path -LiteralPath $paths.Venv) { Move-Item -LiteralPath $paths.Venv -Destination $paths.OldVenv -Force; $movedOld = $true }
        Move-Item -LiteralPath $candidate -Destination $paths.Venv -Force; $movedCandidate = $true
        if (Test-Path -LiteralPath $paths.State -PathType Leaf) { Move-Item -LiteralPath $paths.State -Destination $stateBackup -Force }
        $stateObject = [ordered]@{}; foreach ($property in $State.PSObject.Properties) { $stateObject[$property.Name] = $property.Value }
        $stateObject["schema"] = $script:OwVoiceEnvironmentSchema; $stateObject["project_root"] = $paths.Root; $stateObject["transaction_id"] = $TransactionId; $stateObject["updated_at"] = [DateTime]::UtcNow.ToString("o")
        Write-OwVoiceEnvironmentJsonAtomic -Path $paths.State -Value $stateObject
        $journal.phase = "switched"; Write-OwVoiceEnvironmentJsonAtomic -Path $journalPath -Value $journal
        return [PSCustomObject]@{ ok=$true; code="OK"; phase="switched"; transaction_id=$TransactionId; state_path=$paths.State }
    } catch {
        if ($movedCandidate -and (Test-Path -LiteralPath $paths.Venv) -and -not (Test-Path -LiteralPath $candidate)) { Move-Item -LiteralPath $paths.Venv -Destination $candidate -Force }
        if ($movedOld -and (Test-Path -LiteralPath $paths.OldVenv) -and -not (Test-Path -LiteralPath $paths.Venv)) { Move-Item -LiteralPath $paths.OldVenv -Destination $paths.Venv -Force }
        if (Test-Path -LiteralPath $stateBackup) { Move-Item -LiteralPath $stateBackup -Destination $paths.State -Force }
        if (Test-Path -LiteralPath $journalPath) { Remove-Item -LiteralPath $journalPath -Force -ErrorAction SilentlyContinue }
        return [PSCustomObject]@{ ok=$false; code="ENV_SWITCH_FAILED"; message=$_.Exception.Message; transaction_id=$TransactionId }
    } finally { if ($null -ne $lock) { $lock.Dispose() } }
}

function Restore-OwVoiceEnvironment {
    param([Parameter(Mandatory=$true)][string]$ProjectRoot, [Parameter(Mandatory=$true)][string]$TransactionId)
    $paths = Get-OwVoiceEnvironmentPath $ProjectRoot; $journalPath = Get-OwVoiceEnvironmentJournalPath $paths $TransactionId
    if (-not (Test-Path -LiteralPath $journalPath -PathType Leaf)) { return [PSCustomObject]@{ ok=$false; code="ENV_TRANSACTION_NOT_FOUND"; message="找不到环境事务。" } }
    try { $journal = Get-Content -LiteralPath $journalPath -Raw -Encoding UTF8 | ConvertFrom-Json } catch { return [PSCustomObject]@{ ok=$false; code="ENV_JOURNAL_INVALID"; message="环境事务记录损坏。" } }
    $lock = $null
    try {
        $lock = Enter-OwVoiceEnvironmentLock $paths.Lock
        if (Test-Path -LiteralPath $paths.Venv) { Remove-Item -LiteralPath $paths.Venv -Recurse -Force }
        if (Test-Path -LiteralPath $paths.Candidate) { Remove-Item -LiteralPath $paths.Candidate -Recurse -Force }
        if (Test-Path -LiteralPath $paths.OldVenv) { Move-Item -LiteralPath $paths.OldVenv -Destination $paths.Venv -Force }
        if (Test-Path -LiteralPath ([string]$journal.state_backup_path)) { Move-Item -LiteralPath ([string]$journal.state_backup_path) -Destination $paths.State -Force } elseif ($journal.previous_state_exists -ne $true -and (Test-Path -LiteralPath $paths.State)) { Remove-Item -LiteralPath $paths.State -Force }
        Remove-Item -LiteralPath $journalPath -Force
        return [PSCustomObject]@{ ok=$true; code="OK"; phase="rolled_back"; transaction_id=$TransactionId }
    } catch { return [PSCustomObject]@{ ok=$false; code="ENV_RESTORE_FAILED"; message=$_.Exception.Message; transaction_id=$TransactionId } } finally { if ($null -ne $lock) { $lock.Dispose() } }
}

function Complete-OwVoiceEnvironmentTransaction {
    param([Parameter(Mandatory=$true)][string]$ProjectRoot, [Parameter(Mandatory=$true)][string]$TransactionId, $State=$null)
    $paths = Get-OwVoiceEnvironmentPath $ProjectRoot; $journalPath = Get-OwVoiceEnvironmentJournalPath $paths $TransactionId
    if (-not (Test-Path -LiteralPath $journalPath -PathType Leaf)) {
        if ($null -ne $State) { Write-OwVoiceEnvironmentJsonAtomic -Path $paths.State -Value $State; return [PSCustomObject]@{ ok=$true; code="OK"; phase="state_saved"; transaction_id=$TransactionId } }
        return [PSCustomObject]@{ ok=$false; code="ENV_TRANSACTION_NOT_FOUND"; message="找不到环境事务。" }
    }
    $lock = $null
    try { $lock = Enter-OwVoiceEnvironmentLock $paths.Lock; if (Test-Path -LiteralPath $paths.OldVenv) { Remove-Item -LiteralPath $paths.OldVenv -Recurse -Force }; $journal = Get-Content -LiteralPath $journalPath -Raw -Encoding UTF8 | ConvertFrom-Json; if (Test-Path -LiteralPath ([string]$journal.state_backup_path)) { Remove-Item -LiteralPath ([string]$journal.state_backup_path) -Force }; Remove-Item -LiteralPath $journalPath -Force; return [PSCustomObject]@{ ok=$true; code="OK"; phase="committed"; transaction_id=$TransactionId } } catch { return [PSCustomObject]@{ ok=$false; code="ENV_COMPLETE_FAILED"; message=$_.Exception.Message; transaction_id=$TransactionId } } finally { if ($null -ne $lock) { $lock.Dispose() } }
}

function Remove-OwVoiceEnvironmentTemporaryFiles {
    param([Parameter(Mandatory=$true)][string]$ProjectRoot, [Parameter(Mandatory=$true)][string]$TransactionId)
    $paths = Get-OwVoiceEnvironmentPath $ProjectRoot; $journalPath = Get-OwVoiceEnvironmentJournalPath $paths $TransactionId
    if (-not (Test-Path -LiteralPath $journalPath -PathType Leaf)) { return [PSCustomObject]@{ ok=$true; code="OK"; phase="nothing_to_remove" } }
    try { $journal = Get-Content -LiteralPath $journalPath -Raw -Encoding UTF8 | ConvertFrom-Json; $candidate = [System.IO.Path]::GetFullPath([string]$journal.candidate_path); if ($candidate.Equals([System.IO.Path]::GetFullPath($paths.Candidate), [System.StringComparison]::OrdinalIgnoreCase) -and (Test-Path -LiteralPath $candidate)) { Remove-Item -LiteralPath $candidate -Recurse -Force }; Remove-Item -LiteralPath $journalPath -Force; return [PSCustomObject]@{ ok=$true; code="OK"; phase="temporary_files_removed"; transaction_id=$TransactionId } } catch { return [PSCustomObject]@{ ok=$false; code="ENV_TEMPORARY_CLEANUP_FAILED"; message=$_.Exception.Message; transaction_id=$TransactionId } }
}

Export-ModuleMember -Function @("Read-OwVoiceEnvironmentSpec", "Get-OwVoiceEnvironmentState", "Get-OwVoiceEnvironmentPlan", "Get-OwVoiceEnvironmentFingerprint", "Get-OwVoicePythonInfo", "Test-OwVoicePythonCompatible", "New-OwVoiceEnvironmentState", "Get-OwVoiceAvailableFreeBytes", "Test-OwVoiceEnvironmentPreflight", "Invoke-OwVoiceTrackedProcess", "Get-OwVoiceLastProcessFailureMessage", "New-OwVoiceCandidateEnvironment", "Test-OwVoiceCandidateEnvironment", "Switch-OwVoiceEnvironment", "Restore-OwVoiceEnvironment", "Complete-OwVoiceEnvironmentTransaction", "Remove-OwVoiceEnvironmentTemporaryFiles")
