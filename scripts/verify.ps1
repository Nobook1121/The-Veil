$ErrorActionPreference = "Stop"

$RepositoryRoot = Resolve-Path (Join-Path $PSScriptRoot "..")

function Invoke-Checked {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Command,

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Arguments
  )

  & $Command @Arguments
  $exitCode = $LASTEXITCODE
  if ($exitCode -ne 0) {
    exit $exitCode
  }
}

function Invoke-ProcessTree {
  param(
    [Parameter(Mandatory = $true)]
    [string]$FilePath,

    [Parameter(Mandatory = $true)]
    [string[]]$Arguments,

    # 整轮看门狗：pytest 自身只做「单用例」超时，收集阶段或卡在非用例代码时
    # 还需要一层整轮上限。
    [int]$TimeoutSeconds = 1800
  )

  # 用 Start-Process 拿到 pid，才能在中断 / 超时时按进程树收尾：
  # pytest 会再派生 Python 子进程，只杀父进程会留下孤儿继续吃 CPU。
  $process = Start-Process -FilePath $FilePath -ArgumentList $Arguments -NoNewWindow -PassThru
  $timedOut = $false
  try {
    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
      $timedOut = $true
    }
  } finally {
    if (-not $process.HasExited) {
      & taskkill /T /F /PID $process.Id 2>$null | Out-Null
      $process.WaitForExit(10000) | Out-Null
    }
  }

  if ($timedOut) {
    Write-Warning "命令超过 ${TimeoutSeconds}s 未结束，已按进程树强制结束：$FilePath $($Arguments -join ' ')"
    return 124
  }
  return $process.ExitCode
}

function Test-OnlyConftestBaseline {
  $conftest = Get-Item -Path "tests\conftest.py" -ErrorAction SilentlyContinue
  if ($null -eq $conftest) {
    return $false
  }

  $pythonFiles = @(Get-ChildItem -Path "tests" -File -Recurse -Filter "*.py" -ErrorAction SilentlyContinue)
  $testFiles = @(Get-ChildItem -Path "tests" -File -Recurse -Include "test_*.py", "*_test.py" -ErrorAction SilentlyContinue)

  return (
    $pythonFiles.Count -eq 1 -and
    $pythonFiles[0].FullName -eq $conftest.FullName -and
    $testFiles.Count -eq 0
  )
}

Push-Location $RepositoryRoot
try {
  Invoke-Checked python -m py_compile server.py backend\trpg_server\users\manager.py

  if (Test-Path "package.json") {
    Invoke-Checked npm run typecheck
    Invoke-Checked npm run build:frontend
  }

  $frontendFiles = @(
    "dist\public\index.html",
    "dist\public\js\react\main.css"
  )

  foreach ($file in $frontendFiles) {
    if (-not (Test-Path $file)) {
      Write-Error "Missing frontend build output: $file"
      exit 1
    }
  }

  $jsFiles = @(
    "dist\public\data\tools\diceTool.js",
    "dist\public\data\tools\checkTool.js",
    "dist\public\data\tools\toolManager.js",
    "dist\public\js\config\TestRequestConfig.js",
    "dist\public\js\config\ConfigManager.js",
    "dist\public\js\config\AIPlatformManager.js",
    "dist\public\js\api-client.js",
    "dist\public\js\dom-utils.js",
    "dist\public\js\models\ScenarioModel.js",
    "dist\public\js\views\ScenarioView.js",
    "dist\public\js\controllers\ScenarioController.js",
    "dist\public\js\tabs.js",
    "dist\public\js\platform-ui.js",
    "dist\public\js\cookie-consent.js",
    "dist\public\js\generated\templates.js",
    "dist\public\js\chat.js",
    "dist\public\js\latex.js",
    "dist\public\js\auth\api.js",
    "dist\public\js\auth\state.js",
    "dist\public\js\auth\floating-field.js",
    "dist\public\js\auth\login-view.js",
    "dist\public\js\auth\register-view.js",
    "dist\public\js\auth\profile-dialog.js",
    "dist\public\js\auth\user-card.js",
    "dist\public\js\auth\index.js",
    "dist\public\js\i18n.js",
    "dist\public\js\network.js",
    "dist\public\js\dialog.js",
    "dist\public\js\qr.js",
    "dist\public\js\vendor\qrcode.js",
    "dist\public\js\rooms.js",
    "dist\public\js\room-console.js",
    "dist\public\js\scenario.js",
    "dist\public\js\character-sheet.js",
    "dist\public\js\main.js",
    "dist\public\js\react\main.js"
  )

  foreach ($file in $jsFiles) {
    Invoke-Checked node --check $file
  }

  # 系统临时目录不可写（受限沙箱、只读挂载）时，Python 标准库 tempfile 会把
  # 当前工作目录当作最后兜底候选，临时文件就散落在仓库根。这里在启动 pytest
  # 前把 TMP/TEMP 指到仓库内可写目录，pytest 的全局输出捕获也会落在这里。
  $tempRoot = Join-Path $RepositoryRoot ".pytest-temp"
  New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null
  $tempNames = @("TMPDIR", "TEMP", "TMP")
  $savedTemp = @{}
  foreach ($name in $tempNames) {
    $savedTemp[$name] = [Environment]::GetEnvironmentVariable($name)
    Set-Item -Path "Env:$name" -Value $tempRoot
  }

  try {
    $pytestExitCode = Invoke-ProcessTree -FilePath "python" -Arguments @(
      "-W", "error::DeprecationWarning",
      "-W", "ignore::DeprecationWarning:certifi.core",
      "-m", "pytest", "-q"
    )
  } finally {
    foreach ($name in $tempNames) {
      if ($null -ne $savedTemp[$name]) {
        Set-Item -Path "Env:$name" -Value $savedTemp[$name]
      } else {
        Remove-Item -Path "Env:$name" -ErrorAction SilentlyContinue
      }
    }
  }

  if ($pytestExitCode -eq 0) {
    $global:LASTEXITCODE = 0
  } elseif ($pytestExitCode -eq 5 -and (Test-OnlyConftestBaseline)) {
    $global:LASTEXITCODE = 0
  } else {
    exit $pytestExitCode
  }
} finally {
  Pop-Location
}
