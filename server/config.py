"""환경변수 기반 설정.

하드웨어 구성이 자주 바뀌므로 코드를 고치지 않고 환경변수로 조정한다.
"""

import os


def _float(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


# --- 아두이노 (pH / 전도도) ---
# 미지정이면 /dev/ttyUSB*, /dev/ttyACM* 를 훑어서 먼저 찾은 것을 쓴다.
SERIAL_PORT = os.environ.get("SERIAL_PORT") or None
SERIAL_BAUD = _int("SERIAL_BAUD", 9600)

# 마지막 측정값이 이 시간(초)보다 오래되면 stale 로 표시한다.
# 아두이노는 1초마다 보내므로 몇 초 이상 끊기면 문제가 있다는 뜻.
SENSOR_STALE_AFTER_S = _float("SENSOR_STALE_AFTER_S", 5.0)

# --- 카메라 ---
CAMERA_INDEX = _int("CAMERA_INDEX", 0)
CAMERA_WIDTH = _int("CAMERA_WIDTH", 1280)
CAMERA_HEIGHT = _int("CAMERA_HEIGHT", 720)

# --- 인증 ---
# 설정하면 모든 요청에 X-API-Key 헤더가 필요하다.
# 외부(Tailscale/터널)로 노출하기 전에 반드시 설정할 것.
API_KEY = os.environ.get("API_KEY") or None
