"""rosbridge 서비스와 이미지 토픽. 최신 프레임만 보관하고 요청 ID로 응답을 구분한다."""
import base64
import json
import threading
import time
from uuid import uuid4

import websocket

from .runtime import Cancelled, ExecutionError, Frame, PauseReceipt


def stamp_seconds(message):
    stamp = message.get("header", {}).get("stamp", {})
    return stamp.get("sec", 0) + stamp.get("nanosec", 0) / 1e9


class Rosbridge:
    def __init__(self, url):
        self.ws = websocket.create_connection(url, timeout=5)
        self.cv = threading.Condition()
        self.send_lock = threading.Lock()
        self.pending, self.frames, self.subscribed = {}, {}, set()
        self.error, self.closed = None, False
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _send(self, payload):
        with self.send_lock:
            self.ws.send(json.dumps(payload))

    def _read(self):
        try:
            while not self.closed:
                try:
                    raw = self.ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if not raw:
                    raise ExecutionError("ROS 연결 종료")
                msg = json.loads(raw)
                with self.cv:
                    if msg.get("op") == "service_response" and msg.get("id") in self.pending:
                        self.pending[msg["id"]] = msg
                    elif msg.get("op") == "publish" and msg.get("topic") in self.subscribed:
                        self.frames[msg["topic"]] = msg["msg"]
                    self.cv.notify_all()
        except Exception as exc:
            with self.cv:
                self.error = f"ROS 연결 오류: {type(exc).__name__}"
                self.cv.notify_all()

    def call(self, service, service_type, args, timeout=15):
        request_id = uuid4().hex
        with self.cv:
            self.pending[request_id] = None
        try:
            self._send({"op": "call_service", "id": request_id, "service": service,
                        "type": service_type, "args": args})
            with self.cv:
                ready = self.cv.wait_for(lambda: self.pending[request_id] is not None or self.error,
                                         timeout=timeout)
                if self.error:
                    raise ExecutionError(self.error)
                if not ready:
                    raise ExecutionError(f"ROS 서비스 시간 초과: {service}")
                response = self.pending[request_id]
                if response.get("result") is False:
                    raise ExecutionError(f"ROS 서비스 거절: {service}")
                return response.get("values", {})
        finally:
            with self.cv:
                self.pending.pop(request_id, None)

    def subscribe(self, topics):
        for topic in topics:
            if topic in self.subscribed:
                continue
            with self.cv:
                self.subscribed.add(topic)
            self._send({"op": "subscribe", "topic": topic, "type": "sensor_msgs/msg/CompressedImage",
                        "queue_length": 1, "throttle_rate": 100})

    def capture(self, topics, after, timeout=10):
        deadline = time.monotonic() + timeout
        with self.cv:
            while True:
                if self.error:
                    raise ExecutionError(self.error)
                messages = [self.frames.get(t, {}) for t in topics]
                if messages and all(stamp_seconds(m) > after for m in messages):
                    times = [stamp_seconds(m) for m in messages]
                    if max(times) - min(times) > 0.5:
                        raise ExecutionError("카메라 프레임의 촬영 시각 차이가 큽니다")
                    frames = []
                    for topic, msg in zip(topics, messages):
                        fmt = msg.get("format", "").lower()
                        mime = "image/jpeg" if "jpeg" in fmt or "jpg" in fmt else "image/png" if "png" in fmt else ""
                        if not mime or "compresseddepth" in fmt:
                            raise ExecutionError("완료 판단에는 RGB JPEG/PNG 카메라 토픽이 필요합니다")
                        raw = msg.get("data", "")
                        data = base64.b64decode(raw, validate=True) if isinstance(raw, str) else bytes(raw)
                        if not data or len(data) > 10_000_000:
                            raise ExecutionError("카메라 이미지 크기 오류")
                        frames.append(Frame(topic, stamp_seconds(msg), data, mime))
                    return frames
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ExecutionError("정지 후 새 카메라 프레임 수신 시간 초과")
                self.cv.wait(min(remaining, 0.2))

    def close(self):
        self.closed = True
        self.ws.close()
        self.thread.join(timeout=6)


class PhysicalAct:
    """추가한 /agent/control 확장이 필요하다. 기본 FINISH를 hold 성공으로 해석하지 않는다."""
    def __init__(self, ros, initial_robot_type):
        self.ros, self.initial_robot_type = ros, initial_robot_type
        self.execution_id, self.topics, self.initialized = "idle", (), False

    def _control(self, operation, **payload):
        result = self.ros.call("/agent/control", "physical_ai_interfaces/srv/SendCommand",
                               {"command": 0, "task_info": {"task_instruction": [json.dumps(
                                   {"operation": operation, "execution_id": self.execution_id, **payload})]}},
                               timeout=120 if operation == "prepare" else 15)
        if not result.get("success"):
            raise ExecutionError(result.get("message", "ACT 제어 거절"))
        return json.loads(result["message"])

    def prepare(self, cap, config, execution_id):
        config.validate(live=True)
        self.execution_id, self.topics = execution_id, config.camera_topics
        self._control("prepare", action=cap.id, model_revision=config.model_revision, fps=config.fps,
                      robot_type=config.robot_type, instruction=cap.description)
        available = self.ros.call("/image/get_available_list", "physical_ai_interfaces/srv/GetImageTopicList", {})
        bases = set(available.get("image_topic_list", []))
        if not available.get("success") or any(t.removesuffix("/compressed") not in bases for t in self.topics):
            raise ExecutionError("완료 판단 카메라가 ACT 카메라 목록과 일치하지 않습니다")
        self.ros.subscribe(self.topics)
        self._wait_pause(15)

    def _receipt(self, state):
        if state.get("execution_id") != self.execution_id or state.get("state") != "paused" \
                or not state.get("stopped_at"):
            raise ExecutionError("ACT 정지 상태 불일치")
        return PauseReceipt(self.execution_id, state["stopped_at"],
                            state.get("transfer_ready") is True, state.get("evidence", ""))

    def _wait_pause(self, timeout, cancel=None):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cancel is not None and cancel.is_set():
                self._control("pause")
                raise Cancelled("사용자 중단")
            state = self._control("status")
            if state.get("error"):
                raise ExecutionError(state["error"])
            if state.get("state") == "paused":
                return self._receipt(state)
            time.sleep(0.05)
        raise ExecutionError("ACT 실행/정지 확인 시간 초과")

    def run_window(self, seconds, cancel):
        if cancel.is_set():
            raise Cancelled("사용자 중단")
        self._control("run", duration_s=seconds)
        return self._wait_pause(seconds + 30, cancel)

    def pause(self):
        if not self.initialized:
            state = self._control("initialize", robot_type=self.initial_robot_type)
            self.execution_id = state["execution_id"]
            self.initialized = True
        self._control("pause")
        return self._wait_pause(15)

    def capture(self, after):
        return self.ros.capture(self.topics, after)

    def finish(self):
        state = self._control("finish")
        self._receipt(state)
