"""ACT-specific visual criteria, validated against separate dataset episodes."""
import hashlib
import json
from pathlib import Path

from .registry import CAPABILITIES, ZERO_SHOT_ACTIONS


class DecisionPrompts:
    def __init__(self, path):
        raw = Path(path).read_bytes()
        self.rubrics = json.loads(raw)
        if set(self.rubrics) != set(CAPABILITIES):
            raise ValueError('모든 ACT의 판정 프롬프트가 필요합니다')
        for rubric in self.rubrics.values():
            if not isinstance(rubric, dict) or set(rubric) != {'focus', 'complete', 'incomplete', 'camera'} \
                    or any(not isinstance(v, str) or not v.strip() for v in rubric.values()):
                raise ValueError('판정 프롬프트에는 대상·완료·미완료·카메라 설명이 필요합니다')
        self.fingerprint = hashlib.sha256(raw).hexdigest()

    def question(self, action, *, has_examples):
        rubric = self.rubrics[action]
        shared = f"ACT {action}. 판정 대상: {rubric['focus']}. 현재 장면을 complete 또는 incomplete로 분류한다. "
        task = ("라벨된 두 참조 예시 중 현재 관측의 물체·그리퍼 관계가 어느 상태에 해당하는지 비교하라. "
                "배경·조명·정확한 픽셀 위치의 유사성이 아니라 잡음/놓음/분리/결합 관계를 비교하라. "
                if has_examples and action not in ZERO_SHOT_ACTIONS else
                "참조 예시 없이 대상 용기의 현재 자세만 분류하라. " if action in ZERO_SHOT_ACTIONS else
                "참조 예시 없이 현재 관측의 물체·그리퍼 관계를 완료·미완료 조건과 비교하라. ")
        instructions = (shared + task + rubric['camera']
                        + " 완료 상태를 확인할 수 없으면 incomplete를 선택한다. 과거 동작이나 액체 양은 추정하지 않는다.")
        return {'type': 'choice', 'name': 'completion', 'instructions': instructions,
                'choices': [{'value': 'complete', 'description': rubric['complete']},
                            {'value': 'incomplete', 'description': rubric['incomplete']}]}
