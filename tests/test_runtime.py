import threading
from dataclasses import replace

import pytest

from armchemist.mock import MockAct, MockJudge, MockRail
from armchemist.planning import compile_plan
from armchemist.registry import CAPABILITIES, ActionConfig, demo_actions, duration_statistics
from armchemist.runtime import DecisionUnavailable, ExecutionError, Frame, Runner, Verdict


def build(outcomes=(), **kwargs):
    arm, rail = MockAct(), MockRail()
    runner = Runner(arm, rail, MockJudge(outcomes), demo_actions(), **kwargs)
    return runner, arm, rail


@pytest.mark.parametrize('attempt', [1, 2, 3])
def test_full_mean_windows_and_stop_before_judging(attempt):
    runner, arm, rail = build(['incomplete'] * (attempt - 1) + ['complete'])
    actions = demo_actions()
    actions['pickup_bottle1'] = ActionConfig(0.02)
    runner.actions = actions
    result = runner.run(compile_plan('병 잡기', ['pickup_bottle1']))
    assert result['status'] == 'finished'
    assert [c for c in arm.calls if c[0] == 'run'] == [('run', 0.02)] * attempt
    assert arm.calls.count(('finish',)) == 1
    for i, call in enumerate(arm.calls):
        if call[0] == 'capture':
            assert arm.calls[i - 1] == ('pause',)
    assert rail.moves == []


def test_three_incomplete_abort_without_next_act():
    runner, arm, _ = build(['incomplete'] * 3)
    result = runner.run(compile_plan('병 잡고 내려놓기', ['pickup_bottle1', 'pickdown_bottle1']))
    assert result['status'] == 'failed'
    assert len([c for c in arm.calls if c[0] == 'run']) == 3
    assert result['completed_actions'] == []
    assert ('prepare', 'pickdown_bottle1') not in arm.calls
    assert runner.facts is None


def test_judge_latency_keeps_hold():
    runner, arm, _ = build()
    class Judge:
        def check(self, cap, frames):
            assert arm.paused
            assert len([c for c in arm.calls if c[0] == 'run']) == 1
            return Verdict('complete', 'test')
    runner.judge = Judge()
    assert runner.run(compile_plan('test', ['pickup_bottle1']))['status'] == 'finished'


def test_api_error_retries_judgment_not_arm():
    runner, arm, _ = build([DecisionUnavailable('timeout'), 'complete'])
    assert runner.run(compile_plan('test', ['pickup_bottle1']))['status'] == 'finished'
    assert len([c for c in arm.calls if c[0] == 'run']) == 1
    assert runner.judge.calls == 2


def test_unknown_judgment_does_not_move_again():
    runner, arm, _ = build(['unknown'])
    assert runner.run(compile_plan('test', ['pickup_bottle1']))['status'] == 'failed'
    assert len([c for c in arm.calls if c[0] == 'run']) == 1


def test_old_frame_prevents_judgment_and_next_action():
    runner, arm, _ = build()
    arm.capture = lambda after: [Frame('camera', after, b'old')]
    result = runner.run(compile_plan('test', ['pickup_bottle1']))
    assert result['status'] == 'failed'
    assert runner.judge.calls == 0


def test_cancel_during_judgment_does_not_advance():
    runner, arm, rail = build()
    cancel = threading.Event()
    class Judge:
        def check(self, cap, frames):
            cancel.set()
            return Verdict('complete', 'late answer')
    runner.judge = Judge()
    result = runner.run(compile_plan('test', ['pickup_bottle1', 'open_cap']), cancel)
    assert result['status'] == 'cancelled'
    assert rail.moves == []
    assert ('finish',) not in arm.calls


def test_pause_failure_still_stops_rail():
    runner, arm, rail = build()
    def fail():
        raise ExecutionError('hold failed')
    arm.pause = fail
    rail.ready = True
    result = runner.run(compile_plan('test', ['pickup_bottle1']))
    assert result['status'] == 'failed'
    assert not rail.ready
    assert result['stop_errors']
    assert runner.judge.calls == 0


def test_ending_pose_transfer_and_same_station_skip():
    runner, arm, rail = build()
    names = ['pickup_bottle1', 'open_cap', 'pour_bottle', 'close_cap', 'pickdown_bottle1']
    result = runner.run(compile_plan('test', names))
    assert result['status'] == 'finished'
    assert rail.moves == ['실험존', '시약존']
    assert [c[1] for c in arm.calls if c[0] == 'prepare'] == names
    assert runner.facts == {'left': 'empty', 'right': 'empty', 'cap': 'closed'}


def test_does_not_assume_empty_hands_on_second_run():
    runner, _, _ = build()
    assert runner.run(compile_plan('test', ['pickup_bottle1']))['status'] == 'finished'
    assert runner.run(compile_plan('again', ['pickup_bottle1']))['status'] == 'failed'
    plan = compile_plan('put back', ['pickdown_bottle1'], initial_facts=runner.facts)
    assert runner.run(plan)['status'] == 'finished'


def test_unverified_transfer_fails_before_rail():
    runner, arm, rail = build()
    pause = arm.pause
    arm.pause = lambda: replace(pause(), transfer_ready=False)
    result = runner.run(compile_plan('test', ['setup_sensors']))
    assert result['status'] == 'failed'
    assert rail.moves == []


@pytest.mark.parametrize('names', [['stow_arm'], ['pour_bottle'], ['pickup_cylinder', 'pickup_cylinder'], ['pickup_bottle1', 'open_cap', 'pickup_cylinder']])
def test_unsupported_or_impossible_plan_rejected(names):
    with pytest.raises(ValueError):
        compile_plan('test', names)


