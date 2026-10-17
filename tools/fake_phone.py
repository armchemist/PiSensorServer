"""스마트폰 앱 대역. 앱이 없을 때 /ws/phone, /ws/phone/video 를 손으로 확인한다.

    python tools/fake_phone.py ws://127.0.0.1:8100 [--jpeg 사진.jpg]

- /ws/phone 에 hello·status 를 보내고, 서버가 보내는 expression/track 을 출력한다.
  track start 를 받으면 가짜 track_result 를 1초마다 보낸다.
- --jpeg 를 주면 /ws/phone/video 로 그 파일을 1fps 로 보낸다.

websockets 패키지가 필요하다 (uvicorn[standard] 에 포함).
"""

import argparse
import json
import threading
import time

from websockets.sync.client import connect

ap = argparse.ArgumentParser()
ap.add_argument("base", help="예: ws://192.168.0.10:8100")
ap.add_argument("--device-id", default="phone1")
ap.add_argument("--jpeg")
args = ap.parse_args()
tracking = threading.Event()


def video():
    jpeg = open(args.jpeg, "rb").read()
    with connect(f"{args.base}/ws/phone/video") as ws:
        ws.send(json.dumps({"type": "hello", "device_id": args.device_id}))
        while True:
            ws.send(jpeg)
            time.sleep(1)


if args.jpeg:
    threading.Thread(target=video, daemon=True).start()

with connect(f"{args.base}/ws/phone") as ws:
    ws.send(json.dumps({"type": "hello", "device_id": args.device_id,
                        "capabilities": ["camera", "tracking", "face"]}))
    ws.send(json.dumps({"type": "status", "camera": "streaming" if args.jpeg else "off", "tracking": False}))
    print("연결됨. 서버 메시지를 기다린다 (Ctrl+C 종료)", flush=True)
    while True:
        try:
            msg = json.loads(ws.recv(timeout=1))
            print("받음:", msg, flush=True)
            if msg.get("type") == "track":
                (tracking.set if msg["action"] == "start" else tracking.clear)()
                ws.send(json.dumps({"type": "status", "camera": "streaming", "tracking": tracking.is_set()}))
        except TimeoutError:
            pass
        if tracking.is_set():
            ws.send(json.dumps({"type": "track_result", "target": "face", "found": True, "tracking_id": 1,
                                "bbox": [0.4, 0.3, 0.2, 0.25], "ts": time.time()}))
