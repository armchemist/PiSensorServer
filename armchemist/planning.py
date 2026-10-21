"""LLM은 ACT ID만 고른다. 순서의 실행 가능성과 레일 삽입은 코드에서 결정한다."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from uuid import uuid4

from .registry import CAPABILITIES, INITIAL_FACTS, STATIONS


@dataclass(frozen=True)
class Step:
    kind: str
    name: str
    station: str


@dataclass(frozen=True)
class Plan:
    id: str
    version: int
    graphSnapshotId: str
    goal: str
    actions: tuple[str, ...]
    steps: tuple[Step, ...]
    initial_facts: tuple[tuple[str, str], ...]
    initial_station: str | None
    config_fingerprint: str = ""

    def to_dict(self):
        return asdict(self)


def compile_plan(goal, names, graph_id="", *, initial_station=None, initial_facts=None):
    names = tuple(names)
    if not names or len(names) > 30:
        raise ValueError("ACT는 1~30개여야 합니다")
    if initial_station is not None and initial_station not in STATIONS:
        raise ValueError("알 수 없는 시작 스테이션")
    facts = dict(INITIAL_FACTS if initial_facts is None else initial_facts)
    if set(facts) != set(INITIAL_FACTS):
        raise ValueError("초기 물체/뚜껑 상태가 필요합니다")
    start = tuple(sorted(facts.items()))
    steps, station = [], initial_station
    for name in names:
        if name not in CAPABILITIES:
            raise ValueError(f"미등록 ACT: {name}")
        cap = CAPABILITIES[name]
        for key, value in cap.requires:
            if facts[key] != value:
                raise ValueError(f"{name}: {key}={value} 필요 (현재 {facts[key]})")
        if station != cap.station:
            steps.append(Step("rail", "move_rail", cap.station))
            station = cap.station
        steps.append(Step("act", name, station))
        facts.update(cap.effects)
    return Plan(uuid4().hex, 1, graph_id, goal, names, tuple(steps), start, initial_station)


class DemoPlanner:
    """오프라인 데모는 명시한 ACT ID만 파싱한다. 자연어를 이해했다고 가장하지 않는다."""

    def select(self, messages, graph):
        text = messages[-1]["content"]
        names = [s.strip() for s in text.replace("→", ",").split(",")]
        if not names or any(n not in CAPABILITIES for n in names):
            raise ValueError("모의 모드에서는 ACT ID를 쉼표로 구분해 입력하세요. 자연어 계획에는 OpenAI 연결이 필요합니다.")
        return names
