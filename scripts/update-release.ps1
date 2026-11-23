<#
  The Veil · 从 GitHub Releases 更新到最新版本（随发布包分发）

  用法：
    powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\update-release.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\update-release.ps1 -CheckOnly
    powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\update-release.ps1 -Force

  做的事情（顺序固定，任一步失败都中止，不会留下半更新状态）：
    1. 读取本地版本（backend/trpg_server/version.py 的 APP_VERSION）
    2. 查询 GitHub Releases 最新版本，比较版本号
    3. 下载 the-veil-v<版本>.zip 与 .sha256，校验 SHA-256
    4. 把 data/ 备份到 _backup/<版本>-<时间>/
    5. 解压到临时目录，只替换程序文件（保留 data/、.venv/、_backup/）
    6. 校验新版本文件，提示重新启动

  不依赖 Python、不依赖 git，只需要本机 Windows PowerShell 5.1。
#>
param(
    [switch]$CheckOnly,
    [switch]$Force,
    [switch]$SkipBackup
)

$ErrorActionPreference = 'Stop'
$Repo = 'Nobook1121/The-Veil'
$Root = Split-Path -Parent $PSScriptRoot
$VersionFile = Join-Path $Root 'backend/trpg_server/version.py'

function Write-Step([string]$Text) {
    Write-Host "[update] $Text" -ForegroundColor Cyan
}

function Fail([string]$Text) {
    Write-Host "[update] $Text" -ForegroundColor Red
    exit 1
}

try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
} catch {
    # 旧系统没有 Tls12 枚举时保持默认
}

if (-not (Test-Path $VersionFile)) {
    Fail "这里不像 The Veil 的安装目录：找不到 backend/trpg_server/version.py"
}

$LocalText = [System.IO.File]::ReadAllText($VersionFile, [System.Text.Encoding]::UTF8)
$Match = [regex]::Match($LocalText, 'APP_VERSION\s*=\s*"([^"]+)"')
if (-not $Match.Success) {
    Fail '无法从 backend/trpg_server/version.py 读取本地版本号'
}
$LocalVersion = $Match.Groups[1].Value
Write-Step "本地版本: $LocalVersion"

# ---------------------------------------------------------------- 查询最新版本
$Headers = @{ 'User-Agent' = 'The-Veil-Updater'; 'Accept' = 'application/vnd.github+json' }
$Release = $null
try {
    $Release = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases/latest" -Headers $Headers -TimeoutSec 30
} catch {
    Fail "无法访问 GitHub Releases（$($_.Exception.Message)）。请检查网络后重试，或手动下载新版本。"
}

$RemoteTag = [string]$Release.tag_name
$RemoteVersion = $RemoteTag.TrimStart('v', 'V')
if ([string]::IsNullOrWhiteSpace($RemoteVersion)) {
    Fail 'GitHub 返回的版本号为空'
}
Write-Step "最新版本: $RemoteVersion ($RemoteTag)"

function Compare-Version([string]$A, [string]$B) {
    $pa = @($A.Split('.') | ForEach-Object { [int]($_ -replace '[^0-9].*$', '') })
    $pb = @($B.Split('.') | ForEach-Object { [int]($_ -replace '[^0-9].*$', '') })
    $len = [Math]::Max($pa.Count, $pb.Count)
    for ($i = 0; $i -lt $len; $i++) {
        $va = if ($i -lt $pa.Count) { $pa[$i] } else { 0 }
        $vb = if ($i -lt $pb.Count) { $pb[$i] } else { 0 }
        if ($va -lt $vb) { return -1 }
        if ($va -gt $vb) { return 1 }
    }
    return 0
}

$cmp = Compare-Version $LocalVersion $RemoteVersion
if ($cmp -eq 0 -and -not $Force) {
    Write-Step "已经是最新版本（$LocalVersion）。"
    if ($CheckOnly) { exit 0 }
    exit 0
}
if ($cmp -gt 0 -and -not $Force) {
    Write-Step "本地版本 $LocalVersion 比 GitHub 最新版 $RemoteVersion 新，无需更新。"
    exit 0
}
if ($CheckOnly) {
    Write-Step "有可用更新：$LocalVersion → $RemoteVersion（未更新，使用了 -CheckOnly）。"
    exit 0
}

# ---------------------------------------------------------------- 下载与校验
$AssetName = "the-veil-v$RemoteVersion.zip"
$Assets = @($Release.assets)
$ZipAsset = $Assets | Where-Object { $_.name -eq $AssetName } | Select-Object -First 1
$HashAsset = $Assets | Where-Object { $_.name -eq "$AssetName.sha256" } | Select-Object -First 1
if (-not $ZipAsset) {
    Fail "GitHub 上找不到 $AssetName（发布包可能还在上传中，请稍后重试）"
}

