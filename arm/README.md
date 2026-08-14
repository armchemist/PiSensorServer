# 로봇 팔 (OpenManipulator / ROS 2)

PC에서 버튼을 누르면 SSH로 라즈베리파이에 접속해 ROS 2 launch를 실행하는 GUI.

| 파일 | 역할 |
| --- | --- |
| `ros_launcher.py` | PC에서 실행하는 tkinter GUI |
| `requirements.txt` | PC측 의존성 (`paramiko`) |

## 실행

**PC(맥)에서** 실행한다. 라즈베리파이가 아니다.

```bash
pip install -r requirements.txt

export RASPBERRY_PASSWORD='라즈베리파이 비밀번호'
python3 ros_launcher.py
```

비밀번호는 공개 저장소에 올라가지 않도록 환경변수로 뺐다. 설정하지 않으면
GUI에 안내 문구가 뜨고 실행되지 않는다.

접속 대상은 환경변수로 바꿀 수 있다.

| 환경변수 | 기본값 |
| --- | --- |
| `RASPBERRY_IP` | `192.168.0.2` |
| `RASPBERRY_USER` | `pi` |
| `RASPBERRY_PASSWORD` | (없음 — 반드시 설정) |

네트워크를 옮겨 다니면 IP가 바뀌므로 `RASPBERRY_IP=pi.local` 을 쓰는 편이 낫다.

## 라즈베리파이 쪽 준비 (아직 안 됨)

이 스크립트는 파이에 다음이 설치돼 있다고 가정한다. **현재 미설치 상태라
버튼을 눌러도 동작하지 않는다.**

- ROS 2 Jazzy (`/opt/ros/jazzy`)
- `~/open_manipulator_ws` 워크스페이스에 `open_manipulator_bringup` 빌드

ROS 2 Jazzy는 Ubuntu 24.04에서만 공식 지원된다. 파이에 설치된 OS가
24.04인지 먼저 확인할 것.

```bash
ssh pi 'lsb_release -a && uname -m'
```

## 알려진 문제

**SSH 세션이 닫히면서 ROS 프로세스가 같이 종료된다.**

`run_ros_launch()` 안에서 `ssh` 가 지역변수라, 함수가 끝나면 가비지 컬렉션되며
연결이 닫힌다. 그때 원격 프로세스에 SIGHUP이 전달돼 launch가 죽는다.

`nohup ... &` 로 프로세스를 분리하는 것이 정석이지만, ROS를 아직 설치하지
않아 실제 동작을 확인할 수 없어 손대지 않았다. ROS 설치 후 버튼을 눌러 증상을
확인한 뒤 고치는 편이 안전하다.
