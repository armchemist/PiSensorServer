import json

import pytest

from armchemist.decision_prompts import DecisionPrompts
from armchemist.openai_client import DecisionsJudge
from armchemist.registry import CAPABILITIES
from armchemist.runtime import Frame
from armchemist.settings import Settings


def test_production_prompt_and_examples_reach_decisions_together():
    class Client:
        def post(self, path, body):
            self.body = body
            return {'answers': [{'name': 'completion', 'type': 'choice',
                                 'choice': 'complete', 'confidence': .12}]}
    settings = Settings()
    client = Client()
    judge = DecisionsJudge(client, examples=settings.decision_examples, prompts=settings.decision_prompts)
    assert judge.check(CAPABILITIES['close_cap'], [Frame('head', 1, b'current')]).status == 'complete'
    question = client.body['questions'][0]
    assert '두 참조 예시' in question['instructions']
    assert '노란색 뚜껑' in question['instructions']
    assert '두 손가락 사이가 비어' in question['choices'][0]['description']
    text = ' '.join(p.get('text', '') for p in client.body['input'][0]['content'])
    assert '정답: complete' in text and '정답: incomplete' in text


def test_prompt_edits_invalidate_confirmed_settings(tmp_path):
    settings = Settings()
    path = tmp_path / 'prompts.json'
    rubrics = dict(settings.decision_prompts.rubrics)
    rubrics['close_cap'] = {**rubrics['close_cap'], 'camera': '새 관찰 기준'}
    path.write_text(json.dumps(rubrics))
    before = settings.fingerprint
    settings.decision_prompts = DecisionPrompts(path)
    assert settings.fingerprint != before
    del rubrics['open_cap']
    path.write_text(json.dumps(rubrics))
    with pytest.raises(ValueError, match='모든 ACT'):
        DecisionPrompts(path)
