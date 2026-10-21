"""메인 YAML을 API, 모델 설치 도구, ROS 노드에서 함께 사용한다."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path

import yaml

from .registry import ActionConfig, CAPABILITIES, ZERO_SHOT_ACTIONS
from .decision_examples import DecisionExamples
from .decision_prompts import DecisionPrompts

DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / 'config/main.yaml'


class Settings:
    def __init__(self, path=None):
        self.path = Path(path or os.getenv('AGENT_CONFIG', DEFAULT_CONFIG)).expanduser().resolve()
        self.raw = yaml.safe_load(self.path.read_text())
        if self.raw.get('schema_version') != 1 or self.raw.get('mode') not in ('mock', 'live'):
            raise ValueError('schema_version: 1 및 mode: mock/live 설정 필요')
        self.mode = self.raw['mode']
        self.robot = self.raw['robot']
        self.profiles = self.robot['profiles']
        for key in ('initial_profile', 'hold_profile'):
            if self.robot[key] not in self.profiles:
                raise ValueError(f'등록되지 않은 robot.{key}')
        for name, profile in self.profiles.items():
            cameras, topics, order = (profile[k] for k in ('cameras', 'joint_topics', 'joint_order'))
            if not cameras or not topics or not order:
                raise ValueError(f'{name}: 카메라/관절 설정 필요')
            if any(not isinstance(t, str) or not t.startswith('/') for t in [*cameras.values(), *topics.values()]):
                raise ValueError(f'{name}: ROS 절대 토픽 이름 필요')
            if any(not k.startswith('leader') or k not in topics or not v for k, v in order.items()):
                raise ValueError(f'{name}: 명령 토픽과 관절 순서 불일치')
            joints = [j for names in order.values() for j in names]
            if len(joints) != len(set(joints)):
                raise ValueError(f'{name}: 중복 관절')
        limits = self.robot['transfer_limits']
        if not isinstance(limits, dict) or any(
            not isinstance(v, list) or len(v) != 2
            or any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in v)
            or v[0] > v[1] for v in limits.values()
        ):
            raise ValueError('transfer_limits: 관절별 [최소, 최대] 유한한 라디안 필요')
        hold = self.profiles[self.robot['hold_profile']]
        hold_joints = {j for names in hold['joint_order'].values() for j in names}
        for profile in self.profiles.values():
            if any(j not in hold_joints for names in profile['joint_order'].values() for j in names):
                raise ValueError('hold_profile은 모든 ACT의 관절과 그리퍼를 포함해야 합니다')
            if any(hold['joint_topics'].get(k) != v for k, v in profile['joint_topics'].items()):
                raise ValueError('hold_profile과 ACT 프로파일의 관절 토픽이 다릅니다')
        if limits and set(limits) != hold_joints:
            raise ValueError('transfer_limits에는 양팔과 그리퍼의 모든 관절이 필요합니다')
        facts = self.raw['initial_facts']
        if set(facts) != {'left', 'right', 'cap'} or facts['left'] not in ('empty', 'bottle') \
                or facts['right'] not in ('empty', 'cylinder', 'cap') or facts['cap'] not in ('open', 'closed'):
            raise ValueError('잘못된 initial_facts')
        self.actions = {}
        for name, value in self.raw['actions'].items():
            if name not in CAPABILITIES or value['model_repo'] != CAPABILITIES[name].model_repo:
                raise ValueError(f'미등록 ACT/모델 저장소: {name}')
            folder = Path(value['model_subfolder'])
            if folder.is_absolute() or '..' in folder.parts:
                raise ValueError('model_subfolder는 저장소 안의 상대 경로여야 합니다')
            profile = self.profiles[value['robot_type']]
            camera_topics = tuple(profile['cameras'][c] for c in value['judge_cameras'])
            if any('depth' in c.lower() for c in value['judge_cameras']):
                raise ValueError('완료 판정에는 RGB 카메라를 선택하세요')
            keys = ('mean_duration_s', 'model_revision', 'dataset_revision', 'duration_source', 'fps', 'robot_type')
            action = ActionConfig(**{k: value[k] for k in keys},
                                  policy_path=str(self.policy_path(name)), camera_topics=camera_topics,
                                  completion_mode=value.get('completion_mode', 'vision'))
            action.validate(live=True)
            if action.completion_mode == 'mean_window' and name not in ZERO_SHOT_ACTIONS:
                raise ValueError('mean_window 완료 처리는 붓기 ACT에만 허용합니다')
            self.actions[name] = action
        if not self.actions:
            raise ValueError('최소 한 개 ACT 설정 필요')
        examples_path = self.raw['openai'].get('decision_examples')
        self.decision_examples = DecisionExamples(self.resolve(examples_path)) if examples_path else None
        prompts_path = self.raw['openai'].get('decision_prompts')
        self.decision_prompts = DecisionPrompts(self.resolve(prompts_path)) if prompts_path else None
        if self.decision_examples:
            for name in self.actions:
                if name not in ZERO_SHOT_ACTIONS and name not in self.decision_examples.actions:
                    raise ValueError(f'{name}: 등록된 판정 예시가 없습니다')

    def resolve(self, value):
        path = Path(value).expanduser()
        return (path if path.is_absolute() else self.path.parent / path).resolve()

    def policy_path(self, name):
        path = Path(self.raw['actions'][name]['policy_path']).expanduser()
        return path if path.is_absolute() else self.resolve(self.robot['model_root']) / path

    def secret(self, section, key, *, required=False):
        env_name = self.raw[section][key]
        value = os.getenv(env_name, '')
        if required and not value:
            raise ValueError(f'{env_name} 환경변수가 필요합니다')
        return value

    def ros_parameters(self):
        params = {}
        for name, p in self.profiles.items():
            params[name] = {
                'observation_list': [*p['cameras'], 'state'],
                'camera_topic_list': [f'{k}:{v}' for k, v in p['cameras'].items()],
                'joint_topic_list': [f'{k}:{v}' for k, v in p['joint_topics'].items()],
                'joint_list': list(p['joint_order']), 'joint_order': p['joint_order'],
                'rosbag_extra_topic_list': [''],
            }
        return {'physical_ai_server': {'ros__parameters': params}}

    @property
    def fingerprint(self):
        examples = self.decision_examples.fingerprint if self.decision_examples else ''
        prompts = self.decision_prompts.fingerprint if self.decision_prompts else ''
        return hashlib.sha256((json.dumps(self.raw, sort_keys=True) + examples + prompts).encode()).hexdigest()


def validate_model(settings, name):
    """실제 다운로드된 관측 차원과 이름을 로봇 프로파일과 비교한다."""
    path = settings.policy_path(name)
    for filename in ('config.json', 'model.safetensors'):
        if not (path / filename).is_file():
            raise ValueError(f'{name}: {path / filename} 없음; download-models를 실행하세요')
    config = json.loads((path / 'config.json').read_text())
    profile = settings.profiles[settings.actions[name].robot_type]
    features = config['input_features']
    cameras = {k.removeprefix('observation.images.') for k in features if k.startswith('observation.images.')}
    joint_count = sum(len(names) for names in profile['joint_order'].values())
    if config.get('type') != 'act' or cameras != set(profile['cameras']) \
            or features['observation.state']['shape'] != [joint_count] \
            or config['output_features']['action']['shape'] != [joint_count]:
        raise ValueError(f'{name}: 모델과 프로파일의 카메라/관절 차원 불일치')
    manifest = json.loads((path / 'armchemist-model.json').read_text())
    expected = {k: settings.raw['actions'][name][k] for k in ('model_repo', 'model_revision', 'model_subfolder')}
    if manifest != expected:
        raise ValueError(f'{name}: 설치된 모델 revision과 메인 YAML 불일치')


def download_models(settings, names, *, downloader=None):
    if downloader is None:
        from huggingface_hub import snapshot_download
        downloader = snapshot_download
    import shutil
    for name in names:
        value = settings.raw['actions'][name]
        folder = value['model_subfolder'].strip('/')
        prefix = folder + '/' if folder else ''
        source = Path(downloader(repo_id=value['model_repo'], revision=value['model_revision'],
                                 allow_patterns=[prefix + '*.json', prefix + '*.safetensors'])) / folder
        if any(not (source / f).is_file() for f in ('config.json', 'model.safetensors')):
            raise ValueError(f'{name}: 선택한 revision/subfolder에 체크포인트 파일이 없습니다')
        target = settings.policy_path(name)
        target.mkdir(parents=True, exist_ok=True)
        for file in source.iterdir():
            if file.is_file() and file.suffix in ('.json', '.safetensors'):
                shutil.copy2(file, target / file.name)
        manifest = {k: value[k] for k in ('model_repo', 'model_revision', 'model_subfolder')}
        (target / 'armchemist-model.json').write_text(json.dumps(manifest, indent=2) + '\n')
        validate_model(settings, name)