def test_exact_station_mapping_and_no_unavailable_actions():
    assert len(CAPABILITIES) == 9
    plan = compile_plan('test', ['pickup_cylinder', 'pour_cylinder', 'pickdown_cylinder'])
    assert [s.station for s in plan.steps if s.kind == 'rail'] == ['실린더존', '실험존', '실린더존']


def test_bad_settings_fail_before_hardware():
    runner, arm, _ = build()
    runner.actions['pickup_bottle1'] = ActionConfig(float('nan'))
    assert runner.run(compile_plan('test', ['pickup_bottle1']))['status'] == 'failed'
    assert arm.calls == []
    with pytest.raises(ValueError):
        ActionConfig(20).validate(live=True)


def test_duration_excludes_selected_episodes_and_uses_fps():
    result = duration_statistics([{'episode_index': 0, 'length': 150}, {'episode_index': 1, 'length': 300}], 15, [0])
    assert result['mean_duration_s'] == 20
    with pytest.raises(ValueError):
        duration_statistics([{'episode_index': 0, 'length': 150}], 15, [99])


@pytest.mark.parametrize('action,facts,next_action', [
    ('pour_bottle', {'left': 'bottle', 'right': 'cap', 'cap': 'open'}, 'close_cap'),
    ('pour_cylinder', {'left': 'empty', 'right': 'cylinder', 'cap': 'closed'}, 'pickdown_cylinder'),
])
@pytest.mark.parametrize('attempts', [1, 2, 3])
def test_pour_uses_camera_and_retries_until_upright(action, facts, next_action, attempts):
    runner, arm, _ = build(['incomplete'] * (attempts - 1) + ['complete', 'complete'], initial_facts=facts)
    runner.actions[action] = ActionConfig(.02)
    result = runner.run(compile_plan('pour and continue', [action, next_action], initial_facts=facts))
    assert result['status'] == 'finished'
    assert result['completed_actions'] == [action, next_action]
    assert [c for c in arm.calls if c[0] == 'run'] == [('run', .02)] * attempts + [('run', .01)]
    assert runner.judge.calls == attempts + 1
    assert arm.calls.count(('capture',)) == attempts + 1
    assert len([e for e in result['events'] if e['phase'] == 'judged' and e['action'] == action]) == attempts
    assert not [e for e in result['events'] if e['phase'] == 'completion_assumed']


@pytest.mark.parametrize('outcomes', [['incomplete'] * 3, ['unknown']])
def test_pour_stops_if_not_upright_or_not_visible(outcomes):
    facts = {'left': 'empty', 'right': 'cylinder', 'cap': 'closed'}
    runner, arm, _ = build(outcomes, initial_facts=facts)
    runner.actions['pour_cylinder'] = ActionConfig(.01, completion_mode='vision')
    result = runner.run(compile_plan('pour', ['pour_cylinder', 'pickdown_cylinder'], initial_facts=facts))
    assert result['status'] == 'failed'
    assert result['completed_actions'] == []
    assert ('prepare', 'pickdown_cylinder') not in arm.calls
    assert runner.judge.calls == len(outcomes)


@pytest.mark.parametrize('failure', ['execution', 'wrong_receipt', 'cancel'])
def test_pour_does_not_mask_errors_or_cancellation(failure):
    facts = {'left': 'empty', 'right': 'cylinder', 'cap': 'closed'}
    runner, arm, _ = build(initial_facts=facts)
    def fail_window(seconds, cancel):
        if failure == 'execution':
            raise ExecutionError('inference or hold failed')
        if failure == 'cancel':
            cancel.set()
            return arm.pause()
        return replace(arm.pause(), execution_id='stale')
    arm.run_window = fail_window
    result = runner.run(compile_plan('pour', ['pour_cylinder', 'pickdown_cylinder'], initial_facts=facts))
    assert result['status'] == ('cancelled' if failure == 'cancel' else 'failed')
    assert result['completed_actions'] == []
    assert ('prepare', 'pickdown_cylinder') not in arm.calls
    assert runner.judge.calls == 0


@pytest.mark.parametrize('action,facts,next_action', [
    ('pour_bottle', {'left': 'bottle', 'right': 'cap', 'cap': 'open'}, 'close_cap'),
    ('pour_cylinder', {'left': 'empty', 'right': 'cylinder', 'cap': 'closed'}, 'pickdown_cylinder'),
])
def test_demo_pour_finishes_one_window_without_camera_or_judge(action, facts, next_action):
    runner, arm, _ = build(['complete'], initial_facts=facts)
    runner.actions[action] = ActionConfig(.02, completion_mode='mean_window')
    result = runner.run(compile_plan('pour and continue', [action, next_action], initial_facts=facts))
    assert result['status'] == 'finished'
    assert result['completed_actions'] == [action, next_action]
    assert [c for c in arm.calls if c[0] == 'run'] == [('run', .02), ('run', .01)]
    assert runner.judge.calls == arm.calls.count(('capture',)) == 1  # next action only
    events = [e for e in result['events'] if e.get('action') == action]
    assert not any(e['phase'] in ('judging', 'judged', 'decision_retry') for e in events)
    assert next(e for e in events if e['phase'] == 'execution_window_complete')['visual_verified'] is False
    assert next(e for e in events if e['phase'] == 'action_complete')['completion_basis'] == 'mean_window'


def test_non_pour_cannot_skip_visual_completion():
    runner, arm, _ = build()
    runner.actions['pickup_bottle1'] = ActionConfig(.02, completion_mode='mean_window')
    result = runner.run(compile_plan('pickup', ['pickup_bottle1']))
    assert result['status'] == 'failed'
    assert not arm.calls
