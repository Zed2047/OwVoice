param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = "Stop"
$root = [System.IO.Path]::GetFullPath($ProjectRoot)
$scriptDirectories = @(
    (Join-Path $root "scripts"),
    (Join-Path $root "updater")
)
$batchFiles = @()
foreach ($directory in @($root) + $scriptDirectories) {
    if (-not (Test-Path -LiteralPath $directory -PathType Container)) { continue }
    foreach ($pattern in @("*.bat", "*.cmd")) {
        $search = if ($directory -eq $root) { Get-ChildItem -LiteralPath $directory -Filter $pattern -File } else { Get-ChildItem -LiteralPath $directory -Filter $pattern -Recurse -File }
        $batchFiles += @($search)
    }
}
$files = @()
foreach ($directory in $scriptDirectories) {
    if (-not (Test-Path -LiteralPath $directory -PathType Container)) { continue }
    foreach ($pattern in @("*.ps1", "*.psm1", "*.psd1")) {
        $files += @(Get-ChildItem -LiteralPath $directory -Filter $pattern -Recurse -File)
    }
}
$failures = New-Object System.Collections.Generic.List[string]
foreach ($file in $batchFiles) {
    $bytes = [System.IO.File]::ReadAllBytes($file.FullName)
    if (@($bytes | Where-Object { $_ -gt 0x7F }).Count -gt 0) {
        [void]$failures.Add("批处理文件必须只使用 ASCII：$($file.FullName)")
    }
    $content = [System.Text.Encoding]::ASCII.GetString($bytes)
    if ($content -match "(?<!`r)`n" -or $content -match "`r(?!`n)") {
        [void]$failures.Add("批处理文件不是 CRLF 换行：$($file.FullName)")
    }
}
foreach ($file in $files) {
    $bytes = [System.IO.File]::ReadAllBytes($file.FullName)
    $hasBom = $bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF
    if (-not $hasBom) {
        [void]$failures.Add("缺少 UTF-8 BOM：$($file.FullName)")
        continue
    }
    $content = [System.Text.Encoding]::UTF8.GetString($bytes)
    if ($content -match "(?<!`r)`n" -or $content -match "`r(?!`n)") {
        [void]$failures.Add("不是 CRLF 换行：$($file.FullName)")
    }
    $tokens = $null
    $parseErrors = $null
    [System.Management.Automation.Language.Parser]::ParseFile($file.FullName, [ref]$tokens, [ref]$parseErrors) | Out-Null
    if ($parseErrors.Count -gt 0) {
        [void]$failures.Add("PowerShell 语法错误：$($file.FullName)（$($parseErrors.Count) 个）")
    }
}
foreach ($moduleFile in ($files | Where-Object { $_.Extension -in @(".psm1", ".psd1") })) {
    $loadedModules = @()
    try {
        $loadedModules = @(Import-Module -Name $moduleFile.FullName -Force -PassThru -ErrorAction Stop)
    } catch {
        [void]$failures.Add("PowerShell 模块导入失败：$($moduleFile.FullName)（$($_.Exception.Message)）")
    } finally {
        foreach ($loadedModule in $loadedModules) {
            Remove-Module -ModuleInfo $loadedModule -Force -ErrorAction SilentlyContinue
        }
    }
}
if ($failures.Count -gt 0) {
    $failures | ForEach-Object { Write-Error $_ }
    exit 1
}
Write-Host ("文本兼容性检查通过：{0} 个 PowerShell 脚本、{1} 个批处理文件。" -f $files.Count, $batchFiles.Count) -ForegroundColor Green
