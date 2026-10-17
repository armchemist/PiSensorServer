"""중앙 서버로의 연결 (파이2 쪽). CENTRAL_URL 이 없으면 아무것도 하지 않는다.

같은 프로세스 안에서 스레드 두 개가 돈다. 시리얼·GPIO 를 여는 별도 프로세스를
만들지 않고 기존 sensor_reader / rail / stations 를 그대로 쓴다.

    보고 스레드   1초마다 센서·레일 상태를 POST. 레일이 움직이는 중에도 계속 돈다.
    명령 스레드   PENDING 명령을 가져와 RUNNING 승인을 받은 뒤에만 이동한다.
                  이동은 이 스레드에서 블로킹으로 돌므로 이동 중엔 폴링하지 않는다.

물리 이동이 두 번 일어나지 않도록 진행 단계를 rail/uplink_command.json 에 남긴다.

    claiming  RUNNING 승인 대기. 아직 안 움직였다.
    running   이동 중. 여기서 파이가 죽으면 결과를 모른다 → 재시작 시 UNKNOWN 보고.
    result    결과 보고 대기. 보고만 재시도하고 다시 움직이지 않는다.
"""

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import config
from .hardware.rail import RailBusy, rail
from .hardware.sensors import reader as sensor_reader
from .hardware.stations import StationError, StationStale, stations

STATE_PATH = Path(__file__).resolve().parents[1] / "rail" / "uplink_command.json"
HTTP_TIMEOUT_S = 3.0
# 승인 응답을 이 시간 안에 못 받으면 포기한다. 통신 장애가 길게 이어진 뒤
# 운영자가 잊은 명령이 뒤늦게 움직이는 것을 막는다.
CLAIM_TIMEOUT_S = 10.0
STATION_TOLERANCE_MM = 0.5  # /rail/where 와 같은 기준

_stop = threading.Event()


class CentralRejected(Exception):
    """중앙 서버가 4xx 로 거절. 재시도해도 결과가 같다."""

    def __init__(self, status, detail):
        super().__init__(f"{status}: {detail}")
        self.status = status


