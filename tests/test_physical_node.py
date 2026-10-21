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
