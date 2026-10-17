<#
  The Veil · 启动环境准备脚本

  目标：让完全不懂编程、甚至还没装 Python 的电脑也能一键启动。

  本脚本只负责「把环境准备好」，不启动服务器；服务器由 start.cmd 在准备完成后启动。

  它会依次完成：
    1. 找到一个可用的 Python 3.11+，找不到就自动下载安装（优先 winget，失败则用
       Python 官方安装包按「仅当前用户」静默安装，不需要管理员权限）。
    2. 在项目目录创建独立虚拟环境 .venv，并安装 requirements.txt 里的依赖。
    3. 项目里放了本地 Embedding 模型（data/runtime/models/embedding）时，补齐
       sentence-transformers（含 CPU 版 torch），让本地向量模型真正可用。
    4. 缺少前端构建产物（dist/public）时尝试自动构建，必要时顺带安装 Node.js。
    5. 用标记文件记录已安装的依赖指纹，第二次启动会直接跳过安装，几秒即可。

  参数：
    -Force      忽略缓存标记，强制重新安装依赖。
    -DryRun     只打印将要执行的动作，不改动任何文件或系统环境。
    -IndexUrl   指定依赖源（PyPI 镜像地址），例如
                -IndexUrl https://pypi.tuna.tsinghua.edu.cn/simple
                不指定时会尊重环境变量 PIP_INDEX_URL；两者都没有时才自动挑选最快的源。
#>
[CmdletBinding()]
param(
    [switch]$Force,
    [switch]$DryRun,
    [string]$IndexUrl = ''
)

$ErrorActionPreference = 'Stop'

# PS 5.1 的进度条会让 Invoke-WebRequest 下载慢上数倍，这里直接关掉。
$ProgressPreference = 'SilentlyContinue'

# cmd 里已经 chcp 65001，这里同步把 PowerShell 输出也设为 UTF-8，避免中文乱码。
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
} catch { }

$Root         = $PSScriptRoot
$VenvDir      = Join-Path $Root '.venv'
$VenvPython   = Join-Path $VenvDir 'Scripts\python.exe'
$Requirements = Join-Path $Root 'requirements.txt'
$MarkerFile   = Join-Path $VenvDir '.requirements.sha256'
$DistIndex    = Join-Path $Root 'dist\public\index.html'
$LocalEmbeddingDir = Join-Path $Root 'data\runtime\models\embedding'
# torch 的 CPU 专用轮子：默认从 PyPI 拉到的是 CUDA 版，体积大好几倍（数 GB）。
$TorchCpuIndex = 'https://download.pytorch.org/whl/cpu'

$MinVersion      = [version]'3.11'
$PreferredPython = '3.12'

# 依赖源候选：国内镜像在前，官方 PyPI 兜底。自动探测时按可达性与延迟重新排序。
$PipIndexCandidates = @(
    'https://pypi.tuna.tsinghua.edu.cn/simple',
    'https://mirrors.aliyun.com/pypi/simple/',
    'https://mirrors.cloud.tencent.com/pypi/simple',
    'https://pypi.mirrors.ustc.edu.cn/simple/',
    'https://pypi.org/simple'
)

# ---------------------------------------------------------------- 日志输出
# 输出格式与后端日志完全一致：[时间][级别][线程] 消息
# 参见 backend/trpg_server/logging_config.py 的 CompactFormatter（WARNING 记作 WARN、
# CRITICAL 记作 FATAL）；这里沿用同一套前缀，方便把启动日志和服务日志对照阅读。
$LogThread = 'MainThread'

function Write-Log([string]$Level, [string]$Text) {
    $stamp = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss,fff')
    switch ($Level) {
        'ERROR' { $color = 'Red' }
        'WARN'  { $color = 'Yellow' }
        'DEBUG' { $color = 'DarkGray' }
        default { $color = 'Gray' }
    }
    Write-Host "[$stamp][$Level][$LogThread] $Text" -ForegroundColor $color
}

# 保留原来的调用名，内部统一走 Write-Log，避免大范围改动调用点。
function Write-Head([string]$Text) {
    Write-Host ''
    Write-Log 'INFO' "======== $Text ========"
}

function Write-Note([string]$Text) { Write-Log 'INFO' $Text }
function Write-Ok([string]$Text)   { Write-Log 'INFO' $Text }
function Write-Warn([string]$Text) { Write-Log 'WARN' $Text }

