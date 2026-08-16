"""USB 카메라 캡처.

핸들을 열어둔 채 재사용한다. 요청마다 열고 닫으면 1초 가까이 걸린다.
동시 요청이 같은 핸들을 건드리지 않도록 락으로 감싼다.
"""

import threading
import time

try:
    import cv2
except ImportError:  # opencv 미설치 시에도 서버는 떠야 한다
    cv2 = None

from .. import config


class Camera:
    def __init__(self):
        self._cap = None
        self._error = None
        self._lock = threading.Lock()

    def _ensure_open(self):
        """호출자가 이미 락을 잡고 있어야 한다."""
        if cv2 is None:
            raise RuntimeError("opencv 미설치 (sudo apt install python3-opencv)")
        if self._cap is not None and self._cap.isOpened():
            return
        cap = cv2.VideoCapture(config.CAMERA_INDEX)
        if not cap.isOpened():
            cap.release()
            raise RuntimeError(
                f"카메라를 열 수 없음 (index={config.CAMERA_INDEX}). "
                "ls /dev/video* 로 확인하세요"
            )
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, config.CAMERA_WIDTH)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, config.CAMERA_HEIGHT)
        self._cap = cap

    def _grab_jpeg(self, quality, discard_stale):
        """호출자가 이미 락을 잡고 있어야 한다."""
        self._ensure_open()
        if discard_stale:
            # 단발 촬영에서는 버퍼에 남아 있던 오래된 프레임을 한 장 버린다.
            # 스트리밍에서는 계속 읽으므로 버릴 필요가 없다.
            self._cap.read()
        ok, frame = self._cap.read()
        if not ok:
            self.close_locked()
            raise RuntimeError("프레임을 읽지 못함")
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        if not ok:
            raise RuntimeError("JPEG 인코딩 실패")
        self._error = None
        return buf.tobytes(), frame.shape

    def capture_jpeg(self, quality=90):
        """JPEG 바이트를 반환한다."""
        with self._lock:
            try:
                return self._grab_jpeg(quality, discard_stale=True)
            except Exception as exc:
                self._error = f"{type(exc).__name__}: {exc}"
                raise

    def stream_jpeg(self, fps=10, quality=80):
        """JPEG 바이트를 계속 내보내는 제너레이터.

        프레임마다 락을 잡았다 놓는다. 스트리밍 중에도 /camera/capture 가
        끼어들 수 있어야 하기 때문이다.
        """
        interval = 1.0 / max(1, fps)
        while True:
            started = time.monotonic()
            with self._lock:
                try:
                    jpeg, _ = self._grab_jpeg(quality, discard_stale=False)
                except Exception as exc:
                    self._error = f"{type(exc).__name__}: {exc}"
                    raise
            yield jpeg
            # 파이 CPU를 다 쓰지 않도록 프레임 간격을 맞춘다
            remaining = interval - (time.monotonic() - started)
            if remaining > 0:
                time.sleep(remaining)

    def status(self):
        with self._lock:
            return {
                "available": cv2 is not None,
                "opened": self._cap is not None and self._cap.isOpened(),
                "index": config.CAMERA_INDEX,
                "error": self._error,
            }

    def close_locked(self):
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def close(self):
        with self._lock:
            self.close_locked()


camera = Camera()
