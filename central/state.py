"""레일 명령 상태와 로컬 기록. HTTP 를 모르는 순수 로직이라 테스트로 바로 검증한다.

물리 이동이 두 번 일어나지 않게 하는 규칙이 전부 여기 있다.

    PENDING ──claim(RUNNING)──▶ RUNNING ──▶ DONE / FAILED / ABORTED
       │                          │
       └─▶ FAILED / ABORTED       └─(서버 재시작)─▶ UNKNOWN ──(늦은 결과 보고)─▶ DONE / FAILED / ABORTED

- 파이는 RUNNING 보고가 200 으로 승인된 뒤에만 움직인다.
- GET 은 PENDING 만 내준다. RUNNING/UNKNOWN 은 다시 내주지 않는다.
- 같은 상태를 다시 보고하면(응답 유실 후 재시도) 200 을 돌려주되 아무것도 바꾸지 않는다.
- UNKNOWN 은 RUNNING 으로 되돌릴 수 없다. 자동 재실행이 없다는 뜻이다.
- 활성(PENDING/RUNNING) 또는 미해결(UNKNOWN) 명령이 있으면 새 명령을 받지 않는다.

ponytail: 단일 프로세스·단일 워커 전제의 전역 Lock. 워커를 늘리려면 파일 락이 아니라 DB 가 필요하다.
"""

import json
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

STATUSES = ("PENDING", "RUNNING", "DONE", "FAILED", "ABORTED", "UNKNOWN")
ACTIVE = ("PENDING", "RUNNING")
FINAL = ("DONE", "FAILED", "ABORTED")
MAX_SPEED_MM_S = 30.0  # rail/stage.py 의 MAX_SPEED_MM_S 와 같은 값. 파이도 다시 검사한다.

ALLOWED = {
    "PENDING": {"RUNNING", "FAILED", "ABORTED"},
    "RUNNING": {"DONE", "FAILED", "ABORTED", "UNKNOWN"},
    "UNKNOWN": set(FINAL),
}


