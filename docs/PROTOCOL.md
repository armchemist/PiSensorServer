# ARMchemist 통신 규약 (POC)

중앙 서버(데스크톱, `:8100`)를 중심으로 파이2(센서·레일), 스마트폰, 향후 모듈이 붙는다.
구현: `central/main.py`(API), `central/state.py`(레일 명령 규칙), `server/uplink.py`(파이2 쪽).

```
 파이2 (PiSensorServer :8000) ──HTTP 1초 보고 / 1초 명령 폴링──▶ 중앙 서버 :8100
 중앙 서버 ──HTTP POST /rail/stop──▶ 파이2 :8000            (정지만 서버→파이 직접 호출)
 스마트폰 ──WS /ws/phone (JSON) + WS /ws/phone/video (JPEG)──▶ 중앙 서버
 운영자/향후 웹 ──REST──▶ 중앙 서버
```

- 모든 시각은 epoch 초(float). 문자열 인코딩은 UTF-8.
- 인증: 중앙 서버에 `API_KEY` 를 설정하면 `X-API-Key` 헤더 또는 `?api_key=` 필요 (`/health` 제외).
  WebSocket 은 `?api_key=` 를 쓴다. 키가 틀리면 1008 로 닫힌다.
- 오류 응답은 FastAPI 기본형 `{"detail": "..."}`.

## 1. 연결 확인 / 전체 상태

| | |
| --- | --- |
| `GET /health` | `{"ok": true, "ts": ...}` — 키 없이 됨 |
| `GET /status` | 장치별 최신 보고(`online` = 5초 이내 보고), 레일 명령, 팔 안전 상태, 폰 연결·추적·영상 요약 |

웹 대시보드는 아직 없다. 웹을 만들 때는 우선 `GET /status` 를 1초 폴링하면 된다.

## 2. 파이 → 중앙: 상태 보고

`POST /devices/{device_id}/report` (파이2 기본 `device_id=pi2`, 1초 간격)

```json
{
  "ts": 1760500000.0,
  "sensors": {
    "status": "ok", "ph": 7.02, "conductivity": 612.4, "tds": 306.2,
    "temp_c": 25.0, "age_s": 0.3, "error": null
  },
  "rail": {
    "available": true, "moving": false, "position_known": true, "position_mm": 400.0,
    "calibrated": true, "calibrating": false,
    "station": "실험존", "stations": ["시약존", "실린더존", "실험존"],
    "stations_stale": false, "error": null
  }
}
```

센서 필드 (아두이노 스케치 `sensors/arduino_sensors` 출력에서 옮김):

| 필드 | 원본 | 단위 | 비고 |
| --- | --- | --- | --- |
| `ph` | `ph` | pH | 임시 1점 보정 상태 (스케치 주석 참고) |
| `conductivity` | `ec` | µS/cm | SEN0244, 25℃ 보정식 |
| `tds` | `tds` | ppm | `ec × 0.5` |
| `temp_c` | `temp` | ℃ | **실측 아님**, 스케치에 박힌 가정 수온 |

`sensors.status`: `ok` / `stale`(마지막 값이 5초 이상 됨) / `no_data`(아직 한 번도 못 읽음) / `error`(포트 없음 등, `error` 에 사유).
값이 없으면 `ph` 등 필드가 빠진다.

`rail.available=false` 면 stage 로드 실패(lgpio 없음 등), `error` 에 사유. 이때 나머지 레일 필드는 없다.
`station` 은 등록 스테이션 ±0.5mm 안에 있고 이동 중이 아닐 때만 값이 있다.

중앙 서버는 최신 보고를 메모리에, 센서 부분을 `data/sensors-YYYY-MM-DD.jsonl` 에 남긴다.

## 3. 레일 명령

자동 이동 대상은 `시약존`, `실린더존`, `실험존` 세 개뿐. 속도 0 < `speed_mm_s` ≤ 30.

### 3.1 API

| | 호출자 | |
| --- | --- | --- |
| `POST /rail/command` | 운영자·향후 실행기 | `{"station": "실험존", "speed_mm_s": 5.0, "device_id": "pi2", "id": null}` → 201 명령 |
| `GET /rail/command?device_id=pi2` | 파이 | `{"command": {...}}` — **PENDING 만** 준다. 없으면 `null` |
| `POST /rail/command/{id}/result` | 파이 (예외: UNKNOWN 해제는 운영자) | `{"status": "RUNNING"\|"DONE"\|"FAILED"\|"ABORTED"\|"UNKNOWN", "error": null, "result": {...}}` |
| `POST /rail/stop?device_id=pi2` | 운영자 | PENDING 명령 취소 + 파이 `POST /rail/stop` 호출 |
| `POST /arm/state` | 운영자 (향후 PiRobotControl) | 4절 |

명령 객체:

```json
{"id": "5001b2dd...", "device_id": "pi2", "station": "실험존", "speed_mm_s": 5.0,
 "status": "PENDING", "created_at": 1760500000.0, "updated_at": 1760500000.0,
 "error": null, "result": null}
```

