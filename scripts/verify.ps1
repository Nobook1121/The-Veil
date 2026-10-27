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

  python -W error::DeprecationWarning -W ignore::DeprecationWarning:certifi.core -m pytest -q
  $pytestExitCode = $LASTEXITCODE

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
