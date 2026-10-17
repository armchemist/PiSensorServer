"""ArUco 시약 확인: 검출, 판정 규칙, API, 폰 얼굴 연동. 실제 카메라 없이 생성한 마커 이미지로 검증한다.

    .venv/Scripts/python -m pytest -q tests/test_aruco.py
"""

import time

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

import central.main as cm
from central import aruco
from central.state import Store

REAGENTS = {1: "시약 1", 2: "시약 2", 3: "시약 3"}


def frame(*ids, side=120, w=640, h=480):
    """흰 배경에 마커를 가로로 나열한 JPEG."""
    canvas = np.full((h, w), 255, np.uint8)
    d = aruco.Detector()
    for n, i in enumerate(ids):
        x = 40 + n * (side + 60)
        canvas[100:100 + side, x:x + side] = d.marker_image(i, side)
    return cv2.imencode(".jpg", canvas, [cv2.IMWRITE_JPEG_QUALITY, 70])[1].tobytes()


def test_detect_ids_and_size():
    det = aruco.Detector("DICT_4X4_50").detect(frame(3, 1, side=100))
    assert det["ids"] == [1, 3]
    assert det["size"] == [640, 480]
    assert all(90 < m["side_px"] < 110 for m in det["markers"])
    assert aruco.Detector().detect(frame())["ids"] == []
    assert aruco.Detector().detect(b"garbage")["ids"] == []
    assert aruco.annotate(frame(3), det).startswith(b"\xff\xd8")


def test_verifier_rules():
    v = aruco.Verifier(REAGENTS, confirm_frames=3, timeout_s=60)
    assert v.snapshot()["active"] is False
    one, other, two, none = frame(3), frame(2), frame(2, 3), frame()
    det = aruco.Detector().detect

    v.start(3, "phone1")
    v.observe(det(one)); v.observe(det(one))
    assert v.active and v.live == "CONFIRMING" and v.streak == 2
    v.observe(det(none))  # 한 번 놓치면 연속 카운트 리셋
    assert v.live == "NOT_DETECTED" and v.streak == 0
    v.observe(det(two))  # 목표가 섞여 있어도 둘 이상 보이면 성공이 아니다
    assert v.live == "AMBIGUOUS" and v.result is None
    for _ in range(3):
        v.observe(det(one))
    assert not v.active and v.result["status"] == "MATCH" and v.result["observed_name"] == "시약 3"

    v.start(3, "phone1")
    for _ in range(3):
        v.observe(det(other))
    assert v.result["status"] == "MISMATCH" and v.result["observed_id"] == 2

    v.start(3, "phone1")
    v.observe(det(other)); v.observe(det(one)); v.observe(det(other))  # ID 가 바뀌면 리셋
    assert v.active and v.streak == 1

    v.stop()
    assert v.result["status"] == "NOT_DETECTED" and not v.active

    v = aruco.Verifier(REAGENTS, confirm_frames=3, timeout_s=0.05)
    v.start(1, "phone1")
    v.observe(det(two))
    time.sleep(0.1)
    v.tick()
    assert v.result["status"] == "AMBIGUOUS" and not v.active

    # 영상이 끊기면 live 는 STALE
    v.start(1, "phone1")
    assert v.snapshot(frame_age_s=None)["live"] == "STALE"
    assert v.snapshot(frame_age_s=10, stale_after_s=3)["live"] == "STALE"
    assert v.snapshot(frame_age_s=0.5, stale_after_s=3)["live"] == "NOT_DETECTED"


def test_head_state_mapping():
    v = aruco.Verifier(REAGENTS)
    assert aruco.head_state_for(v.snapshot()) is None
    v.start(3, "phone1")
    assert aruco.head_state_for(v.snapshot(0.1))[0] == "working"
    assert aruco.head_state_for(v.snapshot(None)) == ("error", "카메라 영상이 끊겼어요")
    for _ in range(3):
        v.observe(aruco.Detector().detect(frame(2)))
    st, msg = aruco.head_state_for(v.snapshot(0.1))
    assert st == "danger" and "다릅니다" in msg
    v.start(3, "phone1")
    for _ in range(3):
        v.observe(aruco.Detector().detect(frame(3)))
    assert aruco.head_state_for(v.snapshot(0.1)) == ("done", "시약을 확인했어요 (시약 3)")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(cm, "store", Store(tmp_path))
    monkeypatch.setattr(cm, "verifier", aruco.Verifier(REAGENTS, 3, 20))
    monkeypatch.setattr(cm, "_verify_pushed", None)
    cm.phones.clear()
    cm.frames.clear()
    cm.head_state.clear()
    with TestClient(cm.app) as c:
        yield c
    cm.head_state.clear()
    cm.frames.clear()