`POST /rail/command` 거절: 400 (스테이션·속도), 409 (활성 명령 있음 / UNKNOWN 미해결 / 팔 안전 미확인).
같은 `id` 로 다시 보내면 새로 만들지 않고 기존 명령을 돌려준다(호출자 재시도용).

### 3.2 상태 전이

```
PENDING ─RUNNING(파이의 실행 승인 요청)─▶ RUNNING ─▶ DONE | FAILED | ABORTED
   │                                       │
   └─▶ FAILED | ABORTED                    └─(중앙 재시작 / 파이가 이동 중 재시작)─▶ UNKNOWN
                                                                                       │
                                               DONE | FAILED | ABORTED ◀─(늦은 결과 / 운영자)─┘
```

- 장치당 명령은 하나만 보관한다. PENDING/RUNNING/UNKNOWN 이 있으면 새 명령 409.
- 같은 상태를 다시 보고하면 200, 아무것도 안 바뀐다(응답 유실 후 재시도용).
- 허용되지 않은 전이는 409. 특히 **UNKNOWN → RUNNING 은 불가** (자동 재실행 없음).
- 중앙 서버 기동 시: RUNNING → UNKNOWN, PENDING → ABORTED (재시작 전 요청이 뒤늦게 실행되지 않게).
- 전이는 전부 `data/commands.jsonl` 에, 현재 명령은 `data/rail_command.json`(임시 파일 교체로 원자적 저장)에 남는다.
- **UNKNOWN 해제**: 운영자가 레일을 눈으로 확인한 뒤 `POST /rail/command/{id}/result {"status": "FAILED", "error": "운영자 확인"}`.
  파이가 늦게라도 결과를 보내면 그것으로도 풀린다.

### 3.3 파이 쪽 규칙 (`server/uplink.py`)

1. GET 으로 PENDING 을 받으면 진행 단계 `claiming` 을 `rail/uplink_command.json` 에 쓰고 `RUNNING` 을 보고한다.
2. **200 을 받은 뒤에만** `running` 을 쓰고 이동한다 (기존 `/rail/goto` 와 같은 경로: `stations.target_mm` → `rail.move_to`).
   - 4xx(팔 확인 만료, UNKNOWN 등) → 실행하지 않고 버린다.
   - 네트워크 오류 → 다음 틱에 같은 명령으로 `RUNNING` 재요청(서버는 멱등 200). 10초 안에 승인 못 받으면 FAILED 로 포기.
3. 이동은 명령 스레드에서 블로킹으로 돈다 → 이동 중엔 새 명령을 폴링하지 않는다. 보고 스레드는 계속 돈다.
4. 끝나면 `result` 단계로 쓰고 결과를 보고한다. 실패하면 **결과 보고만** 재시도하고, 그동안 새 명령을 받지 않는다.
   404/409 로 거절되면 버린다.
5. 파이 재시작 시 `claiming` → FAILED("실행하지 않음"), `running` → UNKNOWN 으로 보고한다. 다시 움직이지 않는다.
   stage 는 재시작 후 위치를 '모름'으로 시작하므로 운영자가 `/rail/resume` 또는 `/rail/set_position` 하기 전엔 이동이 거부된다(기존 동작).

결과 매핑: 성공 DONE(`result: {"station", "position_mm"}`), 스테이션 미등록·stale·RailBusy·소프트 리밋 → FAILED,
`MoveAborted`(정지) → ABORTED, 그 밖의 예외 → FAILED. 오류 문자열은 `error` 에.

### 3.4 정지

`POST /rail/stop` → 중앙이 PENDING 명령을 ABORTED 로 바꾸고, 파이 `POST {PI_URL}/rail/stop` 을 스레드에서 호출(3초 타임아웃).
`PI_URL` 이 없으면 마지막 보고가 온 IP 의 8000 포트. 응답: `{"cancelled": id|null, "pi": {"ok", "status", "body"}}`.
움직이던 이동은 파이에서 `MoveAborted` → ABORTED 로 보고되고, 위치는 '모름'이 된다.

**소프트웨어 정지는 네트워크가 살아 있어야 동작한다. 물리 비상정지 스위치(모터 전원 차단)를 따로 둘 것.**

## 4. 로봇팔 안전 (레일 인터락)

레일 위에 로봇팔이 타므로, 팔이 안전 자세라는 확인 없이 레일을 움직이지 않는다.

`POST /arm/state`

```json
{"safe": true, "source": "operator", "device_id": "pi1", "pose": null, "note": "팔 접힘 확인"}
```

- 확인은 **`ARM_SAFE_TTL_S`(기본 30초) 동안만** 유효하고, **명령 하나가 RUNNING 될 때 소비된다.**
- 명령 생성 시와 파이의 RUNNING 요청 시 두 번 검사한다. 두 번째에서 실패하면 명령은 FAILED.
- POC 절차: 운영자가 팔을 눈으로 확인 → `/arm/state` → 30초 안에 `/rail/command`.