$TempRoot = Join-Path $env:TEMP ("veil-update-" + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $TempRoot -Force | Out-Null
$ZipPath = Join-Path $TempRoot $AssetName
$HashPath = Join-Path $TempRoot "$AssetName.sha256"

Write-Step "下载发布包: $AssetName"
try {
    Invoke-WebRequest -Uri $ZipAsset.browser_download_url -OutFile $ZipPath -Headers $Headers -TimeoutSec 600
} catch {
    Fail "下载失败：$($_.Exception.Message)"
}

$ExpectedHash = $null
if ($HashAsset) {
    try {
        Invoke-WebRequest -Uri $HashAsset.browser_download_url -OutFile $HashPath -Headers $Headers -TimeoutSec 60
        $HashText = [System.IO.File]::ReadAllText($HashPath, [System.Text.Encoding]::UTF8).Trim()
        $ExpectedHash = ($HashText -split '\s+')[0].ToLowerInvariant()
    } catch {
        Write-Host "[update] 警告：.sha256 下载失败，跳过哈希校验" -ForegroundColor Yellow
    }
}
$ActualHash = (Get-FileHash -Path $ZipPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($ExpectedHash) {
    if ($ActualHash -ne $ExpectedHash) {
        Remove-Item -Recurse -Force $TempRoot -ErrorAction SilentlyContinue
        Fail "SHA-256 校验失败（下载损坏或被替换），已中止。期望 $ExpectedHash，实际 $ActualHash"
    }
    Write-Step 'SHA-256 校验通过'
} else {
    Write-Step "未取得 .sha256，已计算 SHA-256: $ActualHash"
}

# ---------------------------------------------------------------- 解压
$ExtractRoot = Join-Path $TempRoot 'extract'
New-Item -ItemType Directory -Path $ExtractRoot -Force | Out-Null
try {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::ExtractToDirectory($ZipPath, $ExtractRoot)
} catch {
    Fail "解压失败：$($_.Exception.Message)"
}

$Staged = Get-ChildItem -Path $ExtractRoot -Directory | Select-Object -First 1
if (-not $Staged) { Fail '发布包结构异常：解压后没有顶层目录' }
$StagedRoot = $Staged.FullName
$StagedVersionFile = Join-Path $StagedRoot 'backend/trpg_server/version.py'
if (-not (Test-Path $StagedVersionFile)) {
    Fail '发布包结构异常：缺少 backend/trpg_server/version.py'
}
$StagedVersion = [regex]::Match(
    [System.IO.File]::ReadAllText($StagedVersionFile, [System.Text.Encoding]::UTF8),
    'APP_VERSION\s*=\s*"([^"]+)"'
).Groups[1].Value
if ($StagedVersion -ne $RemoteVersion) {
    Fail "发布包版本号与 GitHub 不一致（包内 $StagedVersion，应为 $RemoteVersion），已中止"
}

# ---------------------------------------------------------------- 备份 data
$DataDir = Join-Path $Root 'data'
$BackupRoot = Join-Path $Root '_backup'
if ((-not $SkipBackup) -and (Test-Path $DataDir)) {
    $Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $BackupDir = Join-Path $BackupRoot "$LocalVersion-$Stamp"
    New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
    Write-Step "备份 data/ → _backup/$LocalVersion-$Stamp（可能需要一点时间）"
    & robocopy $DataDir $BackupDir /E /R:1 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) {
        Fail "备份 data/ 失败（robocopy 退出码 $LASTEXITCODE）。为避免丢数据，已中止更新。"
    }
    Write-Step 'data/ 备份完成'
} else {
    Write-Step '跳过 data/ 备份（-SkipBackup）'
}

# ---------------------------------------------------------------- 替换程序文件
# 只覆盖程序目录；data、.venv、_backup、日志与本地构建缓存保持不动。
Write-Step '替换程序文件（保留 data/ 与 .venv/）'
$ExcludeDirs = @('data', '.venv', 'venv', '_backup', 'node_modules', '.git', '__pycache__')
& robocopy $StagedRoot $Root /E /R:1 /W:1 /NFL /NDL /NJH /NJS /NP /XD @ExcludeDirs | Out-Null
$code = $LASTEXITCODE
if ($code -ge 8) {
    Remove-Item -Recurse -Force $TempRoot -ErrorAction SilentlyContinue
    Fail "替换文件失败（robocopy 退出码 $code）。data/ 未被修改，可重试或手动解压覆盖。"
}

# ---------------------------------------------------------------- 收尾校验
$NewVersion = [regex]::Match(
    [System.IO.File]::ReadAllText($VersionFile, [System.Text.Encoding]::UTF8),
    'APP_VERSION\s*=\s*"([^"]+)"'
).Groups[1].Value
if ($NewVersion -ne $RemoteVersion) {
    Fail "更新后版本仍是 $NewVersion（应为 $RemoteVersion）。请用 _backup 目录手动恢复。"
}

Remove-Item -Recurse -Force $TempRoot -ErrorAction SilentlyContinue

Write-Host ''
Write-Host "[update] 已更新到 $RemoteVersion。" -ForegroundColor Green
Write-Host "[update] 请关闭当前启动窗口（Ctrl+C），再重新双击 start.cmd。" -ForegroundColor Green
Write-Host "[update] 若新版本启动失败：把 _backup/$LocalVersion-* 目录内容覆盖回 data/ 即可回退。" -ForegroundColor DarkGray
