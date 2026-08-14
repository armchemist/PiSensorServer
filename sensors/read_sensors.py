#!/usr/bin/env python3
"""아두이노가 USB 시리얼로 보내는 pH/전도도 값을 읽는다.

아두이노 쪽 스케치는 sensors/arduino_sensors/arduino_sensors.ino.

    python3 read_sensors.py                 # 화면에 출력
    python3 read_sensors.py --csv log.csv   # 화면 출력 + CSV 기록
    python3 read_sensors.py --port /dev/ttyACM0
"""

import argparse
import csv
import glob
import json
import sys
import time
from datetime import datetime

try:
    import serial
except ImportError:
    sys.exit("pyserial이 없습니다:  sudo apt install -y python3-serial")

BAUD = 9600
FIELDS = ["ph", "ph_v", "ec", "tds", "tds_v", "temp"]


def find_port():
    """우노는 보통 /dev/ttyACM0, 호환보드(CH340)는 /dev/ttyUSB0 로 잡힌다."""
    ports = sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*"))
    if not ports:
        sys.exit(
            "아두이노를 찾을 수 없습니다.\n"
            "  - USB 케이블 연결 확인\n"
            "  - ls /dev/ttyACM* /dev/ttyUSB*\n"
            "  - 권한 오류면:  sudo usermod -aG dialout $USER  (후 재로그인)"
        )
    return ports[0]


def open_serial(port):
    ser = serial.Serial(port, BAUD, timeout=2)
    # 우노는 시리얼 포트가 열리면 DTR 신호로 자동 리셋된다.
    # 부팅 중에 나오는 깨진 출력을 버리려면 잠깐 기다렸다 버퍼를 비운다.
    time.sleep(2)
    ser.reset_input_buffer()
    return ser


def read_one(ser):
    """JSON 한 줄을 읽어 dict로. 파싱 실패하면 None."""
    raw = ser.readline().decode("utf-8", errors="replace").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None  # 리셋 직후의 잘린 줄 등


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", help="시리얼 포트 (미지정 시 자동 탐색)")
    ap.add_argument("--csv", help="측정값을 기록할 CSV 파일")
    args = ap.parse_args()

    port = args.port or find_port()
    print(f"포트: {port}  (Ctrl+C로 종료)")

    ser = open_serial(port)

    writer = None
    csv_file = None
    if args.csv:
        csv_file = open(args.csv, "a", newline="")
        writer = csv.writer(csv_file)
        if csv_file.tell() == 0:
            writer.writerow(["time"] + FIELDS)

    bad = 0
    try:
        while True:
            data = read_one(ser)
            if data is None:
                bad += 1
                if bad % 10 == 0:
                    print(f"  (읽기 실패 {bad}회 - 스케치가 업로드됐는지 확인)")
                continue
            bad = 0

            now = datetime.now()
            print(
                f"{now:%H:%M:%S}  "
                f"pH {data.get('ph', float('nan')):5.2f} ({data.get('ph_v', 0):.3f}V)   "
                f"EC {data.get('ec', float('nan')):7.1f} uS/cm   "
                f"TDS {data.get('tds', float('nan')):7.1f} ppm"
            )

            if writer:
                writer.writerow(
                    [now.isoformat(timespec="seconds")]
                    + [data.get(k, "") for k in FIELDS]
                )
                csv_file.flush()

    except KeyboardInterrupt:
        print("\n종료")
    finally:
        ser.close()
        if csv_file:
            csv_file.close()


if __name__ == "__main__":
    main()