향후 PiRobotControl 자동 보고(미구현, 같은 엔드포인트): `{"safe": <자세가 레일 이동 안전 자세인가>, "source": "robot",
"device_id": "pi1", "pose": "home"}` 를 1초 이하 간격으로 보낸다. TTL·소비 규칙은 같다 — 보고가 끊기면 자동으로 이동 불가.
`safe` 판정(관절각 허용 범위 등)은 PiRobotControl 쪽 책임이다.

## 5. 스마트폰

### 5.1 `WS /ws/phone` — 상태·명령 (JSON 텍스트)

연결 후 10초 안에 첫 메시지로 hello. 아니면 `{"type":"error"}` 후 1008 로 닫힌다.

폰 → 서버:

```json
{"type": "hello", "device_id": "phone1", "capabilities": ["camera", "tracking", "face"]}
{"type": "status", "camera": "streaming", "tracking": false}
{"type": "track_result", "target": "face", "found": true, "tracking_id": 3, "bbox": [0.2, 0.3, 0.4, 0.4], "ts": 1760500000.0}
```

`bbox` = `[x, y, width, height]`, 0~1 정규화, 좌상단 원점. `found=false` 면 `bbox` 는 `null`.

서버 → 폰:

```json
{"type": "welcome", "device_id": "phone1"}
{"type": "expression", "value": "working"}
{"type": "track", "action": "start", "target": "face"}
{"type": "error", "error": "..."}
```

표정: `idle`, `thinking`, `working`, `danger`, `done`, `error`. 추적 대상: `face` 만.
같은 `device_id` 로 다시 붙으면 이전 연결을 닫는다. 끊기면 폰이 재접속한다(지수 백오프 권장, 최대 5초).

### 5.2 `WS /ws/phone/video` — JPEG 프레임

첫 메시지 `{"type": "hello", "device_id": "phone1"}` (텍스트), 이후 **바이너리 메시지 1개 = JPEG 1장**.
권장 1~3fps, 긴 변 320~640px, 오디오 없음. 전송이 밀리면 폰이 오래된 프레임을 버린다.
서버는 마지막 1장만 메모리에 둔다(디스크 저장 없음). JPEG(`FF D8`)가 아니거나 2MB 초과면 무시.

### 5.3 테스트용 REST

| | |
| --- | --- |
| `POST /phone/{device_id}/expression` | `{"value": "danger"}` → 폰에 expression 전달. 미연결 404, 잘못된 값 422 |
| `POST /phone/{device_id}/track` | `{"action": "start"\|"stop", "target": "face"}` |
| `GET /phone/{device_id}/frame` | 마지막 JPEG. 헤더 `X-Frame-Age-S`, `X-Frame-Count`. 없으면 404 |

## 6. 향후 모듈 계약 (미구현 — 문서만)

이 모듈들은 아직 중앙 서버에 없다. 붙일 때 아래 형태를 지킨다. 같은 프로세스 안의 Python 함수 호출을 기본으로 한다.

### 6.1 LLM — 자연어 → 실험 계획

입력:

```json
{"request_id": "r-001", "text": "pH 7 완충액의 전도도를 측정해줘", "context": {"stations": ["시약존", "실린더존", "실험존"]}}
```

출력 (계획):

```json
{
  "request_id": "r-001",
  "plan_id": "p-001",
  "summary": "완충액을 실험존으로 옮겨 전도도 측정",
  "steps": [
    {"step": 1, "kind": "rail", "station": "시약존"},
    {"step": 2, "kind": "act", "task_id": "pick_beaker", "params": {}},
    {"step": 3, "kind": "rail", "station": "실험존"},
    {"step": 4, "kind": "measure", "sensors": ["ph", "conductivity"], "duration_s": 30}
  ],
  "needs_feasibility_check": true
}
```

`kind`: `rail`(→ `POST /rail/command`), `act`(→ 6.3), `measure`(→ 2절 보고값 읽기), `expression`(→ 5.3).
LLM 은 mm 좌표를 만들지 않는다. 스테이션 이름만 쓴다.

### 6.2 GraphDB — 실행 가능성 검토

입력: 6.1 의 계획 JSON. 출력:

```json
{"plan_id": "p-001", "feasible": false,
 "issues": [{"step": 2, "code": "REAGENT_MISSING", "message": "완충액 재고 없음"}],
 "checked_at": 1760500000.0}
```

`feasible=false` 면 실행하지 않는다. 데이터 구조는 바뀔 예정이라 이 입출력 형태만 고정한다.

### 6.3 ACT — 로봇팔 동작 실행

```
start(task_id: str, params: dict) -> {"execution_id": str, "status": "RUNNING"}
status(execution_id: str)          -> {"execution_id", "task_id", "status", "result": dict|null, "error": str|null}
stop(execution_id: str)            -> {"execution_id", "status": "ABORTED"}
```

`status`: 레일 명령과 같은 집합 `PENDING`/`RUNNING`/`DONE`/`FAILED`/`ABORTED`/`UNKNOWN`.
ACT 실행 중에는 팔이 안전 자세가 아니므로 `/arm/state {"safe": false, "source": "robot"}` 이 보고돼야 하고,
끝나서 안전 자세로 돌아온 뒤에만 `safe: true`. 레일 명령과 ACT 실행은 동시에 돌지 않는다.
