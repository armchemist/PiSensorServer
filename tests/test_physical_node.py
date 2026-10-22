"""ROS 서비스 확장의 제어 경계 테스트. 실제 모터/ROS/GPU 시험을 대신하지 않는다."""
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture
def node_type(monkeypatch):
    rclpy = types.ModuleType('rclpy')
    interface = types.ModuleType('physical_ai_interfaces.srv')
    interface.SendCommand = interface.SetRobotType = object
    server = types.ModuleType('physical_ai_server.physical_ai_server')
    server.PhysicalAIServer = type('PhysicalAIServer', (), {})
    msgs = types.ModuleType('trajectory_msgs.msg')
    msgs.JointTrajectory = type('JointTrajectory', (), {})
    class Point:
        def __init__(self):
            self.time_from_start = types.SimpleNamespace(nanosec=0)
    msgs.JointTrajectoryPoint = Point
    for name, mod in [('rclpy', rclpy), ('physical_ai_interfaces.srv', interface), ('physical_ai_server.physical_ai_server', server), ('trajectory_msgs.msg', msgs)]:
        monkeypatch.setitem(sys.modules, name, mod)
    spec = importlib.util.spec_from_file_location('tested_physical_node', Path(__file__).parents[1] / 'armchemist/physical_node.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.AgentPhysicalAIServer


def test_late_policy_result_cannot_publish_after_deadline(node_type):
    node = node_type.__new__(node_type)
    node.agent_state = 'running'
    node.deadline = 0
    publications = []
    node._original_publish = publications.append
    node._publish_inference({'joint': 'late action'})
    assert publications == []
    assert node.agent_state == 'pausing'


def test_paused_node_does_not_publish_policy_actions(node_type):
    node = node_type.__new__(node_type)
    node.agent_state = 'paused'
    node._original_publish = lambda _: pytest.fail('action emitted while paused')
    node._publish_inference({'joint': 'cached action'})


def test_hold_uses_physical_ai_joint_order_prefix(node_type):
    node = node_type.__new__(node_type)
    calls = []
    node.hold_publishers = {'leader': types.SimpleNamespace(publish=calls.append)}
    node.hold_joint_order = {'leader': ['joint1', 'gripper']}
    node._publish_hold({'joint1': .7, 'gripper': .2})
    assert calls[0].joint_names == ['joint1', 'gripper']
    assert calls[0].points[0].positions == [.7, .2]
    assert calls[0].points[0].velocities == [0, 0]


def test_missing_gripper_position_rejects_hold(node_type):
    node = node_type.__new__(node_type)
    node.hold_publishers = {'leader': types.SimpleNamespace(publish=lambda _: None)}
    node.hold_joint_order = {'leader': ['joint1', 'gripper']}
    with pytest.raises(RuntimeError):
        node._publish_hold({'joint1': .7})


def test_old_execution_id_cannot_resume(node_type):
    node = node_type.__new__(node_type)
    node.execution_id = 'new'
    request = types.SimpleNamespace(task_info=types.SimpleNamespace(task_instruction=[json.dumps({'operation': 'run', 'execution_id': 'old', 'duration_s': 15})]))
    response = types.SimpleNamespace()
    result = node.agent_control(request, response)
    assert not result.success
    assert 'ID mismatch' in result.message


def test_hold_observes_both_arms_independent_of_active_policy(node_type):
    node = node_type.__new__(node_type)
    node._ros_now = lambda: 10.0
    def state(name, position):
        return types.SimpleNamespace(name=[name], position=[position], header=types.SimpleNamespace(stamp=types.SimpleNamespace(sec=10, nanosec=0)))
    node.hold_messages = {'left': state('l_gripper', .1), 'right': state('r_gripper', .2)}
    # 추론 communicator가 한쪽 팔만 구독해도 정지 관측에는 양팔이 들어간다.
    node.communicator = types.SimpleNamespace(follower_topic_msgs={'left': node.hold_messages['left']})
    assert node._joints()[0] == {'l_gripper': .1, 'r_gripper': .2}
    node.hold_messages['right'] = None
    with pytest.raises(RuntimeError, match='Waiting'):
        node._joints()


def test_policy_idle_stops_only_after_min_fraction(node_type):
    node = node_type.__new__(node_type)
    node.idle_stop = {'action_tolerance_rad': 0.02, 'idle_s': 1.0, 'min_fraction': 0.5, 'max_factor': 2.0}
    node.window_duration, node.min_run_s = 20.0, 5.0
    node.started_at = 0.0
    action = {'leader': types.SimpleNamespace(points=[types.SimpleNamespace(positions=[0.1, 0.2])])}
    moved = {'leader': types.SimpleNamespace(points=[types.SimpleNamespace(positions=[0.2, 0.2])])}
    assert not node._policy_idle(action, 1.0)
    assert not node._policy_idle(action, 2.5)   # 1초 넘게 그대로지만 평균의 절반(5초) 전
    assert not node._policy_idle(moved, 5.5)    # 움직이면 다시 잰다
    assert not node._policy_idle(moved, 6.0)
    assert node._policy_idle(moved, 6.6)
    node.action_idle_s = 0  # 붓기처럼 ACT 별로 끄면 평균 시간 고정 window
    assert not node._policy_idle(moved, 20.0)


def test_end_pose_moves_act_joints_and_keeps_gripper(node_type):
    node = node_type.__new__(node_type)
    node.execution_id, node.agent_state, node.agent_error = 'e1', 'paused', ''
    node.action_name, node.end_pose_speed = 'setup_sensors', 0.5
    node.settings = types.SimpleNamespace(raw={'actions': {'setup_sensors': {'end_pose': {'joint1': 1.0}}}})
    node._joints = lambda: ({'joint1': 0.0, 'gripper': 0.6}, 1.0)
    node._snapshot = lambda: {'state': node.agent_state}
    calls = []
    node.hold_publishers = {'leader': types.SimpleNamespace(publish=calls.append)}
    node.hold_joint_order = {'leader': ['joint1', 'gripper']}
    request = types.SimpleNamespace(task_info=types.SimpleNamespace(task_instruction=[json.dumps({'operation': 'end_pose', 'execution_id': 'e1'})]))
    result = node.agent_control(request, types.SimpleNamespace())
    assert result.success, result.message
    point = calls[0].points[0]
    assert point.positions == [1.0, 0.6]                  # 그리퍼는 지금 값 유지
    assert point.time_from_start.sec == 2                 # 1 rad / 0.5 rad/s = 2초
    assert node.agent_state == 'moving'


def test_held_frames_keep_last_good_frame_after_camera_gap(node_type):
    frames = node_type.__init__.__globals__['HeldFrames']
    now = [0.0]
    held = frames({'wrist': None}, warmup_s=5, clock=lambda: now[0])
    held['wrist'] = 'good'
    now[0] = 0.1
    held['wrist'] = 'good2'
    for i in range(50):  # 4.0초에 끊겼다 돌아온 뒤 15fps 로 들어오는 하얀 화면 5초
        now[0] = 4.0 + i * 0.1
        held['wrist'] = 'white'
    assert held['wrist'] == 'good2'
    now[0] = 9.05
    held['wrist'] = 'fixed'
    assert held['wrist'] == 'fixed'
    held['wrist'] = None  # communicator 초기화는 그대로 통과
    assert held['wrist'] is None


def test_pause_while_idle_clears_old_hold_error_and_rechecks(node_type):
    node = node_type.__new__(node_type)
    node.agent_state, node.agent_error, node.execution_id = 'paused', 'Arm moved after confirmed hold', 'idle'
    node.started_at, node.ran_s = None, None
    node._snapshot = lambda: {}
    request = types.SimpleNamespace(task_info=types.SimpleNamespace(task_instruction=[json.dumps({'operation': 'pause', 'execution_id': 'idle'})]))
    response = types.SimpleNamespace()
    node.agent_control(request, response)
    assert response.success and node.agent_error == '' and node.agent_state == 'pausing'
    node.agent_state, node.agent_error = 'running', 'Stale ACT camera observation'
    node.agent_control(request, response)
    assert node.agent_error == 'Stale ACT camera observation'


def test_hold_keeps_last_policy_gripper_command(node_type):
    node = node_type.__new__(node_type)
    node.agent_state, node.deadline, node.idle_stop, node.gripper_cmd = 'running', None, {}, {}
    node.window_duration, node.action_idle_s = 10, None
    node._original_publish = lambda msgs: None
    point = types.SimpleNamespace(positions=[0.5, 1.2])
    node._publish_inference({'leader_right': types.SimpleNamespace(
        joint_names=['r_joint1', 'r_gripper_joint_1'], points=[point])})
    sent = []
    node.hold_publishers = {'leader_right': types.SimpleNamespace(publish=sent.append)}
    node.hold_joint_order = {'leader_right': ['r_joint1', 'r_gripper_joint_1']}
    msgs = sys.modules['trajectory_msgs.msg']
    node._publish_hold({'r_joint1': 0.4, 'r_gripper_joint_1': 0.9})  # 그리퍼는 물체에 막혀 0.9 에서 멈춤
    assert sent[0].points[0].positions == [0.4, 1.2]


def test_paused_hold_ignores_gripper_and_small_drift(node_type):
    node = node_type.__new__(node_type)
    node.agent_state, node.agent_error, node.move_until = 'paused', '', 0
    node.hold_tolerance, node.hold_drift, node.transfer_limits = 0.01, 0.05, {}
    ref = {'l_joint4': -1.06, 'l_gripper_joint_1': 0.27}
    node.hold_positions, node.settle_ref, node.last_joint_stamp = ref, ref, 0
    node.stable_since, node.pause_since = 0, 0
    samples = iter([({'l_joint4': -1.03, 'l_gripper_joint_1': -0.02}, 1), ({'l_joint4': -1.0, 'l_gripper_joint_1': -0.02}, 2)])
    node._joints = lambda: next(samples)
    node.agent_tick()
    assert node.agent_error == '' and node.agent_state == 'paused'
    node.agent_tick()
    assert 'l_joint4' in node.agent_error
