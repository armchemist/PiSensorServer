@echo off
chcp 65001 >nul
cd /d "%~dp0"
title ARMchemist 중앙 서버 종료
echo start_server.bat 가 이 저장소의 .venv 로 띄운 서버만 종료합니다. 다른 Python 프로세스는 건드리지 않습니다.
powershell -NoProfile -ExecutionPolicy Bypass -File tools\central_ctl.ps1 stop
echo.
pause
