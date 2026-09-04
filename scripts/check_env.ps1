param(
    [string]$OutputPath = ""
)

$ErrorActionPreference = "SilentlyContinue"
$projectDir = Split-Path -Parent $PSScriptRoot
Set-Location $projectDir
if ([string]::IsNullOrWhiteSpace($OutputPath)) { $OutputPath = Join-Path $projectDir "environment-report.txt" }

$report = [System.Collections.Generic.List[string]]::new()
$report.Add("OwVoice environment report")
$report.Add("Generated: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz')")
$report.Add("Project: $projectDir")
$report.Add("OS: $([Environment]::OSVersion.VersionString)")
$report.Add("OS architecture: $env:PROCESSOR_ARCHITECTURE / process=$env:PROCESSOR_ARCHITEW6432")

$pythonCommands = @("py.exe", "python.exe")
foreach ($commandName in $pythonCommands) {
    $command = Get-Command $commandName
    if ($null -ne $command) {
        $info = (& $command.Source -c "import platform, struct, sys; print('version=%d.%d.%d;bits=%d;implementation=%s' % (sys.version_info[0], sys.version_info[1], sys.version_info[2], struct.calcsize('P') * 8, platform.python_implementation()))")
        $report.Add("$commandName path: $($command.Source)")
        $report.Add("$commandName info: $($info -join ' ')")
    } else {
        $report.Add("${commandName}: not found")
    }
}

$venvPython = Join-Path $projectDir ".venv\Scripts\python.exe"
if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
    $venvInfo = (& $venvPython -c "import platform, struct, sys; print('version=%d.%d.%d;bits=%d;implementation=%s' % (sys.version_info[0], sys.version_info[1], sys.version_info[2], struct.calcsize('P') * 8, platform.python_implementation()))")
    $report.Add("venv path: $venvPython")
    $report.Add("venv info: $($venvInfo -join ' ')")
    $torchInfo = (& $venvPython -c "import torch; print('torch=%s;cuda_build=%s;cuda_available=%s' % (torch.__version__, torch.version.cuda, torch.cuda.is_available()))")
    if ($LASTEXITCODE -eq 0) { $report.Add("venv torch: $($torchInfo -join ' ')") } else { $report.Add("venv torch: import failed") }
} else {
    $report.Add("venv path: not found")
}

$nvidia = Get-Command nvidia-smi.exe
if ($null -ne $nvidia) {
    $report.Add("nvidia-smi path: $($nvidia.Source)")
    $nvidiaInfo = (& $nvidia.Source --query-gpu=name,driver_version,memory.total --format=csv,noheader)
    $report.Add("nvidia-smi info: $($nvidiaInfo -join ' | ')")
} else {
    $report.Add("nvidia-smi: not found")
}

$report.Add("requirements-cpu.txt: $(Test-Path -LiteralPath (Join-Path $projectDir 'requirements-cpu.txt'))")
$report.Add("requirements-gpu.txt: $(Test-Path -LiteralPath (Join-Path $projectDir 'requirements-gpu.txt'))")
$report.Add("GPT-SoVITS api.py: $(Test-Path -LiteralPath (Join-Path $projectDir 'GPT-SoVITS\api.py'))")

$report | Tee-Object -FilePath $OutputPath -Encoding UTF8
Write-Host "报告已保存到：$OutputPath" -ForegroundColor Green
