<#
  The Veil · 发布包打包脚本（开发用，不随发布包分发）

  用法：
    powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\package-release.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\package-release.ps1 -Version 1.0.2

  产物：
    release/the-veil-v<版本>.zip
    release/the-veil-v<版本>.zip.sha256

  规则（与项目发布约束一致）：
    - 文件名前缀使用 the-veil，而不是 aibasedtrpg。
    - 排除密钥、虚拟环境、构建缓存、开发文档、changelog、开发脚本、本机用户数据。
    - 保留预构建前端 dist/public、部署指南、许可证汇总、配置模板、示例剧本与最小运行资源。
#>
param(
    [string]$Version = ''
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot

# 版本唯一真源：backend/trpg_server/version.py。显式传参只允许与它一致。
$VersionFile = Join-Path $Root 'backend/trpg_server/version.py'
if (-not (Test-Path $VersionFile)) {
    throw "缺少版本真源文件: backend/trpg_server/version.py"
}
$VersionSource = [System.IO.File]::ReadAllText($VersionFile, [System.Text.Encoding]::UTF8)
$VersionMatch = [regex]::Match($VersionSource, 'APP_VERSION\s*=\s*"([^"]+)"')
if (-not $VersionMatch.Success) {
    throw "无法从 version.py 解析 APP_VERSION"
}
$VersionFromFile = $VersionMatch.Groups[1].Value
if ([string]::IsNullOrWhiteSpace($Version)) {
    $Version = $VersionFromFile
} elseif ($Version -ne $VersionFromFile) {
    throw "传入版本 $Version 与 version.py 的 $VersionFromFile 不一致"
}
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

# 首启状态的真实来源：data/config/general.toml 首次启动时从 config/templates/general.toml 复制，
# [site] setup_dismissed 必须为 false，且包内不能带任何 OWNER/运行数据。
function Test-SetupDismissedDisabled([string]$TomlText) {
    $site = [regex]::Match($TomlText, '(?ms)^\s*\[site\]\s*\r?\n(.*?)(?=^\s*\[|\z)')
    if (-not $site.Success) {
        return $false
    }
    return [regex]::IsMatch($site.Groups[1].Value, '(?m)^\s*setup_dismissed\s*=\s*false\s*(#.*)?$')
}

function Assert-FirstRunReady {
    $problems = New-Object System.Collections.Generic.List[string]

    $templatePath = Join-Path $App 'config/templates/general.toml'
    if (-not (Test-Path $templatePath)) {
        $problems.Add('缺少 config/templates/general.toml（首次启动无法生成站点配置）')
    } else {
        $toml = [System.IO.File]::ReadAllText($templatePath, [System.Text.Encoding]::UTF8)
        if (-not (Test-SetupDismissedDisabled $toml)) {
            $problems.Add('config/templates/general.toml 的 [site] setup_dismissed 不是 false（首启会跳过安装向导）')
        }
    }

    foreach ($item in @('data/config', 'data/runtime', 'data/users', 'backend/data')) {
        if (Test-Path (Join-Path $App $item)) {
            $problems.Add("混入了运行时/用户数据目录: $item")
        }
    }

    Get-ChildItem -Path $App -Recurse -File -Force | Where-Object {
        $_.Name -in @('users.sqlite3', 'users.json')
    } | ForEach-Object {
        $problems.Add("混入了用户数据库: $($_.FullName.Substring($App.Length + 1))")
    }

    if (-not (Test-Path (Join-Path $App 'dist/public/js/setup-wizard.js'))) {
        $problems.Add('缺少首启向导脚本 dist/public/js/setup-wizard.js')
    }

    if ($problems.Count -gt 0) {
        $detail = ($problems | ForEach-Object { "  - $_" }) -join [Environment]::NewLine
        throw @"
发布包首启校验失败，已中止打包：
$detail
要求：包内不得包含 OWNER/用户数据与 data/config、data/runtime；
配置模板的 [site] setup_dismissed 必须为 false，且必须包含首启向导脚本。
"@
    }
    Write-Step '首启校验通过: setup_dismissed=false，未混入用户/运行数据'
}

function Assert-ZipFirstRunReady([string]$ZipPath) {
    $archive = [System.IO.Compression.ZipFile]::OpenRead($ZipPath)
    $problems = New-Object System.Collections.Generic.List[string]
    try {
        $entries = @($archive.Entries)
        foreach ($entry in $entries) {
            $relative = (($entry.FullName -replace '\\', '/') -replace '^[^/]+/', '')
            foreach ($prefix in @('data/config/', 'data/runtime/', 'data/users/', 'backend/data/')) {
                if ($relative.StartsWith($prefix)) {
                    $problems.Add("压缩包内混入: $relative")
                }
            }
            if ($entry.Name -in @('users.sqlite3', 'users.json')) {
                $problems.Add("压缩包内混入用户数据库: $relative")
            }
        }

        $templateEntry = $entries | Where-Object {
            (($_.FullName -replace '\\', '/') -replace '^[^/]+/', '') -eq 'config/templates/general.toml'
        } | Select-Object -First 1
        if (-not $templateEntry) {
            $problems.Add('压缩包缺少 config/templates/general.toml')
        } else {
            $reader = New-Object System.IO.StreamReader($templateEntry.Open(), [System.Text.Encoding]::UTF8)
            try {
                $toml = $reader.ReadToEnd()
            } finally {
                $reader.Dispose()
            }
            if (-not (Test-SetupDismissedDisabled $toml)) {
                $problems.Add('压缩包内配置模板的 [site] setup_dismissed 不是 false')
            }
        }
    } finally {
        $archive.Dispose()
    }

    if ($problems.Count -gt 0) {
        Remove-Item -Path $ZipPath -Force -ErrorAction SilentlyContinue
        $detail = ($problems | ForEach-Object { "  - $_" }) -join [Environment]::NewLine
        throw "发布包首启校验失败（已删除坏包 $Name.zip）：$([Environment]::NewLine)$detail"
    }
    Write-Step '压缩包首启校验通过'
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

# ---------------------------------------------------------------- 排除仅本地使用的 skill
# skills/ 下被 .gitignore 忽略的目录属于开发者本机的 agent 参考 skill，
# 只在本机保留，不随发布包分发。这里按 git 忽略状态统一处理，避免逐个点名。
if (Get-Command git -ErrorAction SilentlyContinue) {
    $skillsRoot = Join-Path $Root 'skills'
    if (Test-Path $skillsRoot) {
        Get-ChildItem -Path $skillsRoot -Directory | ForEach-Object {
            git -C $Root check-ignore -q "skills/$($_.Name)"
            if ($LASTEXITCODE -eq 0) {
                $skillTarget = Join-Path $App "skills/$($_.Name)"
                if (Test-Path $skillTarget) {
                    Remove-Item -Path $skillTarget -Recurse -Force
                    Write-Step "跳过仅本地使用的 skill: $($_.Name)"
                }
            }
        }
    }
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
Get-ChildItem -Path $App -Recurse -Force -File -Include '.env', '.gitignore' | ForEach-Object {
    Remove-Item -Path $_.FullName -Force
}

# 品牌设计草案不进入发布包（源码构建只需要正式图标）
$brandConcepts = Join-Path $App 'frontend/src/assets/brand/concepts'
if (Test-Path $brandConcepts) {
    Remove-Item -Path $brandConcepts -Recurse -Force
}

# ---------------------------------------------------------------- 构建清单与致谢校验
# 发布包内写一份构建清单：关于页优先读 data/runtime/build.json，缺失时回退到这里。
$Sha = 'local'
if (Get-Command git -ErrorAction SilentlyContinue) {
    $Sha = (git -C $Root rev-parse --short HEAD 2>$null)
    if ([string]::IsNullOrWhiteSpace($Sha)) {
        $Sha = 'local'
    }
}
$Manifest = [ordered]@{
    version  = $Version
    mode     = 'release'
    sha      = $Sha
    built_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssK')
    channel  = 'stable'
}
$ManifestPath = Join-Path $App 'backend/trpg_server/build_manifest.json'
[System.IO.File]::WriteAllText(
    $ManifestPath,
    ($Manifest | ConvertTo-Json),
    (New-Object System.Text.UTF8Encoding($false))
)
Write-Host "构建清单: version=$Version sha=$Sha" -ForegroundColor DarkGray

# 占位符不允许进入发布包：credits.json 缺失或含 CHANGE_ME 都直接中止。
$CreditsPath = Join-Path $App 'backend/trpg_server/resources/credits.json'
if (-not (Test-Path $CreditsPath)) {
    throw '缺少 backend/trpg_server/resources/credits.json'
}
$CreditsText = [System.IO.File]::ReadAllText($CreditsPath, [System.Text.Encoding]::UTF8)
if ($CreditsText -match 'CHANGE_ME') {
    throw 'credits.json 仍包含 CHANGE_ME 占位符，不能进入发布包'
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
    'data/users',
    'backend/data',
    'data/assets/aiplatform',
    'data/assets/avatars',
    'data/assets/bgm'
)
foreach ($item in $forbidden) {
    Assert-Absent $item
}

# 首启防线：打包结束前直接校验暂存产物，防止打出「解压后不进入安装向导」的坏包
Assert-FirstRunReady

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

# 再对最终 zip 做一次独立审计；不通过会删除坏包并非零退出
Assert-ZipFirstRunReady $Zip

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