function Fail([string]$Text) {
    Write-Host ''
    Write-Log 'ERROR' $Text
    exit 1
}

# 执行外部命令并丢弃其输出。winget 会把进度写到 stderr，在 $ErrorActionPreference = 'Stop'
# 下会变成终止性错误中断脚本，因此这里临时切回 Continue 并吞掉输出。
function Invoke-Quiet([string]$Exe, [string[]]$Arguments) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Exe @Arguments 2>&1 | Out-Null
    } catch {
    } finally {
        $ErrorActionPreference = $previous
    }
}

# ------------------------------------------------------------ Python 探测
# 运行一个候选命令并返回其版本号（形如 3.12.4）；不可用则返回 $null。
function Get-PythonVersion([string]$Exe, [string[]]$PrefixArgs) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $output = $null
    $code = 1
    try {
        $output = & $Exe @PrefixArgs -c "import sys; print('%d.%d.%d' % sys.version_info[:3])" 2>$null
        $code = $LASTEXITCODE
    } catch {
        $output = $null
        $code = 1
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($code -ne 0) { return $null }
    $line = ($output | Select-Object -First 1)
    if ($line -and $line.Trim() -match '^\d+\.\d+\.\d+$') { return $line.Trim() }
    return $null
}

# 常见安装位置（官方安装包默认装到这些目录），用于 winget/静默安装后重新定位。
function Get-KnownPythonPaths {
    $bases = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python'),
        (Join-Path $env:ProgramFiles 'Python'),
        'C:\Python312',
        'C:\Python311'
    )
    $found = @()
    foreach ($base in $bases) {
        if (-not (Test-Path $base)) { continue }
        $dirs = @(Get-ChildItem -Path $base -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match '^Python3\d+$' })
        foreach ($dir in $dirs) {
            $exe = Join-Path $dir.FullName 'python.exe'
            if (Test-Path $exe) { $found += $exe }
        }
    }
    return @($found | Sort-Object -Descending)
}

# 依次尝试：虚拟环境 → py 启动器 → 已知安装路径 → PATH。返回 @{Exe;Args;Version} 或 $null。
function Find-Python {
    $candidates = @()
    if (Test-Path $VenvPython) { $candidates += @{ Exe = $VenvPython; Args = @() } }
    $candidates += @{ Exe = 'py'; Args = @("-$PreferredPython") }
    $candidates += @{ Exe = 'py'; Args = @('-3.11') }
    $candidates += @{ Exe = 'py'; Args = @('-3') }
    foreach ($path in Get-KnownPythonPaths) { $candidates += @{ Exe = $path; Args = @() } }
    $candidates += @{ Exe = 'python'; Args = @() }
    $candidates += @{ Exe = 'python3'; Args = @() }

    foreach ($candidate in $candidates) {
        $version = Get-PythonVersion $candidate.Exe $candidate.Args
        if (-not $version) { continue }
        if ([version]$version -ge $MinVersion) {
            return @{ Exe = $candidate.Exe; Args = $candidate.Args; Version = $version }
        }
    }
    return $null
}

