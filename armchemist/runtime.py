"""평균 시간 단위 ACT 실행과 정지 후 판단. 카메라 시각은 로봇의 ROS 시계 기준이다."""
from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, replace
from typing import Callable, Protocol
from uuid import uuid4

from .planning import Plan, compile_plan
from .registry import ActionConfig, CAPABILITIES, Capability, INITIAL_FACTS, ZERO_SHOT_ACTIONS


class ExecutionError(RuntimeError):
    pass


class Cancelled(ExecutionError):
    pass


class DecisionUnavailable(ExecutionError):
    """판정 통신만 재시도할 수 있다. ACT를 재실행할 근거가 아니다."""


@dataclass(frozen=True)
class PauseReceipt:
    execution_id: str
    stopped_at: float
    transfer_ready: bool
    evidence: str
    stop_reason: str = ""
    ran_s: float | None = None


@dataclass(frozen=True)
class Frame:
    topic: str
    captured_at: float
    data: bytes
    mime_type: str = "image/jpeg"


@dataclass(frozen=True)
class Verdict:
    status: str  # complete / incomplete / unknown
    detail: str
    response_id: str = ""
    confidence: float | None = None


class ActBackend(Protocol):
    def prepare(self, cap: Capability, config: ActionConfig, execution_id: str): ...
    def run_window(self, seconds: float, cancel: threading.Event,
                   monitor: Callable[[], bool] | None = None, monitor_every: float = 0,
                   monitor_after_s: float = 0) -> PauseReceipt: ...

    def move_end_pose(self) -> PauseReceipt: ...
    def pause(self) -> PauseReceipt: ...
    def finish(self): ...
    def capture(self, after: float) -> list[Frame]: ...


class RailBackend(Protocol):
    def station(self) -> str | None: ...
    def arm_state(self, ready: bool, evidence: str): ...
    def move(self, station: str, cancel: threading.Event) -> str: ...
    def stop(self): ...


class Judge(Protocol):
    def check(self, cap: Capability, frames: list[Frame]) -> Verdict: ...


