import json
import time

from fastapi.testclient import TestClient

from armchemist.api import create_app
from armchemist.storage import Repository


def draft(actions):
    return {'nodes': [{'id': 'draft-1', 'labels': ['Experiment'], 'properties': {'kind': 'draft', 'name': 'test', 'description': 'test', 'actions': actions}}], 'relationships': []}


def test_chat_to_confirm_run_report_and_idempotent_start():
    with TestClient(create_app()) as client:
        request = {'messages': [{'role': 'user', 'content': 'pickup_bottle1, pickdown_bottle1'}], 'state': {}}
        result = client.post('/chat', json=request)
        assert result.status_code == 200
        events = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith('data: ')]
        assert [e['type'] for e in events] == ['status', 'graph', 'plan', 'text-delta', 'done']
        plan = next(e['plan'] for e in events if e['type'] == 'plan')
        start = {'plan_id': plan['id'], 'version': plan['version'], 'graphSnapshotId': plan['graphSnapshotId'], 'confirmed': False}
        assert client.post('/runs', json=start).status_code == 409
        assert client.app.state.runner.act.calls == []
        start['confirmed'] = True
        run = client.post('/runs', json=start).json()
        for _ in range(100):
            value = client.get('/runs/' + run['id']).json()
            if value['status'] not in ('queued', 'running'):
                break
            time.sleep(.01)
        assert value['status'] == 'finished', value
        assert client.get('/reports/' + run['id']).json()['completed_actions'] == ['pickup_bottle1', 'pickdown_bottle1']
        assert client.post('/runs', json=start).json()['id'] == run['id']
        assert value['mode'] == 'mock'


def test_invalid_plan_cannot_be_saved_or_executed():
    with TestClient(create_app()) as client:
        snapshot = client.post('/graph/drafts', json=draft(['pour_bottle'])).json()['graphSnapshotId']
        assert client.post('/plans', json={'goal': 'bad', 'actions': ['pour_bottle'], 'graphSnapshotId': snapshot}).status_code == 422
        assert client.post('/plans', json={'goal': 'changed', 'actions': ['pickup_bottle1'], 'graphSnapshotId': snapshot}).status_code == 422


def test_unknown_natural_language_does_not_fake_success_offline():
    with TestClient(create_app()) as client:
        result = client.post('/chat', json={'messages': [{'role': 'user', 'content': 'something unsupported'}]})
        events = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith('data: ')]
        assert any(e['type'] == 'error' for e in events)
        assert not any(e['type'] == 'plan' for e in events)


def test_plan_is_stable_for_same_confirmed_configuration():
    with TestClient(create_app()) as client:
        snapshot = client.post('/graph/drafts', json=draft(['pickup_bottle1'])).json()['graphSnapshotId']
        request = {'goal': 'test', 'actions': ['pickup_bottle1'], 'graphSnapshotId': snapshot}
        a, b = client.post('/plans', json=request), client.post('/plans', json=request)
        assert a.json()['id'] == b.json()['id']


def test_restart_marks_unfinished_run_unknown(tmp_path):
    path = str(tmp_path / 'db.sqlite')
    repo = Repository(path)
    repo.put('run', 'r', {'id': 'r', 'status': 'running', 'plan': {'id': 'p'}})
    repo.db.close()
    repo = Repository(path)
    assert repo.get('run', 'r')['status'] == 'unknown'


def test_graph_source_does_not_claim_live_without_database():
    with TestClient(create_app()) as client:
        assert client.get('/graph').json()['source'] == 'mock'
        assert client.get('/health').json()['graph_source'] == 'mock'


def test_configuration_change_invalidates_confirmed_plan(tmp_path):
    repo = Repository(str(tmp_path / 'agent.sqlite3'))
    with TestClient(create_app(repository=repo, config_fingerprint='original')) as client:
        snapshot = client.post('/graph/drafts', json=draft(['pickup_bottle1'])).json()['graphSnapshotId']
        plan = client.post('/plans', json={'goal': 'test', 'actions': ['pickup_bottle1'], 'graphSnapshotId': snapshot}).json()
    with TestClient(create_app(repository=repo, config_fingerprint='new-model')) as client:
        result = client.post('/runs', json={'plan_id': plan['id'], 'version': plan['version'], 'graphSnapshotId': snapshot, 'confirmed': True})
        assert result.status_code == 409
        assert '설정' in result.json()['detail']
        assert client.app.state.runner.act.calls == []


def test_second_server_cannot_recover_first_servers_running_record(tmp_path, monkeypatch):
    from armchemist.api import from_environment
    monkeypatch.setenv('AGENT_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('AGENT_MODE', 'mock')
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    with TestClient(from_environment()):
        repo = Repository(str(tmp_path / 'agent.sqlite3'), recover_runs=False)
        repo.put('run', 'active', {'id': 'active', 'status': 'running'})
        import pytest
        with pytest.raises(BlockingIOError):
            from_environment()
        assert repo.get('run', 'active')['status'] == 'running'
