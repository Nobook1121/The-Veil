@echo off
chcp 65001 >nul
title The Veil - 更新
echo.
echo   正在检查 GitHub 上的最新版本...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\update-release.ps1" %*
echo.
pause
