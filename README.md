# PiSensorServer — 중앙 서버 (노트북/데스크톱)

> **이 저장소의 중앙 서버(`central/`, `start_server.bat`)는 라즈베리파이가 아니라 노트북/데스크톱 PC 에서 돌린다.**
> 이름에 "Pi" 가 붙어 있지만 파이에 올리는 서버가 아니다.
> 로봇팔 쪽 파이(파이1) 서버는 [PiRobotControl](https://github.com/armchemist/PiRobotControl) 저장소다.
>
> 파이2(센서·레일) 쪽 코드(`server/`, `sensors/`, `rail/`)는 역사적 이유로 아직 이 저장소에 같이 있다.
> 파이2에서 돌리는 것은 그쪽(`python -m server.run`)뿐이고, `central/` 은 파이에서 실행하지 않는다.

열화상 카메라, 수질 센서(pH/전도도), 리니어 스테이지(레일)는 파이2가 맡고(`server/`), 파이2는 중앙 서버에 보고한다.

## 중앙 서버 (노트북/데스크톱) — POC

이제 데스크톱 PC 의 중앙 서버(`central/`, 포트 8100)가 중심이다. 파이2는 1초마다 센서·레일
상태를 중앙에 보고하고, 레일 명령을 폴링해 실행한다. 스마트폰(휴머노이드 머리)은 WebSocket 으로 붙는다.
통신 규약 전체는 [`docs/PROTOCOL.md`](docs/PROTOCOL.md). 아래 "두 개의 파이" 절의
"클라우드 에이전트가 양쪽을 직접 호출" 구조는 이것으로 대체된다.

```
        데스크톱 중앙 서버 :8100  (central/, Windows 가능, DB 없음 — data/*.json[l])
          ▲        ▲          ▲
   보고·폴링 │   정지 │ WS        │ REST
          │        ▼          │
     파이2 :8000 (server/)   스마트폰 앱      운영자 / 향후 웹
```

### 실행 (리눅스 노트북)

```bash
./run_central.sh        # 처음엔 .venv 를 만든다. 대시보드: http://<노트북IP>:8100/ui
```

### 실행 (데스크톱, Windows)

```powershell
cd PiSensorServer
py -3.12 -m venv .venv            # 또는 uv venv -p 3.11 .venv
.venv\Scripts\python -m pip install -r requirements-central.txt
.venv\Scripts\python -m uvicorn central.main:app --host 0.0.0.0 --port 8100
```

**워커는 하나만** (`--workers` 를 주지 말 것). 명령 상태가 프로세스 메모리 + 파일 Lock 에 있다.
`http://localhost:8100/docs` 의 Swagger UI 로 모든 API 를 눌러볼 수 있다. Windows 에서는 이게 제일 편하다
(PowerShell 5.1 은 한글 응답이 깨져 보이고, git-bash 의 `curl -d '한글'` 은 본문이 깨진다).

| 환경변수 (중앙) | 기본값 | |
| --- | --- | --- |
| `PI_URL` | 마지막 보고 IP:8000 | `/rail/stop` 을 전달할 파이 주소. 예: `http://100.64.0.20:8000` |
| `ARM_SAFE_TTL_S` | `30` | 팔 안전 확인 유효 시간(초) |
| `DATA_DIR` | `./data` | 센서 이력·명령 기록 |

PowerShell 에서 환경변수: `$env:PI_URL = "..."` 후 같은 창에서 실행.

**인증은 없다** (POC, Tailscale·LAN 안에서만 쓴다). 인터넷에 열지 말 것.

### 네트워크: LAN IP / Tailscale / 방화벽

```powershell
ipconfig                      # LAN IPv4 (예: 192.168.0.10)
tailscale ip -4               # Tailscale IP (100.x.y.z)
```

- Windows 방화벽은 처음 실행 시 python 허용 창을 띄운다. **개인 네트워크** 를 허용할 것. 놓쳤다면 관리자 PowerShell:
  `New-NetFirewallRule -DisplayName "ARMchemist 8100" -Direction Inbound -Protocol TCP -LocalPort 8100 -Action Allow -Profile Private`
  (네트워크가 '공용'으로 잡혀 있으면 `-Profile Any` 이거나 네트워크를 개인으로 바꾼다.)
- 확인은 **다른 장치에서** 한다: 파이에서 `curl http://<데스크톱IP>:8100/health`,
  폰 브라우저에서 같은 주소. 데스크톱 자신에서 되는 것은 방화벽 확인이 아니다.
- Tailscale: 양쪽 다 `tailscale status` 에 보이는지, `tailscale ping <상대>` 가 되는지. 그 다음 100.x 주소로 `/health`.
- 데스크톱 IP 가 바뀌면 파이의 `CENTRAL_URL` 도 바꿔야 한다. Tailscale IP 는 고정이라 그쪽이 편하다.

### 파이2 연결

파이의 `.env` (systemd 유닛이 읽는다)에 추가하고 서비스를 재시작한다.

```bash
CENTRAL_URL=http://100.64.0.10:8100     # 데스크톱의 Tailscale 또는 LAN IP
sudo systemctl restart pi-sensor-server
journalctl -u pi-sensor-server -f       # "[uplink] ... 보고 시작" 이 보이면 연결
```

`CENTRAL_URL` 이 없으면 기존처럼 단독으로 돈다. 보고·폴링은 기존 서버 프로세스 안의 스레드라 시리얼·GPIO 를 따로 열지 않는다.

### 레일 이동 절차 (POC)

1. 레일 보정·스테이션 등록은 기존대로 파이의 `/rail/ui` 에서. 재시작 후엔 `/rail/resume` 또는 `/rail/set_position`.
2. **로봇팔이 레일 이동에 안전한 자세인지 눈으로 확인**하고 `POST /arm/state {"safe": true}`. 30초, 이동 1회만 유효.
3. 30초 안에 `POST /rail/command {"station": "실험존", "speed_mm_s": 5}`.
4. `GET /status` 의 `rail_commands.pi2.status` 가 `DONE` 이 될 때까지 본다.
5. 멈추려면 `POST /rail/stop`. 결과가 `UNKNOWN` 이면 레일을 눈으로 보고 PROTOCOL.md 3.2 절대로 해제.

> ⚠️ **물리 비상정지 스위치(모터 전원 차단)를 반드시 따로 둘 것.** `/rail/stop` 은 네트워크·서버·파이가 모두
> 살아 있을 때만 동작하는 소프트웨어 정지다.

### Mock 으로 확인 (하드웨어 없이)

```bash
# 리눅스/파이/맥. Windows 는 한글 본문을 파일로: curl --data-binary @body.json
S=http://<데스크톱IP>:8100
curl -X POST $S/devices/pi2/report -H 'Content-Type: application/json' \
  -d '{"ts":1760500000,"sensors":{"status":"ok","ph":7.02,"conductivity":612.4,"age_s":0.3},"rail":{"available":false,"moving":false}}'
curl $S/status
curl -X POST $S/arm/state -H 'Content-Type: application/json' -d '{"safe":true}'
curl -X POST $S/rail/command -H 'Content-Type: application/json' -d '{"station":"실험존","speed_mm_s":5}'
curl "$S/rail/command?device_id=pi2"                       # 파이 흉내: PENDING 조회
curl -X POST $S/rail/command/<id>/result -H 'Content-Type: application/json' -d '{"status":"RUNNING"}'
curl -X POST $S/rail/command/<id>/result -H 'Content-Type: application/json' -d '{"status":"DONE"}'
```

스마트폰 앱 대신 `tools/fake_phone.py` 로 WebSocket 을 확인한다.

```powershell
.venv\Scripts\python tools\fake_phone.py ws://127.0.0.1:8100 --jpeg 아무사진.jpg
# 다른 창에서 (또는 Swagger UI 에서)
# PowerShell 5.1 은 따옴표를 벗기므로 작은따옴표 안에 \" 를 쓴다
curl.exe -X POST http://127.0.0.1:8100/phone/phone1/expression -H "Content-Type: application/json" -d '{\"value\":\"danger\"}'
curl.exe -X POST http://127.0.0.1:8100/phone/phone1/track -H "Content-Type: application/json" -d '{\"action\":\"start\",\"target\":\"face\"}'
curl.exe -o frame.jpg http://127.0.0.1:8100/phone/phone1/frame
```

fake_phone 창에 `받음: {'type': 'expression', ...}` 이 찍히고, track start 후 `/status` 의 `phones.phone1.last_track` 이 채워진다.

### Windows 더블클릭 실행

| 파일 | 하는 일 |
| --- | --- |
| `start_server.bat` | 이미 떠 있으면 대시보드만 열기 → `.venv` 없으면 생성 → 패키지 없으면 `requirements-central.txt` 설치 → `uvicorn central.main:app` 을 숨은 창으로 실행(PID 는 `data/central.pid`, 로그 `logs/central.log`) → `/health` 응답 대기(최대 30초) → 브라우저로 `/ui` 열기. 폰에 넣을 PC IP 목록도 찍는다 |
| `check_server.bat` | 서버 프로세스, `/health`, `/status` 요약(pi2·phone1·verify), 로그 마지막 10줄 |
| `stop_server.bat` | **이 저장소의 `.venv` 로 뜬 `uvicorn central.main:app` 만** 종료. 다른 Python 은 건드리지 않는다 |

세 파일 모두 `tools/central_ctl.ps1` 과 함께 저장소 안에 있어야 한다. 서버는 콘솔 창 없이 돌므로 로그는 `logs/central.log` 또는 `check_server.bat` 로 본다.

### 테스트

```powershell
.venv\Scripts\python -m pip install pytest httpx
.venv\Scripts\python -m pytest -q tests
```

상태 전이, 중복 실행 방지, 재시작 복구, 응답 유실·결과 재전송, 폰 WS·JPEG 를 검증한다. 파이 쪽 `server/uplink.py` 는
가짜 레일로 실제 uvicorn 중앙 서버와 HTTP 로 통신시켜 검증한다. **실제 GPIO·모터·아두이노 동작은 검증 범위 밖이다.**

## 두 개의 파이

실험대는 파이 두 대로 나뉜다. 기준은 *센서/구동* 이 아니라 **움직이는 것 /
고정된 것** 이다.

| | 파이1 ([PiRobotControl](https://github.com/armchemist/PiRobotControl)) | 파이2 (이 저장소) |
| --- | --- | --- |
| 위치 | 레일 위 — 같이 움직인다 | **고정** |
| 하드웨어 | 로봇 팔 USB×2, 핸디캠 USB×2 | 열화상 USB, 우노(pH·전도도), **레일 구동** |
| 저장소 | `PiRobotControl` | `PiSensorServer` |

### ⚠️ 레일이 왜 여기 있나

레일 **위에 타는** 것은 파이1인데 레일을 **미는** 것은 파이2다. 헷갈리기 쉬운
부분이라 적어 둔다.

스텝모터 드라이버는 GPIO로 직결돼야 하는데, 움직이는 파이에서 고정된 드라이버로
배선을 끌면 이동할 때마다 굽혀진다. 그래서 구동 코드가 고정된 쪽에 있다.

파이1이 레일 위에 올라간 이유도 같은 종류의 문제다. 팔과 캠으로 가는 USB
케이블이 레일 이동 중에 늘어졌다 당겨지지 않게 하려는 것.

그래서 이 저장소 이름은 `PiSensorServer` 지만 **센서만 있는 게 아니다.**
액추에이터(레일)가 하나 같이 있다.

### 서로를 모른다

두 파이는 서로 통신하지 않는다. 클라우드 에이전트가 양쪽 주소를 각각 호출한다.

```
        클라우드 (LLM 에이전트)
          │              │
     HTTP │              │ HTTP
  ┌───────┴──────┐  ┌────┴─────────┐
  │ 파이1 (동적)  │  │ 파이2 (정적)  │
  │ 레일 위에 탑승 │  │  ← 이 저장소  │
  └──┬────────┬──┘  └─┬───┬────┬───┘
     │        │       │   │    │
    USB×2   USB×2   GPIO USB  USB
    로봇팔   핸디캠   레일  열화상 우노
                                │
                          pH / 전도도
```

⚠️ **팔이 뻗은 상태에서 레일이 움직이면 충돌한다.** 두 파이가 서로의 상태를
모르므로 동시 동작 방지가 어느 쪽에도 구현돼 있지 않다. 지금은 에이전트가 순서를
지켜야 하는데, 그건 안전장치가 아니다.

## 구조

| 디렉토리 | 분류 | 내용 |
| --- | --- | --- |
| [`sensors/`](sensors/) | 측정 | pH(SEN0161), 전도도(SEN0244) — 아두이노 우노 경유 |
| [`rail/`](rail/) | 구동 | 리니어 스테이지 (스텝모터 + 볼스크류) |
| [`server/`](server/) | 통합 | 위 하드웨어 + 열화상을 HTTP API로 노출 |

열화상 카메라는 별도 디렉토리 없이 [`server/hardware/thermal.py`](server/hardware/thermal.py)
에 있다. 커널이 UVC로 잡아주므로 파이 쪽에 따로 둘 코드가 없기 때문이다.
사용법과 장치 규격은 [`server/README.md`](server/README.md) 의 "열화상 카메라" 절에 있다.

각 디렉토리의 `README.md` 에 배선과 사용법이 있다.

최종 목표는 클라우드의 멀티모달 에이전트가 화학 실험을 수행하는 것이다. 에이전트는
USB에 직접 접근할 수 없으므로, 실험대 쪽의 라즈베리파이가 하드웨어를 물고
HTTP API로 노출한다. 엔드포인트는 에이전트의 도구와 1:1로 대응한다.

## 하드웨어 구성 (파이2)

```
                    ┌─────────────────┐
   PC (맥) ──SSH────┤ 라즈베리파이 4B  │
                    │  Ubuntu 24.04   │
                    └──┬───┬───┬──────┘
                       │   │   │
             GPIO ─────┘   │   └───── USB
        (스텝모터 드라이버)  │      (열화상 TMC160F)
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

열화상은 아날로그가 아니라 USB 비디오(UVC)라 우노를 거치지 않고 직결한다.

## 라즈베리파이 접속

`avahi-daemon`(mDNS)을 설치해 둬서 IP가 바뀌어도 이름으로 붙는다.

```bash
ssh pi@pi.local
```

맥의 `~/.ssh/config` 에 별칭이 있으면 `ssh pi` 만으로도 된다.

WiFi는 SD카드의 `network-config` 에 여러 개를 등록해 둬서, 부팅 시 잡히는
네트워크에 자동으로 붙는다. 장소를 옮겨도 카드를 다시 꽂을 필요가 없다.

⚠️ 파이 두 대를 같이 쓰면 **mDNS 이름이 각각 필요하다.** 둘 다 `pi.local` 이면
충돌한다. `pi1.local` / `pi2.local` 처럼 hostname 을 나눠 둘 것.

## 파이2 서버 실행 (라즈베리파이에서만)

```bash
cd ~/PiSensorServer
.venv/bin/python -m server.run
```

설치와 운용은 [`server/README.md`](server/README.md) 참고.
중앙 서버(`central/`)는 여기서가 아니라 노트북/데스크톱에서 `start_server.bat`(Windows) 또는 `./run_central.sh`(리눅스)로 띄운다.
