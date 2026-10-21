"""외부 API나 로봇을 호출하지 않는 명시적 모의 실행."""
import threading
import time
from collections import deque

from .runtime import Cancelled, Frame, PauseReceipt, Verdict


class MockAct:
    def __init__(self):
        self.execution_id = "idle"
        self.calls = []
        self.paused = True

    def prepare(self, cap, config, execution_id):
        self.execution_id = execution_id
        self.calls.append(("prepare", cap.id))

    def run_window(self, seconds, cancel: threading.Event):
        self.calls.append(("run", seconds))
        self.paused = False
        interrupted = cancel.wait(seconds)
        receipt = self.pause()
        if interrupted:
            raise Cancelled("사용자 중단")
        return receipt

    def pause(self):
        self.paused = True
        self.calls.append(("pause",))
        return PauseReceipt(self.execution_id, time.time(), True, "mock-only")

    def capture(self, after):
        if not self.paused:
            raise RuntimeError("정지 전에 촬영했습니다")
        self.calls.append(("capture",))
        return [Frame("mock-camera", max(time.time(), after + 0.001), b"mock-only")]

    def finish(self):
        if not self.paused:
            raise RuntimeError("동작 중 종료")
        self.calls.append(("finish",))


class MockRail:
    def __init__(self, station="시약존"):
        self.current = station
        self.ready = False
        self.moves = []

    def station(self):
        return self.current

    def arm_state(self, ready, evidence):
        self.ready = ready

    def move(self, station, cancel):
        if cancel.is_set():
            raise Cancelled("사용자 중단")
        if not self.ready:
            raise RuntimeError("팔 상태 미확인")
        self.moves.append(station)
        self.current, self.ready = station, False
        return f"mock-rail-{len(self.moves)}"

    def stop(self):
        self.ready = False


class MockJudge:
    def __init__(self, outcomes=()):
        self.outcomes = deque(outcomes)
        self.calls = 0

    def check(self, cap, frames):
        self.calls += 1
        result = self.outcomes.popleft() if self.outcomes else "complete"
        if isinstance(result, Exception):
            raise result
        return Verdict(result, "모의 판정", f"mock-decision-{self.calls}")