# 列出可用的 Python 安装包下载地址：官方源优先，国内镜像兜底。
# 动态解析不到版本（例如 python.org 被墙）时退回到几个固定版本。
function Get-PythonInstallerUrls {
    $urls = @()
    $versions = @()
    try {
        $page = (Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/' -UseBasicParsing -TimeoutSec 20).Content
        $versionMatches = @([regex]::Matches($page, 'href="(3\.12\.\d+)/"') |
            ForEach-Object { [version]$_.Groups[1].Value } | Sort-Object -Descending)
        foreach ($version in $versionMatches) { $versions += $version.ToString() }
    } catch { }
    if ($versions.Count -eq 0) { $versions = @('3.12.10', '3.12.9', '3.12.8') }

    foreach ($version in $versions[0..([Math]::Min(2, $versions.Count - 1))]) {
        $file = "python-$version-amd64.exe"
        $urls += "https://www.python.org/ftp/python/$version/$file"
        $urls += "https://mirrors.huaweicloud.com/python/$version/$file"
    }
    return $urls
}

# 校验下载到的文件确实是 Windows 安装包（以 MZ 开头且体积足够），
# 避免镜像把错误页面 / 重定向页存成 .exe 后被静默执行。
function Test-InstallerFile([string]$Path) {
    try {
        $info = Get-Item -Path $Path -ErrorAction Stop
        if ($info.Length -lt 15MB) { return $false }
        $stream = [System.IO.File]::OpenRead($Path)
        try {
            $header = New-Object byte[] 2
            $null = $stream.Read($header, 0, 2)
        } finally {
            $stream.Close()
        }
        return ($header[0] -eq 0x4D -and $header[1] -eq 0x5A)
    } catch {
        return $false
    }
}

# 自动安装 Python。返回是否成功找到可用的 Python。
function Install-Python {
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        Write-Note "正在通过 winget 安装 Python $PreferredPython（可能弹出系统授权窗口）..."
        Invoke-Quiet $winget.Source @('install', '--exact', "--id", "Python.Python.$PreferredPython",
            '--silent', '--accept-package-agreements', '--accept-source-agreements')
    }
    if (Find-Python) { return $true }

    # winget 不可用或失败：改用安装包，仅当前用户安装，不需要管理员权限。
    $target = Join-Path $env:TEMP 'the-veil-python-setup.exe'
    foreach ($url in Get-PythonInstallerUrls) {
        Write-Note "正在下载 Python 安装包（约 25MB）：$url"
        try {
            Invoke-WebRequest -Uri $url -OutFile $target -UseBasicParsing -TimeoutSec 600
        } catch {
            Write-Warn '这个下载源不可用，换一个再试 ...'
            continue
        }
        if (-not (Test-InstallerFile $target)) {
            Write-Warn '下载到的文件不是有效的安装包，换一个下载源再试 ...'
            continue
        }
        Write-Note '正在静默安装 Python（仅当前用户）...'
        Start-Process -FilePath $target -Wait -ArgumentList @(
            '/quiet', 'InstallAllUsers=0', 'PrependPath=1',
            'Include_pip=1', 'Include_launcher=1', 'Include_test=0'
        )
        if (Find-Python) { return $true }
    }
    return $false
}

# --------------------------------------------------------------- 依赖源选择
# 探测单个依赖源的可达性与延迟；不可达返回 $null。
function Measure-Index([string]$Url) {
    try {
        $watch = [System.Diagnostics.Stopwatch]::StartNew()
        $null = Invoke-WebRequest -Uri $Url -Method Head -TimeoutSec 5 -UseBasicParsing
        $watch.Stop()
        return [int]$watch.ElapsedMilliseconds
    } catch {
        return $null
    }
}

# 决定使用哪个依赖源（可返回多个，依次重试）：
#   1) 命令行 -IndexUrl   2) 环境变量 PIP_INDEX_URL   3) 自动挑选最快且可达的源。
function Resolve-PipIndex {
    if ($IndexUrl) { return @($IndexUrl) }
    if ($env:PIP_INDEX_URL) { return @($env:PIP_INDEX_URL) }

    Write-Note '正在挑选最快的依赖源 ...'
    $ranked = @()
    foreach ($url in $PipIndexCandidates) {
        $ms = Measure-Index $url
        if ($null -ne $ms) { $ranked += [pscustomobject]@{ Url = $url; Ms = $ms } }
    }
    if ($ranked.Count -eq 0) {
        Write-Warn '所有依赖源都没探测通，将按顺序依次尝试。'
        return $PipIndexCandidates
    }
    $ordered = @($ranked | Sort-Object -Property Ms | ForEach-Object { $_.Url })
    Write-Note "选中：$($ordered[0])"
    return $ordered
}

# 拼出 pip 的公共参数（超时与重试都收紧一些，便于失败后快速换源）。
function New-PipArguments([string[]]$Extra) {
    $arguments = @('-m', 'pip') + $Extra + @(
        '--disable-pip-version-check', '--prefer-binary', '--retries', '3', '--timeout', '30'
    )
    return $arguments
}

# ------------------------------------------------------------------ 各步骤
function Ensure-Venv($python) {
    if (Test-Path $VenvPython) {
        Write-Ok '已有独立虚拟环境 .venv'
        return
    }
    if (-not $python) {
        Write-Warn 'Python 不可用，跳过虚拟环境创建'
        return
    }
    if ($DryRun) {
        Write-Note '（DryRun）将创建虚拟环境 .venv'
        return
    }
    Write-Note '正在创建独立虚拟环境 .venv ...'
    & $python.Exe @($python.Args) -m venv $VenvDir
    if (-not (Test-Path $VenvPython)) {
        Fail '虚拟环境创建失败，请确认 Python 安装完整后重试。'
    }
    Write-Ok '虚拟环境创建完成'
}