class CommandError(Exception):
    """요청을 받아들일 수 없음. status 는 HTTP 상태 코드."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class Store:
    def __init__(self, data_dir, arm_safe_ttl_s=30.0):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.cmd_path = self.dir / "rail_command.json"
        self.log_path = self.dir / "commands.jsonl"
        self.arm_safe_ttl_s = arm_safe_ttl_s
        self.lock = threading.Lock()
        self.commands = {}  # device_id -> 마지막 명령 (활성이든 끝났든)
        self.devices = {}  # device_id -> {"report":..., "received_at":..., "host":...}
        self.arm = None  # 마지막 로봇팔 안전 보고. 재시작하면 사라진다(의도).
        self._load()

    # --- 파일 ---

    def _load(self):
        try:
            self.commands = json.loads(self.cmd_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.commands = {}
        # 재시작 직전에 움직이고 있었는지 서버는 알 수 없다.
        for cmd in self.commands.values():
            if cmd["status"] == "RUNNING":
                self._transition(cmd, "UNKNOWN", "중앙 서버 재시작 — 실행 결과 미확인")
            elif cmd["status"] == "PENDING":
                # 재시작 전 요청이 한참 뒤에 실행되지 않도록 버린다.
                self._transition(cmd, "ABORTED", "중앙 서버 재시작 — 미실행 명령 폐기")
        self._save()

    def _save(self):
        tmp = self.cmd_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.commands, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.cmd_path)

    def _log(self, path, record):
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _transition(self, cmd, status, error=None, result=None):
        prev = cmd["status"]
        cmd["status"] = status
        cmd["updated_at"] = time.time()
        if error is not None:
            cmd["error"] = error
        if result is not None:
            cmd["result"] = result
        self._log(self.log_path, {
            "ts": cmd["updated_at"], "id": cmd["id"], "device_id": cmd["device_id"],
            "station": cmd["station"], "from": prev, "to": status, "error": error,
        })

    # --- 장치 보고 ---

    def report(self, device_id, report, host=None):
        now = time.time()
        with self.lock:
            self.devices[device_id] = {"report": report, "received_at": now, "host": host}
            day = datetime.now().strftime("%Y-%m-%d")
            self._log(self.dir / f"sensors-{day}.jsonl",
                      {"received_at": now, "device_id": device_id,
                       "ts": report.get("ts"), "sensors": report.get("sensors")})

    # --- 로봇팔 안전 ---

    def set_arm(self, state):
        with self.lock:
            self.arm = {**state, "received_at": time.time()}
            return self.arm_status()

    def arm_status(self):
        """safe 는 '최근 ARM_SAFE_TTL_S 이내에 safe=true 로 보고됐고 아직 소비되지 않음'."""
        if self.arm is None:
            return {"safe": False, "reason": "안전 확인 기록 없음"}
        age = time.time() - self.arm["received_at"]
        if not self.arm.get("safe"):
            return {"safe": False, "reason": "안전하지 않다고 보고됨", "age_s": round(age, 1)}
        if self.arm.get("consumed_by"):
            return {"safe": False, "reason": f"확인이 이미 명령 {self.arm['consumed_by']} 에 쓰임"}
        if age > self.arm_safe_ttl_s:
            return {"safe": False, "reason": f"확인 후 {age:.0f}s 경과 (유효 {self.arm_safe_ttl_s:.0f}s)"}
        return {"safe": True, "age_s": round(age, 1)}

    # --- 레일 명령 ---

    def create(self, device_id, station, speed_mm_s, cmd_id=None):
        # 스테이션 이름은 파이가 정한다(/rail/ui). 보고가 있으면 그 목록으로 미리 거르고,
        # 없으면 파이가 실행 시 다시 검사해 FAILED 로 돌려준다.
        listed = (self.devices.get(device_id) or {}).get("report", {}).get("rail", {}).get("stations")
        if listed is not None and station not in listed:
            raise CommandError(400, f"{device_id} 에 등록되지 않은 스테이션: {station!r} (등록: {', '.join(listed)})")
        if not 0 < speed_mm_s <= MAX_SPEED_MM_S:
            raise CommandError(400, f"speed_mm_s 는 0 초과 {MAX_SPEED_MM_S} 이하")
        with self.lock:
            cur = self.commands.get(device_id)
            if cur and cmd_id and cur["id"] == cmd_id:
                return cur  # 같은 id 재요청은 새 명령이 아니다
            if cur and cur["status"] in ACTIVE:
                raise CommandError(409, f"활성 명령이 있습니다: {cur['id']} ({cur['status']})")
            if cur and cur["status"] == "UNKNOWN":
                raise CommandError(409, f"결과 미확인 명령 {cur['id']} 이 있습니다. 레일 상태를 "
                                        "눈으로 확인하고 결과를 보고해 해제하세요.")
            arm = self.arm_status()
            if not arm["safe"]:
                raise CommandError(409, f"로봇팔 안전 미확인: {arm['reason']}. POST /arm/state 먼저.")
            now = time.time()
            cmd = {"id": cmd_id or uuid.uuid4().hex, "device_id": device_id, "station": station,
                   "speed_mm_s": speed_mm_s, "status": "PENDING", "created_at": now,
                   "updated_at": now, "error": None, "result": None}
            self.commands[device_id] = cmd
            self._log(self.log_path, {"ts": now, "id": cmd["id"], "device_id": device_id,
                                      "station": station, "from": None, "to": "PENDING", "error": None})
            self._save()
            return cmd

    def pending(self, device_id):
        with self.lock:
            cur = self.commands.get(device_id)
            return cur if cur and cur["status"] == "PENDING" else None

    def report_result(self, cmd_id, status, error=None, result=None):
        if status not in STATUSES or status == "PENDING":
            raise CommandError(400, f"잘못된 상태: {status}")
        with self.lock:
            cmd = next((c for c in self.commands.values() if c["id"] == cmd_id), None)
            if cmd is None:
                raise CommandError(404, f"모르는 명령: {cmd_id}")
            if cmd["status"] == status:
                return cmd  # 응답 유실 후 재시도. 바꾸지 않는다.
            if status not in ALLOWED.get(cmd["status"], ()):
                raise CommandError(409, f"{cmd['status']} → {status} 전이 불가")
            if status == "RUNNING":
                # 이동 직전 마지막 안전 확인. 확인 하나는 이동 하나에만 쓴다.
                arm = self.arm_status()
                if not arm["safe"]:
                    self._transition(cmd, "FAILED", f"이동 직전 로봇팔 안전 미확인: {arm['reason']}")
                    self._save()
                    raise CommandError(409, cmd["error"])
                self.arm["consumed_by"] = cmd_id
            self._transition(cmd, status, error, result)
            self._save()
            return cmd

    def cancel_pending(self, device_id, reason):
        """정지 요청 시 아직 시작 안 한 명령을 버린다. 버렸으면 그 명령을 돌려준다."""
        with self.lock:
            cur = self.commands.get(device_id)
            if cur and cur["status"] == "PENDING":
                self._transition(cur, "ABORTED", reason)
                self._save()
                return cur
            return None
