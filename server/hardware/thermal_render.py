"""열화상 프레임(센티켈빈 uint16)을 사람이 볼 수 있는 JPEG 로 바꾼다.

측정 자체와는 무관한 표시 전용 코드라서 thermal.py 와 분리했다. thermal.py 는
numpy 만 있으면 돌아가고, 여기서만 OpenCV 를 쓴다.
"""

import time

try:
    import cv2
except ImportError:  # opencv 미설치 시에도 서버는 떠야 한다
    cv2 = None

try:
    import numpy as np
except ImportError:
    np = None

from .thermal import ck_to_c, thermal

COLORMAPS = {}
if cv2 is not None:
    COLORMAPS = {
        "inferno": cv2.COLORMAP_INFERNO,
        "jet": cv2.COLORMAP_JET,
        "turbo": cv2.COLORMAP_TURBO,
        "hot": cv2.COLORMAP_HOT,
        "magma": cv2.COLORMAP_MAGMA,
        "gray": None,  # 컬러맵 없이 흑백
    }

# ROI 사각형 색(BGR). 여러 개를 구분할 수 있게 순서대로 돌려 쓴다.
ROI_COLORS = [
    (255, 255, 255),
    (0, 255, 255),
    (255, 255, 0),
    (0, 255, 0),
    (255, 0, 255),
]


def _require_deps():
    if cv2 is None:
        raise RuntimeError("opencv 미설치 (sudo apt install -y python3-opencv)")
    if np is None:
        raise RuntimeError("numpy 미설치 (pip install numpy)")


def scale_range(frame, tmin=None, tmax=None):
    """표시에 쓸 온도 범위(℃)를 정한다.

    지정하지 않으면 프레임의 2~98 백분위로 잡는다. 최소·최대를 그대로 쓰면
    핫픽셀 하나 때문에 화면 전체가 어두워지기 때문이다.
    """
    if tmin is None or tmax is None:
        lo, hi = np.percentile(frame, [2, 98])
        auto_min, auto_max = ck_to_c(float(lo)), ck_to_c(float(hi))
        tmin = auto_min if tmin is None else tmin
        tmax = auto_max if tmax is None else tmax
    if tmax - tmin < 0.5:  # 너무 좁으면 노이즈만 크게 보인다
        mid = (tmin + tmax) / 2
        tmin, tmax = mid - 0.25, mid + 0.25
    return float(tmin), float(tmax)


def render(
    frame,
    width=640,
    colormap="inferno",
    tmin=None,
    tmax=None,
    smooth=False,
    rois=(),
    quality=85,
):
    """프레임을 JPEG 바이트로. 실제로 쓴 온도 범위도 같이 돌려준다."""
    _require_deps()

    tmin, tmax = scale_range(frame, tmin, tmax)
    celsius = frame.astype(np.float32) / 100.0 - 273.15
    norm = (celsius - tmin) / (tmax - tmin)
    img = np.clip(norm * 255.0, 0, 255).astype(np.uint8)

    cmap = COLORMAPS.get(colormap, cv2.COLORMAP_INFERNO)
    img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR) if cmap is None else cv2.applyColorMap(img, cmap)

    # 160x120 은 너무 작아서 그대로 못 쓴다. 기본은 NEAREST — bbox 를 픽셀 경계에
    # 맞춰 찍어야 하므로 보간으로 뭉개지 않는 편이 낫다.
    height = round(width * frame.shape[0] / frame.shape[1])
    img = cv2.resize(
        img,
        (width, height),
        interpolation=cv2.INTER_CUBIC if smooth else cv2.INTER_NEAREST,
    )

    sx, sy = width / frame.shape[1], height / frame.shape[0]
    for i, roi in enumerate(rois):
        color = ROI_COLORS[i % len(ROI_COLORS)]
        p1 = (round(roi.x * sx), round(roi.y * sy))
        p2 = (round((roi.x + roi.w) * sx), round((roi.y + roi.h) * sy))
        cv2.rectangle(img, p1, p2, color, 2)
        # 라벨은 ASCII 만 — cv2 는 한글 글꼴이 없어서 이름 대신 번호와 온도를 찍는다.
        label = f"#{i + 1}"
        if roi.last:
            label += f" {roi.last['mean_c']:.2f}C"
        ty = p1[1] - 6 if p1[1] > 18 else p2[1] + 16
        cv2.putText(img, label, (p1[0], ty), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)

    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise RuntimeError("JPEG 인코딩 실패")
    return buf.tobytes(), (tmin, tmax)


def stream(fps=9, **kwargs):
    """MJPEG 용 제너레이터. 센서가 9fps 라 그보다 높이 잡을 이유는 없다."""
    interval = 1.0 / max(1, fps)
    last_at = None
    while True:
        started = time.monotonic()
        frame, at = thermal.latest()
        # 새 프레임이 아직 안 왔으면 굳이 다시 인코딩하지 않는다.
        if frame is not None and at != last_at:
            last_at = at
            jpeg, _ = render(frame, rois=thermal.list_rois(), **kwargs)
            yield jpeg
        remaining = interval - (time.monotonic() - started)
        if remaining > 0:
            time.sleep(remaining)
