# 하드웨어 API 서버 (파이2 — 고정된 쪽)

클라우드에서 도는 에이전트는 USB에 손을 뻗을 수 없다. 실험대 쪽 컴퓨터
(라즈베리파이)가 하드웨어를 물고 HTTP로 노출하고, 에이전트는 그 API를 호출한다.

```
클라우드 (LLM 에이전트)
     │  HTTP
┌────┴─────────┐
│ 파이2 (정적)  │  ← 이 서버
└─┬───┬───┬────┘
  │   │   └── USB  → 아두이노 → pH / 전도도
  │   └────── USB  → 열화상 (TMC160F)
  └────────── GPIO → 레일 (스텝모터 드라이버)
```

로봇 팔과 핸디캠은 레일 위에서 같이 움직이는 파이1이 맡는다 —
[PiRobotControl](https://github.com/armchemist/PiRobotControl). 두 파이는 서로를
모르고, 에이전트가 양쪽 주소를 각각 호출한다.

엔드포인트는 에이전트의 도구(tool)와 1:1로 대응하도록 설계했다. SSH로 셸 명령을
보내는 방식과 달리 출력 파싱이 안정적이고, 상태를 조회할 수 있으며, 한계를
서버에서 강제할 수 있다.

## 설치

라즈베리파이에서. 하드웨어를 직접 다루는 패키지는 apt로, 나머지는 venv에 받는다.

```bash
sudo apt update
sudo apt install -y python3-venv python3-serial python3-opencv
```

`python3-serial`(우노)과 `python3-opencv`(열화상 렌더링)를 apt로 받는 이유는,
ARM에서 pip로 설치하면 빌드에 아주 오래 걸리거나 실패하기 때문이다.

opencv 는 열화상 **캡처**에는 쓰지 않는다. 프레임은 V4L2 로 직접 읽고, opencv 는
컬러맵을 입혀 JPEG 로 만드는 표시 단계에만 쓴다. 이유는 아래 열화상 절 참고.

```bash
cd ~/PiSensorServer
python3 -m venv --system-site-packages .venv
.venv/bin/pip install fastapi "uvicorn[standard]"
```

`--system-site-packages` 가 있어야 venv 안에서 apt로 설치한 `cv2` 와 `serial` 을
볼 수 있다. 빼먹으면 열화상 렌더링과 센서가 동작하지 않는다.

Ubuntu 24.04는 시스템 파이썬에 pip 설치를 막아 두었다(PEP 668).
`--break-system-packages` 로 우회할 수도 있지만, 시스템 패키지와 충돌할 수 있어
venv를 쓰는 편이 안전하다.

## 실행

저장소 루트에서:

```bash
.venv/bin/python -m server.run
```

포트를 바꾸려면 `PORT=9000 .venv/bin/python -m server.run`.

### uvicorn 을 직접 부르지 않는 이유

`uvicorn --host` 로는 IPv4와 IPv6 중 한쪽만 열린다.

| 명령 | 결과 |
| --- | --- |
| `--host 0.0.0.0` | IPv4만. IPv6 전용 네트워크(아이폰 핫스팟 등)에서 접속 불가 |
| `--host ::` | IPv6만. 일반 공유기(IPv4)에서 접속 불가 |

커널 기본값은 듀얼 스택이지만, 파이썬 `asyncio` 의 `create_server()` 가 IPv6
주소로 바인드할 때 `IPV6_V6ONLY` 소켓 옵션을 명시적으로 켜기 때문이다.

`server/run.py` 는 소켓을 직접 만들어 그 옵션을 끈 뒤 uvicorn 에 넘긴다.
장소를 옮겨 네트워크가 바뀌어도 실행 명령을 바꿀 필요가 없다.

기동 시 어떻게 바인드됐는지 첫 줄에 찍힌다.

```
바인드: :: (IPv4+IPv6)  포트 8000
```

위 명령은 포그라운드로 돈다. SSH 창을 닫거나 Ctrl+C 를 누르면 서버도 같이
죽으므로, 실제 운용에서는 아래 systemd 등록을 쓴다.

## systemd 등록 (권장)

클라우드 에이전트가 붙을 시스템이라 사람이 매번 띄워주는 구조로는 운영이 안 된다.
등록해 두면 부팅 시 자동 시작되고, 죽으면 알아서 다시 뜬다.

```bash
sudo cp server/pi-sensor-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pi-sensor-server
```

| 명령 | 용도 |
| --- | --- |
| `systemctl status pi-sensor-server` | 상태 확인 |
| `journalctl -u pi-sensor-server -f` | 로그 실시간 |
| `sudo systemctl restart pi-sensor-server` | 코드 수정 후 재시작 |
| `sudo systemctl stop pi-sensor-server` | 잠시 내리기 |
| `sudo systemctl disable pi-sensor-server` | 자동 시작 해제 |

유닛 파일은 경로가 `/home/pi/PiSensorServer` 로 박혀 있다. 다른
곳에 두었다면 `WorkingDirectory` 와 `ExecStart` 를 고칠 것.

### 환경변수는 .env 로

`API_KEY` 같은 값은 저장소 루트의 `.env` 에 넣는다. 유닛이 읽어 간다.

```bash
cat > ~/PiSensorServer/.env <<'EOF'
API_KEY=충분히 긴 무작위 문자열
EOF
sudo systemctl restart pi-sensor-server
```

`.env` 는 `.gitignore` 에 있어서 커밋되지 않는다.

### ⚠️ 시리얼 포트 충돌

서비스가 떠 있으면 아두이노 시리얼 포트를 서버가 잡고 있다. `read_sensors.py`
를 직접 돌리려면 먼저 서비스를 내려야 한다.

```bash
sudo systemctl stop pi-sensor-server
python3 sensors/read_sensors.py
sudo systemctl start pi-sensor-server
```

문서는 <http://pi.local:8000/docs> 에서 확인할 수 있다(Swagger UI). 브라우저에서
직접 호출해볼 수 있어 디버깅에 편하다.

## 엔드포인트

| 메서드 | 경로 | 설명 |
| --- | --- | --- |
| `GET` | `/health` | 모든 하드웨어의 연결 상태 |
| `GET` | `/sensors` | 마지막으로 읽은 pH / 전도도 |
| `GET` | `/thermal/ui` | **bbox 지정 웹 UI.** 브라우저로 열면 된다 |
| `GET` | `/thermal/stream` | 열화상 MJPEG 스트림 (ROI 사각형 포함) |
| `GET` | `/thermal/frame` | 열화상 한 장 (JPEG) |
| `GET` | `/thermal/stats` | 화면 전체의 최저/최고/평균 ℃ 와 최고온 지점 |
| `POST` | `/thermal/roi` | 추적할 영역 등록 `{"x":60,"y":45,"w":40,"h":30}` |
| `GET` | `/thermal/rois` | 등록된 영역과 각각의 최신 값 |
| `GET` | `/thermal/roi/{id}` | 한 영역의 시계열. `?limit=600` 으로 최근 것만 |
| `GET` | `/thermal/roi/{id}/series.csv` | 시계열 CSV 내려받기 |
| `PATCH` | `/thermal/roi/{id}` | 이름·좌표 수정 |
| `POST` | `/thermal/roi/{id}/reset` | 좌표는 두고 시계열만 비움 |
| `DELETE` | `/thermal/roi/{id}` | 영역 삭제 |
| `DELETE` | `/thermal/rois` | 전체 삭제 |
| `GET` | `/rail/position` | 캐리지 현재 위치 |
| `POST` | `/rail/move` | 상대 이동 `{"mm": 10, "speed_mm_s": 5}` |
| `POST` | `/rail/move_to` | 절대 위치 이동 |
| `POST` | `/rail/set_position` | 위치를 잃었을 때 실제 위치를 알려줌 |
| `POST` | `/rail/resume` | 저장된 위치를 그대로 이어 씀 (캐리지를 건드리지 않았을 때) |
| `POST` | `/rail/jog` | 상대 이동. 보정 중에는 절대 기준 없이 동작 |
| `GET` | `/rail/calibration` | 보정 상태 (스트로크, 보정 여부, 위치 신뢰 여부) |
| `POST` | `/rail/calibration/begin` | 지금 자리를 시작 지점으로 지정 |
| `POST` | `/rail/calibration/end` | 지금 자리를 종료 지점으로 확정, 저장 |
| `POST` | `/rail/calibration/cancel` | 보정 취소 |
| `GET` | `/rail/ui` | 보정·스테이션 등록 웹 UI (브라우저에서 열기) |
| `GET` | `/rail/stations` | 스테이션 목록 |
| `POST` | `/rail/stations` | 등록. `mm` 생략 시 현재 위치(티치인) |
| `PATCH` | `/rail/stations/{name}` | 좌표·이름 수정 |
| `DELETE` | `/rail/stations/{name}` | 삭제 |
| `POST` | `/rail/stations/revalidate` | 재보정 후에도 좌표가 유효하다고 인정 |
| `POST` | `/rail/goto` | **에이전트용.** 이름으로 이동 `{"station": "시약존"}` |
| `GET` | `/rail/where` | 지금 어느 스테이션에 있나 |

### 상태 코드

| 코드 | 의미 |
| --- | --- |
| `400` | 소프트 리밋 위반 등 요청 자체가 잘못됨 |
| `409` | 레일이 이미 이동 중 |
| `422` | 입력값 검증 실패 (속도가 음수 등) |
| `503` | 하드웨어가 연결되지 않았거나 초기화 실패 |

에이전트가 실패 원인을 구분할 수 있도록 나눠 뒀다. `503` 은 하드웨어를 꽂으면
해결되고, `400` 은 명령을 바꿔야 해결된다.

## 브라우저에서 볼 때 (API 키를 걸었다면)

⚠️ `API_KEY` 를 설정했다면 브라우저에서는 `?api_key=...` 를 붙여야 한다.
`<img>` 태그에는 헤더를 넣을 방법이 없어서 쿼리스트링도 받도록 해 두었다.

```
http://pi.local:8000/thermal/ui?api_key=키
```

`/thermal/ui` 는 한 번 이렇게 열어두면 키를 브라우저에 저장해서 다음부터는
그냥 열어도 된다. **다만 쿼리스트링은 접근 로그와 브라우저 기록에 남는다.**
외부에 노출한 환경이라면 Tailscale 내부망에서 보는 편이 낫다.

## 레일 보정

리미트스위치가 없어서 원점을 기계적으로 찾을 수 없다. **사용자가 양 끝을 직접
지정**하고, 그 사이 거리를 유효 스트로크로 삼는다. 사용자가 요청할 때만 들어가는
별도 모드다 — 서버가 알아서 시작하지 않는다.

```bash
# 1. 캐리지를 시작 지점에 두고
curl -X POST http://lab-pi:8000/rail/calibration/begin

# 2. 반대쪽 끝까지 조금씩 (미세조정은 0.5mm 씩)
curl -X POST http://lab-pi:8000/rail/jog \
     -H 'Content-Type: application/json' -d '{"mm": 10, "speed_mm_s": 5}'

# 3. 그 자리를 끝으로 확정
curl -X POST http://lab-pi:8000/rail/calibration/end
```

`end` 응답에 실측 스트로크가 들어 있고, `rail/calibration.json` 에 저장된다.
서버를 껐다 켜도 유지된다.

보정 중에는 소프트 리밋이 없는 대신 한 번에 20mm 까지만 움직일 수 있고,
일반 이동(`/rail/move`, `/rail/move_to`)은 400 으로 거부된다.

### 재시작 후에는 위치를 다시 확인한다

스트로크는 그대로 신뢰하지만 **위치는 신뢰하지 않는다.** 전원이 꺼진 사이
캐리지가 손으로 밀렸을 수 있기 때문이다. 첫 이동 전에 둘 중 하나를 부른다.

```bash
curl -X POST http://lab-pi:8000/rail/resume                  # 안 건드렸음
curl -X POST http://lab-pi:8000/rail/set_position \
     -H 'Content-Type: application/json' -d '{"mm": 120}'    # 건드렸음
```

확인 전에 이동을 시도하면 `400` 이 난다. `/health` 의 `rail.position_known` 으로
지금 확인이 필요한 상태인지 알 수 있다.

자세한 내용은 [`rail/README.md`](../rail/README.md) 참고.

## 스테이션 — 레일 위 이름 붙은 지점

`37.5mm` 대신 `시약존`. **에이전트에게 mm 를 주면 LLM 이 숫자를 만들어낸다.**
이름을 닫힌 집합으로 두면 잘못된 값은 이동이 아니라 `404` 가 되고, 환각해도
물리적으로 아무 일이 없다. 장비를 옮겨 실제 위치가 바뀌어도 이 표만 고치면
에이전트 쪽 코드는 그대로다.

좌표는 **점**이다. `goto` 하면 캐리지가 정확히 그 자리에 선다.

### 등록은 티치인으로

자로 재서 mm 를 넣는 것보다, 캐리지를 그 자리로 옮긴 뒤 이름만 붙이는 편이
정확하다. `mm` 을 생략하면 지금 위치가 기록된다.

```bash
# 캐리지를 시약 놓는 자리로 옮긴 뒤
curl -X POST http://lab-pi:8000/rail/stations \
     -H 'Content-Type: application/json' -d '{"name": "시약존"}'
```

### 에이전트는 goto 만 쓴다

```bash
curl -X POST http://lab-pi:8000/rail/goto \
     -H 'Content-Type: application/json' -d '{"station": "시약존"}'
```

`/rail/move_to` 는 mm 를 직접 받으므로 **사람이 보정·디버깅할 때만** 쓴다.
에이전트에게 노출하는 도구 목록에서는 빼는 것이 좋다.

### ⚠️ 재보정하면 좌표가 막힌다

스테이션의 mm 는 보정으로 정한 원점 기준이다. 다시 보정하면 원점이 달라질 수
있어서 저장된 좌표가 엉뚱한 곳을 가리킬 수 있다.

그래서 어느 보정 기준인지 함께 저장해 두고, 달라지면 `stale` 로 표시하고
`goto` 를 `409` 로 거부한다. 둘 중 하나로 푼다.

- 스테이션을 **다시 등록**한다 (권장)
- 같은 자리에서 다시 보정한 경우처럼 실제로 안 바뀌었다면
  `POST /rail/stations/revalidate` 로 인정한다

번거롭지만, 틀린 자리로 로봇 팔이 가는 것보다 낫다.

저장 위치는 `rail/stations.json`. 보정값과 수명이 달라 파일을 나눴다.

## 웹 UI

브라우저에서 열면 보정과 스테이션 등록을 클릭으로 할 수 있다.

```
http://lab-pi:8000/rail/ui
```

- 현재 위치·스트로크·보정 여부를 한눈에
- 위치를 모르는 상태면 확인 패널이 먼저 뜬다
- 보정이 끝나면 **위치 슬라이더**가 나타난다. 끌어서 놓으면 그 자리로 이동
- **이동량(mm)과 속도(mm/s)를 직접 입력**하고 `앞으로`/`뒤로` 로 이동
- 이동 중에는 위치가 **속도로 추정되어 실시간으로 표시된다.** 서버는 이동이
  끝나야 응답하므로 그동안 실측값을 물어볼 수 없다. 추정 중에는 `≈` 를 붙여
  실측과 구분하고, 응답이 오면 실제 값으로 덮인다
- 자주 쓰는 값은 빠른 이동 버튼(±0.5 ~ ±10mm)으로
- 속도는 브라우저가 기억한다. 입력 상한은 서버에서 받아 오므로
  `MAX_SPEED_MM_S` 를 고치면 UI 도 따라온다
- 캐리지를 옮긴 뒤 이름을 넣고 **여기를 등록**
- 재보정으로 좌표가 막히면 경고와 함께 인정 버튼이 나온다

API 키를 걸었다면 `?api_key=...` 를 붙여 한 번 열면 이후 브라우저가 기억한다.

## 열화상 카메라 (ThermoEye TMC160F)

USB로 파이에 꽂기만 하면 커널 `uvcvideo` 가 잡아 `/dev/video0` 로 뜬다. 별도
드라이버가 필요 없다.

### 이 장치의 사실들

문서 없이 알아낸 것이라 적어 둔다. 다시 조사하지 않도록.

| 항목 | 값 |
| --- | --- |
| USB ID | `28e9:160b` (`Thermoeye TMC160F`) |
| 구성 | UVC(비디오) + CDC-ACM(시리얼) 복합 장치 |
| 포맷 | `Y16` — 16비트 그레이스케일 **단 하나뿐** |
| 해상도 | 160×120 고정 (한 프레임 38400바이트) |
| 프레임률 | **9fps 고정** (인터벌 1,111,111 × 100ns) |
| 전송 | full-speed 아이소크로너스, 엔드포인트 `0x81` |
| **픽셀 단위** | **센티켈빈.** `℃ = 값 / 100 - 273.15` |

실온에서 29800 언저리(=25℃)가 나온다. 값이 30000을 넘으면 뜨겁다는 뜻이 아니라
그냥 27℃ 라는 뜻이니 헷갈리지 말 것.

CDC-ACM 쪽(`/dev/ttyACM*`)은 제어용 채널로 보이는데, 아무것도 보내지 않으면
조용하고 명령 규격을 모른다. 영상만 쓰는 지금은 건드릴 필요가 없다.

### ⚠️ 켜자마자의 10프레임은 버려야 한다

전원이 들어간 직후 약 10프레임(~1초)은 측정값이 아니라 **고정 패턴**(값 0~242)
이고, 그 다음 한 장은 0~65535 로 크게 튄다. 그 이후부터 정상이다.

이걸 모르면 "프레임은 오는데 값이 안 변한다"로 한참 헤맨다. `thermal.py` 는
켈빈 범위(20000~45000)를 벗어난 프레임을 버리는 것으로 걸러낸다.

### ⚠️ 장치는 한 번에 한 프로세스만

`/dev/video0` 은 스트리밍 중이면 다른 프로세스가 열 수 없다. 두 번째는
`OSError: [Errno 16] Device or resource busy` 로 막힌다. 시리얼 포트와 같은
상황이다.

서비스가 떠 있는 상태에서 테스트 스크립트를 돌리면 스크립트가 실패하고,
반대로 테스트 스크립트가 물고 있으면 **서비스 쪽 열화상만 조용히 죽는다**
(서버는 정상으로 뜨고 `/health` 의 `thermal.error` 에만 나타난다). 값이 안
들어오면 여기부터 확인할 것.

```bash
sudo systemctl stop pi-sensor-server     # 테스트 전에 내리고
# ... 스크립트 실행 ...
sudo systemctl start pi-sensor-server    # 끝나면 올린다
```

무엇이 물고 있는지는 이렇게 찾는다.

```bash
sudo fuser -v /dev/video0
```

### ⚠️ 맥에서는 안 된다

맥에 꽂으면 macOS UVC 드라이버가 장치를 물기는 하는데 **Y16 을 AVFoundation 에
노출하지 않는다.** 그래서 `ffmpeg -f avfoundation -list_devices` 목록에도,
OpenCV 에도 아예 안 뜬다. `libuvc` 로 우회해도 `uvc_open` 이 접근 거부로 막힌다.

생 libusb 로 비디오 인터페이스를 claim 하는 것까지는 되므로 UVC 스트리밍
프로토콜을 직접 구현하면 이론상 가능하지만, 파이에서는 한 줄도 쓰이지 않는
코드다. **열화상은 파이에 꽂는다**로 정리했다.

UI 는 웹이라서 이게 불편하지 않다. 파이가 서빙하고 맥 브라우저로 열면 클릭은
그대로 맥에서 한다.

### ⚠️ 화면 위치별 절대 온도는 믿지 말 것

이 센서는 프레임 전체에 고정 패턴이 있다. 왼쪽 가장자리가 눈에 띄게 밝고
중앙은 비네팅으로 낮게 나온다. 균일한 벽을 찍어도 2℃ 가까이 퍼진다.

- **하면 안 되는 것** — 화면 왼쪽 영역과 오른쪽 영역의 온도를 서로 비교
- **해도 되는 것** — 고정된 ROI 하나를 시간에 따라 추적. 고정 패턴은 상수라
  변화량에서 상쇄된다

이 기능의 목적(반응 진행에 따른 온도 변화 추적)에는 후자면 충분하다. 절대값이
필요하면 온도를 아는 기준물을 화면에 같이 넣고 그 ROI 와의 차이를 쓰는 게 낫다.

### bbox 지정하기

브라우저에서 연다. 맥에서 열어도 된다.

```
http://pi.local:8000/thermal/ui
```

영상 위에서 **드래그하면 그 영역이 추적 대상으로 등록**되고, 곧바로 시계열이
쌓이기 시작한다. 등록된 영역은 영상에도 사각형으로 그려지고, 오른쪽 표에 현재
평균/최저/최고 온도가, 아래 그래프에 평균 온도 추이가 나온다.

- 이름을 눌러 고칠 수 있다 (시료 이름 등)
- `CSV` 버튼으로 시계열을 내려받는다
- `시계열만 초기화` 는 좌표를 그대로 두고 데이터만 비운다. 실험을 다시 시작할 때
- 컬러맵과 온도 범위는 **보기용일 뿐** 기록되는 값에는 영향을 주지 않는다.
  범위를 고정하면 색이 흔들리지 않아 변화를 눈으로 쫓기 좋다

좌표는 화면에 몇 배로 확대해 보여주든 **항상 센서 픽셀(160×120) 기준**으로
주고받는다. 창 크기를 바꿔도 등록된 영역은 그대로다.

API 로 직접 등록해도 된다. 에이전트가 쓸 때는 이쪽이다.

```bash
curl -X POST http://pi.local:8000/thermal/roi \
  -H 'Content-Type: application/json' \
  -d '{"x":60,"y":45,"w":40,"h":30,"name":"비커"}'

curl 'http://pi.local:8000/thermal/roi/<id>?limit=60'
```

### 시계열은 1초 간격으로 쌓인다

센서는 9fps 지만 그대로 다 쌓으면 금방 넘치고, 화학 반응 추적에 9Hz 해상도가
필요하지도 않다. 기본은 1초 간격이고 ROI 하나당 24시간치(86400개)를 들고 있다.
넘치면 오래된 것부터 버린다.

표에 보이는 현재값은 샘플 간격과 무관하게 **매 프레임 갱신**된다. 시계열에만
간격이 적용된다.

더 촘촘하게 남기려면 `THERMAL_SAMPLE_INTERVAL_S` 를 줄인다. 다만 **메모리에만**
있으므로 서버를 재시작하면 사라진다. 오래 걸리는 실험이라면 CSV 로 주기적으로
내려받아 둘 것.

## 하드웨어가 없어도 뜬다

센서, 열화상, 레일 중 무엇이 빠져 있어도 서버는 정상적으로 시작한다. 없는 것은
`/health` 에 이유와 함께 보고되고, 해당 엔드포인트만 `503` 을 돌려준다.

하나씩 붙여가며 확인할 수 있도록 한 것이다. USB를 나중에 꽂아도 센서 리더가
백그라운드에서 재연결을 시도하므로 서버를 다시 띄울 필요가 없다.

## 센서 값은 캐시된다

아두이노는 1초 주기로 값을 보낸다. 요청마다 시리얼을 읽으면 최대 1초를 기다려야
하고, 시리얼 포트는 하나만 열 수 있어 동시 요청도 처리하지 못한다.

그래서 백그라운드 스레드가 계속 읽어 최신값을 들고 있고, `/sensors` 는 그 값을
즉시 돌려준다. 대신 값이 얼마나 오래됐는지 함께 알려준다.

```json
{ "ph": 7.02, "ec": 612.4, "age_s": 0.4, "stale": false }
```

`stale: true` 면 아두이노와의 연결이 끊겼다는 뜻이다. 에이전트는 이 값을 보고
오래된 측정값으로 판단하지 않도록 해야 한다.

## 외부 노출

파이는 공유기 NAT 뒤에 있어서 클라우드에서 직접 접근할 수 없다. 포트포워딩은
학교/실험실 네트워크에서 대개 불가능하고, 클라이언트 격리가 켜진 망에서는 같은
네트워크 안에서도 막힌다.

파이가 **바깥으로 나가는 연결만** 쓰는 방식이 안전하다.

- **Tailscale** — 설치가 가장 간단하고 NAT/방화벽과 무관하다
- **Cloudflare Tunnel** — HTTPS 주소를 받는다
- **작업 큐 폴링** — 파이가 주기적으로 할 일을 물어본다. 가장 방화벽 친화적

### ⚠️ 노출 전에 API 키를 설정할 것

```bash
export API_KEY='충분히 긴 무작위 문자열'
.venv/bin/python -m server.run
```

설정하면 모든 요청에 `X-API-Key` 헤더가 필요해진다. 설정하지 않으면 인증 없이
누구나 레일을 움직일 수 있다. **로컬 테스트 중에만 생략할 것.**

## 안전

에이전트를 안전장치로 믿으면 안 된다. 프롬프트는 지켜질 수도, 지켜지지 않을 수도
있다. 한계는 이 서버의 코드에 강제되어야 한다.

현재 적용된 것:

- 레일은 `STROKE_MM` 를 벗어나는 이동을 거부한다 (`rail/stage.py` 의 소프트 리밋)
- 서버를 띄운 직후에는 캐리지 위치를 모르는 상태이므로 이동이 거부된다.
  `/rail/resume` 또는 `/rail/set_position` 으로 확인해 줘야 움직인다
- 이동이 중단돼 위치를 잃으면, 다시 알려주기 전까지 이동을 거부한다
- 동시에 두 개의 이동 명령이 실행되지 않는다 (`409`)
- 속도는 스키마에서 상한이 걸린다

앞으로 필요한 것:

- 물리 비상정지 버튼 (소프트웨어 밖에 있어야 한다)
- 되돌릴 수 없는 동작(시약 주입, 가열)은 별도 확인 단계
- 명령 로그 — 무엇을 왜 했는지 추적할 수 있어야 한다

## 설정 (환경변수)

| 변수 | 기본값 | 설명 |
| --- | --- | --- |
| `SERIAL_PORT` | 자동 탐색 | 아두이노 포트 |
| `SERIAL_BAUD` | `9600` | 스케치와 일치해야 함 |
| `SENSOR_STALE_AFTER_S` | `5.0` | 이보다 오래되면 `stale` |
| `THERMAL_DEVICE` | `/dev/video0` | 열화상 카메라 노드 |
| `THERMAL_SAMPLE_INTERVAL_S` | `1.0` | ROI 시계열 샘플 간격(초) |
| `THERMAL_SERIES_MAX` | `86400` | ROI 하나당 최대 샘플 수 |
| `API_KEY` | (없음) | 설정 시 인증 필수 |

⚠️ 이 파이에는 다른 카메라가 없어서 `/dev/video0` 이 보통 그대로 맞는다. 다만
V4L2 장치를 하나라도 더 꽂으면 번호가 밀리므로 그때는 `THERMAL_DEVICE` 를 명시해야
한다. 어느 쪽이 어느 번호인지는 이렇게 확인한다.

```bash
for d in /dev/video*; do echo "$d: $(cat /sys/class/video4linux/$(basename $d)/name)"; done
```

열화상은 `TMC160F` 로 나온다. 단, **한 장치가 노드를 두 개 만든다** — 둘 다
`TMC160F` 로 보이지만 뒤쪽(`video1`)은 메타데이터 노드라 영상이 나오지 않는다.
**번호가 작은 쪽**을 쓴다.
