@echo off
REM ============================================================
REM  The Veil - one-click launcher for Windows
REM
REM  Usage:
REM    start.cmd            start with the configured port
REM    start.cmd 8090       start with a custom port
REM
REM  Double-click to run: this script prepares the environment
REM  (installing Python automatically when it is missing) and then
REM  starts the service. No prior command-line knowledge required.
REM
REM  NOTE: this file must stay pure ASCII and use CRLF line endings.
REM  The cmd parser reads batch files byte-by-byte using the console
REM  code page; non-ASCII text or LF-only endings can corrupt parsing
REM  and make the window flash and close. All Chinese text lives in
REM  start-bootstrap.ps1 instead (saved as UTF-8 with BOM).
REM ============================================================
setlocal EnableExtensions

cd /d "%~dp0"

REM Let Python and PowerShell print UTF-8 text correctly in this console.
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
chcp 65001 >nul 2>&1
title The Veil

echo.
echo  ============================================================
echo     The Veil - AI Tabletop RPG Platform
echo  ============================================================

REM ---------- Step 1: prepare the environment ----------
if exist "%~dp0start-bootstrap.ps1" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-bootstrap.ps1"
  if errorlevel 1 (
    echo.
    echo [The Veil] Environment setup failed. Read the messages above, fix the issue, then try again.
    echo.
    set "EXITCODE=1"
    goto :hold
  )
)

REM ---------- Step 2: make sure Python is available ----------
set "PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"

"%PYTHON%" --version >nul 2>&1
if errorlevel 1 (
  echo.
  echo [The Veil] Python was not found. Install Python 3.11+ and try again:
  echo            https://www.python.org/downloads/
  echo            Remember to tick "Add python.exe to PATH" during setup.
  echo.
  set "EXITCODE=1"
  goto :hold
)

REM ---------- Step 3: start the service ----------
"%PYTHON%" server.py %*
set "EXITCODE=%ERRORLEVEL%"

if not "%EXITCODE%"=="0" (
  echo.
  echo [The Veil] The service exited with code %EXITCODE%.
)

:hold
REM Keep the window open when launched by double-click (cmd.exe /c) so any
REM error message stays visible. Inside an already-open console we exit normally.
echo %cmdcmdline% | find /i "/c" >nul
if not errorlevel 1 pause
exit /b %EXITCODE%
