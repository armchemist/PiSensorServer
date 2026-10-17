"""ARMchemist 중앙 서버 (데스크톱). 파이·스마트폰이 여기로 붙는다.

실행 (단일 워커 전제):
    python -m uvicorn central.main:app --host 0.0.0.0 --port 8100

GPIO·시리얼 같은 하드웨어 라이브러리를 import 하지 않는다. Windows 에서도 뜬다.
통신 계약은 docs/PROTOCOL.md.
"""

import asyncio
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Literal, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from fastapi import WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from .state import MAX_SPEED_MM_S, CommandError, Store

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("DATA_DIR") or REPO_ROOT / "data")
API_KEY = os.environ.get("API_KEY") or None
# 파이의 기존 API 주소. 비우면 마지막으로 보고가 온 IP 의 8000 포트를 쓴다.
PI_URL = os.environ.get("PI_URL") or None
PI_API_KEY = os.environ.get("PI_API_KEY") or None
ARM_SAFE_TTL_S = float(os.environ.get("ARM_SAFE_TTL_S", 30))
DEVICE_OFFLINE_AFTER_S = 5.0
MAX_FRAME_BYTES = 2 * 1024 * 1024
EXPRESSIONS = ("idle", "thinking", "working", "danger", "done", "error")

store = Store(DATA_DIR, arm_safe_ttl_s=ARM_SAFE_TTL_S)
phones = {}  # device_id -> {"ws", "hello", "status", "track", "connected_at"}
frames = {}  # device_id -> {"jpeg", "received_at", "count"}  메모리에만


def _key_ok(header_key, query_key):
    return not API_KEY or API_KEY in (header_key, query_key)


def require_api_key(x_api_key: str = Header(default=None), api_key: str = Query(default=None)):
    """API_KEY 환경변수가 설정된 경우에만 검사한다 (파이 서버와 같은 방식)."""
    if not _key_ok(x_api_key, api_key):
        raise HTTPException(status_code=401, detail="유효하지 않은 API 키")


app = FastAPI(title="ARMchemist Central", version="0.1.0")
auth = [Depends(require_api_key)]


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
    """간단한 대시보드. 페이지 자체는 키 없이 열리고, API 호출에 입력한 키를 붙인다."""
    return FileResponse(Path(__file__).with_name("ui.html"))


@app.get("/status", dependencies=auth)
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
        phone_view.setdefault(dev, {"connected": False})["video"] = {
            "last_frame_age_s": round(now - f["received_at"], 1),
            "bytes": len(f["jpeg"]), "count": f["count"]}
    return {"devices": devices, "rail_commands": store.commands,
            "arm": {**store.arm_status(), "last": store.arm}, "phones": phone_view}


# --- 파이 보고 ----------------------------------------------------------------


class Report(BaseModel):
    ts: float
    sensors: dict
    rail: dict


@app.post("/devices/{device_id}/report", dependencies=auth)
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


@app.post("/arm/state", dependencies=auth)
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


@app.post("/rail/command", status_code=201, dependencies=auth)
def rail_command_create(body: RailCommandRequest):
    return _cmd_call(store.create, body.device_id, body.station, body.speed_mm_s, body.id)


@app.get("/rail/command", dependencies=auth)
def rail_command_poll(device_id: str = "pi2"):
    """파이 폴링용. PENDING 명령만 준다. 없으면 {"command": null}."""
    return {"command": store.pending(device_id)}


@app.post("/rail/command/{cmd_id}/result", dependencies=auth)
def rail_command_result(cmd_id: str, body: RailResult):
    return _cmd_call(store.report_result, cmd_id, body.status, body.error, body.result)


def _post_pi_stop(url):
    req = urllib.request.Request(url, data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json",
                                          **({"X-API-Key": PI_API_KEY} if PI_API_KEY else {})})
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return {"ok": True, "status": resp.status, "body": json.loads(resp.read() or b"null")}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status": exc.code, "body": exc.read().decode(errors="replace")}
    except (urllib.error.URLError, OSError) as exc:
        return {"ok": False, "status": None, "body": str(exc)}


@app.post("/rail/stop", dependencies=auth)
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
    """연결 직후 hello 를 받는다. 키 검사·형식이 틀리면 닫고 None."""
    if not _key_ok(ws.headers.get("x-api-key"), ws.query_params.get("api_key")):
        await ws.close(code=1008)
        return None
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
            frames[dev] = {"jpeg": jpeg, "received_at": time.time(), "count": count}
    except (WebSocketDisconnect, RuntimeError):
        pass


@app.get("/phone/{device_id}/frame", dependencies=auth,
         responses={200: {"content": {"image/jpeg": {}}}}, response_class=Response)
def phone_frame(device_id: str):
    f = frames.get(device_id)
    if f is None:
        raise HTTPException(status_code=404, detail=f"{device_id} 의 프레임이 없습니다")
    return Response(content=f["jpeg"], media_type="image/jpeg",
                    headers={"X-Frame-Age-S": f"{time.time() - f['received_at']:.2f}",
                             "X-Frame-Count": str(f["count"])})


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


@app.post("/phone/{device_id}/expression", dependencies=auth)
async def phone_expression(device_id: str, body: ExpressionRequest):
    return await _send_phone(device_id, {"type": "expression", "value": body.value})


@app.post("/phone/{device_id}/track", dependencies=auth)
async def phone_track(device_id: str, body: TrackRequest):
    return await _send_phone(device_id, {"type": "track", "action": body.action, "target": body.target})
