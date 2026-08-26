"""실험 장비 HTTP API (파이2 — 고정된 쪽).

클라우드의 에이전트는 USB에 손을 뻗을 수 없으므로, 실험대 쪽 컴퓨터(라즈베리파이)가
하드웨어를 물고 API로 노출한다. 엔드포인트는 에이전트 도구와 1:1로 대응한다.

이 파이가 맡는 것은 **고정된 것들**이다.

    열화상 (USB)  +  우노(pH/전도도, USB)  +  레일 구동 (GPIO)

로봇 팔과 핸디캠은 레일 위에서 같이 움직이는 파이1이 맡는다. 그쪽은 별도
저장소(PiRobotControl)다. 레일을 **구동**하는 쪽이 여기인 게 헷갈리기 쉬운데,
스텝모터 드라이버에 GPIO로 직결돼야 하기 때문이다. 레일 위에 타는 것은 파이1,
레일을 미는 것은 파이2다.

실행:
    python -m server.run

uvicorn 을 직접 부르지 않는 이유는 server/run.py 참고 (IPv4/IPv6 동시 바인드).
"""

import csv
import io
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import config
from .hardware import thermal_render
from .hardware.rail import RailBusy, rail
from .hardware.sensors import reader as sensor_reader
from .hardware.stations import (
    StationError,
    StationNotFound,
    StationStale,
    stations,
)
from .hardware.thermal import thermal

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 센서는 백그라운드에서 계속 읽는다. 하드웨어가 없어도 예외를 내지 않고
    # 재연결을 시도하므로, 나중에 USB를 꽂으면 저절로 붙는다.
    sensor_reader.start()
    # 열화상은 시계열을 놓치면 안 되므로 요청과 무관하게 계속 읽는다.
    thermal.start()
    yield
    sensor_reader.stop()
    thermal.close()
    rail.close()


def require_api_key(
    x_api_key: str = Header(default=None),
    api_key: str = Query(default=None),
):
    """API_KEY 환경변수가 설정된 경우에만 검사한다.

    브라우저의 <img src=...> 는 헤더를 붙일 수 없어서 쿼리스트링도 받아준다.
    다만 쿼리스트링은 접근 로그와 브라우저 기록에 남으므로, 외부로 노출한
    환경이라면 헤더 쪽을 쓰는 게 낫다.
    """
    if config.API_KEY and config.API_KEY not in (x_api_key, api_key):
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
        "thermal": thermal.status(),
        "rail": rail.status(),
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


# --- 열화상 카메라 ----------------------------------------------------------
#
# 값은 전부 섭씨(℃)로 나간다. 센서 원시값은 센티켈빈이지만 그건 thermal.py 안에
# 가둬 둔다. ROI 좌표는 항상 센서 픽셀(160x120) 기준이다 — 화면에 몇 배로
# 확대해 보여주든 서버가 받는 좌표는 바뀌지 않는다.


class RoiRequest(BaseModel):
    x: int = Field(ge=0, lt=160, description="좌상단 x (센서 픽셀, 0~159)")
    y: int = Field(ge=0, lt=120, description="좌상단 y (센서 픽셀, 0~119)")
    w: int = Field(gt=0, le=160, description="폭(픽셀)")
    h: int = Field(gt=0, le=120, description="높이(픽셀)")
    name: Optional[str] = Field(default=None, description="표시용 이름")


BOUNDARY = "frame"


def _render_opts(width, colormap, tmin, tmax, smooth):
    return {
        "width": width,
        "colormap": colormap,
        "tmin": tmin,
        "tmax": tmax,
        "smooth": smooth,
    }


@app.get("/thermal/ui", include_in_schema=False)
def thermal_ui():
    """드래그로 bbox 를 그리는 웹 UI. 브라우저에서 이 주소를 열면 된다."""
    return FileResponse(STATIC_DIR / "thermal.html")


