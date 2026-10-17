"""ARMchemist 중앙 서버 (데스크톱). 파이·스마트폰이 여기로 붙는다.

실행 (단일 워커 전제):
    python -m uvicorn central.main:app --host 0.0.0.0 --port 8100

GPIO·시리얼 같은 하드웨어 라이브러리를 import 하지 않는다. Windows 에서도 뜬다.
통신 계약은 docs/PROTOCOL.md.
"""

import asyncio
import contextlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Literal, Optional

import cv2
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi import WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from . import aruco
from .state import MAX_SPEED_MM_S, CommandError, Store

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("DATA_DIR") or REPO_ROOT / "data")
# 파이의 기존 API 주소. 비우면 마지막으로 보고가 온 IP 의 8000 포트를 쓴다.
PI_URL = os.environ.get("PI_URL") or None
ARM_SAFE_TTL_S = float(os.environ.get("ARM_SAFE_TTL_S", 30))
DEVICE_OFFLINE_AFTER_S = 5.0
MAX_FRAME_BYTES = 2 * 1024 * 1024
EXPRESSIONS = ("idle", "thinking", "working", "danger", "done", "error")

store = Store(DATA_DIR, arm_safe_ttl_s=ARM_SAFE_TTL_S)
phones = {}  # device_id -> {"ws", "hello", "status", "track", "connected_at"}
frames = {}  # device_id -> {"jpeg", "received_at", "count", "detect"}  메모리에만
head_state = {}  # device_id -> {"state", "message", "source", "ts"}  마지막으로 폰에 보낸 얼굴 상태
aruco_cfg = aruco.load_config()
detector = aruco.Detector(aruco_cfg["dictionary"])
verifier = aruco.Verifier(aruco_cfg["reagents"], aruco_cfg["confirm_frames"], aruco_cfg["timeout_s"])
_verify_pushed = None  # 검증이 마지막으로 폰에 보낸 (state, message)


@contextlib.asynccontextmanager
async def _lifespan(app):
    task = asyncio.create_task(_verify_ticker())
    yield
    task.cancel()


app = FastAPI(title="ARMchemist Central", version="0.2.0", lifespan=_lifespan)


def _cmd_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except CommandError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


# --- 상태 -------------------------------------------------------------------


@app.get("/health")
def health():
    return {"ok": True, "ts": time.time()}


@app.get("/ui", include_in_schema=False)
def ui():
    """간단한 대시보드. /status 를 1초마다 읽는다."""
    return FileResponse(Path(__file__).with_name("ui.html"))


@app.get("/status")
async def status():
    # async: 폰 연결 dict 를 바꾸는 이벤트 루프와 같은 스레드에서 읽는다.
    now = time.time()
    devices = {
        dev: {"online": now - d["received_at"] <= DEVICE_OFFLINE_AFTER_S,
              "age_s": round(now - d["received_at"], 1), "host": d["host"], **d["report"]}
        for dev, d in list(store.devices.items())
    }
    phone_view = {
        dev: {"connected": True, "connected_at": p["connected_at"],
              "capabilities": p["hello"].get("capabilities"), "status": p["status"],
              "last_track": p["track"]}
        for dev, p in phones.items()
    }
    for dev, f in frames.items():
        d = f.get("detect") or {}
        phone_view.setdefault(dev, {"connected": False})["video"] = {
            "last_frame_age_s": round(now - f["received_at"], 1),
            "stale": now - f["received_at"] > aruco_cfg["stale_after_s"],
            "bytes": len(f["jpeg"]), "count": f["count"], "size": d.get("size"),
            "detect": {"ids": d.get("ids", []), "markers": d.get("markers", []), "ms": d.get("ms")}}
    for dev, h in head_state.items():
        phone_view.setdefault(dev, {"connected": False})["head"] = h
    return {"devices": devices, "rail_commands": store.commands,
            "arm": {**store.arm_status(), "last": store.arm}, "phones": phone_view,
            "verify": verifier.snapshot(_frame_age(verifier.device_id), aruco_cfg["stale_after_s"])}


# --- 파이 보고 ----------------------------------------------------------------


class Report(BaseModel):
    ts: float
    sensors: dict
    rail: dict


