"""리니어 스테이지 제어를 rail/stage.py 에 위임한다.

stage.py 는 임포트 시점에 /dev/gpiochip 을 열고, 실패하면 예외를 던진다.
서버는 스테이지가 없어도 떠야 하므로 임포트를 실제 사용 시점까지 미룬다.

이동은 초 단위로 걸리는 블로킹 작업이라 동시에 두 개가 실행되면 안 된다.
락을 비블로킹으로 잡아서, 이미 움직이는 중이면 곧바로 거부한다.
"""

import sys
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RAIL_DIR = REPO_ROOT / "rail"


class RailBusy(RuntimeError):
    """이미 이동 중."""


class Rail:
    def __init__(self):
        self._stage = None
        self._error = None
        self._move_lock = threading.Lock()

    def _load(self):
        """stage 모듈을 지연 임포트한다. 실패하면 RuntimeError."""
        if self._stage is not None:
            return self._stage
        if str(RAIL_DIR) not in sys.path:
            sys.path.insert(0, str(RAIL_DIR))
        try:
            import stage  # noqa: PLC0415  (지연 임포트가 의도)

            self._stage = stage
            self._error = None
        except Exception as exc:
            self._error = f"{type(exc).__name__}: {exc}"
            raise RuntimeError(
                f"스테이지를 초기화할 수 없음: {self._error}. "
                "python3-lgpio 가 설치돼 있는지, 사용자가 dialout 그룹인지 "
                "확인하세요 (sudo apt install -y python3-lgpio)"
            ) from exc
        return self._stage

    @property
    def soft_limit_error(self):
        """호출자가 except 절에 쓸 수 있도록 예외 클래스를 노출한다."""
        return self._load().SoftLimitError

    def position(self):
        stage = self._load()
        return {
            "mm": round(stage.position_mm(), 3),
            "stroke_mm": stage.STROKE_MM,
            "steps_per_mm": stage.STEPS_PER_MM,
        }

    def move(self, distance_mm, speed_mm_s):
        """상대 이동. 이미 이동 중이면 RailBusy."""
        stage = self._load()
        if not self._move_lock.acquire(blocking=False):
            raise RailBusy("이미 이동 중입니다")
        try:
            stage.move_mm(distance_mm, speed_mm_s=speed_mm_s)
            return round(stage.position_mm(), 3)
        finally:
            self._move_lock.release()

    def move_to(self, target_mm, speed_mm_s):
        """절대 위치 이동."""
        stage = self._load()
        if not self._move_lock.acquire(blocking=False):
            raise RailBusy("이미 이동 중입니다")
        try:
            stage.move_to_mm(target_mm, speed_mm_s=speed_mm_s)
            return round(stage.position_mm(), 3)
        finally:
            self._move_lock.release()

    def jog(self, distance_mm, speed_mm_s):
        """보정용 상대 이동. 보정 중이면 소프트 리밋 없이 움직인다."""
        stage = self._load()
        if not self._move_lock.acquire(blocking=False):
            raise RailBusy("이미 이동 중입니다")
        try:
            stage.jog_mm(distance_mm, speed_mm_s=speed_mm_s)
            return round(stage.position_mm(), 3)
        finally:
            self._move_lock.release()

    def set_position(self, value_mm):
        """캐리지를 손으로 옮겼을 때 현재 위치를 다시 알려준다."""
        stage = self._load()
        stage.set_position_mm(value_mm)
        return round(stage.position_mm(), 3)

    def resume_position(self):
        """저장된 위치를 그대로 이어 쓴다 — 캐리지를 건드리지 않았을 때."""
        stage = self._load()
        stage.resume_position()
        return stage.calibration()

    # --- 보정 ---
    # 사용자가 명시적으로 요청할 때만 들어간다. 리미트 스위치가 없어서
    # 양 끝을 사람이 직접 지정하는 방식이다.

    def calibration(self):
        return self._load().calibration()

    def begin_calibration(self):
        stage = self._load()
        if not self._move_lock.acquire(blocking=False):
            raise RailBusy("이동 중에는 보정을 시작할 수 없습니다")
        try:
            return stage.begin_calibration()
        finally:
            self._move_lock.release()

    def end_calibration(self):
        return self._load().end_calibration()

    def cancel_calibration(self):
        return self._load().cancel_calibration()

    def status(self):
        loaded = self._stage is not None
        status = {
            "available": loaded,
            "moving": self._move_lock.locked(),
            "error": self._error,
            "position_mm": round(self._stage.position_mm(), 3) if loaded else None,
        }
        if loaded:
            # 보정 여부와 위치 신뢰 여부는 /health 만 보고도 알 수 있어야 한다.
            cal = self._stage.calibration()
            status.update(
                stroke_mm=cal["stroke_mm"],
                calibrated=cal["calibrated"],
                calibrating=cal["calibrating"],
                position_known=cal["position_known"],
            )
        return status

    def close(self):
        if self._stage is not None:
            try:
                self._stage.cleanup()
            except Exception:
                pass
            self._stage = None


rail = Rail()