@app.get("/thermal/stats")
def thermal_stats():
    """현재 프레임 전체의 온도 통계(℃)와 가장 뜨거운 픽셀 위치."""
    try:
        return thermal.frame_stats()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get(
    "/thermal/frame",
    responses={200: {"content": {"image/jpeg": {}}}},
    response_class=Response,
)
def thermal_frame(
    width: int = Query(default=640, ge=160, le=1920),
    colormap: str = Query(default="inferno"),
    tmin: float = Query(default=None, description="색 범위 하한(℃). 생략하면 자동"),
    tmax: float = Query(default=None, description="색 범위 상한(℃). 생략하면 자동"),
    smooth: bool = Query(default=False, description="보간 확대. 기본은 픽셀 그대로"),
):
    """현재 열화상 한 장을 컬러맵 입힌 JPEG 로. ROI 사각형도 같이 그려진다."""
    try:
        frame, _ = thermal.require_frame()
        jpeg, (lo, hi) = thermal_render.render(
            frame, rois=thermal.list_rois(), **_render_opts(width, colormap, tmin, tmax, smooth)
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return Response(
        content=jpeg,
        media_type="image/jpeg",
        headers={"X-Temp-Range": f"{lo:.2f},{hi:.2f}"},
    )


@app.get("/thermal/stream")
def thermal_stream(
    fps: int = Query(default=9, ge=1, le=9),
    width: int = Query(default=640, ge=160, le=1920),
    colormap: str = Query(default="inferno"),
    tmin: float = Query(default=None),
    tmax: float = Query(default=None),
    smooth: bool = Query(default=False),
):
    """MJPEG 실시간 스트림. 센서가 9fps 고정이라 그 이상은 의미가 없다."""
    try:
        frame, _ = thermal.require_frame()
        thermal_render.render(frame, **_render_opts(width, colormap, tmin, tmax, smooth))
    except RuntimeError as exc:
        # 스트리밍이 시작되면 상태 코드를 못 바꾸므로 먼저 한 장 만들어 본다.
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    def frames():
        try:
            for jpeg in thermal_render.stream(
                fps=fps, **_render_opts(width, colormap, tmin, tmax, smooth)
            ):
                yield (
                    f"--{BOUNDARY}\r\n"
                    f"Content-Type: image/jpeg\r\n"
                    f"Content-Length: {len(jpeg)}\r\n\r\n"
                ).encode() + jpeg + b"\r\n"
        except (GeneratorExit, RuntimeError):
            return  # 창을 닫았거나 카메라가 빠졌다

    return StreamingResponse(
        frames(), media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}"
    )


@app.post("/thermal/roi", status_code=201)
def thermal_add_roi(req: RoiRequest):
    """추적할 영역을 등록한다. 등록 즉시 시계열이 쌓이기 시작한다."""
    roi = thermal.add_roi(req.x, req.y, req.w, req.h, req.name)
    return roi.as_dict()


@app.get("/thermal/rois")
def thermal_list_rois():
    """등록된 영역과 각각의 최신 값."""
    return {"rois": [r.as_dict() for r in thermal.list_rois()]}


def _require_roi(roi_id):
    roi = thermal.get_roi(roi_id)
    if roi is None:
        raise HTTPException(status_code=404, detail=f"그런 ROI 가 없습니다: {roi_id}")
    return roi


@app.get("/thermal/roi/{roi_id}")
def thermal_get_roi(
    roi_id: str,
    series: bool = Query(default=True),
    limit: int = Query(default=None, ge=1, description="최근 N개만. 생략하면 전부"),
):
    """한 영역의 시계열. mean_c 가 '지정 영역의 평균 값'이다."""
    return _require_roi(roi_id).as_dict(with_series=series, limit=limit)


@app.get("/thermal/roi/{roi_id}/series.csv")
def thermal_roi_csv(roi_id: str):
    """실험 기록용 CSV. 표계산 프로그램에 그대로 넣을 수 있다."""
    roi = _require_roi(roi_id)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["epoch_s", "mean_c", "min_c", "max_c"])
    writer.writerows(roi.series)
    return Response(
        content=out.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="thermal-{roi_id}.csv"'
        },
    )


class RoiPatch(BaseModel):
    name: Optional[str] = None
    x: Optional[int] = Field(default=None, ge=0, lt=160)
    y: Optional[int] = Field(default=None, ge=0, lt=120)
    w: Optional[int] = Field(default=None, gt=0, le=160)
    h: Optional[int] = Field(default=None, gt=0, le=120)


@app.patch("/thermal/roi/{roi_id}")
def thermal_update_roi(roi_id: str, req: RoiPatch):
    """이름이나 좌표를 고친다. 좌표를 옮겨도 쌓인 시계열은 그대로 둔다 —
    지우고 싶으면 /reset 을 따로 부른다."""
    roi = _require_roi(roi_id)
    if req.name is not None:
        roi.name = req.name
    if any(v is not None for v in (req.x, req.y, req.w, req.h)):
        roi.x, roi.y, roi.w, roi.h = thermal.clamp_roi(
            req.x if req.x is not None else roi.x,
            req.y if req.y is not None else roi.y,
            req.w if req.w is not None else roi.w,
            req.h if req.h is not None else roi.h,
        )
    return roi.as_dict()


@app.post("/thermal/roi/{roi_id}/reset")
def thermal_reset_roi(roi_id: str):
    """좌표는 두고 쌓인 시계열만 비운다. 실험을 다시 시작할 때 쓴다."""
    _require_roi(roi_id)
    thermal.reset_series(roi_id)
    return {"ok": True, "id": roi_id}


