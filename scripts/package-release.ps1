<#
  The Veil · 发布包打包脚本（开发用，不随发布包分发）

  用法：
    powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\package-release.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\package-release.ps1 -Version 1.0.0

  产物：
    release/the-veil-v<版本>.zip
    release/the-veil-v<版本>.zip.sha256

  规则（与项目发布约束一致）：
    - 文件名前缀使用 the-veil，而不是 aibasedtrpg。
    - 排除密钥、虚拟环境、构建缓存、开发文档、changelog、开发脚本、本机用户数据。
    - 保留预构建前端 dist/public、部署指南、许可证汇总、配置模板、示例剧本与最小运行资源。
#>
param(
    [string]$Version = '1.0.0'
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Name = "the-veil-v$Version"
$Staging = Join-Path $env:TEMP "veil-release-$Name"
$App = Join-Path $Staging $Name
$OutDir = Join-Path $Root 'release'
$Zip = Join-Path $OutDir "$Name.zip"
$HashFile = "$Zip.sha256"

function Write-Step([string]$Text) {
    Write-Host "[package] $Text"
}

function Copy-Relative([string]$Relative) {
    $source = Join-Path $Root $Relative
    if (-not (Test-Path $source)) {
        throw "缺少必需文件/目录: $Relative"
    }
    $target = Join-Path $App $Relative
    $parent = Split-Path -Parent $target
    if (-not (Test-Path $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    Copy-Item -Path $source -Destination $target -Recurse -Force
}

function Assert-Present([string]$Relative) {
    $path = Join-Path $App $Relative
    if (-not (Test-Path $path)) {
        throw "发布包缺少必需内容: $Relative"
    }
}

function Assert-Absent([string]$Relative) {
    $path = Join-Path $App $Relative
    if (Test-Path $path) {
        throw "发布包混入了禁止内容: $Relative"
    }
}

if (Test-Path $Staging) {
    Remove-Item -Path $Staging -Recurse -Force
}
New-Item -ItemType Directory -Path $App -Force | Out-Null

Write-Step "暂存目录: $Staging"

# ---------------------------------------------------------------- 根目录文件
$rootFiles = @(
    'server.py',
    'start.cmd',
    'start-bootstrap.ps1',
    'requirements.txt',
    'requirements-vector.txt',
    'requirements-vector-qdrant.txt',
    'requirements-ocr.txt',
    'package.json',
    'package-lock.json',
    'tsconfig.json',
    'tsconfig.frontend.json',
    'tsconfig.react.json',
    'LICENSE',
    'THIRD-PARTY-LICENSES.md',
    'README.md',
    'AGENT-INSTALL.md',
    'DEPLOYMENT.md',
    '使用指南.txt'
)
foreach ($file in $rootFiles) {
    Copy-Relative $file
}

# ---------------------------------------------------------------- 运行必需目录
$runtimeDirs = @(
    'backend',
    'config',
    'dist',
    'skills',
    'frontend/src',
    'data/assets/theme',
    'data/assets/vendor',
    'data/occupations',
    'data/weapons'
)
foreach ($dir in $runtimeDirs) {
    Copy-Relative $dir
}

# ---------------------------------------------------------------- 单文件运行资源
Copy-Relative 'data/character_skills.json'
Copy-Relative 'scripts/build-frontend.mjs'
Copy-Relative 'scripts/relocate-tools.mjs'

# 示例剧本（包内改名为「示例剧本」，避免沿用系统测试命名）
Copy-Relative 'data/scenarios/scenario-1793438085450'
$sampleScenario = Join-Path $App 'data/scenarios/scenario-1793438085450'
Get-ChildItem -Path $sampleScenario -Recurse -File -Filter '*.json' | ForEach-Object {
    $text = [System.IO.File]::ReadAllText($_.FullName, [System.Text.Encoding]::UTF8)
    $updated = $text.Replace('裂隙回廊（系统测试剧本）', '裂隙回廊（示例剧本）')
    if ($updated -ne $text) {
        [System.IO.File]::WriteAllText($_.FullName, $updated, (New-Object System.Text.UTF8Encoding($false)))
        Write-Step "示例剧本标题已更新: $($_.FullName.Substring($App.Length + 1))"
    }
}

# ---------------------------------------------------------------- 清理禁止内容
$forbiddenNames = @('__pycache__', '.pytest_cache', '.DS_Store', 'Thumbs.db', '.venv', 'node_modules', '.git')
Get-ChildItem -Path $App -Recurse -Force -Directory | Where-Object { $forbiddenNames -contains $_.Name } | ForEach-Object {
    Remove-Item -Path $_.FullName -Recurse -Force
}
Get-ChildItem -Path $App -Recurse -Force -File -Include '*.pyc', '*.pyo' | ForEach-Object {
    Remove-Item -Path $_.FullName -Force
}
Get-ChildItem -Path $App -Recurse -Force -File -Include '.env' | ForEach-Object {
    Remove-Item -Path $_.FullName -Force
}

# 品牌设计草案不进入发布包（源码构建只需要正式图标）
$brandConcepts = Join-Path $App 'frontend/src/assets/brand/concepts'
if (Test-Path $brandConcepts) {
    Remove-Item -Path $brandConcepts -Recurse -Force
}

# ---------------------------------------------------------------- 必需内容断言
$required = @(
    'AGENT-INSTALL.md',
    'DEPLOYMENT.md',
    'THIRD-PARTY-LICENSES.md',
    'LICENSE',
    '使用指南.txt',
    'config/templates/general.toml',
    'dist/public/index.html',
    'dist/public/js/react/main.js',
    'dist/public/js/react/main.css',
    'backend/trpg_server/__init__.py',
    'data/character_skills.json',
    'data/assets/vendor/font-awesome',
    'data/scenarios/scenario-1793438085450/scenario.json'
)
foreach ($item in $required) {
    Assert-Present $item
}

$forbidden = @(
    'docs',
    'tests',
    'release/changelog',
    '.gitignore',
    '.git',
    '.venv',
    'node_modules',
    '.worktrees',
    'data/config',
    'data/runtime',
    'data/assets/aiplatform',
    'data/assets/avatars',
    'data/assets/bgm'
)
foreach ($item in $forbidden) {
    Assert-Absent $item
}

# ---------------------------------------------------------------- 生成压缩包
if (-not (Test-Path $OutDir)) {
    New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
}
if (Test-Path $Zip) {
    Remove-Item -Path $Zip -Force
}

Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::CreateFromDirectory(
    $Staging,
    $Zip,
    [System.IO.Compression.CompressionLevel]::Optimal,
    $false
)

$hash = (Get-FileHash -Path $Zip -Algorithm SHA256).Hash.ToLowerInvariant()
$zipSizeMb = [math]::Round((Get-Item $Zip).Length / 1MB, 2)
"$hash  $Name.zip" | Set-Content -Path $HashFile -Encoding ASCII

# ---------------------------------------------------------------- 汇总
$fileCount = (Get-ChildItem -Path $App -Recurse -File | Measure-Object).Count
$uncompressedMb = [math]::Round((Get-ChildItem -Path $App -Recurse -File | Measure-Object Length -Sum).Sum / 1MB, 2)

Write-Step "文件数: $fileCount"
Write-Step "解压后大小: $uncompressedMb MB"
Write-Step "压缩包: $Zip ($zipSizeMb MB)"
Write-Step "SHA256: $hash"
Write-Step "顶层内容:"
Get-ChildItem -Path $App -Force | Sort-Object Name | ForEach-Object {
    $kind = if ($_.PSIsContainer) { 'dir ' } else { 'file' }
    Write-Host ("  [{0}] {1}" -f $kind, $_.Name)
}

Write-Host ''
Write-Host "完成。校验文件: $HashFile"
