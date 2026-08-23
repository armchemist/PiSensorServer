"""ThermoEye TMC160F 열화상 카메라 (V4L2 / Y16).

일반 USB 카메라와 달리 OpenCV 로 열지 않는다. 이 장치는 Y16
(16비트 그레이스케일) 한 가지 포맷만 내보내는데, OpenCV 의 Y16 처리는 버전마다
달라서 16비트가 8비트로 뭉개지는 경우가 있다. 그래서 V4L2 를 직접 ioctl 로
다룬다. 커널 uvcvideo 가 프레임 재조립까지 해주므로 mmap 으로 받기만 하면 된다.

픽셀 값의 단위는 **센티켈빈**이다. 섭씨로 바꾸려면 값/100 - 273.15.
실온에서 29800 안팎(=25℃)이 나온다.

전원이 들어간 직후 약 10프레임(~1초)은 실제 측정값이 아니라 고정 패턴(0~242)을
내보내고, 그 다음 한 장은 값이 크게 튄다. 이후부터 정상이다. 그래서 켈빈 범위를
벗어난 프레임은 버린다.

주의: 이 센서는 프레임 전체에 고정 패턴(왼쪽 가장자리가 밝고 중앙이 비네팅)이
있다. 화면의 서로 다른 지점끼리 절대 온도를 비교하는 건 믿을 게 못 되지만,
고정된 ROI 하나를 시간에 따라 추적하는 용도라면 그 패턴이 상쇄되므로 괜찮다.
"""

import fcntl
import mmap
import os
import select
import struct
import threading
import time
import uuid
from collections import deque

try:
    import numpy as np
except ImportError:  # numpy 가 없어도 서버는 떠야 한다
    np = None

from .. import config

# --- V4L2 ioctl 상수 -------------------------------------------------------
# _IOWR('V', n, size) = (3 << 30) | (size << 16) | (ord('V') << 8) | n
# 구조체 크기는 64비트(arm64/x86_64) 기준이다.
VIDIOC_S_FMT = 0xC0D05605
VIDIOC_REQBUFS = 0xC0145608
VIDIOC_QUERYBUF = 0xC0585609
VIDIOC_QBUF = 0xC058560F
VIDIOC_DQBUF = 0xC0585611
VIDIOC_STREAMON = 0x40045612
VIDIOC_STREAMOFF = 0x40045613

BUF_TYPE_VIDEO_CAPTURE = 1
MEMORY_MMAP = 1
PIX_FMT_Y16 = 0x20363159  # 'Y16 '

BUFFER_COUNT = 4

# 정상 프레임 판정 범위(센티켈빈). 워밍업 중의 고정 패턴(0~242)과 그 직후의
# 튀는 프레임(0~65535)은 둘 다 min 이 0 이라 여기서 걸러진다.
VALID_MIN_CK = 20000  # -73.15℃
VALID_MAX_CK = 45000  # 176.85℃

ABSOLUTE_ZERO_C = 273.15


def ck_to_c(value):
    """센티켈빈 → 섭씨."""
    return value / 100.0 - ABSOLUTE_ZERO_C


class Roi:
    """추적할 관심영역. 좌표는 항상 센서 픽셀 기준(160x120)이다."""

    def __init__(self, x, y, w, h, name=None):
        self.id = uuid.uuid4().hex[:8]
        self.name = name or f"ROI {self.id[:4]}"
        self.x, self.y, self.w, self.h = x, y, w, h
        self.created_at = time.time()
        self.series = deque(maxlen=config.THERMAL_SERIES_MAX)
        self.last = None

    def as_dict(self, with_series=False, limit=None):
        d = {
            "id": self.id,
            "name": self.name,
            "x": self.x,
            "y": self.y,
            "w": self.w,
            "h": self.h,
            "created_at": self.created_at,
            "samples": len(self.series),
            "last": self.last,
        }
        if with_series:
            items = list(self.series)
            if limit is not None:
                items = items[-limit:]
            d["series"] = [
                {"t": t, "mean_c": mean, "min_c": mn, "max_c": mx}
                for t, mean, mn, mx in items
            ]
        return d