def _request(method, path, body=None):
    """JSON 요청. 4xx 는 CentralRejected, 네트워크 오류·5xx 는 그대로 던진다(재시도 대상)."""
    headers = {"Content-Type": "application/json"}
    req = urllib.request.Request(
        config.CENTRAL_URL.rstrip("/") + path, method=method, headers=headers,
        data=json.dumps(body, ensure_ascii=False).encode() if body is not None else None,
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            return json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        if 400 <= exc.code < 500:
            raise CentralRejected(exc.code, exc.read().decode(errors="replace")) from exc
        raise


# --- 상태 보고 ----------------------------------------------------------------


def sensors_snapshot():
    """아두이노 JSON(ph, ec[uS/cm], tds[ppm], temp) 을 보고 형식으로 옮긴다."""
    data = sensor_reader.latest()
    error = sensor_reader.status()["error"]
    if data is None:
        return {"status": "error" if error else "no_data", "error": error}
    return {
        "status": "stale" if data["stale"] else "ok",
        "ph": data.get("ph"),
        "conductivity": data.get("ec"),  # uS/cm
        "tds": data.get("tds"),  # ppm
        "temp_c": data.get("temp"),  # 스케치에 박힌 가정 수온. 실측 아님
        "age_s": data["age_s"],
        "error": error,
    }


def rail_snapshot():
    moving = rail.status()["moving"]
    try:
        cal = rail.calibration()  # 처음 부르면 stage 를 로드한다(GPIO 확보)
    except RuntimeError as exc:
        return {"available": False, "moving": moving, "error": str(exc)}
    listed = stations.list(cal["stroke_mm"], cal["calibration_id"])
    station = None
    if cal["position_known"] and not moving:
        station = next((s["name"] for s in listed["stations"]
                        if abs(s["position_mm"] - cal["position_mm"]) <= STATION_TOLERANCE_MM), None)
    return {
        "available": True,
        "moving": moving,
        "position_known": cal["position_known"],
        "position_mm": cal["position_mm"],
        "calibrated": cal["calibrated"],
        "calibrating": cal["calibrating"],
        "station": station,
        "stations": [s["name"] for s in listed["stations"]],
        "stations_stale": listed["stale"],
        "error": None,
    }


def report_once():
    _request("POST", f"/devices/{config.DEVICE_ID}/report",
             {"ts": time.time(), "sensors": sensors_snapshot(), "rail": rail_snapshot()})


# --- 레일 명령 ----------------------------------------------------------------


def _load_state():
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save_state(state):
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def _clear_state():
    try:
        STATE_PATH.unlink()
    except FileNotFoundError:
        pass


def _to_result(cmd, status, error=None, result=None):
    _save_state({"phase": "result", "command": cmd,
                 "result": {"status": status, "error": error, "result": result}})


def recover():
    """파이 재시작 직후 한 번. 끊긴 명령을 다시 실행하지 않고 결과만 남긴다."""
    st = _load_state()
    if not st:
        return
    if st["phase"] == "claiming":
        _to_result(st["command"], "FAILED", "파이 재시작 — 실행하지 않음")
    elif st["phase"] == "running":
        _to_result(st["command"], "UNKNOWN", "이동 중 파이 재시작 — 실제 위치 확인 필요")


def execute(cmd):
    """기존 /rail/goto 와 같은 경로. (status, error, result) 를 돌려준다."""
    try:
        cal = rail.calibration()
        target = stations.target_mm(cmd["station"], cal["stroke_mm"], cal["calibration_id"])
        pos = rail.move_to(target, cmd["speed_mm_s"])
        return "DONE", None, {"station": cmd["station"], "position_mm": pos}
    except (StationError, StationStale, RailBusy) as exc:
        return "FAILED", f"이동 전 거부: {exc}", None
    except Exception as exc:  # SoftLimitError, MoveAborted, 펄스 오류 등 — stage 가 위치 신뢰를 관리한다
        status = "ABORTED" if type(exc).__name__ == "MoveAborted" else "FAILED"
        return status, f"{type(exc).__name__}: {exc}", None


def command_step():
    """명령 스레드의 한 번. 네트워크 오류는 그대로 던지고 다음 틱에 이어서 한다."""
    st = _load_state()

    if st and st["phase"] == "result":
        r = st["result"]
        try:
            _request("POST", f"/rail/command/{st['command']['id']}/result", r)
        except CentralRejected as exc:
            if exc.status not in (404, 409):
                raise  # 401 등은 설정 문제. 결과를 버리지 않고 계속 재시도한다.
            _log(f"결과 보고 거절, 버림: {exc}")
        _clear_state()
        return

    if st and st["phase"] == "running":
        return  # 같은 프로세스에서 이 상태가 보이면 안 된다. recover() 가 정리한다.

    if st and st["phase"] == "claiming":
        cmd = st["command"]
        if time.time() - st["claimed_at"] > CLAIM_TIMEOUT_S:
            _to_result(cmd, "FAILED", "승인 응답 시간 초과 — 실행하지 않음")
            return
    else:
        cmd = _request("GET", f"/rail/command?device_id={config.DEVICE_ID}")["command"]
        if not cmd:
            return
        _save_state({"phase": "claiming", "command": cmd, "claimed_at": time.time()})

    try:
        _request("POST", f"/rail/command/{cmd['id']}/result", {"status": "RUNNING"})
    except CentralRejected as exc:
        _log(f"실행 승인 거절, 실행 안 함: {exc}")
        _clear_state()
        return

    _save_state({"phase": "running", "command": cmd})
    status, error, result = execute(cmd)
    _to_result(cmd, status, error, result)
    command_step()  # 결과를 곧바로 한 번 보고해 본다


# --- 수명 ---------------------------------------------------------------------

_last_log = {}


def _log(msg, key=None):
    """같은 오류를 1초마다 찍지 않는다."""
    key = key or msg
    if _last_log.get(threading.current_thread().name) != key:
        _last_log[threading.current_thread().name] = key
        print(f"[uplink] {msg}", file=sys.stderr, flush=True)


def _loop(fn, interval_s):
    name = threading.current_thread().name
    while not _stop.is_set():
        try:
            fn()
            if _last_log.get(name) not in (None, "ok"):
                _log(f"{fn.__name__} 복구됨", key="ok")
        except Exception as exc:
            _log(f"{fn.__name__}: {type(exc).__name__}: {exc}")
        _stop.wait(interval_s)


def start():
    if not config.CENTRAL_URL:
        return  # 단독 운용
    recover()
    _stop.clear()
    for fn, interval in ((report_once, config.REPORT_INTERVAL_S), (command_step, config.POLL_INTERVAL_S)):
        threading.Thread(target=_loop, args=(fn, interval), name=f"uplink-{fn.__name__}",
                         daemon=True).start()
    print(f"[uplink] {config.CENTRAL_URL} 로 보고 시작 (device_id={config.DEVICE_ID})",
          file=sys.stderr, flush=True)


def stop():
    _stop.set()
