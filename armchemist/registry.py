"""실제 확보한 ACT만 선언한다. 실장비의 시간과 모델 선택은 배포 설정으로 주입한다."""
from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass


STATIONS = ("시약존", "실린더존", "실험존")
INITIAL_FACTS = {"left": "empty", "right": "empty", "cap": "closed"}
ZERO_SHOT_ACTIONS = frozenset({"pour_bottle", "pour_cylinder"})


@dataclass(frozen=True)
class Capability:
    id: str
    description: str
    station: str
    model_repo: str
    arm: str
    completion: str
    requires: tuple[tuple[str, str], ...]
    effects: tuple[tuple[str, str], ...]

    def to_dict(self):
        return asdict(self)


def _cap(name, description, station, arm, completion, requires, effects, repo=None):
    return Capability(name, description, station, repo or f"gc22-shin/{name}_act", arm,
                      completion, tuple(requires.items()), tuple(effects.items()))


CAPABILITIES = {c.id: c for c in [
    _cap("pickup_bottle1", "시약병 잡기", "시약존", "left",
         "왼쪽 그리퍼가 시약병을 잡아 받침에서 들어 올린 상태다.",
         {"left": "empty"}, {"left": "bottle"}),
    _cap("pickdown_bottle1", "시약병 내려놓기", "시약존", "left",
         "시약병이 지정된 받침에 놓여 있고 왼쪽 그리퍼가 병을 놓았다.",
         {"left": "bottle", "cap": "closed"}, {"left": "empty"}),
    _cap("pickup_cylinder", "실린더 잡기", "실린더존", "right",
         "오른쪽 그리퍼가 실린더를 잡아 받침에서 들어 올린 상태다.",
         {"right": "empty"}, {"right": "cylinder"},
         "Dominho/act-armchemist-pickup-cylinder-80k"),
    _cap("pickdown_cylinder", "실린더 내려놓기", "실린더존", "right",
         "실린더가 지정된 받침에 놓여 있고 오른쪽 그리퍼가 실린더를 놓았다.",
         {"right": "cylinder"}, {"right": "empty"}),
    _cap("open_cap", "시약병 뚜껑 열기", "실험존", "dual",
         "왼손이 병을 잡고 오른손이 분리된 뚜껑을 잡으며 병 입구가 열려 있다.",
         {"left": "bottle", "right": "empty", "cap": "closed"},
         {"right": "cap", "cap": "open"}, "Dominho/act-armchemist-open-cap-100k"),
    _cap("close_cap", "시약병 뚜껑 닫기", "실험존", "dual",
         "뚜껑이 시약병에 다시 결합되어 닫혀 있고 오른손이 뚜껑을 놓았다.",
         {"left": "bottle", "right": "cap", "cap": "open"},
         {"right": "empty", "cap": "closed"}),
    _cap("pour_bottle", "시약병 붓기", "실험존", "left",
         "시약병의 현재 자세만 확인한다. 병이 기울어져 있지 않고 세워져 있으면 complete, "
         "기울어져 있거나 자세를 확인할 수 없으면 incomplete이다. "
         "액체 이송 여부나 투입량은 완료 조건에 포함하지 않는다.",
         {"left": "bottle", "cap": "open"}, {}),
    _cap("pour_cylinder", "실린더 붓기", "실험존", "right",
         "실린더의 현재 자세만 확인한다. 실린더가 기울어져 있지 않고 세워져 있으면 complete, "
         "기울어져 있거나 자세를 확인할 수 없으면 incomplete이다. "
         "액체 이송 여부나 투입량은 완료 조건에 포함하지 않는다.",
         {"right": "cylinder"}, {}),
    _cap("setup_sensors", "센서 설치", "실험존", "right",
         "측정할 용기 안에 센서가 설치되어 있고 오른손이 센서를 놓았다.",
         {"right": "empty"}, {}),
]}


@dataclass(frozen=True)
class ActionConfig:
    mean_duration_s: float
    policy_path: str = ""
    model_revision: str = ""
    dataset_revision: str = ""
    duration_source: str = ""
    fps: int = 0
    robot_type: str = ""
    camera_topics: tuple[str, ...] = ()
    completion_mode: str = "vision"

    def validate(self, live=False):
        if self.completion_mode not in ('vision', 'mean_window'):
            raise ValueError('completion_mode는 vision 또는 mean_window여야 합니다')
        if isinstance(self.mean_duration_s, bool) or not math.isfinite(self.mean_duration_s) \
                or not 0 < self.mean_duration_s <= 600:
            raise ValueError("평균 실행 시간은 0 초과 600 이하의 유한한 값이어야 합니다")
        if live and (not all((self.policy_path, self.model_revision, self.dataset_revision,
                             self.duration_source, self.robot_type, self.camera_topics))
                     or self.fps not in (15, 30)):
            raise ValueError("실장비 모델/데이터셋 revision, 평균 시간 출처, FPS, robot_type, 카메라 설정 필요")
        if live and (not re.fullmatch(r"[0-9a-f]{40}", self.model_revision)
                     or not re.fullmatch(r"[0-9a-f]{40}", self.dataset_revision)):
            raise ValueError("모델과 데이터셋 revision은 고정된 40자리 커밋 SHA여야 합니다")


def demo_actions():
    # 모의 실행에서만 쓰며 데이터셋 통계로 표시하지 않는다.
    return {name: ActionConfig(0.01, duration_source="mock-only",
                               completion_mode='mean_window' if name in ZERO_SHOT_ACTIONS else 'vision')
            for name in CAPABILITIES}


def duration_statistics(episodes: list[dict], fps: float, excluded=()) -> dict:
    if not math.isfinite(fps) or fps <= 0:
        raise ValueError("잘못된 FPS")
    ids = [e["episode_index"] for e in episodes]
    if len(ids) != len(set(ids)) or set(excluded) - set(ids):
        raise ValueError("중복 에피소드 또는 존재하지 않는 제외 에피소드")
    selected = [e for e in episodes if e["episode_index"] not in excluded]
    if not selected or any(not isinstance(e["length"], int) or e["length"] <= 0 for e in selected):
        raise ValueError("유효한 에피소드 길이가 필요합니다")
    frames = sum(e["length"] for e in selected)
    return {"mean_duration_s": frames / fps / len(selected), "fps": fps,
            "episode_count": len(selected), "total_frames": frames,
            "excluded_episodes": sorted(excluded), "statistic": "mean_episode_frames/fps"}
