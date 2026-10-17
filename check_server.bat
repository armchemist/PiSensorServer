@echo off
chcp 65001 >nul
cd /d "%~dp0"
title ARMchemist 중앙 서버 상태
powershell -NoProfile -ExecutionPolicy Bypass -File tools\central_ctl.ps1 check
echo.
pause
