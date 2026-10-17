# 중앙 서버 종료/상태 확인. stop_server.bat, check_server.bat 가 부른다.
#   powershell -ExecutionPolicy Bypass -File tools\central_ctl.ps1 stop
#   powershell -ExecutionPolicy Bypass -File tools\central_ctl.ps1 check
# 이 저장소의 .venv 에서 뜬 'uvicorn central.main:app' 만 대상으로 한다. 다른 Python 은 건드리지 않는다.
param([ValidateSet("stop", "check")][string]$Action = "check")

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$venvPy = Join-Path $root ".venv\Scripts\python.exe"
$pidFile = Join-Path $root "data\central.pid"
$logFile = Join-Path $root "logs\central.log"
$url = "http://127.0.0.1:8100"

function Get-ServerProcs {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
        $_.CommandLine -like "*uvicorn*central.main:app*" -and
        ($_.ExecutablePath -ieq $venvPy -or $_.CommandLine -like "*$root*")
    }
}

if ($Action -eq "stop") {
    $procs = @(Get-ServerProcs)
    if (Test-Path $pidFile) {
        $id = [int](Get-Content $pidFile | Select-Object -First 1)
        $byPid = Get-CimInstance Win32_Process -Filter "ProcessId=$id" |
            Where-Object { $_.CommandLine -like "*uvicorn*central.main:app*" }
        if ($byPid) { $procs += $byPid }
    }
    $procs = $procs | Sort-Object ProcessId -Unique
    if (-not $procs) { Write-Host "  실행 중인 중앙 서버가 없습니다." }
    foreach ($p in $procs) {
        Write-Host ("  종료: PID {0}  {1}" -f $p.ProcessId, $p.CommandLine)
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Milliseconds 700
    $left = @(Get-ServerProcs)
    if ($left) { Write-Host ("  [경고] 아직 남은 서버 프로세스: " + (($left | ForEach-Object { $_.ProcessId }) -join ", ")) }
    elseif ($procs) { Write-Host ("  {0}개 프로세스 종료됨. 포트 8100 해제." -f @($procs).Count) }
    if (Test-Path $pidFile) { Remove-Item $pidFile -Force }
    exit 0
}

# check
Write-Host "[프로세스]"
$procs = @(Get-ServerProcs)
if ($procs) {
    foreach ($p in $procs) { Write-Host ("  PID {0}  시작 {1}  {2}" -f $p.ProcessId, $p.CreationDate.ToString("HH:mm:ss"), $p.ExecutablePath) }
} else { Write-Host "  중앙 서버 프로세스 없음" }
if (Test-Path $pidFile) { Write-Host ("  pid 파일: " + (Get-Content $pidFile | Select-Object -First 1)) }

Write-Host ""
Write-Host "[GET $url/health]"
$ok = $false
try {
    $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 "$url/health"
    Write-Host ("  " + $r.StatusCode + " " + $r.Content)
    $ok = $true
} catch { Write-Host ("  응답 없음: " + $_.Exception.Message) }

if ($ok) {
    Write-Host ""
    Write-Host "[GET $url/status 요약]"
    try {
        $s = Invoke-RestMethod -TimeoutSec 3 "$url/status"
        $pi = $s.devices.pi2
        if ($pi) { if ($pi.online) { $piTxt = "온라인" } else { $piTxt = "오프라인 ($($pi.age_s)s 전)" } } else { $piTxt = "보고 없음" }
        Write-Host "  pi2: $piTxt"
        $ph = $s.phones.phone1
        if ($ph -and $ph.connected) { $phTxt = "연결됨" } else { $phTxt = "미연결" }
        if ($ph -and $ph.video) { $phTxt += ", 마지막 프레임 $($ph.video.last_frame_age_s)s 전, 검출 ID [$($ph.video.detect.ids -join ',')]" } else { $phTxt += ", 영상 없음" }
        Write-Host "  phone1: $phTxt"
        $v = $s.verify
        if ($null -ne $v.target_id) {
            if ($v.active) { $vTxt = "진행 중 $($v.live)" } else { $vTxt = "종료" }
            if ($v.result) { $vTxt += " 결과 $($v.result.status)" }
            Write-Host "  verify: 목표 $($v.target_id) $vTxt"
        } else { Write-Host "  verify: 없음" }
        Write-Host "  대시보드: $url/ui"
    } catch { Write-Host ("  /status 읽기 실패: " + $_.Exception.Message) }
}

Write-Host ""
Write-Host "[logs\central.log 마지막 10줄]"
if (Test-Path $logFile) { Get-Content $logFile -Tail 10 | ForEach-Object { "  $_" } } else { Write-Host "  로그 파일 없음" }
if (-not $ok) { exit 1 }