def _wait(cond, n=100):
    for _ in range(n):
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def test_verify_api_match_and_phone_state(client):
    assert client.get("/reagents").json()["dictionary"] == "DICT_4X4_50"
    assert client.get("/aruco/marker/3.png").headers["content-type"] == "image/png"
    assert client.get("/aruco/marker/99.png").status_code == 400

    with client.websocket_connect("/ws/phone") as ctrl:
        ctrl.send_json({"type": "hello", "device_id": "phone1"})
        assert ctrl.receive_json()["type"] == "welcome"

        # 수동 상태 전송
        r = client.post("/phone/phone1/state", json={"state": "thinking", "message": "실험 순서를 계획하고 있어요"})
        assert r.json()["sent"] is True
        msg = ctrl.receive_json()
        assert msg["type"] == "state" and msg["state"] == "thinking" and "계획" in msg["message"]
        assert client.post("/phone/phone1/state", json={"state": "angry"}).status_code == 422

        # 검증 시작 → 폰에 video start 와 '확인 중' 얼굴
        r = client.post("/verify/start", json={"target_id": 3, "device_id": "phone1"})
        assert r.status_code == 200 and r.json()["active"] is True
        assert ctrl.receive_json() == {"type": "video", "action": "start"}
        msg = ctrl.receive_json()
        assert msg["type"] == "state" and msg["state"] == "error"  # 아직 프레임이 없다 → STALE

        with client.websocket_connect("/ws/phone/video") as vid:
            vid.send_json({"type": "hello", "device_id": "phone1"})
            vid.send_bytes(frame(3))
            assert _wait(lambda: cm.frames.get("phone1", {}).get("count") == 1)
            msg = ctrl.receive_json()
            assert msg["state"] == "working"  # 프레임 들어옴 → 확인 중
            vid.send_bytes(frame(3))
            vid.send_bytes(frame(3))
            assert _wait(lambda: cm.verifier.result is not None)
        res = client.get("/verify").json()
        assert res["active"] is False and res["result"]["status"] == "MATCH"
        msg = ctrl.receive_json()
        assert msg["state"] == "done" and "확인했어요" in msg["message"]

        st = client.get("/status").json()
        assert st["verify"]["result"]["status"] == "MATCH"
        assert st["phones"]["phone1"]["video"]["detect"]["ids"] == [3]
        assert st["phones"]["phone1"]["head"]["state"] == "done"
        fr = client.get("/phone/phone1/frame", params={"annotated": 1})
        assert fr.status_code == 200 and fr.content.startswith(b"\xff\xd8")

    # 재접속하면 마지막 얼굴 상태를 다시 받는다
    with client.websocket_connect("/ws/phone") as ctrl:
        ctrl.send_json({"type": "hello", "device_id": "phone1"})
        ctrl.receive_json()
        assert ctrl.receive_json()["state"] == "done"


def test_verify_mismatch_and_ambiguous(client):
    client.post("/verify/start", json={"target_id": 3, "device_id": "phone1"})
    with client.websocket_connect("/ws/phone/video") as vid:
        vid.send_json({"type": "hello", "device_id": "phone1"})
        vid.send_bytes(frame(2, 3))
        assert _wait(lambda: cm.frames.get("phone1", {}).get("count") == 1)
        assert client.get("/verify").json()["live"] == "AMBIGUOUS"
        for _ in range(3):
            vid.send_bytes(frame(2))
        assert _wait(lambda: cm.verifier.result is not None)
    res = client.get("/verify").json()["result"]
    assert res["status"] == "MISMATCH" and res["observed_id"] == 2 and res["target_id"] == 3
    assert cm.head_state["phone1"]["state"] == "danger"

    client.post("/verify/start", json={"target_id": 1, "device_id": "phone1"})
    assert client.post("/verify/stop").json()["result"]["status"] == "NOT_DETECTED"
    assert cm.head_state["phone1"]["state"] == "error"