function Ensure-Dependencies {
    if (-not (Test-Path $VenvPython)) {
        Write-Note '（跳过依赖安装：虚拟环境尚未就绪）'
        return
    }

    $hash = (Get-FileHash -Path $Requirements -Algorithm SHA256).Hash
    if (-not $Force -and (Test-Path $MarkerFile)) {
        $recorded = (Get-Content -Path $MarkerFile -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ($recorded -and $recorded.Trim() -eq $hash) {
            Write-Ok '运行依赖已就绪（跳过安装）'
            return
        }
    }

    if ($DryRun) {
        Write-Note '（DryRun）将安装 requirements.txt 中的运行依赖'
        return
    }

    $indexes = @(Resolve-PipIndex)
    $attempts = [Math]::Min(3, $indexes.Count)

    Write-Note '正在安装运行依赖（首次可能需要几分钟，请耐心等待）...'

    # 先用选中的源顺手升级 pip；失败不影响后续安装。
    $upgradeIndex = $indexes[0]
    $upgradeArgs = New-PipArguments @('install', '--upgrade', 'pip')
    if ($upgradeIndex) { $upgradeArgs += @('--index-url', $upgradeIndex) }
    Invoke-Quiet $VenvPython $upgradeArgs

    $installed = $false
    for ($attempt = 0; $attempt -lt $attempts; $attempt++) {
        $index = $indexes[$attempt]
        if ($index) { Write-Note "使用依赖源：$index" } else { Write-Note '使用依赖源：官方 pypi.org' }
        $installArgs = New-PipArguments @('install', '-r', $Requirements)
        if ($index) { $installArgs += @('--index-url', $index) }
        & $VenvPython @installArgs
        if ($LASTEXITCODE -eq 0) { $installed = $true; break }
        Write-Warn '这个依赖源没能装完，换下一个源重试 ...'
    }

    if (-not $installed) {
        Fail "运行依赖安装失败。可指定一个可用的镜像后重试，例如：`n        powershell -ExecutionPolicy Bypass -File .\start-bootstrap.ps1 -IndexUrl https://pypi.tuna.tsinghua.edu.cn/simple`n        或手动执行：.venv\Scripts\python.exe -m pip install -r requirements.txt"
    }
    Set-Content -Path $MarkerFile -Value $hash -Encoding ascii
    Write-Ok '运行依赖安装完成'
}

# 本地 embedding 模型（sentence-transformers + torch）体积较大，只在「项目里确实放了本地模型」
# 时才补齐：没有模型目录时不下这几百 MB，直接走哈希兜底或管理员开启的云端接口。
function Test-PythonPackage([string]$Name) {
    if (-not (Test-Path $VenvPython)) { return $false }
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $VenvPython -c "import importlib.util; raise SystemExit(0 if importlib.util.find_spec('$Name') else 1)" 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Test-LocalEmbeddingModel {
    if (-not (Test-Path $LocalEmbeddingDir)) { return $false }
    return [bool](Get-ChildItem -Path $LocalEmbeddingDir -Directory -ErrorAction SilentlyContinue | Select-Object -First 1)
}

function Ensure-LocalEmbedding {
    if (-not (Test-LocalEmbeddingModel)) { return }
    if (Test-PythonPackage 'sentence_transformers') {
        Write-Ok '本地 Embedding 模型依赖已就绪'
        return
    }
    if ($DryRun) {
        Write-Note '（DryRun）将安装 sentence-transformers（本地 Embedding 模型依赖，约数百 MB）'
        return
    }
    Write-Note '检测到本地 Embedding 模型，正在补齐 sentence-transformers（首次约需数百 MB，请耐心等待）...'
    $indexes = @(Resolve-PipIndex)
    $mirror = $indexes[0]

    # torch 单独从 CPU 专用源安装（--no-deps）：默认 PyPI 拉到的是体积大好几倍的 CUDA 版，
    # 而 pytorch 源上非 torch 包下载很慢，所以它的纯 Python 依赖改从选定镜像装。
    $torchDeps = @('setuptools', 'sympy', 'networkx', 'jinja2', 'fsspec', 'typing-extensions', 'filelock')
    $depsArgs = New-PipArguments (@('install') + $torchDeps)
    if ($mirror) { $depsArgs += @('--index-url', $mirror) }
    & $VenvPython @depsArgs
    & $VenvPython (New-PipArguments @('install', 'torch', '--no-deps', '--index-url', $TorchCpuIndex))

    if (-not (Test-PythonPackage 'torch')) {
        Write-Warn 'torch 安装失败，已跳过本地 Embedding 模型依赖；语义检索将退化为哈希向量。'
        return
    }
    $installed = $false
    foreach ($index in $indexes) {
        $installArgs = New-PipArguments @('install', 'sentence-transformers')
        if ($index) { $installArgs += @('--index-url', $index) }
        & $VenvPython @installArgs
        if ($LASTEXITCODE -eq 0) { $installed = $true; break }
        Write-Warn '这个依赖源没能装完 sentence-transformers，换下一个源重试 ...'
    }
    if ($installed) {
        Write-Ok '本地 Embedding 模型依赖安装完成'
    } else {
        Write-Warn 'sentence-transformers 安装失败，语义检索将退化为哈希向量；可稍后手动执行：.venv\Scripts\python.exe -m pip install sentence-transformers'
    }
}

function Find-Npm {
    $cmd = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd }
    return (Get-Command npm -ErrorAction SilentlyContinue)
}

function Ensure-Frontend {
    if (Test-Path $DistIndex) {
        Write-Ok '前端页面已就绪'
        return
    }
    Write-Warn '未找到前端构建产物 dist/public。'

    if (-not (Test-Path (Join-Path $Root 'package.json'))) {
        Write-Warn '项目缺少 package.json，无法自动构建前端。'
        return
    }

    $npm = Find-Npm
    if (-not $npm) {
        Write-Note '正在安装 Node.js（用于构建前端页面）...'
        if (-not $DryRun) {
            $winget = Get-Command winget -ErrorAction SilentlyContinue
            if ($winget) {
                Invoke-Quiet $winget.Source @('install', '--exact', '--id', 'OpenJS.NodeJS.LTS',
                    '--silent', '--accept-package-agreements', '--accept-source-agreements')
            }
            $npm = Find-Npm
        }
    }
    if (-not $npm) {
        Write-Warn '没有可用的 Node.js，已跳过前端构建。页面可能显示空白，建议改用包含 dist/ 的发布包。'
        return
    }
    if ($DryRun) {
        Write-Note '（DryRun）将执行 npm install 与 npm run build:frontend'
        return
    }

    Write-Note '正在安装前端依赖并构建页面（首次可能需要几分钟）...'
    Push-Location $Root
    try {
        & $npm.Source install
        & $npm.Source run build:frontend
    } finally {
        Pop-Location
    }
    if (Test-Path $DistIndex) { Write-Ok '前端页面构建完成' } else { Write-Warn '前端构建未生成 dist/public/index.html' }
}

# -------------------------------------------------------------------- 主流程
Write-Head '环境检查'
if ($DryRun) { Write-Warn 'DryRun 模式：只显示计划，不做任何改动。' }
if ($Force) { Write-Note 'Force 模式：将忽略缓存，重新安装依赖。' }

$python = Find-Python
if ($python) {
    Write-Ok "已找到 Python $($python.Version)"
} elseif ($DryRun) {
    Write-Warn "未找到 Python 3.11+；正式运行时这里会自动安装 Python $PreferredPython。"
} else {
    Write-Warn '没有找到可用的 Python 3.11+，开始自动安装（首次约需 1-3 分钟）...'
    if (-not (Install-Python)) {
        Fail "无法自动安装 Python。请手动安装 Python 3.11+ 后重试：https://www.python.org/downloads/`n（安装时请勾选 “Add python.exe to PATH”）"
    }
    $python = Find-Python
    if (-not $python) {
        Fail 'Python 已安装但暂时无法定位，请重新打开 start.cmd（必要时重启电脑）后重试。'
    }
    Write-Ok "Python $($python.Version) 安装完成"
}

Ensure-Venv $python
Ensure-Dependencies
Ensure-LocalEmbedding
Ensure-Frontend

Write-Head '环境已就绪，正在启动服务'
exit 0
