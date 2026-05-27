import tkinter as tk
import paramiko

RASPBERRY_IP = "172.20.10.2"
USERNAME = "raspberry"   # SSH 사용자 이름 확인 필요
PASSWORD = "1234"

COMMAND = """
source /opt/ros/jazzy/setup.bash
source ~/open_manipulator_ws/install/setup.bash
ros2 launch open_manipulator_bringup omx_f.launch.py
"""

def run_ros_launch():
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        ssh.connect(
            hostname=RASPBERRY_IP,
            username=USERNAME,
            password=PASSWORD,
            timeout=5
        )

        ssh.exec_command(f"bash -lc '{COMMAND}'")

        status_label.config(text="ROS2 launch 실행 명령 전송 완료")

    except Exception as e:
        status_label.config(text=f"오류 발생: {e}")

root = tk.Tk()
root.title("Raspberry Pi ROS2 Controller")
root.geometry("400x200")

button = tk.Button(
    root,
    text="OpenManipulator 실행",
    command=run_ros_launch,
    font=("Arial", 16),
    height=2
)
button.pack(pady=40)

status_label = tk.Label(root, text="대기 중")
status_label.pack()

root.mainloop()