@app.post("/devices/{device_id}/report")
def device_report(device_id: str, body: Report, request: Request):
    store.report(device_id, body.model_dump(), host=request.client.host if request.client else None)
    return {"ok": True}


# --- 로봇팔 안전 (POC: 운영자 수동 확인) ----------------------------------------


class ArmState(BaseModel):
    safe: bool = Field(description="레일이 움직여도 되는 자세인가")
    source: Literal["operator", "robot"] = "operator"
    device_id: str = "pi1"
    pose: Optional[str] = Field(default=None, description="robot 보고 시 자세 이름 (예: home)")
    note: Optional[str] = None


@app.post("/arm/state")
def arm_state(body: ArmState):
    return store.set_arm(body.model_dump())


# --- 레일 명령 ----------------------------------------------------------------


class RailCommandRequest(BaseModel):
    station: str
    speed_mm_s: float = Field(default=5.0, gt=0, le=MAX_SPEED_MM_S)
    device_id: str = "pi2"
    id: Optional[str] = Field(default=None, description="재시도 시 같은 id 를 주면 중복 생성 안 됨")


class RailResult(BaseModel):
    status: Literal["RUNNING", "DONE", "FAILED", "ABORTED", "UNKNOWN"]
    error: Optional[str] = None
    result: Optional[dict] = None


@app.post("/rail/command", status_code=201)
def rail_command_create(body: RailCommandRequest):
    return _cmd_call(store.create, body.device_id, body.station, body.speed_mm_s, body.id)


@app.get("/rail/command")
def rail_command_poll(device_id: str = "pi2"):
    """파이 폴링용. PENDING 명령만 준다. 없으면 {"command": null}."""
    return {"command": store.pending(device_id)}


@app.post("/rail/command/{cmd_id}/result")
def rail_command_result(cmd_id: str, body: RailResult):
    return _cmd_call(store.report_result, cmd_id, body.status, body.error, body.result)


