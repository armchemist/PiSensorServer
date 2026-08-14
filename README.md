# RaspberryPiRemoteController

라즈베리파이로 로봇 팔과 리니어 스테이지를 구동하고, 수질 센서(pH/전도도) 값을
읽는 프로젝트.

## 구조

크게 **구동**(액추에이터)과 **측정**(센서)으로 나뉜다.

| 디렉토리 | 분류 | 내용 |
| --- | --- | --- |
| [`arm/`](arm/) | 구동 | 로봇 팔 (OpenManipulator, ROS 2 Jazzy) |
| [`rail/`](rail/) | 구동 | 리니어 스테이지 (스텝모터 + 볼스크류) |
| [`sensors/`](sensors/) | 측정 | pH(SEN0161), 전도도(SEN0244), 카메라 |

각 디렉토리의 `README.md` 에 배선과 사용법이 있다.

## 하드웨어 구성

```
                    ┌─────────────────┐
   PC (맥) ──SSH────┤ 라즈베리파이 4B  │
                    │  Ubuntu 24.04   │
                    └────┬───┬───┬────┘
                         │   │   │
              GPIO ──────┘   │   └────── USB
            (스텝모터 드라이버)│         (카메라)
                             │
                            USB
                             │
                      ┌──────┴──────┐
                      │ 아두이노 우노 │  ← 아날로그 센서용 ADC 역할
                      │ +SensorShield│
                      └──┬────────┬─┘
                     A0  │        │  A1
                    pH(SEN0161)  전도도(SEN0244)
```

라즈베리파이에는 ADC가 없어서 아날로그 센서를 직접 읽지 못한다. 우노가 대신
읽어 USB 시리얼로 넘긴다. 자세한 건 [`sensors/README.md`](sensors/README.md).

## 라즈베리파이 접속

`avahi-daemon`(mDNS)을 설치해 둬서 IP가 바뀌어도 이름으로 붙는다.

```bash
ssh pi@pi.local
```

맥의 `~/.ssh/config` 에 별칭이 있으면 `ssh pi` 만으로도 된다.

WiFi는 SD카드의 `network-config` 에 여러 개를 등록해 둬서, 부팅 시 잡히는
네트워크에 자동으로 붙는다. 장소를 옮겨도 카드를 다시 꽂을 필요가 없다.

## 실행 위치

파일마다 도는 컴퓨터가 다르다.

| 파일 | 실행 위치 | 필요한 패키지 |
| --- | --- | --- |
| `arm/ros_launcher.py` | PC (맥) | `paramiko` |
| `rail/*.py` | 라즈베리파이 | `pigpio` |
| `sensors/read_sensors.py` | 라즈베리파이 | `pyserial` |
| `sensors/arduino_sensors/` | 아두이노 우노 | — |

## 진행 상황

- [x] 라즈베리파이 설치 및 WiFi/SSH 구성
- [x] 리니어 스테이지 제어 코드 (하드웨어 검증 전)
- [x] 센서 읽기 코드 (아두이노 → 파이)
- [ ] pH 캘리브레이션 (pH 4.00/7.00 표준액 필요)
- [ ] USB 카메라 연동
- [ ] ROS 2 Jazzy 설치 및 OpenManipulator 빌드