@app.delete("/thermal/roi/{roi_id}")
def thermal_delete_roi(roi_id: str):
    _require_roi(roi_id)
    thermal.remove_roi(roi_id)
    return {"ok": True, "id": roi_id}


@app.delete("/thermal/rois")
def thermal_clear_rois():
    thermal.clear_rois()
    return {"ok": True}


# --- 레일 ------------------------------------------------------------------


class MoveRequest(BaseModel):
    mm: float = Field(description="이동 거리(mm). +는 정방향, -는 역방향")
    speed_mm_s: float = Field(default=5.0, gt=0, le=200)


class MoveToRequest(BaseModel):
    mm: float = Field(description="목표 절대 위치(mm)")
    speed_mm_s: float = Field(default=5.0, gt=0, le=200)


class SetPositionRequest(BaseModel):
    mm: float = Field(description="현재 캐리지의 실제 위치(mm)")


class JogRequest(BaseModel):
    mm: float = Field(description="이동 거리(mm). +는 정방향, -는 역방향")
    speed_mm_s: float = Field(default=5.0, gt=0, le=200)


def _rail_call(fn, *args):
    """레일 호출의 예외를 HTTP 상태 코드로 옮긴다."""
    try:
        return fn(*args)
    except RailBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        # 소프트 리밋 위반은 사용자 입력 문제이므로 400,
        # 그 외(lgpio 미설치, GPIO 확보 실패 등)는 서비스 불가이므로 503.
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


@app.post("/rail/resume")
def rail_resume():
    """저장된 위치를 그대로 이어 쓴다.

    서버를 다시 띄우면 위치는 항상 '모름'으로 시작한다. 전원이 꺼진 사이
    캐리지가 손으로 밀렸을 수 있기 때문이다. 건드리지 않았다면 이걸 부르고,
    건드렸다면 /rail/set_position 으로 실제 위치를 알려준다.
    """
    return _rail_call(rail.resume_position)


# ===== 보정 =====
# 리미트 스위치가 없어서 원점을 기계적으로 찾을 수 없다. 사용자가 양 끝을
# 직접 지정한다. 사용자가 요청할 때만 들어가는 별도 모드다.
#
#   1. POST /rail/calibration/begin   캐리지를 시작 지점에 두고 호출
#   2. POST /rail/jog                 종료 지점까지 조금씩 이동 (반복)
#   3. POST /rail/calibration/end     그 자리를 반대쪽 끝으로 확정, 저장
#
# 보정 중에는 소프트 리밋이 없다(스트로크를 아직 모른다). 대신 한 번에
# 움직일 수 있는 거리가 제한되고, 일반 이동(/rail/move 등)은 거부된다.


@app.get("/rail/ui", include_in_schema=False)
def rail_ui():
    """레일 보정·스테이션 등록용 웹 UI. 브라우저에서 이 주소를 열면 된다."""
    return FileResponse(STATIC_DIR / "rail.html")


@app.get("/rail/calibration")
def rail_calibration():
    """보정 상태. UI가 보정 버튼을 띄울지 판단하는 데 쓴다."""
    return _rail_call(rail.calibration)


@app.post("/rail/calibration/begin")
def rail_calibration_begin():
    """지금 캐리지가 있는 자리를 시작 지점(기준 0)으로 잡는다."""
    return _rail_call(rail.begin_calibration)


@app.post("/rail/calibration/end")
def rail_calibration_end():
    """지금 자리를 반대쪽 끝으로 확정하고 스트로크를 저장한다."""
    return _rail_call(rail.end_calibration)


@app.post("/rail/calibration/cancel")
def rail_calibration_cancel():
    """보정을 버린다. 스트로크는 그대로 두고 위치만 '모름'이 된다."""
    return _rail_call(rail.cancel_calibration)


@app.post("/rail/jog")
def rail_jog(req: JogRequest):
    """상대 이동. 보정 중에는 절대 기준 없이도 움직인다.

    보정 중이 아니면 /rail/move 와 같다(소프트 리밋 적용).
    """
    pos = _rail_call(rail.jog, req.mm, req.speed_mm_s)
    return {"position_mm": pos}


# ===== 스테이션 =====
# 레일 위 이름 붙은 지점들(시약존, 실험존 ...). 에이전트는 mm 대신 이름을 쓴다.
# 이름이 닫힌 집합이라 잘못된 값은 이동이 아니라 404 가 된다.


class StationRequest(BaseModel):
    name: str = Field(description="스테이션 이름 (예: 시약존)")
    mm: Optional[float] = Field(
        default=None,
        description="좌표(mm). 생략하면 지금 캐리지가 있는 자리를 쓴다(티치인)",
    )
    overwrite: bool = Field(default=False, description="같은 이름이 있으면 덮어쓴다")