class ThermalCamera:
    """백그라운드로 계속 프레임을 읽으면서 등록된 ROI 의 통계를 쌓는다.

    센서가 9fps 로 고정돼 있고 시계열에 구멍이 나면 안 되므로, 요청이 올 때 읽는
    게 아니라 스레드가 계속 돌면서 최신 프레임을 들고 있는 구조다.
    """

    WIDTH, HEIGHT = 160, 120
    FRAME_BYTES = WIDTH * HEIGHT * 2

    def __init__(self):
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()
        self._frame = None  # 최신 정상 프레임 (uint16, 센티켈빈)
        self._frame_at = None
        self._frames_seen = 0
        self._error = None
        self._opened = False
        self._rois = {}
        self._last_sample_at = 0.0

    # --- 수명 주기 ---------------------------------------------------------

    def start(self):
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="thermal", daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    # --- 캡처 루프 ---------------------------------------------------------

    def _run(self):
        """장치가 없거나 도중에 빠져도 죽지 않고 계속 재시도한다."""
        while not self._stop.is_set():
            try:
                self._capture_forever()
            except Exception as exc:
                with self._lock:
                    self._error = f"{type(exc).__name__}: {exc}"
                    self._opened = False
            self._stop.wait(2.0)

    def _capture_forever(self):
        if np is None:
            raise RuntimeError("numpy 미설치 (pip install numpy)")

        fd = os.open(config.THERMAL_DEVICE, os.O_RDWR)
        buffers = []
        streaming = False
        try:
            self._set_format(fd)
            buffers = self._map_buffers(fd)
            fcntl.ioctl(fd, VIDIOC_STREAMON, struct.pack("<I", BUF_TYPE_VIDEO_CAPTURE))
            streaming = True
            with self._lock:
                self._opened = True
                self._error = None

            while not self._stop.is_set():
                # 9fps 라 한 장에 111ms. 2초를 넘기면 장치에 문제가 있는 것이다.
                ready, _, _ = select.select([fd], [], [], 2.0)
                if not ready:
                    raise RuntimeError("프레임 타임아웃 (2초)")
                self._grab_one(fd, buffers)
        finally:
            if streaming:
                try:
                    fcntl.ioctl(
                        fd, VIDIOC_STREAMOFF, struct.pack("<I", BUF_TYPE_VIDEO_CAPTURE)
                    )
                except OSError:
                    pass
            for mm in buffers:
                mm.close()
            os.close(fd)
            with self._lock:
                self._opened = False

    def _set_format(self, fd):
        fmt = bytearray(208)
        struct.pack_into("<I", fmt, 0, BUF_TYPE_VIDEO_CAPTURE)
        # v4l2_format.fmt.pix 는 8바이트 뒤부터: width, height, pixelformat, field
        struct.pack_into("<IIII", fmt, 8, self.WIDTH, self.HEIGHT, PIX_FMT_Y16, 0)
        fcntl.ioctl(fd, VIDIOC_S_FMT, fmt)
        w, h, pixfmt = struct.unpack_from("<III", fmt, 8)
        if (w, h) != (self.WIDTH, self.HEIGHT) or pixfmt != PIX_FMT_Y16:
            raise RuntimeError(
                f"기대한 포맷이 아님: {w}x{h} fmt=0x{pixfmt:08x} "
                "(Y16 160x120 이어야 함)"
            )

    def _map_buffers(self, fd):
        req = bytearray(20)
        struct.pack_into(
            "<III", req, 0, BUFFER_COUNT, BUF_TYPE_VIDEO_CAPTURE, MEMORY_MMAP
        )
        fcntl.ioctl(fd, VIDIOC_REQBUFS, req)
        count = struct.unpack_from("<I", req, 0)[0]
        if count == 0:
            raise RuntimeError("버퍼를 할당하지 못함")

        buffers = []
        for i in range(count):
            buf = self._buf_struct(i)
            fcntl.ioctl(fd, VIDIOC_QUERYBUF, buf)
            offset = struct.unpack_from("<I", buf, 64)[0]
            length = struct.unpack_from("<I", buf, 72)[0]
            buffers.append(
                mmap.mmap(fd, length, mmap.MAP_SHARED, mmap.PROT_READ, offset=offset)
            )
            fcntl.ioctl(fd, VIDIOC_QBUF, buf)
        return buffers

    @staticmethod
    def _buf_struct(index):
        """v4l2_buffer. index/type 은 맨 앞, memory 는 60바이트째에 있다."""
        buf = bytearray(88)
        struct.pack_into("<II", buf, 0, index, BUF_TYPE_VIDEO_CAPTURE)
        struct.pack_into("<I", buf, 60, MEMORY_MMAP)
        return buf

    def _grab_one(self, fd, buffers):
        buf = self._buf_struct(0)
        fcntl.ioctl(fd, VIDIOC_DQBUF, buf)
        index, _, used = struct.unpack_from("<III", buf, 0)
        try:
            if used >= self.FRAME_BYTES:
                raw = buffers[index][: self.FRAME_BYTES]
                frame = np.frombuffer(raw, dtype="<u2").reshape(self.HEIGHT, self.WIDTH)
                self._on_frame(frame.copy())
        finally:
            fcntl.ioctl(fd, VIDIOC_QBUF, buf)

    def _on_frame(self, frame):
        lo, hi = int(frame.min()), int(frame.max())
        if lo < VALID_MIN_CK or hi > VALID_MAX_CK:
            return  # 워밍업 중이거나 값이 튄 프레임
        now = time.time()
        with self._lock:
            self._frame = frame
            self._frame_at = now
            self._frames_seen += 1
            # 시계열은 9fps 를 그대로 쌓으면 금방 넘치므로 간격을 두고 찍는다.
            due = now - self._last_sample_at >= config.THERMAL_SAMPLE_INTERVAL_S
            if due:
                self._last_sample_at = now
            rois = list(self._rois.values())

        # 통계 계산은 락 밖에서 한다. ROI 가 많아도 캡처를 막지 않는다.
        for roi in rois:
            patch = frame[roi.y : roi.y + roi.h, roi.x : roi.x + roi.w]
            if patch.size == 0:
                continue
            mean_c = round(ck_to_c(float(patch.mean())), 3)
            min_c = round(ck_to_c(float(patch.min())), 3)
            max_c = round(ck_to_c(float(patch.max())), 3)
            roi.last = {"t": now, "mean_c": mean_c, "min_c": min_c, "max_c": max_c}
            if due:
                roi.series.append((now, mean_c, min_c, max_c))

    # --- 조회 -------------------------------------------------------------

    def latest(self):
        """최신 프레임(uint16 센티켈빈)과 촬영 시각. 없으면 (None, None)."""
        with self._lock:
            if self._frame is None:
                return None, None
            return self._frame, self._frame_at

    def require_frame(self):
        frame, at = self.latest()
        if frame is None:
            with self._lock:
                err = self._error
            raise RuntimeError(err or "아직 정상 프레임이 없습니다 (워밍업 ~1초)")
        return frame, at

    def frame_stats(self):
        """현재 프레임 전체의 온도 통계(℃)와 가장 뜨거운 픽셀 위치."""
        frame, at = self.require_frame()
        hot_y, hot_x = np.unravel_index(int(frame.argmax()), frame.shape)
        return {
            "t": at,
            "min_c": round(ck_to_c(float(frame.min())), 3),
            "max_c": round(ck_to_c(float(frame.max())), 3),
            "mean_c": round(ck_to_c(float(frame.mean())), 3),
            "hotspot": {"x": int(hot_x), "y": int(hot_y)},
        }

    def status(self):
        with self._lock:
            age = (
                None
                if self._frame_at is None
                else round(time.time() - self._frame_at, 2)
            )
            return {
                "available": np is not None,
                "opened": self._opened,
                "device": config.THERMAL_DEVICE,
                "resolution": f"{self.WIDTH}x{self.HEIGHT}",
                "frames": self._frames_seen,
                "age_s": age,
                "rois": len(self._rois),
                "sample_interval_s": config.THERMAL_SAMPLE_INTERVAL_S,
                "error": self._error,
            }

    # --- ROI --------------------------------------------------------------

    def clamp_roi(self, x, y, w, h):
        """센서 밖으로 나간 좌표를 잘라 넣는다. 최소 1픽셀은 남긴다."""
        x = max(0, min(int(x), self.WIDTH - 1))
        y = max(0, min(int(y), self.HEIGHT - 1))
        w = max(1, min(int(w), self.WIDTH - x))
        h = max(1, min(int(h), self.HEIGHT - y))
        return x, y, w, h

    def add_roi(self, x, y, w, h, name=None):
        roi = Roi(*self.clamp_roi(x, y, w, h), name=name)
        with self._lock:
            self._rois[roi.id] = roi
        return roi

    def get_roi(self, roi_id):
        with self._lock:
            return self._rois.get(roi_id)

    def list_rois(self):
        with self._lock:
            return list(self._rois.values())

    def remove_roi(self, roi_id):
        with self._lock:
            return self._rois.pop(roi_id, None)

    def clear_rois(self):
        with self._lock:
            self._rois.clear()

    def reset_series(self, roi_id=None):
        with self._lock:
            targets = (
                list(self._rois.values())
                if roi_id is None
                else [self._rois.get(roi_id)]
            )
            for roi in targets:
                if roi is not None:
                    roi.series.clear()


thermal = ThermalCamera()
