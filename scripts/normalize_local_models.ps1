param(
    [string]$ProjectDir = (Split-Path -Parent $PSScriptRoot),
    [string]$SourceRoot = "",
    [string]$ConfigPath = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$project = (Resolve-Path -LiteralPath $ProjectDir).Path
if ([string]::IsNullOrWhiteSpace($SourceRoot)) {
    $SourceRoot = Join-Path $project "models"
}
if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
    $ConfigPath = Join-Path $project "config\voices.local.json"
}
$sourceRootPath = (Resolve-Path -LiteralPath $SourceRoot).Path
$targetRoot = Join-Path $project "data\models"
$registryPath = Join-Path $targetRoot "installed-models.json"

if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
    throw "找不到人物配置：$ConfigPath"
}
$config = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
$voices = @($config.voices | Where-Object { $_.enabled -ne $false })
if ($voices.Count -eq 0) {
    throw "voices.local.json 中没有启用的角色。"
}

function Resolve-ProjectFile([string]$value) {
    if ([string]::IsNullOrWhiteSpace($value)) {
        throw "模型配置存在空路径。"
    }
    if ([IO.Path]::IsPathRooted($value)) {
        return (Resolve-Path -LiteralPath $value).Path
    }
    return (Resolve-Path -LiteralPath (Join-Path $project $value)).Path
}

function Assert-UnderRoot([string]$path, [string]$root, [string]$label) {
    $fullPath = [IO.Path]::GetFullPath($path).TrimEnd('\')
    $fullRoot = [IO.Path]::GetFullPath($root).TrimEnd('\') + '\'
    if (-not $fullPath.StartsWith($fullRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "$label 不在旧模型目录内：$path"
    }
}

function Get-FirstValue($object, [string]$firstProperty, [string]$secondProperty, [string]$fallback) {
    $first = [string]$object.$firstProperty
    if (-not [string]::IsNullOrWhiteSpace($first)) {
        return $first
    }
    $second = [string]$object.$secondProperty
    if (-not [string]::IsNullOrWhiteSpace($second)) {
        return $second
    }
    return $fallback
}

$entries = @()
New-Item -ItemType Directory -Force -Path $targetRoot | Out-Null

foreach ($voice in $voices) {
    $id = [string]$voice.id
    if ($id -notmatch '^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$') {
        throw "非法模型 ID：$id"
    }

    $gptSource = Resolve-ProjectFile ([string]$voice.gpt_model)
    $sovitsSource = Resolve-ProjectFile ([string]$voice.sovits_model)
    $referenceSource = Resolve-ProjectFile ([string]$voice.reference_audio)
    foreach ($path in @($gptSource, $sovitsSource, $referenceSource)) {
        Assert-UnderRoot $path $sourceRootPath "模型文件"
    }

    $target = Join-Path $targetRoot $id
    if ((Test-Path -LiteralPath $target) -and -not $Force) {
        throw "目标模型已存在：$target；如需覆盖请显式使用 -Force。"
    }

    $stage = Join-Path $targetRoot (".{0}.staging-{1}" -f $id, [guid]::NewGuid().ToString('N'))
    try {
        New-Item -ItemType Directory -Force -Path (Join-Path $stage "GPT_weights"), (Join-Path $stage "SoVITS_weights") | Out-Null
        Copy-Item -LiteralPath $gptSource -Destination (Join-Path $stage "GPT_weights\model.ckpt")
        Copy-Item -LiteralPath $sovitsSource -Destination (Join-Path $stage "SoVITS_weights\model.pth")
        Copy-Item -LiteralPath $referenceSource -Destination (Join-Path $stage "reference.wav")

        $avatarName = ""
        if (-not [string]::IsNullOrWhiteSpace([string]$voice.avatar)) {
            try {
                $avatarSource = Resolve-ProjectFile ([string]$voice.avatar)
                $extension = [IO.Path]::GetExtension($avatarSource).ToLowerInvariant()
                if ($extension -in @('.png', '.jpg', '.jpeg', '.webp', '.svg')) {
                    $avatarName = "avatar$extension"
                    Copy-Item -LiteralPath $avatarSource -Destination (Join-Path $stage $avatarName)
                }
            } catch [System.Management.Automation.ItemNotFoundException] {
                # 头像是可选展示素材，缺失时由客户端生成占位头像。
            }
        }

        $metadata = [ordered]@{
            id = $id
            name = Get-FirstValue $voice "display_name" "name" $id
            display_name = Get-FirstValue $voice "display_name" "name" $id
            version = "0.0.0-local"
            avatar = $avatarName
            locale = [string]$voice.locale
            mode = [string]$voice.mode
            hero = [string]$voice.hero
            gpt_model = "GPT_weights/model.ckpt"
            sovits_model = "SoVITS_weights/model.pth"
            reference_audio = "reference.wav"
            prompt_text = [string]$voice.prompt_text
            prompt_texts = @($voice.prompt_texts)
            prompt_language = [string]$voice.prompt_language
            enabled = $true
            inference = $voice.inference
            license_note = "用户本地模型；请自行确认训练音频、参考音频和模型的授权。"
        }
        $metadata | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $stage "model.json") -Encoding UTF8

        if (Test-Path -LiteralPath $target) {
            Remove-Item -LiteralPath $target -Recurse -Force
        }
        Move-Item -LiteralPath $stage -Destination $target
        $files = @(Get-ChildItem -LiteralPath $target -Recurse -File | ForEach-Object {
            $_.FullName.Substring($target.Length).TrimStart('\').Replace('\', '/')
        }) | Sort-Object
        $entries += [ordered]@{
            id = $id
            name = [string]$metadata.name
            version = [string]$metadata.version
            path = "models/$id"
            files = $files
            sha256 = ""
            minAppVersion = "0.0.0"
            minEngineVersion = "0.0.0"
        }
        Write-Host "已规范化：$id -> $target" -ForegroundColor Green
    } finally {
        if (Test-Path -LiteralPath $stage) {
            Remove-Item -LiteralPath $stage -Recurse -Force
        }
    }
}

$registry = [ordered]@{ schema = 1; models = @($entries | Sort-Object { $_.id }) }
$registry | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $registryPath -Encoding UTF8
Write-Host "注册表已写入：$registryPath" -ForegroundColor Green
Write-Host "旧目录未删除：$sourceRootPath" -ForegroundColor Yellow