def _post_pi_stop(url):
    req = urllib.request.Request(url, data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return {"ok": True, "status": resp.status, "body": json.loads(resp.read() or b"null")}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status": exc.code, "body": exc.read().decode(errors="replace")}
    except (urllib.error.URLError, OSError) as exc:
        return {"ok": False, "status": None, "body": str(exc)}


@app.post("/rail/stop")
async def rail_stop(device_id: str = "pi2"):
    """시작 전 명령은 버리고, 파이의 기존 POST /rail/stop 을 부른다.

    소프트웨어 정지일 뿐이다. 물리 비상정지 스위치를 대신하지 않는다.
    """
    cancelled = store.cancel_pending(device_id, "정지 요청으로 취소")
    base = PI_URL
    if not base and device_id in store.devices and store.devices[device_id]["host"]:
        host = store.devices[device_id]["host"]
        base = f"http://[{host}]:8000" if ":" in host else f"http://{host}:8000"
    if not base:
        raise HTTPException(status_code=503, detail="파이 주소를 모릅니다. PI_URL 을 설정하세요.")
    # 동기 HTTP 를 이벤트 루프 밖에서 돌린다.
    pi = await asyncio.to_thread(_post_pi_stop, base.rstrip("/") + "/rail/stop")
    return {"cancelled": cancelled["id"] if cancelled else None, "pi": pi}


# --- 스마트폰 -----------------------------------------------------------------


async def _hello(ws):
    """연결 직후 hello 를 받는다. 형식이 틀리면 닫고 None."""
    await ws.accept()
    try:
        msg = await asyncio.wait_for(ws.receive_json(), timeout=10)
    except (asyncio.TimeoutError, ValueError, KeyError, WebSocketDisconnect):
        await ws.close(code=1008)
        return None
    if msg.get("type") != "hello" or not msg.get("device_id"):
        await ws.send_json({"type": "error", "error": "첫 메시지는 {type: hello, device_id} 여야 함"})
        await ws.close(code=1008)
        return None
    return msg


@app.websocket("/ws/phone")
async def ws_phone(ws: WebSocket):
    hello = await _hello(ws)
    if hello is None:
        return
    dev = hello["device_id"]
    old = phones.get(dev)
    if old:  # 같은 폰이 다시 붙으면 새 연결이 이긴다
        await old["ws"].close(code=1000)
    phones[dev] = {"ws": ws, "hello": hello, "status": None, "track": None,
                   "connected_at": time.time()}
    await ws.send_json({"type": "welcome", "device_id": dev})
    if dev in head_state:  # 재접속하면 마지막 얼굴 상태를 복구한다
        await ws.send_json({"type": "state", **head_state[dev]})
    try:
        while True:
            msg = await ws.receive_json()
            kind = msg.get("type")
            if kind == "status":
                phones[dev]["status"] = {**msg, "received_at": time.time()}
            elif kind == "track_result":
                phones[dev]["track"] = {**msg, "received_at": time.time()}
            else:
                await ws.send_json({"type": "error", "error": f"모르는 type: {kind}"})
    except (WebSocketDisconnect, ValueError, RuntimeError):
        pass
    finally:
        if phones.get(dev, {}).get("ws") is ws:
            del phones[dev]


@app.websocket("/ws/phone/video")
async def ws_phone_video(ws: WebSocket):
    hello = await _hello(ws)
    if hello is None:
        return
    dev = hello["device_id"]
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            jpeg = msg.get("bytes")
            if not jpeg or len(jpeg) > MAX_FRAME_BYTES or not jpeg.startswith(b"\xff\xd8"):
                continue  # 텍스트·비JPEG·너무 큰 프레임은 버린다
            count = frames.get(dev, {}).get("count", 0) + 1
            # 모든 프레임에서 ArUco 를 찾는다 (640px 에서 수 ms). 디코드는 스레드에서.
            detect = await asyncio.to_thread(detector.detect, jpeg)
            frames[dev] = {"jpeg": jpeg, "received_at": time.time(), "count": count, "detect": detect}
            if verifier.active and verifier.device_id == dev:
                verifier.observe(detect)
                await _push_verify()
    except (WebSocketDisconnect, RuntimeError):
        pass


@app.get("/phone/{device_id}/frame",
         responses={200: {"content": {"image/jpeg": {}}}}, response_class=Response)
def phone_frame(device_id: str, annotated: bool = False):
    """마지막 JPEG. annotated=1 이면 검출된 마커 박스·ID 를 그려서 준다."""
    f = frames.get(device_id)
    if f is None:
        raise HTTPException(status_code=404, detail=f"{device_id} 의 프레임이 없습니다")
    jpeg = f["jpeg"]
    if annotated and f.get("detect"):
        target = verifier.target_id if verifier.device_id == device_id else None
        jpeg = aruco.annotate(jpeg, f["detect"], target)
    return Response(content=jpeg, media_type="image/jpeg",
                    headers={"X-Frame-Age-S": f"{time.time() - f['received_at']:.2f}",
                             "X-Frame-Count": str(f["count"]), "Cache-Control": "no-store"})


async def _send_phone(device_id, msg):
    p = phones.get(device_id)
    if p is None:
        raise HTTPException(status_code=404, detail=f"스마트폰 {device_id} 이 연결돼 있지 않습니다")
    try:
        await p["ws"].send_json(msg)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"전송 실패: {exc}") from exc
    return {"sent": msg}


class ExpressionRequest(BaseModel):
    value: Literal[EXPRESSIONS]


class TrackRequest(BaseModel):
    action: Literal["start", "stop"]
    target: Literal["face"] = "face"


@app.post("/phone/{device_id}/expression")
async def phone_expression(device_id: str, body: ExpressionRequest):
    return await _send_phone(device_id, {"type": "expression", "value": body.value})


@app.post("/phone/{device_id}/track")
async def phone_track(device_id: str, body: TrackRequest):
    return await _send_phone(device_id, {"type": "track", "action": body.action, "target": body.target})


# --- 휴머노이드 얼굴 상태 -----------------------------------------------------------


class HeadStateRequest(BaseModel):
    state: Literal[EXPRESSIONS]
    message: str = Field(default="", max_length=120, description="얼굴 아래 짧은 문구. 비우면 앱 기본 문구")


