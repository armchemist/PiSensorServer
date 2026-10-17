"""중앙 서버 + 파이 uplink 통신 검증 (하드웨어 없이).

    .venv/Scripts/python -m pytest -q tests        (Windows)
    .venv/bin/python -m pytest -q tests            (Linux)

레일은 FakeRail 로 대신한다. 실제 GPIO·모터 동작은 여기서 검증되지 않는다.
"""

import json
import socket
import threading
import time
import urllib.error

import pytest
import uvicorn
from fastapi.testclient import TestClient

import central.main as cm
from central.state import CommandError, Store
from server import config as pi_config
from server import uplink
from server.hardware.stations import Stations


# --- 상태 머신 ------------------------------------------------------------------


def approve(store):
    store.set_arm({"safe": True, "source": "operator"})


def test_command_lifecycle_and_duplicates(tmp_path):
    s = Store(tmp_path)
    with pytest.raises(CommandError) as e:
        s.create("pi2", "실험존", 5.0)
    assert e.value.status == 409  # 팔 안전 확인 없음

    approve(s)
    s.report("pi2", {"rail": {"stations": ["시약존", "실린더존", "실험존"]}})
    with pytest.raises(CommandError) as e:
        s.create("pi2", "아무데나", 5.0)  # 파이가 보고한 목록에 없음
    assert e.value.status == 400

    cmd = s.create("pi2", "실험존", 5.0, cmd_id="c1")
    assert s.create("pi2", "실험존", 5.0, cmd_id="c1") is cmd  # 같은 id 재요청
    with pytest.raises(CommandError):
        s.create("pi2", "시약존", 5.0)  # 활성 명령은 하나
    assert s.pending("pi2")["id"] == "c1"

    s.report_result("c1", "RUNNING")
    s.report_result("c1", "RUNNING")  # 응답 유실 후 재시도는 무해
    assert s.pending("pi2") is None  # RUNNING 은 다시 내주지 않는다
    s.report_result("c1", "DONE", result={"position_mm": 1.0})
    s.report_result("c1", "DONE")  # 결과 재전송도 무해
    with pytest.raises(CommandError):
        s.report_result("c1", "FAILED")
    with pytest.raises(CommandError):
        s.report_result("c1", "RUNNING")

    # 팔 확인은 이동 하나에 소비된다
    assert s.arm_status()["safe"] is False

    log = [json.loads(l) for l in (tmp_path / "commands.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["to"] for r in log] == ["PENDING", "RUNNING", "DONE"]


def test_arm_ttl_and_claim_time_check(tmp_path):
    s = Store(tmp_path, arm_safe_ttl_s=0.2)
    approve(s)
    s.create("pi2", "실험존", 5.0, cmd_id="c1")
    time.sleep(0.3)  # 생성 후 승인이 만료됨
    with pytest.raises(CommandError):
        s.report_result("c1", "RUNNING")
    assert s.commands["pi2"]["status"] == "FAILED"


def test_restart_recovery(tmp_path):
    s = Store(tmp_path)
    approve(s)
    s.create("pi2", "실험존", 5.0, cmd_id="run")
    s.report_result("run", "RUNNING")

    s2 = Store(tmp_path)  # 서버 재시작
    assert s2.commands["pi2"]["status"] == "UNKNOWN"
    assert s2.pending("pi2") is None
    approve(s2)
    with pytest.raises(CommandError):
        s2.report_result("run", "RUNNING")  # 자동 재실행 금지
    with pytest.raises(CommandError):
        s2.create("pi2", "시약존", 5.0)  # 미해결이면 새 명령도 막음
    s2.report_result("run", "DONE")  # 파이의 늦은 결과로 해제
    s2.create("pi2", "시약존", 5.0, cmd_id="next")

    s3 = Store(tmp_path)  # PENDING 상태로 재시작
    assert s3.commands["pi2"]["status"] == "ABORTED"


# --- HTTP / WebSocket -----------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(cm, "store", Store(tmp_path))
    cm.phones.clear()
    cm.frames.clear()
    cm.head_state.clear()
    with TestClient(cm.app) as c:
        yield c


def test_report_and_status(client, tmp_path):
    assert client.get("/health").json()["ok"] is True
    body = {"ts": time.time(), "sensors": {"status": "ok", "ph": 7.02, "conductivity": 612.4, "age_s": 0.3},
            "rail": {"available": False, "moving": False}}
    assert client.post("/devices/pi2/report", json=body).status_code == 200
    st = client.get("/status").json()
    assert st["devices"]["pi2"]["online"] is True
    assert st["devices"]["pi2"]["sensors"]["ph"] == 7.02
    assert list(tmp_path.glob("sensors-*.jsonl"))


def test_rail_command_api(client):
    assert client.post("/rail/command", json={"station": "실험존"}).status_code == 409
    client.post("/arm/state", json={"safe": True})
    r = client.post("/rail/command", json={"station": "실험존", "speed_mm_s": 5})
    assert r.status_code == 201
    cid = r.json()["id"]
    assert client.get("/rail/command", params={"device_id": "pi2"}).json()["command"]["id"] == cid
    assert client.post(f"/rail/command/{cid}/result", json={"status": "RUNNING"}).status_code == 200
    assert client.get("/rail/command").json()["command"] is None
    assert client.post(f"/rail/command/{cid}/result", json={"status": "DONE"}).json()["status"] == "DONE"
    assert client.post("/rail/command/nope/result", json={"status": "DONE"}).status_code == 404
    assert client.post("/rail/command", json={"station": "실험존", "speed_mm_s": 99}).status_code == 422


def test_rail_stop_without_pi_address(client):
    client.post("/arm/state", json={"safe": True})
    cid = client.post("/rail/command", json={"station": "실험존"}).json()["id"]
    r = client.post("/rail/stop")
    assert r.status_code == 503  # 파이 주소를 모름
    assert client.get("/status").json()["rail_commands"]["pi2"]["status"] == "ABORTED"
    assert cid


def test_phone_ws_commands_and_video(client):
    assert client.post("/phone/phone1/expression", json={"value": "danger"}).status_code == 404

    with client.websocket_connect("/ws/phone") as ws:
        ws.send_json({"type": "hello", "device_id": "phone1", "capabilities": ["camera", "tracking", "face"]})
        assert ws.receive_json() == {"type": "welcome", "device_id": "phone1"}

        assert client.post("/phone/phone1/expression", json={"value": "danger"}).status_code == 200
        assert ws.receive_json() == {"type": "expression", "value": "danger"}
        assert client.post("/phone/phone1/expression", json={"value": "angry"}).status_code == 422

        client.post("/phone/phone1/track", json={"action": "start", "target": "face"})
        assert ws.receive_json() == {"type": "track", "action": "start", "target": "face"}

        ws.send_json({"type": "status", "camera": "streaming", "tracking": True})
        ws.send_json({"type": "track_result", "target": "face", "found": True, "tracking_id": 3,
                      "bbox": [0.2, 0.3, 0.4, 0.4], "ts": time.time()})
        ws.send_json({"type": "bogus"})
        assert ws.receive_json()["type"] == "error"  # 위 두 메시지가 처리된 뒤에 온다
        phone = client.get("/status").json()["phones"]["phone1"]
        assert phone["status"]["camera"] == "streaming"
        assert phone["last_track"]["bbox"] == [0.2, 0.3, 0.4, 0.4]

    assert client.post("/phone/phone1/expression", json={"value": "idle"}).status_code == 404

    jpeg = b"\xff\xd8\xff\xe0" + b"x" * 100 + b"\xff\xd9"
    with client.websocket_connect("/ws/phone/video") as ws:
        ws.send_json({"type": "hello", "device_id": "phone1"})
        ws.send_bytes(b"not a jpeg")
        ws.send_bytes(jpeg)
    for _ in range(50):  # 서버가 마지막 프레임을 처리할 때까지
        if "phone1" in cm.frames:
            break
        time.sleep(0.02)
    r = client.get("/phone/phone1/frame")
    assert r.status_code == 200 and r.content == jpeg
    assert r.headers["content-type"] == "image/jpeg"
    assert client.get("/phone/other/frame").status_code == 404


def test_ws_requires_hello(client):
    with client.websocket_connect("/ws/phone") as ws:
        ws.send_json({"type": "status"})
        assert ws.receive_json()["type"] == "error"


# --- 파이 uplink ↔ 실제 중앙 서버 (HTTP) -------------------------------------------


class FakeRail:
    def __init__(self):
        self.pos = 0.0
        self.moves = []

    def status(self):
        return {"moving": False}

    def calibration(self):
        return {"stroke_mm": 500.0, "calibration_id": "cal1", "position_known": True,
                "position_mm": self.pos, "calibrated": True, "calibrating": False}

    def move_to(self, target, speed):
        self.moves.append(target)
        self.pos = target
        return target


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live(tmp_path, monkeypatch):
    """실제 uvicorn 으로 중앙 서버를 띄우고 uplink 를 그쪽으로 향하게 한다."""
    monkeypatch.setattr(cm, "store", Store(tmp_path / "central"))
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(cm.app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    while not server.started:
        time.sleep(0.02)

    rail = FakeRail()
    st = Stations(path=tmp_path / "stations.json")
    for name, mm in (("시약존", 10.0), ("실린더존", 200.0), ("실험존", 400.0)):
        st.add(name, mm, 500.0, "cal1")
    monkeypatch.setattr(uplink, "rail", rail)
    monkeypatch.setattr(uplink, "stations", st)
    monkeypatch.setattr(uplink, "STATE_PATH", tmp_path / "uplink_command.json")
    monkeypatch.setattr(pi_config, "CENTRAL_URL", f"http://127.0.0.1:{port}")
    yield rail
    server.should_exit = True
    th.join(timeout=5)


def test_uplink_report(live):
    uplink.report_once()
    dev = cm.store.devices["pi2"]["report"]
    assert dev["sensors"]["status"] in ("no_data", "error")  # 아두이노 없음
    assert dev["rail"]["stations"] == ["시약존", "실린더존", "실험존"]
    assert dev["rail"]["station"] is None
    assert cm.store.devices["pi2"]["host"] == "127.0.0.1"


def test_uplink_executes_once(live):
    uplink.command_step()  # 명령 없음
    assert live.moves == []
    cm.store.set_arm({"safe": True})
    cid = cm.store.create("pi2", "실험존", 5.0)["id"]
    uplink.command_step()
    assert live.moves == [400.0]
    assert cm.store.commands["pi2"]["status"] == "DONE"
    assert not uplink.STATE_PATH.exists()
    uplink.command_step()
    assert live.moves == [400.0]
    uplink.report_once()
    assert cm.store.devices["pi2"]["report"]["rail"]["station"] == "실험존"
    assert cid


def test_uplink_lost_claim_response_moves_once(live, monkeypatch):
    real = uplink._request
    calls = {"n": 0}

    def flaky(method, path, body=None):
        out = real(method, path, body)
        if body == {"status": "RUNNING"} and calls["n"] == 0:
            calls["n"] += 1
            raise urllib.error.URLError("응답 유실")  # 서버는 승인했지만 파이는 모름
        return out

    monkeypatch.setattr(uplink, "_request", flaky)
    cm.store.set_arm({"safe": True})
    cm.store.create("pi2", "시약존", 5.0)
    with pytest.raises(urllib.error.URLError):
        uplink.command_step()
    assert live.moves == []  # 승인 응답을 못 받았으니 움직이지 않음
    uplink.command_step()  # 같은 명령 재승인(멱등) → 한 번만 이동
    uplink.command_step()
    assert live.moves == [10.0]
    assert cm.store.commands["pi2"]["status"] == "DONE"


def test_uplink_result_retry_only(live, monkeypatch):
    real = uplink._request
    down = {"on": True}

    def flaky(method, path, body=None):
        if down["on"] and body and body.get("status") == "DONE":
            raise urllib.error.URLError("중앙 서버 다운")
        return real(method, path, body)

    monkeypatch.setattr(uplink, "_request", flaky)
    cm.store.set_arm({"safe": True})
    cm.store.create("pi2", "실린더존", 5.0)
    with pytest.raises(urllib.error.URLError):
        uplink.command_step()
    assert live.moves == [200.0]
    for _ in range(3):
        with pytest.raises(urllib.error.URLError):
            uplink.command_step()
    down["on"] = False
    uplink.command_step()
    assert live.moves == [200.0]  # 결과 보고만 재시도, 재이동 없음
    assert cm.store.commands["pi2"]["status"] == "DONE"


def test_uplink_pi_restart_mid_move_reports_unknown(live):
    cm.store.set_arm({"safe": True})
    cmd = cm.store.create("pi2", "실험존", 5.0)
    cm.store.report_result(cmd["id"], "RUNNING")
    uplink._save_state({"phase": "running", "command": cmd})  # 이동 중 전원 차단 흉내
    uplink.recover()
    uplink.command_step()
    assert live.moves == []
    assert cm.store.commands["pi2"]["status"] == "UNKNOWN"
    cm.store.set_arm({"safe": True})
    with pytest.raises(CommandError):
        cm.store.create("pi2", "시약존", 5.0)  # 사람이 확인하기 전까지 막힘


def test_uplink_claim_rejected_when_arm_expired(live, monkeypatch):
    cm.store.arm_safe_ttl_s = 0.1
    cm.store.set_arm({"safe": True})
    cm.store.create("pi2", "실험존", 5.0)
    time.sleep(0.2)
    uplink.command_step()
    assert live.moves == []
    assert cm.store.commands["pi2"]["status"] == "FAILED"
    assert not uplink.STATE_PATH.exists()

