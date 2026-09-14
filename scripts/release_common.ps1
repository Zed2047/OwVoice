$script:OwVoiceSemVerPattern = '^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$'

function Get-OwVoiceReleaseIdentity {
    param(
        [Parameter(Mandatory=$true)][string]$ProjectRoot,
        [switch]$ValidateMirrors
    )

    $root = [System.IO.Path]::GetFullPath($ProjectRoot)
    $identityPath = Join-Path $root "version.json"
    if (-not (Test-Path -LiteralPath $identityPath -PathType Leaf)) {
        throw "缺少发布身份文件：$identityPath"
    }
    try {
        $identity = Get-Content -LiteralPath $identityPath -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        throw "version.json 不是有效 JSON：$($_.Exception.Message)"
    }

    $version = [string]$identity.version
    $channel = ([string]$identity.channel).ToLowerInvariant()
    $updateSchema = $identity.updateSchema
    if ($version -notmatch $script:OwVoiceSemVerPattern) { throw "version.json 的 version 无效：$version" }
    if ($channel -notin @("stable", "beta", "dev")) { throw "version.json 的 channel 无效：$channel" }
    $schemaIsInteger = ($updateSchema -is [byte]) -or ($updateSchema -is [int16]) -or
        ($updateSchema -is [int32]) -or ($updateSchema -is [int64])
    if (-not $schemaIsInteger -or [int64]$updateSchema -lt 1) {
        throw "version.json 的 updateSchema 必须是正整数。"
    }

    if ($ValidateMirrors) {
        $pyprojectPath = Join-Path $root "pyproject.toml"
        $changelogPath = Join-Path $root "CHANGELOG.md"
        if (-not (Test-Path -LiteralPath $pyprojectPath -PathType Leaf)) { throw "缺少 pyproject.toml。" }
        if (-not (Test-Path -LiteralPath $changelogPath -PathType Leaf)) { throw "缺少 CHANGELOG.md。" }
        $pyproject = Get-Content -LiteralPath $pyprojectPath -Raw -Encoding UTF8
        $projectBlock = [regex]::Match($pyproject, '(?ms)^\[project\]\s*(.*?)(?=^\[|\z)')
        $projectVersion = if ($projectBlock.Success) {
            [regex]::Match($projectBlock.Groups[1].Value, '(?m)^version\s*=\s*"([^"]+)"\s*$')
        } else { $null }
        if ($null -eq $projectVersion -or -not $projectVersion.Success -or $projectVersion.Groups[1].Value -ne $version) {
            throw "pyproject.toml 的项目版本与 version.json 不一致。"
        }
        $changelog = Get-Content -LiteralPath $changelogPath -Raw -Encoding UTF8
        $firstVersion = [regex]::Match($changelog, '(?m)^##\s+v([^\s]+)\s*$')
        if (-not $firstVersion.Success -or $firstVersion.Groups[1].Value -ne $version) {
            throw "CHANGELOG.md 的首个版本与 version.json 不一致。"
        }
    }

    return [PSCustomObject]@{
        version = $version
        tag = "v$version"
        channel = $channel
        updateSchema = [int]$updateSchema
        path = $identityPath
    }
}

function Resolve-OwVoiceReleaseTag {
    param(
        [Parameter(Mandatory=$true)]$Identity,
        [string]$RequestedVersion = ""
    )

    if ([string]::IsNullOrWhiteSpace($RequestedVersion)) { return $Identity.tag }
    $normalized = $RequestedVersion.Trim()
    if ($normalized -notmatch '^v') { $normalized = "v$normalized" }
    if ($normalized -ne $Identity.tag) {
        throw "构建版本 $normalized 与 version.json 中的 $($Identity.tag) 不一致。"
    }
    return $normalized
}

function Assert-OwVoiceReleaseGitTag {
    param(
        [Parameter(Mandatory=$true)]$Identity,
        [Parameter(Mandatory=$true)][string]$ProjectRoot,
        [string]$Commit = "HEAD"
    )

    $git = Get-Command git.exe -ErrorAction SilentlyContinue
    if ($null -eq $git) { throw "无法验证发布 tag：未找到 git.exe。" }
    $commitHash = ((& $git.Source -C $ProjectRoot rev-parse "$Commit`^{commit}" 2>&1) | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) { throw "无法解析待发布提交：$Commit" }
    $tagHash = ((& $git.Source -C $ProjectRoot rev-parse "$($Identity.tag)`^{commit}" 2>&1) | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) { throw "正式发布前必须先创建 tag：$($Identity.tag)" }
    if ($tagHash -ne $commitHash) {
        throw "tag $($Identity.tag) 未指向待发布提交 $commitHash。"
    }
}
