import copy
import json
from pathlib import Path

import pytest
import yaml

from armchemist.settings import Settings, download_models, validate_model


def configured(tmp_path, change=None):
    raw = copy.deepcopy(Settings().raw)
    raw['openai']['decision_examples'] = str(Settings().resolve(raw['openai']['decision_examples']))
    raw['openai']['decision_prompts'] = str(Settings().resolve(raw['openai']['decision_prompts']))
    raw['robot']['model_root'] = str(tmp_path / 'models')
    if change:
        change(raw)
    path = tmp_path / 'main.yaml'
    path.write_text(yaml.safe_dump(raw))
    return Settings(path)


def test_ros_export_preserves_training_order_and_camera_names():
    s = Settings()
    params = s.ros_parameters()['physical_ai_server']['ros__parameters']
    assert params['omx_dual']['joint_list'] == ['leader_left', 'leader_right']
    assert params['omx_left']['joint_order']['leader_left'][-1] == 'l_gripper_joint_1'
    assert 'cam_wrist_left' in params['omx_dual']['observation_list']
    assert s.actions['pickup_cylinder'].fps == 15
    assert s.actions['pickup_cylinder'].policy_path.startswith(str(s.resolve('../models')))


@pytest.mark.parametrize('change', [
    lambda r: r['actions']['open_cap'].update(model_revision='main'),
    lambda r: r['actions']['open_cap'].update(model_subfolder='../outside'),
    lambda r: r['actions']['open_cap'].update(judge_cameras=['cam_head_depth']),
    lambda r: r['robot'].update(hold_profile='omx_left'),
    lambda r: r['robot'].update(transfer_limits={'l_joint1': [-1, 1]}),
    lambda r: r['initial_facts'].update(left='unknown'),
])
def test_invalid_configuration_fails_before_connection(tmp_path, change):
    with pytest.raises(ValueError):
        configured(tmp_path, change)


def test_download_uses_exact_revision_subfolder_and_checks_manifest(tmp_path):
    s = configured(tmp_path)
    name = 'pickup_bottle1'
    value = s.raw['actions'][name]
    source = tmp_path / 'snapshot'
    folder = source / value['model_subfolder']
    folder.mkdir(parents=True)
    catalog = json.loads((Path(__file__).parents[1] / 'models/catalog.json').read_text())[name]
    (folder / 'config.json').write_text(json.dumps({'type': 'act', **catalog}))
    (folder / 'model.safetensors').write_bytes(b'fixture, not actual model weights')
    calls = []
    def downloader(**kwargs):
        calls.append(kwargs)
        return source
    download_models(s, [name], downloader=downloader)
    assert calls[0]['revision'] == value['model_revision']
    assert calls[0]['allow_patterns'] == ['30fps/070k/*.json', '30fps/070k/*.safetensors']
    validate_model(s, name)
    manifest = s.policy_path(name) / 'armchemist-model.json'
    manifest.write_text('{}')
    with pytest.raises(ValueError, match='revision'):
        validate_model(s, name)


def test_wrong_checkpoint_cameras_are_rejected(tmp_path):
    s = configured(tmp_path)
    name = 'open_cap'
    folder = s.policy_path(name)
    folder.mkdir(parents=True)
    (folder / 'model.safetensors').write_bytes(b'fixture')
    (folder / 'config.json').write_text(json.dumps({
        'type': 'act', 'input_features': {'observation.state': {'shape': [6]}},
        'output_features': {'action': {'shape': [6]}},
    }))
    with pytest.raises(ValueError, match='불일치'):
        validate_model(s, name)