async def _set_head_state(device_id, state, message, source):
    """상태를 기억하고, 폰이 붙어 있으면 보낸다. 안 붙어 있으면 다음 hello 때 복구된다."""
    head_state[device_id] = {"state": state, "message": message, "source": source, "ts": time.time()}
    p = phones.get(device_id)
    if p is None:
        return False
    try:
        await p["ws"].send_json({"type": "state", **head_state[device_id]})
        return True
    except Exception:
        return False


@app.post("/phone/{device_id}/state")
async def phone_state(device_id: str, body: HeadStateRequest):
    """운영자·향후 LLM/ACT 실행기가 부른다. 테스트 UI 의 상태 버튼도 이것."""
    sent = await _set_head_state(device_id, body.state, body.message, "manual")
    return {"sent": sent, **head_state[device_id]}


class VideoRequest(BaseModel):
    action: Literal["start", "stop"]


@app.post("/phone/{device_id}/video")
async def phone_video(device_id: str, body: VideoRequest):
    """폰의 전면 카메라 영상 전송을 원격으로 켜고 끈다 (얼굴 화면을 유지한 채)."""
    return await _send_phone(device_id, {"type": "video", "action": body.action})


# --- ArUco 시약 확인 --------------------------------------------------------------


def _frame_age(device_id):
    f = frames.get(device_id)
    return None if f is None else time.time() - f["received_at"]


async def _push_verify():
    """검증 상태가 바뀌었으면 해당 폰 얼굴을 바꾼다."""
    global _verify_pushed
    if verifier.device_id is None:
        return
    snap = verifier.snapshot(_frame_age(verifier.device_id), aruco_cfg["stale_after_s"])
    hs = aruco.head_state_for(snap)
    if hs is not None and hs != _verify_pushed:
        _verify_pushed = hs
        await _set_head_state(verifier.device_id, hs[0], hs[1], "verify")


async def _verify_ticker():
    while True:
        await asyncio.sleep(0.5)
        try:
            verifier.tick()
            await _push_verify()
        except Exception:  # 티커는 죽지 않는다
            pass


@app.get("/reagents")
def reagents():
    return aruco_cfg


@app.post("/reagents/reload")
def reagents_reload():
    """central/reagents.json 을 다시 읽는다. 진행 중인 검증은 중단된다."""
    global aruco_cfg, detector, verifier
    aruco_cfg = aruco.load_config()
    detector = aruco.Detector(aruco_cfg["dictionary"])
    verifier.stop("설정 다시 읽음")
    verifier = aruco.Verifier(aruco_cfg["reagents"], aruco_cfg["confirm_frames"], aruco_cfg["timeout_s"])
    return aruco_cfg


@app.get("/aruco/marker/{marker_id}.png", responses={200: {"content": {"image/png": {}}}},
         response_class=Response)
def aruco_marker(marker_id: int, px: int = 400):
    """테스트용 마커 이미지. 인쇄하거나 화면에 띄워 카메라에 보여준다."""
    if not 0 <= marker_id < 50:
        raise HTTPException(status_code=400, detail="DICT_4X4_50 의 ID 는 0~49")
    img = detector.marker_image(marker_id, max(50, min(px, 2000)))
    img = cv2.copyMakeBorder(img, 40, 40, 40, 40, cv2.BORDER_CONSTANT, value=255)  # 흰 여백 필수
    return Response(content=cv2.imencode(".png", img)[1].tobytes(), media_type="image/png")


class VerifyStart(BaseModel):
    target_id: int = Field(ge=0, le=49)
    device_id: str = "phone1"


@app.get("/verify")
def verify_get():
    return verifier.snapshot(_frame_age(verifier.device_id), aruco_cfg["stale_after_s"])


@app.post("/verify/start")
async def verify_start(body: VerifyStart):
    """목표 시약 ID 확인 시작. 폰에 영상 전송 시작을 요청하고 얼굴을 '확인 중' 으로 바꾼다."""
    global _verify_pushed
    verifier.start(body.target_id, body.device_id)
    _verify_pushed = None
    if body.device_id in phones:
        with contextlib.suppress(Exception):
            await phones[body.device_id]["ws"].send_json({"type": "video", "action": "start"})
    await _push_verify()
    return verify_get()


@app.post("/verify/stop")
async def verify_stop():
    verifier.stop()
    await _push_verify()
    return verify_get()
