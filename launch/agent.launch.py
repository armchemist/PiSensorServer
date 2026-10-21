"""로봇 드라이버는 별도로 기동한다. 기본 PhysicalAIServer와 중복 실행하지 않는다."""
import sys
from pathlib import Path

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, ExecuteProcess
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    return LaunchDescription([
        DeclareLaunchArgument('config', default_value=str(Path(__file__).resolve().parents[1] / 'config/main.yaml'),
                              description='ARMchemist main YAML absolute path'),
        ExecuteProcess(cmd=[sys.executable, '-m', 'armchemist.physical_node', '--config', LaunchConfiguration('config')],
                       cwd=str(Path(__file__).resolve().parents[1]), output='screen'),
        Node(package='rosbridge_server', executable='rosbridge_websocket', name='rosbridge_websocket'),
        Node(package='web_video_server', executable='web_video_server', name='web_video_server'),
    ])