class StationPatch(BaseModel):
    mm: Optional[float] = Field(default=None, description="새 좌표(mm)")
    name: Optional[str] = Field(default=None, description="새 이름")


class GotoRequest(BaseModel):
    station: str = Field(description="이동할 스테이션 이름")
    speed_mm_s: float = Field(default=5.0, gt=0, le=200)


def _station_call(fn, *args, **kwargs):
    """스테이션 호출의 예외를 HTTP 상태 코드로 옮긴다."""
    try:
        return fn(*args, **kwargs)
    except StationNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except StationStale as exc:
        # 사용자가 재등록하거나 revalidate 로 풀어야 하는 상태라 409.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except StationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _rail_frame():
    """스테이션 좌표의 기준이 되는 값 — 스트로크와 보정 id.

    보정 시각이 아니라 id 로 비교한다. 시각은 초 단위라 같은 초에 두 번
    보정하면 구분되지 않고, 그러면 낡은 좌표를 잡아내지 못한다.
    """
    cal = _rail_call(rail.calibration)
    return cal["stroke_mm"], cal["calibration_id"]


@app.get("/rail/stations")
def rail_stations():
    stroke, at = _rail_frame()
    return _station_call(stations.list, stroke, at)


@app.post("/rail/stations")
def rail_station_add(req: StationRequest):
    """스테이션 등록. mm 를 생략하면 지금 캐리지가 있는 자리를 기록한다.

    자로 재서 넣는 것보다 캐리지를 그 자리로 옮긴 뒤 이름만 붙이는 편이
    정확하다. UI 의 '여기를 등록' 버튼이 이걸 쓴다.
    """
    stroke, at = _rail_frame()
    mm = req.mm
    if mm is None:
        cal = _rail_call(rail.calibration)
        if not cal["position_known"]:
            raise HTTPException(
                status_code=400,
                detail="현재 위치를 모르는 상태라 티치인할 수 없습니다. "
                       "/rail/resume 또는 /rail/set_position 을 먼저 부르세요.",
            )
        mm = cal["position_mm"]
    return _station_call(stations.add, req.name, mm, stroke, at, overwrite=req.overwrite)


@app.patch("/rail/stations/{name}")
def rail_station_update(name: str, req: StationPatch):
    stroke, at = _rail_frame()
    return _station_call(
        stations.update, name, stroke, at, position_mm=req.mm, new_name=req.name
    )


@app.delete("/rail/stations/{name}")
def rail_station_remove(name: str):
    return _station_call(stations.remove, name)


@app.post("/rail/stations/revalidate")
def rail_stations_revalidate():
    """재보정 후에도 좌표가 그대로 유효하다고 사용자가 확인해 준다.

    보정 원점이 바뀌면 저장된 mm 가 엉뚱한 곳을 가리킬 수 있어서 기본적으로
    막아 둔다. 같은 자리에서 다시 보정한 경우처럼 실제로 안 바뀌었다면 이걸로 푼다.
    """
    _, at = _rail_frame()
    return _station_call(stations.revalidate, at)


@app.post("/rail/goto")
def rail_goto(req: GotoRequest):
    """이름으로 이동한다. **에이전트가 쓸 것은 이것이다.**

    /rail/move_to 는 mm 를 직접 받으므로 사람이 보정·디버깅할 때만 쓴다.
    """
    stroke, at = _rail_frame()
    target = _station_call(stations.target_mm, req.station, stroke, at)
    pos = _rail_call(rail.move_to, target, req.speed_mm_s)
    return {"station": req.station, "position_mm": pos}


@app.get("/rail/where")
def rail_where():
    """지금 어느 스테이션에 있나. 가장 가까운 것과의 거리도 같이 준다."""
    cal = _rail_call(rail.calibration)
    if not cal["position_known"]:
        return {"position_known": False, "station": None, "nearest": None}

    pos = cal["position_mm"]
    listed = stations.list(cal["stroke_mm"], cal["calibrated_at"])["stations"]
    if not listed:
        return {"position_known": True, "position_mm": pos, "station": None, "nearest": None}

    nearest = min(listed, key=lambda st: abs(st["position_mm"] - pos))
    delta = round(nearest["position_mm"] - pos, 3)
    return {
        "position_known": True,
        "position_mm": pos,
        # 0.5mm 안쪽이면 그 스테이션에 있는 것으로 본다. 스텝 분해능이
        # 1/160mm 라 정확히 일치하는 경우는 드물다.
        "station": nearest["name"] if abs(delta) <= 0.5 else None,
        "nearest": {"name": nearest["name"], "delta_mm": delta},
    }
