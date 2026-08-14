"""실험 장비 HTTP API.

클라우드의 에이전트는 USB에 손을 뻗을 수 없으므로, 실험대 쪽 컴퓨터(라즈베리파이)가
하드웨어를 물고 API로 노출한다. 엔드포인트는 에이전트 도구와 1:1로 대응한다.

실행:
    uvicorn server.main:app --host 0.0.0.0 --port 8000
"""

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field

from . import config
from .hardware.camera import camera
from .hardware.rail import RailBusy, rail
from .hardware.sensors import reader as sensor_reader


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 센서는 백그라운드에서 계속 읽는다. 하드웨어가 없어도 예외를 내지 않고
    # 재연결을 시도하므로, 나중에 USB를 꽂으면 저절로 붙는다.
    sensor_reader.start()
    yield
    sensor_reader.stop()
    camera.close()
    rail.close()


def require_api_key(x_api_key: str = Header(default=None)):
    """API_KEY 환경변수가 설정된 경우에만 검사한다."""
    if config.API_KEY and x_api_key != config.API_KEY:
        raise HTTPException(status_code=401, detail="유효하지 않은 API 키")


app = FastAPI(
    title="Chemistry Lab Hardware API",
    version="0.1.0",
    lifespan=lifespan,
    dependencies=[Depends(require_api_key)],
)


# --- 상태 ------------------------------------------------------------------


@app.get("/health")
def health():
    """각 하드웨어의 연결 상태. 어떤 것이 빠져 있어도 200을 돌려준다."""
    return {
        "sensors": sensor_reader.status(),
        "camera": camera.status(),
        "rail": rail.status(),
        "arm": {"available": False, "error": "ROS 2 미설치"},
    }


# --- 센서 ------------------------------------------------------------------


@app.get("/sensors")
def get_sensors():
    """마지막으로 읽은 pH / 전도도 값."""
    data = sensor_reader.latest()
    if data is None:
        status = sensor_reader.status()
        raise HTTPException(
            status_code=503,
            detail=f"센서 값이 아직 없습니다. {status.get('error') or '연결 대기 중'}",
        )
    return data


# --- 카메라 ----------------------------------------------------------------


@app.get(
    "/camera/capture",
    responses={200: {"content": {"image/jpeg": {}}}},
    response_class=Response,
)
def capture():
    """현재 프레임을 JPEG로 반환한다."""
    try:
        jpeg, shape = camera.capture_jpeg()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return Response(
        content=jpeg,
        media_type="image/jpeg",
        headers={"X-Frame-Shape": "x".join(str(n) for n in shape)},
    )


# --- 레일 ------------------------------------------------------------------


class MoveRequest(BaseModel):
    mm: float = Field(description="이동 거리(mm). +는 정방향, -는 역방향")
    speed_mm_s: float = Field(default=5.0, gt=0, le=200)


class MoveToRequest(BaseModel):
    mm: float = Field(description="목표 절대 위치(mm)")
    speed_mm_s: float = Field(default=5.0, gt=0, le=200)


class SetPositionRequest(BaseModel):
    mm: float = Field(description="현재 캐리지의 실제 위치(mm)")


def _rail_call(fn, *args):
    """레일 호출의 예외를 HTTP 상태 코드로 옮긴다."""
    try:
        return fn(*args)
    except RailBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        # 소프트 리밋 위반은 사용자 입력 문제이므로 400,
        # 그 외(pigpiod 미실행 등)는 서비스 불가이므로 503.
        name = type(exc).__name__
        if name == "SoftLimitError":
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/rail/position")
def rail_position():
    return _rail_call(rail.position)


@app.post("/rail/move")
def rail_move(req: MoveRequest):
    pos = _rail_call(rail.move, req.mm, req.speed_mm_s)
    return {"position_mm": pos}


@app.post("/rail/move_to")
def rail_move_to(req: MoveToRequest):
    pos = _rail_call(rail.move_to, req.mm, req.speed_mm_s)
    return {"position_mm": pos}


@app.post("/rail/set_position")
def rail_set_position(req: SetPositionRequest):
    """이동이 중단돼 위치를 잃었을 때 실제 위치를 다시 알려준다."""
    pos = _rail_call(rail.set_position, req.mm)
    return {"position_mm": pos}


# --- 로봇 팔 ---------------------------------------------------------------


@app.post("/arm/pose")
def arm_pose():
    raise HTTPException(
        status_code=501,
        detail="미구현. 라즈베리파이에 ROS 2 Jazzy 설치가 먼저 필요합니다",
    )
