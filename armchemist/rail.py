"""PiSensorServer 중앙 API. 동작 요청은 재전송하지 않고 ID로만 추적한다."""
import time
from uuid import uuid4

import requests

from .registry import STATIONS
from .runtime import Cancelled, ExecutionError


class CentralRail:
    def __init__(self, base_url, *, api_key="", device_id="pi2", timeout_s=120,
                 session=None, speed_mm_s=5):
        if not 0 < speed_mm_s <= 30:
            raise ValueError("잘못된 레일 속도")
        self.url, self.device_id = base_url.rstrip("/"), device_id
        self.session = session or requests.Session()
        self.headers = {"X-API-Key": api_key} if api_key else {}
        self.timeout_s, self.speed = timeout_s, speed_mm_s

    def request(self, method, path, **kwargs):
        try:
            r = self.session.request(method, self.url + path, headers=self.headers, timeout=5, **kwargs)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as exc:
            raise ExecutionError(f"레일 서버 응답 오류: {type(exc).__name__}") from exc

    def _device(self, status):
        device = status.get("devices", {}).get(self.device_id, {})
        rail = device.get("rail", {})
        if not device.get("online") or not 0 <= device.get("age_s", 999) <= 5:
            raise ExecutionError("레일 장치 상태가 오래되었거나 연결되지 않았습니다")
        if not rail.get("available") or not rail.get("position_known") or not rail.get("calibrated") \
                or rail.get("stations_stale") or rail.get("calibrating") or rail.get("error"):
            raise ExecutionError("레일 위치/보정 상태를 확인할 수 없습니다")
        return device, rail

    def station(self):
        status = self.request("GET", "/status")
        _, rail = self._device(status)
        command = status.get("rail_commands", {}).get(self.device_id, {})
        if rail.get("moving") or command.get("status") in ("PENDING", "RUNNING", "UNKNOWN"):
            raise ExecutionError("레일이 이동 중이거나 이전 명령의 결과가 불명확합니다")
        return rail.get("station")

    def arm_state(self, ready, evidence):
        self.request("POST", "/arm/state", json={"safe": ready, "source": "robot",
                     "device_id": "pi1", "pose": "act-ending-pose", "note": evidence})

    def move(self, station, cancel):
        if station not in STATIONS:
            raise ExecutionError("허용되지 않은 스테이션")
        if cancel.is_set():
            raise Cancelled("사용자 중단")
        command_id = uuid4().hex
        # 응답이 유실돼도 새 id로 다시 보내지 않는다. 상위 실행기가 중단/정지를 요청한다.
        result = self.request("POST", "/rail/command", json={"id": command_id, "station": station,
                              "device_id": self.device_id, "speed_mm_s": self.speed})
        if result.get("id") != command_id:
            raise ExecutionError("레일 명령 ID 불일치")
        deadline = time.monotonic() + self.timeout_s
        while time.monotonic() < deadline:
            if cancel.is_set():
                raise Cancelled("사용자 중단")
            status = self.request("GET", "/status")
            device, rail = self._device(status)
            command = status.get("rail_commands", {}).get(self.device_id, {})
            if command.get("id") != command_id:
                raise ExecutionError("레일 명령이 사라지거나 바뀌었습니다")
            state = command.get("status")
            if state in ("UNKNOWN", "FAILED", "ABORTED"):
                raise ExecutionError(f"레일 명령 {command_id}: {state}")
            if state == "DONE" and not rail.get("moving") and rail.get("station") == station \
                    and device.get("ts", 0) >= command.get("updated_at", float("inf")):
                return command_id
            if state not in ("PENDING", "RUNNING", "DONE"):
                raise ExecutionError("알 수 없는 레일 명령 상태")
            cancel.wait(0.2)
        raise ExecutionError(f"레일 도착 확인 시간 초과: {command_id}")

    def stop(self):
        result = self.request("POST", "/rail/stop", params={"device_id": self.device_id})
        if not result.get("pi", {}).get("ok"):
            raise ExecutionError("Pi 레일 정지 요청 전달 실패")

    def telemetry(self):
        status = self.request("GET", "/status")
        return status.get("devices", {}).get(self.device_id, {})
