import base64
import json
import shutil
from pathlib import Path

import pytest

from armchemist.decision_examples import DecisionExamples
from armchemist.openai_client import DecisionsJudge
from armchemist.registry import CAPABILITIES
from armchemist.runtime import ExecutionError, Frame
from armchemist.settings import Settings
from armchemist.ros import PhysicalAct

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'config/decision_examples/manifest.json'


class Client:
    def __init__(self):
        self.calls = []

    def post(self, path, body):
        self.calls.append((path, body))
        return {'answers': [{'name': 'completion', 'type': 'choice', 'choice': 'complete', 'confidence': .99}]}


def test_reviewed_examples_are_bundled_and_isolated_per_act():
    settings = Settings()
    manifest = json.loads(MANIFEST.read_text())
    assert len(manifest['actions']) == 7
    assert sum(len(g['examples']) for g in manifest['actions']) == 14
    assert sum(len(e['images']) for g in manifest['actions'] for e in g['examples']) == 26
    client = Client()
    judge = DecisionsJudge(client, examples=settings.decision_examples)
    for group in manifest['actions']:
        assert {e['label'] for e in group['examples']} == {'complete', 'incomplete'}
        judge.check(CAPABILITIES[group['action_id']], [Frame('current-camera', 2, b'current-frame')])
        path, body = client.calls[-1]
        assert path == 'decisions'
        content = body['input'][0]['content']
        texts = '\n'.join(p.get('text', '') for p in content)
        assert group['question'] in body['questions'][0]['instructions']
        for other in manifest['actions']:
            for example in other['examples']:
                assert (example['id'] in texts) == (other['action_id'] == group['action_id'])
        images = [p['image_url'] for p in content if p['type'] == 'input_image']
        assert len(images) == 1 + sum(len(e['images']) for e in group['examples'])
        assert base64.b64decode(images[-1].split(',', 1)[1]) == b'current-frame'
        marker = next(i for i, p in enumerate(content) if '현재 판정 대상 시작' in p.get('text', ''))
        assert all(p['type'] != 'input_image' for p in content[marker+1:-1])


@pytest.mark.parametrize('action', ['pour_bottle', 'pour_cylinder'])
def test_pour_requests_current_posture_only_without_examples(action):
    client = Client()
    judge = DecisionsJudge(client, examples=Settings().decision_examples)
    frames = [Frame('head', 2, b'head-current'), Frame('wrist', 2, b'wrist-current')]
    assert judge.check(CAPABILITIES[action], frames).status == 'complete'
    path, body = client.calls[-1]
    content = body['input'][0]['content']
    images = [p['image_url'] for p in content if p['type'] == 'input_image']
    assert path == 'decisions'
    assert [base64.b64decode(i.split(',', 1)[1]) for i in images] == [f.data for f in frames]
    texts = '\n'.join(p.get('text', '') for p in content)
    assert '참조 예시' not in texts and '정답:' not in texts
    instructions = body['questions'][0]['instructions']
    assert action in instructions
    assert '세워져 있으면 complete' in instructions
    assert '기울어져 있거나 자세를 확인할 수 없으면 incomplete' in instructions
    assert 'unknown' not in instructions
    assert '액체 이송 여부나 투입량은 완료 조건에 포함하지 않는다' in instructions


@pytest.mark.parametrize('damage', ['hash', 'unknown', 'unapproved', 'path'])
def test_invalid_examples_fail_at_load(tmp_path, damage):
    destination = tmp_path / 'examples'
    shutil.copytree(MANIFEST.parent, destination)
    path = destination / 'manifest.json'
    data = json.loads(path.read_text())
    example = data['actions'][0]['examples'][0]
    if damage == 'hash':
        (destination / example['images'][0]['path']).write_bytes(b'\xff\xd8\xffmodified')
    elif damage == 'unknown':
        example['label'] = 'unknown'
    elif damage == 'unapproved':
        example['review_status'] = 'pending'
    else:
        example['images'][0]['path'] = '../outside.jpg'
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        DecisionExamples(path)


def test_missing_current_frame_is_not_replaced_by_reference_images():
    client = Client()
    judge = DecisionsJudge(client, examples=DecisionExamples(MANIFEST))
    with pytest.raises(ExecutionError, match='현재'):
        judge.check(CAPABILITIES['open_cap'], [])
    assert client.calls == []


@pytest.mark.parametrize('action', ['pour_bottle', 'pour_cylinder'])
def test_physical_adapter_subscribes_judge_camera_for_pouring(action):
    config = Settings().actions[action]
    class Ros:
        def __init__(self):
            self.calls = []
        def call(self, name, service, body):
            self.calls.append(name)
            return {'success': True, 'image_topic_list':
                    [t.removesuffix('/compressed') for t in config.camera_topics]}
        def subscribe(self, topics):
            self.calls.append(tuple(topics))
    ros = Ros()
    act = PhysicalAct(ros, 'omx_dual')
    control = []
    act._control = lambda operation, **kwargs: control.append(operation)
    act._wait_pause = lambda timeout: control.append('wait_pause')
    act.prepare(CAPABILITIES[action], config, 'execution-id')
    assert control == ['prepare', 'wait_pause']
    assert ros.calls == ['/image/get_available_list', config.camera_topics]
