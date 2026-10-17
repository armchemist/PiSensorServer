@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title ARMchemist 중앙 서버 실행
set PORT=8100
set URL=http://127.0.0.1:%PORT%
if not exist logs mkdir logs
if not exist data mkdir data

echo [1/5] 이미 실행 중인지 확인...
powershell -NoProfile -Command "try { (Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 %URL%/health).StatusCode } catch { exit 1 }" >nul 2>&1
if %errorlevel%==0 (
  echo     이미 %PORT% 포트에서 서버가 응답합니다. 새로 띄우지 않고 대시보드만 엽니다.
  start "" %URL%/ui
  goto :done
)

echo [2/5] Python 가상환경 확인...
if not exist ".venv\Scripts\python.exe" (
  echo     .venv 가 없어 새로 만듭니다.
  set PY=
  for %%P in (py python python3) do (
    if not defined PY ( %%P -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1 && set PY=%%P )
  )
  if not defined PY (
    echo     [오류] Python 3.10 이상을 찾지 못했습니다. https://www.python.org 에서 설치 후 다시 실행하세요.
    goto :fail
  )
  %PY% -m venv .venv || ( echo     [오류] 가상환경 생성 실패 & goto :fail )
)

echo [3/5] 패키지 확인...
.venv\Scripts\python -c "import fastapi, uvicorn, cv2, numpy" >nul 2>&1
if errorlevel 1 (
  echo     필요한 패키지를 설치합니다 (처음 한 번, 1~3분^)...
  .venv\Scripts\python -m pip install --upgrade pip >nul
  .venv\Scripts\python -m pip install -r requirements-central.txt || ( echo     [오류] 패키지 설치 실패. 네트워크를 확인하세요. & goto :fail )
  .venv\Scripts\python -c "import fastapi, uvicorn, cv2, numpy" || ( echo     [오류] 설치 후에도 import 실패 & goto :fail )
)

echo [4/5] 서버 시작 (포트 %PORT%^)...
powershell -NoProfile -Command ^
  "$p = Start-Process -FilePath '.venv\Scripts\python.exe' -ArgumentList '-m','uvicorn','central.main:app','--host','0.0.0.0','--port','%PORT%' -WorkingDirectory '%CD%' -RedirectStandardOutput 'logs\central.out.log' -RedirectStandardError 'logs\central.log' -WindowStyle Hidden -PassThru; Set-Content -Path 'data\central.pid' -Value $p.Id"
if errorlevel 1 ( echo     [오류] 프로세스 시작 실패 & goto :fail )
set /p PID=<data\central.pid
echo     PID %PID%. 로그: logs\central.log

echo [5/5] 서버 응답 대기...
set /a tries=0
:wait
set /a tries+=1
powershell -NoProfile -Command "try { (Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 %URL%/health).StatusCode } catch { exit 1 }" >nul 2>&1
if %errorlevel%==0 goto :up
tasklist /FI "PID eq %PID%" 2>nul | find "%PID%" >nul || (
  echo     [오류] 서버 프로세스가 바로 종료되었습니다. 로그 마지막 줄:
  powershell -NoProfile -Command "Get-Content logs\central.log -Tail 20"
  del data\central.pid >nul 2>&1
  goto :fail
)
if %tries% geq 30 (
  echo     [오류] 30초 안에 /health 가 응답하지 않습니다. 로그를 확인하세요: logs\central.log
  goto :fail
)
timeout /t 1 /nobreak >nul
goto :wait

:up
echo.
echo  서버 실행 중: %URL%/ui   (상태 확인: check_server.bat, 종료: stop_server.bat^)
echo  스마트폰 앱 서버 주소에는 아래 IP 중 폰과 같은 네트워크의 것을 넣으세요:
powershell -NoProfile -Command "Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } | ForEach-Object { '    http://' + $_.IPAddress + ':%PORT%   (' + $_.InterfaceAlias + ')' }"
echo.
netsh advfirewall firewall show rule name="ARMchemist Central 8100" >nul 2>&1
if errorlevel 1 (
  echo  [안내] Windows 방화벽에 8100 포트 허용 규칙이 없습니다. 폰이 서버에 못 붙으면 이것 때문일 수 있습니다.
  choice /c YN /t 20 /d N /m " 지금 관리자 권한으로 방화벽 규칙을 추가할까요 (UAC 창이 뜹니다, 20초 뒤 자동으로 N)"
  if not errorlevel 2 powershell -NoProfile -Command "Start-Process netsh -Verb RunAs -Wait -ArgumentList 'advfirewall firewall add rule name=\"ARMchemist Central 8100\" dir=in action=allow protocol=TCP localport=%PORT% profile=any'"
)
start "" %URL%/ui
echo  이 창은 닫아도 서버는 계속 돕니다.
choice /c YN /t 15 /d N /m " 실시간 로그를 볼까요 (15초 뒤 자동으로 N)"
if errorlevel 2 goto :done
powershell -NoProfile -Command "Get-Content logs\central.log -Tail 30 -Wait"
goto :done

:fail
echo.
echo  실행 실패. 위 메시지를 확인하세요.
pause
exit /b 1

:done
endlocal