class Runner:
    def __init__(self, act: ActBackend, rail: RailBackend, judge: Judge,
                 actions: dict[str, ActionConfig], *, live=False,
                 decision_attempts=3, on_event: Callable | None = None, initial_facts=None,
                 min_confidence=0.0, live_judge_s=0.0):
        if not 1 <= decision_attempts <= 5:
            raise ValueError("판정 통신 시도 횟수는 1~5여야 합니다")
        self.act, self.rail, self.judge, self.actions = act, rail, judge, dict(actions)
        self.live, self.decision_attempts = live, decision_attempts
        # min_confidence 미만의 complete 는 미완료로 본다. live_judge_s > 0 이면 실행 중에도 그 간격으로 판정한다.
        self.min_confidence, self.live_judge_s = min_confidence, live_judge_s
        self.on_event = on_event
        self._lock = threading.Lock()
        self.facts = dict(INITIAL_FACTS if initial_facts is None else initial_facts)

    def _accept(self, verdict):
        if verdict.status == "complete" and verdict.confidence is not None \
                and verdict.confidence < self.min_confidence:
            return replace(verdict, status="incomplete",
                           detail=f"{verdict.detail} (기준 {self.min_confidence} 미만)")
        return verdict

    def run(self, plan: Plan, cancel=None, run_id=None):
        if not self._lock.acquire(blocking=False):
            raise ExecutionError("이미 실행 중입니다")
        cancel = cancel or threading.Event()
        report = {"id": run_id or uuid4().hex, "plan": plan.to_dict(),
                  "mode": "live" if self.live else "mock", "status": "running",
                  "started_at": time.time(), "events": [], "completed_actions": [], "error": None}
        touched = False

        def emit(phase, **data):
            event = {"phase": phase, "at": time.time(), **data}
            report["events"].append(event)
            if self.on_event:
                self.on_event(report)

        def check_cancel():
            if cancel.is_set():
                raise Cancelled("사용자가 실행을 중단했습니다")

        try:
            if self.facts is None or dict(plan.initial_facts) != self.facts:
                raise ExecutionError("계획의 초기 상태가 현재 장비 상태와 다릅니다. 상태 확인 후 다시 계획해야 합니다")
            # 저장된 계획의 단계가 변조되거나 설정이 빠진 경우 움직이기 전에 거부한다.
            expected = compile_plan(plan.goal, plan.actions, plan.graphSnapshotId,
                                    initial_station=plan.initial_station,
                                    initial_facts=dict(plan.initial_facts))
            if plan.steps != expected.steps:
                raise ExecutionError("계획 단계가 ACT 목록과 일치하지 않습니다")
            for name in plan.actions:
                if name not in self.actions:
                    raise ExecutionError(f"실행 설정 없음: {name}")
                self.actions[name].validate(self.live)
                if self.actions[name].completion_mode == 'mean_window' and name not in ZERO_SHOT_ACTIONS:
                    raise ExecutionError('mean_window 완료 처리는 붓기 ACT에만 허용합니다')
            check_cancel()
            emit("preflight")
            # 첫 ACT도 시작 상태의 관절 정지와 레일 위치를 확인한 뒤 시작한다.
            touched = True
            pause = self.act.pause()
            current_station = self.rail.station()
            if getattr(self.rail, "fixed", False):
                away = [n for n in plan.actions if CAPABILITIES[n].station != current_station]
                if away:
                    raise ExecutionError(f"레일 미사용 모드는 {current_station} ACT만 실행합니다: {', '.join(away)}")
            for name in plan.actions:
                cap, config = CAPABILITIES[name], self.actions[name]
                check_cancel()
                if current_station != cap.station:
                    pause = self.act.pause()
                    if not pause.transfer_ready:
                        raise ExecutionError("현재 종료 자세의 레일 이동 가능 여부가 확인되지 않았습니다")
                    self.rail.arm_state(True, pause.evidence)
                    emit("rail_moving", action=name, station=cap.station)
                    command_id = self.rail.move(cap.station, cancel)
                    check_cancel()
                    current_station = self.rail.station()
                    if current_station != cap.station:
                        raise ExecutionError("레일 명령 완료 후 목표 스테이션이 확인되지 않았습니다")
                    emit("rail_arrived", station=current_station, command_id=command_id)
                self.rail.arm_state(False, "ACT 실행 준비")
                execution_id = uuid4().hex
                emit("preparing", action=name, execution_id=execution_id)
                self.act.prepare(cap, config, execution_id)
                check_cancel()
                for attempt in range(1, 4):
                    check_cancel()
                    emit("executing", action=name, attempt=attempt,
                         duration_s=config.mean_duration_s, execution_id=execution_id)
                    monitor = None
                    if config.completion_mode != 'mean_window' and self.live_judge_s:
                        def monitor(name=name, cap=cap, attempt=attempt):
                            # 팔을 멈추지 않고 최근 프레임으로 판정한다. 판정 실패는 실행을 멈추지 않는다.
                            try:
                                verdict = self._accept(self.judge.check(cap, self.act.capture(after=time.time() - 3)))
                            except Exception as exc:
                                emit("live_judge_error", action=name, attempt=attempt, error=str(exc))
                                return False
                            emit("live_judged", action=name, attempt=attempt, verdict=asdict(verdict))
                            return verdict.status == "complete"
                    # 실행 중 판정은 평균 시간의 절반 이후부터. 그 전에는 데모에서도 끝나지 않는다.
                    pause = self.act.run_window(config.mean_duration_s, cancel, monitor=monitor,
                                                monitor_every=self.live_judge_s,
                                                monitor_after_s=config.mean_duration_s / 2)
                    if pause.execution_id != execution_id:
                        raise ExecutionError("이전 ACT의 정지 확인 응답입니다")
                    check_cancel()
                    emit("paused", action=name, attempt=attempt, stopped_at=pause.stopped_at,
                         stop_reason=pause.stop_reason, ran_s=pause.ran_s)
                    execution_only = config.completion_mode == 'mean_window'
                    if execution_only:
                        emit("execution_window_complete", action=name, attempt=attempt,
                             duration_s=config.mean_duration_s, stopped_at=pause.stopped_at,
                             visual_verified=False)
                    else:
                        frames = self.act.capture(after=pause.stopped_at)
                        if not frames or any(not f.data or f.captured_at <= pause.stopped_at for f in frames):
                            raise ExecutionError("정지 확인 이후 촬영된 새 영상이 없습니다")
                        emit("judging", action=name, attempt=attempt,
                             frames=[{"topic": f.topic, "captured_at": f.captured_at} for f in frames])
                        verdict = None
                        for request_attempt in range(1, self.decision_attempts + 1):
                            check_cancel()
                            try:
                                verdict = self.judge.check(cap, frames)
                                break
                            except DecisionUnavailable as exc:
                                emit("decision_retry", action=name, request_attempt=request_attempt, error=str(exc))
                                if request_attempt == self.decision_attempts:
                                    raise
                                if cancel.wait(min(request_attempt, 2)):
                                    check_cancel()
                        check_cancel()
                        verdict = self._accept(verdict)
                        emit("judged", action=name, attempt=attempt, verdict=asdict(verdict))
                    check_cancel()
                    if execution_only or verdict.status == "complete":
                        # 실행 중 판정·상한으로 끝 동작이 잘릴 수 있다. ACT 별 데모 끝 자세로 옮긴다.
                        emit("end_pose_moving", action=name)
                        pause = self.act.move_end_pose()
                        check_cancel()
                        emit("end_pose_reached", action=name, stopped_at=pause.stopped_at)
                        self.act.finish()
                        self.facts.update(cap.effects)
                        report["completed_actions"].append(name)
                        emit("action_complete", action=name, attempt=attempt,
                             completion_basis="mean_window" if execution_only else "visual_decision")
                        if hasattr(self.rail, "telemetry"):
                            try:
                                emit("telemetry", data=self.rail.telemetry())
                            except Exception as exc:
                                emit("telemetry_unavailable", error=str(exc))
                        break
                    if verdict.status != "incomplete":
                        raise ExecutionError("영상만으로 완료 여부를 판단할 수 없습니다: " + verdict.detail)
                    if attempt == 3:
                        raise ExecutionError(f"{name}: 총 3회 실행 후에도 미완료")
            report["status"] = "finished"
            report["finished_at"] = time.time()
            emit("finished")
        except Exception as exc:
            report["status"] = "cancelled" if isinstance(exc, Cancelled) else "failed"
            report["error"] = str(exc)
            report["finished_at"] = time.time()
            if touched:
                self.facts = None
                # 한 정지 요청이 실패해도 나머지 장치 정지를 계속 시도한다.
                errors = []
                for label, stop in (("arm", self.act.pause), ("rail", self.rail.stop),
                                    ("arm_state", lambda: self.rail.arm_state(False, "실행 중단"))):
                    try:
                        stop()
                    except Exception as stop_error:
                        errors.append(f"{label}: {stop_error}")
                report["stop_errors"] = errors
            emit(report["status"], error=str(exc))
        finally:
            report["finished_at"] = time.time()
            self._lock.release()
        return report
