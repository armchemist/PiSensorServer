"""Physical AI Tools 서버의 실행 제어 확장. 동일 ROS 환경에서 기존 서버 대신 기동한다.

추론/전처리/관절 변환은 상위 서버를 재사용한다. /agent/control만 추가하며,
Web UI의 별도 실행 명령은 거부해 두 실행기가 동시에 팔을 제어하지 않도록 한다.
ROS 의존성은 로봇 PC에서만 설치한다. 배포 방법은 docs/deployment.md를 참고한다.
"""
import json
import math
import time

import rclpy
from physical_ai_interfaces.srv import SendCommand, SetRobotType
from physical_ai_server.physical_ai_server import PhysicalAIServer
from trajectory_msgs.msg import JointTrajectory, JointTrajectoryPoint


class AgentPhysicalAIServer(PhysicalAIServer):
    def __init__(self, settings):
        self.settings = settings
        self.agent_state = "uninitialized"
        self.execution_id = "idle"
        self.agent_error = ""
        self.window_duration = 0
        self.deadline = None
        self.waiting_since = None
        self.pause_since = None
        self.stopped_at = None
        self.hold_positions = None
        self.stable_since = None
        self.last_joint_stamp = 0
        self.transfer_ready = False
        self.transfer_limits = settings.robot['transfer_limits']
        super().__init__()
        # 단일 팔 정책을 실행할 때도 다른 팔·그리퍼를 계속 관측하고 정지한다.
        from sensor_msgs.msg import JointState
        from rclpy.qos import qos_profile_sensor_data
        profile = settings.profiles[settings.robot['hold_profile']]
        self.hold_joint_order = profile['joint_order']
        self.hold_messages, self.hold_publishers, self.hold_subscriptions = {}, {}, []
        for name, topic in profile['joint_topics'].items():
            if name.startswith('follower'):
                self.hold_messages[name] = None
                self.hold_subscriptions.append(self.create_subscription(
                    JointState, topic, lambda msg, key=name: self.hold_messages.__setitem__(key, msg),
                    qos_profile_sensor_data))
            elif name in self.hold_joint_order:
                self.hold_publishers[name] = self.create_publisher(JointTrajectory, topic, 10)
        self.agent_service = self.create_service(SendCommand, "/agent/control", self.agent_control)
        self.agent_timer = self.create_timer(0.02, self.agent_tick)

    def get_robot_type_list(self):
        return list(self.settings.profiles)

    def user_interaction_callback(self, request, response):
        response.success = False
        response.message = "Agent mode: use /agent/control"
        return response

    def set_robot_type_callback(self, request, response):
        response.success = False
        response.message = "Agent mode: robot profile is selected by /agent/control"
        return response

    def _profile(self, name):
        if name not in self.robot_type_list:
            raise ValueError(f"Unknown robot profile: {name}")
        if self.timer_manager:
            self.timer_manager.stop_all()
        self.on_inference = False
        if self.inference_manager and self.inference_manager.policy is not None:
            self.inference_manager.clear_policy()
        req = SetRobotType.Request()
        req.robot_type = name
        res = PhysicalAIServer.set_robot_type_callback(self, req, SetRobotType.Response())
        if not res.success:
            raise RuntimeError(res.message)
        self._original_publish = self.communicator.publish_action
        self.communicator.publish_action = self._publish_inference
        self.communicator.operation_mode = 'inference'

    def _publish_inference(self, joint_msg_datas):
        now = time.monotonic()
        if self.agent_state != "running":
            return
        if self.deadline is not None and now >= self.deadline:
            self._begin_pause()
            return
        # 모델 로딩/관측 대기를 제외하고 첫 액션 명령부터 실행 시간을 센다.
        if self.deadline is None:
            self.deadline = now + self.window_duration
        self._original_publish(joint_msg_datas)

    def _inference_timer_callback(self):
        if self.agent_state != "running":
            return
        now = time.monotonic()
        if (self.deadline is not None and now >= self.deadline) or (
                self.deadline is None and now - self.waiting_since > 10):
            if self.deadline is None:
                self.agent_error = "No action published within 10 seconds"
            self._begin_pause()
            return
        try:
            self._joints()
            for msg in self.communicator.camera_topic_msgs.values():
                if msg is None or not 0 <= self._ros_now() - self._stamp(msg) <= 1:
                    raise RuntimeError("Stale ACT camera observation")
            super()._inference_timer_callback()
            if not self.on_inference:
                raise RuntimeError("Physical AI inference stopped unexpectedly")
        except Exception as exc:
            self.agent_error = str(exc)
            self._begin_pause()

    def _ros_now(self):
        return self.get_clock().now().nanoseconds / 1e9

    @staticmethod
    def _stamp(msg):
        return msg.header.stamp.sec + msg.header.stamp.nanosec / 1e9

    def _joints(self):
        messages = list(self.hold_messages.values())
        if not messages or any(m is None for m in messages):
            raise RuntimeError("Waiting for joint states")
        positions, stamps = {}, []
        for msg in messages:
            stamp = self._stamp(msg)
            if not 0 <= self._ros_now() - stamp <= 0.5:
                raise RuntimeError("Stale joint states")
            if not msg.name or len(msg.name) != len(msg.position):
                raise RuntimeError("Invalid joint state")
            for name, position in zip(msg.name, msg.position):
                if not math.isfinite(position):
                    raise RuntimeError("Nonfinite joint position")
                positions[name] = position
            stamps.append(stamp)
        return positions, min(stamps)

    def _begin_pause(self):
        self.agent_state = "pausing"
        self.deadline = None
        self.pause_since = time.monotonic()
        self.hold_positions, self.stable_since, self.stopped_at = None, None, None
        self.transfer_ready = False

    def _publish_hold(self, positions):
        publishers = self.hold_publishers
        if not publishers:
            raise RuntimeError("No joint trajectory publishers")
        for name, publisher in publishers.items():
            names = list(self.hold_joint_order[name])
            if not names or any(j not in positions for j in names):
                raise RuntimeError("Hold joint mapping incomplete")
            msg, point = JointTrajectory(), JointTrajectoryPoint()
            msg.joint_names = names
            point.positions = [positions[j] for j in names]
            point.velocities = [0.0] * len(names)
            point.time_from_start.nanosec = 100_000_000
            msg.points = [point]
            publisher.publish(msg)

    def agent_tick(self):
        if self.agent_state not in ("pausing", "paused"):
            return
        try:
            positions, stamp = self._joints()
            if self.hold_positions is None:
                self.hold_positions = positions
                self.last_joint_stamp = stamp
                self._publish_hold(positions)
                return
            if stamp <= self.last_joint_stamp:
                return
            self.last_joint_stamp = stamp
            if set(positions) != set(self.hold_positions):
                raise RuntimeError("Joint set changed during hold")
            within = all(abs(positions[j] - self.hold_positions[j]) <= 0.003 for j in positions)
            if not within:
                self.stable_since = None
                if self.agent_state == "paused":
                    raise RuntimeError("Arm moved after confirmed hold")
            elif self.stable_since is None:
                self.stable_since = time.monotonic()
            elif time.monotonic() - self.stable_since >= 0.3:
                if self.agent_state != "paused":
                    self.stopped_at = self._ros_now()
                self.agent_state = "paused"
                # 정지와 레일 이동 가능 여부는 다르다. 보정된 관절 범위가 없으면 이동 불가.
                self.transfer_ready = bool(self.transfer_limits) and all(
                    j in positions and bounds[0] <= positions[j] <= bounds[1]
                    for j, bounds in self.transfer_limits.items()) and set(positions) <= set(self.transfer_limits)
            if self.agent_state == "pausing" and time.monotonic() - self.pause_since > 10:
                raise RuntimeError("Hold confirmation timed out")
        except Exception as exc:
            if self.agent_state == "paused" or time.monotonic() - self.pause_since > 10:
                self.agent_error = str(exc)
                self.transfer_ready = False

    def _snapshot(self):
        return {"execution_id": self.execution_id, "state": self.agent_state,
                "stopped_at": self.stopped_at, "transfer_ready": self.transfer_ready,
                "evidence": "fresh joint states held within 0.003 rad for 0.3s; configured transfer limits",
                "error": self.agent_error}

    def agent_control(self, request, response):
        try:
            data = json.loads(request.task_info.task_instruction[0])
            operation = data["operation"]
            if operation not in ("initialize", "prepare") and data.get("execution_id") != self.execution_id:
                raise ValueError("Execution ID mismatch")
            if operation == "initialize":
                if self.agent_state != "uninitialized":
                    raise ValueError("Already initialized; reconcile existing execution before reconnecting")
                self._profile(data["robot_type"])
                self._begin_pause()
            elif operation == "prepare":
                if self.agent_state != "paused" or self.agent_error:
                    raise ValueError("Prepare requires verified hold")
                from .settings import validate_model
                action = self.settings.actions[data['action']]
                if (data['robot_type'], data['fps'], data['model_revision']) != (
                        action.robot_type, action.fps, action.model_revision):
                    raise ValueError('API and robot model configuration differ')
                validate_model(self.settings, data['action'])
                self.action_config = action
                self._profile(action.robot_type)
                self.execution_id = data["execution_id"]
                self.agent_error = ""
                self.agent_state = "preparing"
                req = SendCommand.Request()
                req.command = SendCommand.Request.START_INFERENCE
                req.task_info.policy_path = action.policy_path
                req.task_info.fps = int(data["fps"])
                req.task_info.task_name = "armchemist"
                req.task_info.task_type = "inference"
                req.task_info.user_id = "armchemist"
                req.task_info.task_instruction = [data["instruction"]]
                req.task_info.num_episodes = 1
                result = PhysicalAIServer.user_interaction_callback(self, req, SendCommand.Response())
                if not result.success or not self.inference_manager.load_policy():
                    raise RuntimeError("Policy preparation failed")
                features = self.inference_manager.policy.config.input_features
                state_features = features.get('observation.state')
                if state_features is None or tuple(state_features.shape) != (len(self.total_joint_order),):
                    raise RuntimeError("Policy and robot joint dimensions differ")
                cameras = {key.removeprefix('observation.images.') for key in features
                           if key.startswith('observation.images.')}
                if cameras != set(self.communicator.camera_topics):
                    raise RuntimeError("Policy and robot camera names differ")
                self._begin_pause()
            elif operation == "run":
                if self.agent_state != "paused" or self.agent_error or self.inference_manager.policy is None:
                    raise ValueError("Run requires a loaded policy and verified hold")
                duration = float(data["duration_s"])
                if not math.isfinite(duration) or not 0 < duration <= 600:
                    raise ValueError("Invalid execution duration")
                if not math.isclose(duration, self.action_config.mean_duration_s, abs_tol=1e-6):
                    raise ValueError("API and robot mean duration differ")
                # 이전 window의 ACT action queue가 현재 카메라 상태를 무시하지 않게 비운다.
                self.inference_manager.policy.reset()
                self.window_duration, self.deadline = duration, None
                self.waiting_since = time.monotonic()
                self.agent_state = "running"
                self.stopped_at = None
                self.transfer_ready = False
            elif operation == "pause":
                if self.agent_state == "uninitialized":
                    raise ValueError("Initialize a robot profile first")
                self._begin_pause()
            elif operation == "finish":
                if self.agent_state != "paused" or self.agent_error:
                    raise ValueError("Finish requires verified hold")
                self.on_inference = False
                if self.timer_manager:
                    self.timer_manager.stop(timer_name="inference")
                if self.inference_manager.policy is not None:
                    self.inference_manager.clear_policy()
            elif operation != "status":
                raise ValueError("Unknown operation")
            response.success = True
            response.message = json.dumps(self._snapshot())
        except Exception as exc:
            response.success = False
            response.message = str(exc)
        return response


def main():
    import argparse
    import tempfile
    import yaml
    from .settings import Settings
    parser = argparse.ArgumentParser()
    parser.add_argument('--config')
    args, ros_args = parser.parse_known_args()
    settings = Settings(args.config)
    # 동일 YAML의 프로파일을 ROS 파라미터로 전달한다. 설치된 upstream 파일 수정 불필요.
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml') as params:
        yaml.safe_dump(settings.ros_parameters(), params, sort_keys=False)
        params.flush()
        rclpy.init(args=[*ros_args, '--ros-args', '--params-file', params.name])
        node = AgentPhysicalAIServer(settings)
    try:
        # 단일 executor로 타이머·서비스·관절 명령 간 경쟁을 막는다.
        rclpy.spin(node)
    finally:
        if node.communicator:
            node._begin_pause()
            try:
                positions, _ = node._joints()
                node._publish_hold(positions)
            except Exception as exc:
                node.get_logger().error(f"Shutdown hold failed: {exc}")
        node._cleanup_hf_api_worker()
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
