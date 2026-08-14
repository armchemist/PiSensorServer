"""아두이노에서 오는 pH/전도도 값을 백그라운드로 계속 읽어 캐시한다.

요청이 올 때마다 시리얼을 읽으면 안 되는 이유가 두 가지 있다.
  1. 아두이노는 1초 주기로 값을 보내므로, 요청 시점에 읽으면 최대 1초를 기다린다
  2. 시리얼 포트는 한 번에 하나만 열 수 있어서 동시 요청을 처리할 수 없다

그래서 스레드 하나가 계속 읽어 최신값을 들고 있고, API는 그 값을 즉시 돌려준다.
연결이 끊기면 백그라운드에서 알아서 재연결을 시도한다.
"""

import glob
import json
import threading
import time
from datetime import datetime

try:
    import serial
except ImportError:  # pyserial 미설치 시에도 서버는 떠야 한다
    serial = None

from .. import config

RECONNECT_DELAY_S = 3.0


def find_port():
    """아두이노로 보이는 시리얼 포트를 찾는다. 없으면 None."""
    # 리눅스: CH340 계열은 ttyUSB, 정품 우노는 ttyACM
    # macOS(개발용): cu.usbserial / cu.usbmodem
    patterns = [
        "/dev/ttyUSB*",
        "/dev/ttyACM*",
        "/dev/cu.usbserial*",
        "/dev/cu.usbmodem*",
    ]
    for pattern in patterns:
        found = sorted(glob.glob(pattern))
        if found:
            return found[0]
    return None


class SensorReader:
    def __init__(self):
        self._latest = None
        self._latest_at = 0.0
        self._port = None
        self._error = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None

    # --- 수명 관리 ---

    def start(self):
        if serial is None:
            self._error = "pyserial 미설치 (sudo apt install python3-serial)"
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3)

    # --- 조회 ---

    def latest(self):
        """마지막 측정값 + 얼마나 오래됐는지. 아직 값이 없으면 None."""
        with self._lock:
            if self._latest is None:
                return None
            age = time.time() - self._latest_at
            data = dict(self._latest)
        data["age_s"] = round(age, 2)
        data["stale"] = age > config.SENSOR_STALE_AFTER_S
        return data

    def status(self):
        with self._lock:
            has_data = self._latest is not None
            age = time.time() - self._latest_at if has_data else None
            return {
                "connected": self._error is None and has_data
                and age <= config.SENSOR_STALE_AFTER_S,
                "port": self._port,
                "error": self._error,
                "age_s": round(age, 2) if age is not None else None,
            }

    # --- 내부 ---

    def _run(self):
        while not self._stop_event.is_set():
            port = config.SERIAL_PORT or find_port()
            if port is None:
                self._set_error("아두이노를 찾을 수 없음 (USB 연결 확인)")
                self._stop_event.wait(RECONNECT_DELAY_S)
                continue
            try:
                self._read_loop(port)
            except Exception as exc:  # 끊김/권한 등 무엇이든 재시도한다
                self._set_error(f"{type(exc).__name__}: {exc}")
                self._stop_event.wait(RECONNECT_DELAY_S)

    def _read_loop(self, port):
        ser = serial.Serial(port, config.SERIAL_BAUD, timeout=2)
        try:
            # 우노는 포트를 열면 DTR로 자동 리셋된다. 부팅 중 깨진 출력을 버린다.
            time.sleep(2)
            ser.reset_input_buffer()
            with self._lock:
                self._port = port
                self._error = None

            while not self._stop_event.is_set():
                raw = ser.readline().decode("utf-8", errors="replace").strip()
                if not raw:
                    continue
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue  # 리셋 직후의 잘린 줄 등은 그냥 버린다
                data["ts"] = datetime.now().isoformat(timespec="seconds")
                with self._lock:
                    self._latest = data
                    self._latest_at = time.time()
        finally:
            ser.close()

    def _set_error(self, message):
        with self._lock:
            self._error = message


reader = SensorReader()
