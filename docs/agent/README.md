# ARMchemist Agent

NewUI 채팅으로 보유 ACT의 순서를 구성하고, Physical AI Tools의 ACT 추론과 PiSensorServer의 레일을 연결한다. 기존 `agent_structure` 코드를 대체한 실행 서버·ROS 확장 노드를 포함한다. 웹 UI는 별도 [NewUI 저장소](https://github.com/armchemist/NewUI)에서 관리하며, 이번 커밋에는 UI 변경을 포함하지 않는다.

## 동작

각 ACT는 **데이터셋 평균 시간 실행 → 현재 양팔·그리퍼 자세로 정지 → 정지 확인 이후 새 영상 → Decisions 완료 판정** 순서로 진행한다. 미완료이면 평균 시간 전체를 다시 실행하고, 최대 **총 3회**에서 중단한다. 판정 응답을 기다리는 동안 정지 상태를 유지한다. 완료하면 다음 ACT로 넘어가며, 스테이션이 달라질 때 종료 자세 그대로 레일을 이동한다.

판단 통신 오류는 판단 요청만 재시도한다. 채팅은 계획을 만들며, 사용자가 구성을 확정하고 실행 버튼을 눌러야 장비를 호출한다. 확정 계획의 중복 요청을 차단하고, 설정이 바뀌면 기존 계획을 다시 확인하게 한다.

## 설치와 로컬 실행

```bash
uv sync --extra neo4j --extra models
uv run python -m armchemist.cli check-config
uv run python -m armchemist.cli serve
```

API 문서는 http://127.0.0.1:8000/api/docs 에서, 상태는 `/api/health`에서 확인한다. 기본 설정은 장비를 호출하지 않는 `mock` 모드다. UI 없이도 API와 자동 테스트를 실행할 수 있다. `/api/chat` 요청의 사용자 메시지에는 OpenAI 키 없이 시험할 때 다음처럼 ACT ID를 넣는다.

```text
pickup_bottle1, open_cap, pour_bottle, close_cap, pickdown_bottle1
```

자연어 계획을 사용하려면 `.env.example`을 `.env`로 복사해 `OPENAI_API_KEY`를 입력하고 아래처럼 실행한다. 키는 브라우저에 전달하지 않는다. mock 모드에서는 키가 있어도 ACT·레일·완료 판정은 모의 실행이다.

```bash
uv run --env-file .env python -m armchemist.cli serve
```

## 설정은 메인 YAML 한 곳에서

[config/main.yaml](../../config/main.yaml)에 서비스 주소, GraphDB, 로봇 프로파일, 카메라·관절 토픽, 모델 저장 위치, 9개 ACT의 체크포인트·revision·FPS·평균 시간을 모았다. `.env`에는 API 키와 비밀번호만 둔다.

| 설정 | 내용 |
|---|---|
| `mode` | `mock` 또는 `live` |
| `server`, `data_dir` | API 주소와 실행 기록 디렉터리 |
| `openai` | 계획/판정 모델, 응답 제한 시간, 판정 신뢰도 |
| `rail` | PiSensorServer 중앙 주소·장치 ID |
| `graph` | Neo4j 주소·사용자·DB. URI가 비어 있으면 명시적인 로컬 mock 그래프 |
| `robot.rosbridge_url` | 로봇 PC의 rosbridge 주소 |
| `robot.model_root` | **로봇 PC**에서 체크포인트를 저장할 디렉터리 |
| `robot.profiles` | 학습 관측 이름과 관절 순서, 현장 카메라·제어기 토픽 |
| `robot.hold_profile`, `robot.transfer_limits` | 양팔·그리퍼 전체 정지 관측과 보정된 레일 이동 허용 범위 |
| `actions` | ACT별 모델 저장소·고정 revision·subfolder·로컬 경로·평균 실행 시간 |

상대 경로는 YAML 파일 위치 기준이며, `actions.*.policy_path`만 `robot.model_root` 기준이다. `--config config/local.yaml`로 별도 배포 파일을 지정할 수 있다. API PC와 로봇 PC에 같은 설정을 배포하되 `model_root`는 로봇 PC의 경로를 사용한다. 실제 모델 로딩 경로는 로봇 노드가 자신의 YAML에서 결정한다.

**현장에서 채울 값:** 서버 주소, 실제 ROS 토픽, 확인한 초기 물체 상태, `transfer_limits`. 현재 토픽 경로는 배치 예시다. 카메라 feature 이름과 관절 순서는 학습 메타데이터를 반영했다. `cam_head_depth`도 모델이 요구하는 3채널 영상으로 제공해야 하며 raw depth 토픽으로 대체할 수 없다. 완료 판단에는 RGB 카메라만 사용한다.

레일 스테이션의 실제 좌표는 PiSensorServer의 기존 보정 설정이 관리한다. 이 에이전트는 시약존·실린더존·실험존 이름을 전달한다. 보정되지 않은 이동 허용 범위를 임의 숫자로 채우지 않으며, 기본 빈 설정에서는 자동 레일 이동이 거부된다.

## 모델 설치와 실장비 기동

모델 가중치는 Git에 넣지 않는다. [모델 안내](../../models/README.md)와 [catalog.json](../../models/catalog.json)에 위치와 관측 형식을 남겼다. 메인 YAML의 체크포인트는 실제 존재하는 고정 revision의 **초기 후보**이며 실장비 성공률로 선정한 최적 체크포인트는 아니다.

로봇 PC에서 체크포인트를 내려받고 검사한다.

```bash
uv run --extra models python -m armchemist.cli download-models
uv run python -m armchemist.cli check-config --models
# 특정 모델만 설치할 수도 있다.
uv run --extra models python -m armchemist.cli download-models pickup_bottle1
```

기존 ACT 추론이 동작하는 ROS·LeRobot·CUDA 환경에서 기본 PhysicalAIServer 대신 확장 노드를 실행한다. YAML로부터 ROS 파라미터를 자동 생성하므로 upstream 설정 파일을 수정할 필요가 없다.

```bash
# 로봇 PC: 기존 ROS/Physical AI Tools 환경을 활성화한 뒤 저장소 루트에서 실행
python -m armchemist.physical_node --config config/main.yaml
# rosbridge와 영상 서버까지 함께 띄우려면 위 명령 대신 사용
ros2 launch /absolute/path/armchemist_agent/launch/agent.launch.py \
  config:=/absolute/path/armchemist_agent/config/main.yaml
```

장비 설정 후 YAML의 `mode`를 `live`로 바꾸고 API 서버를 시작한다. 로봇 드라이버·PiSensorServer 기동, GraphDB 인벤토리 가져오기, 중단/복구와 실장비 확인 순서는 [배포 문서](deployment.md)에 있다.

## 검증과 진행 상태

```bash
uv run pytest -q
uv run ruff check armchemist tests launch
```

[단계별 계획](implementation-plan.md), [검증 기록](verification.md)을 참고한다. NewUI 연동 변경과 브라우저 시험 스크립트는 별도 UI 작업으로 보관하고 있으며, 이번 푸시에는 포함하지 않는다. UI 연동 반영 전에는 위 API 문서와 백엔드 테스트로 확인한다.

로컬 자동 테스트와 모의 브라우저 통합 검증을 수행했다. **실장비가 아직 준비되지 않아 실제 GPU 추론·모터 hold·레일 이동·클라우드 판정·배포 Neo4j는 미검증**이다. 장비 연결 후 단일 ACT부터 확인한다.

## 출처

- UI 연동 기준: [armchemist/NewUI](https://github.com/armchemist/NewUI) `f88a7a6343`. UI 코드는 이 저장소에 포함하지 않는다.
- 추론: [ROBOTIS-GIT/physical_ai_tools](https://github.com/ROBOTIS-GIT/physical_ai_tools) `27192daf75fa5f421a3bd1825a50ee63d649dccd`의 `PhysicalAIServer`를 재사용. 해당 버전의 LeRobot 기준은 `989f3d05ba47f872d75c587e76838e9cc574857a`.
- 레일: [PiSensorServer 프로토콜](https://github.com/armchemist/PiSensorServer/blob/main/docs/PROTOCOL.md).
- OpenAI: [Decisions](https://developers.openai.com/api/docs/guides/decisions), [Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs).
