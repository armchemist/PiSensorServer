# 실장비 연결과 배포

## 1. 실행 환경

에이전트 서버는 Python 3.10 이상이 필요하다. 별도 NewUI 빌드에는 Node가 필요하다. Physical AI Tools 확장 노드는 **기존 ACT 추론이 동작하던 로봇 PC의 ROS·LeRobot·CUDA 환경**에서 실행한다. 에이전트의 일반 Python 가상환경에 ROS와 Torch를 새로 섞지 않는다.

참조 Physical AI Tools revision은 `27192daf75fa5f421a3bd1825a50ee63d649dccd`다. `physical_ai_server`와 `physical_ai_interfaces`, `rosbridge_server`, `web_video_server`, 실제 OMX follower controller가 설치되어 있어야 한다.

## 2. ACT 배포 설정

메인 설정은 `config/main.yaml`이다. 다른 배포 파일이 필요하면 같은 디렉터리의 `config/local.yaml`로 복사하고 CLI의 `--config` 또는 `AGENT_CONFIG`로 지정한다. YAML 경로 기준 상대 경로를 사용하며 `actions.*.policy_path`는 `robot.model_root` 기준이다.

- `robot.model_root`: 로봇 PC의 모델 저장 경로. API PC에 가중치가 있을 필요는 없다.
- `actions`: 저장소, 40자리 revision, subfolder, 정책 경로, FPS, 평균 시간과 데이터셋 revision을 함께 관리한다. 등록되지 않은 ACT/저장소는 거부한다.
- `robot.profiles`: 왼팔·오른팔·양팔의 학습 카메라 이름과 관절 순서를 담는다. 토픽 경로는 장비 배치에 맞게 수정한다. depth 이름의 관측도 학습과 동일한 3채널 영상이어야 한다.
- `actions.*.judge_cameras`: 해당 프로파일의 RGB 카메라 이름. ACT가 구독하는 토픽으로 자동 연결한다.
- `robot.hold_profile`: 모델 전환과 관계없이 양팔·그리퍼 전체를 계속 관측하고 정지하는 프로파일. 단일 팔 모델을 실행해도 반대쪽 팔을 제외하지 않는다.
- `initial_facts`: 확인한 `left`, `right`, `cap` 상태. 이후 상태는 실행 결과로 갱신·저장한다.

모델 설치는 로봇 PC에서 수행한다. 메인 YAML에 적힌 체크포인트는 초기 후보이므로 현장 모델 선택에 따라 바꿀 수 있다.

```bash
uv run --extra models python -m armchemist.cli download-models
uv run python -m armchemist.cli check-config --models
```

`config/duration-statistics.json`은 원본 에피소드 프레임 수/FPS를 기준으로 계산했다. 시약병 붓기와 센서 설치는 ambiguous 에피소드 포함/제외 변형이 있으므로 선택한 모델에 해당하는 변형을 사용한다. 15 Hz 다운샘플 모델의 시간 반올림 차이와 녹화 대기 구간은 실장비 검증에서 확인한다. 이 값은 클라우드 API가 제공하는 상수가 아니다.

```bash
uv run python -m armchemist.cli check-config
uv run python -m armchemist.cli duration meta/info.json meta/episodes.jsonl \
  --dataset-revision <dataset-commit-sha> --exclude 0 1 --output durations.json
```

선행 조건·효과와 완료 조건은 `armchemist/registry.py`에 있다. 현재 병·뚜껑·실린더의 파지 상태를 추적하며, 뚜껑 열기 이후 오른손이 뚜껑을 잡는 흐름을 전제로 한다. 현장 모델의 종료 상태가 다르면 영상으로 확인해 레지스트리 조건을 맞춰야 한다. 임의 부피를 정밀 투입하거나 미등록 기구를 조작하는 기능은 없다.

## 3. 로봇 PC의 서버 확장

기본 Physical AI Tools 서버와 확장 서버를 동시에 띄우지 않는다. follower 드라이버는 유지하고, 기본 `physical_ai_server` 프로세스를 확장 노드로 교체한다. Web UI/teleop 등 다른 명령 생성기는 동시에 팔을 제어하지 않아야 한다.

기존 ROS 작업공간과 Python 환경을 활성화하고, 이 저장소 디렉터리에서 실행한다.

```bash
python -m armchemist.physical_node --config config/main.yaml
```

rosbridge와 web_video_server도 함께 필요하면 다음 launch 파일을 사용한다.

```bash
ros2 launch /absolute/path/armchemist_agent/launch/agent.launch.py \
  config:=/absolute/path/armchemist_agent/config/main.yaml
```

확장 노드가 메인 YAML의 프로파일을 ROS 파라미터로 자동 변환한다. upstream 패키지의 config 목록을 수정하지 않는다. 생성 결과를 확인하려면 다음 명령을 사용한다.

```bash
uv run python -m armchemist.cli export-ros /tmp/armchemist-ros.yaml
```

OMX 기본 `camera1`/6관절 프로파일을 양팔 모델에 그대로 사용하지 않는다. 확장 서버는 카메라 이름·관절 차원·설치 manifest의 revision 불일치를 거부한다. ROS 파라미터 자동 생성에는 PyYAML이 필요하므로 기존 로봇 Python 환경에서도 `import yaml`이 가능해야 한다.

추가 서비스 `/agent/control`은 기존 `physical_ai_interfaces/srv/SendCommand` 타입을 재사용한다. `command=0`, `task_info.task_instruction[0]`에 JSON을 보낸다. 이는 **이 저장소가 추가한 확장**이며 upstream에 원래 있는 API라고 가정하지 않는다. 요청은 `operation`(initialize/prepare/run/pause/status/finish)과 `execution_id`를 포함한다. `run`의 `duration_s`는 로봇 PC에서 첫 명령 송신부터 계산하므로 네트워크 응답 지연으로 실행 시간이 늘어나지 않는다. 정책 추론이 늦게 끝나도 deadline 이후 생성된 액션은 송신하지 않는다.

정지 시 측정한 현재 위치와 0 속도로 `JointTrajectory`를 송신한다. 관절 상태가 0.5초 이내의 새 데이터이고, 0.003 rad 범위에서 0.3초 유지되어야 정지 확인을 내린다. 카메라는 계속 구독하며, 그 이후의 ROS timestamp를 가진 프레임만 판단에 사용한다. 서버와 카메라의 ROS 시계가 일치해야 한다. 이 소프트웨어 타이머는 하드 실시간 보증이 아니다. 제어기별 hold 동작과 위 허용치는 실제 장비에서 검증해야 한다.

## 4. 종료 자세에서 레일 이동

자동 stow/home은 없다. `robot.transfer_limits`에 `{관절명: [최소 라디안, 최대 라디안]}` 형태로 현장에서 보정한 허용 범위를 입력한다. 양팔과 그리퍼의 모든 관절을 포함해야 한다. 빈 설정이면 팔 정지는 가능하지만 자동 레일 이동은 거부한다. 기존 PiSensorServer의 팔 상태 TTL/명령별 소비 규칙은 유지한다.

PiSensorServer의 스테이션 좌표는 그 서버의 보정 파일을 사용한다. 에이전트는 `시약존`, `실린더존`, `실험존` 이름만 전송한다. 중앙 서버의 명령 ID와 DONE, 그 이후의 새 장치 보고에서 스테이션 도착까지 확인한다. 중앙 서버와 Pi의 epoch 시계는 동기화해야 한다.

## 5. 에이전트 API 시작

`.env.example`을 `.env`로 복사해 API 키와 비밀번호를 채운다. 메인 YAML에서 `mode: live`, `robot.rosbridge_url`, `rail.central_url`, 실제 토픽과 이동 허용 범위를 설정한다. 키를 `VITE_*`에 넣지 않는다.

```bash
uv sync --extra neo4j --extra models
uv run --env-file .env python -m armchemist.cli serve
# 다른 설정 파일
uv run --env-file .env python -m armchemist.cli --config config/local.yaml serve
```

http://127.0.0.1:8000/api/docs 에서 API를 확인하고 `/api/health`에서 모드가 `live`인지 확인한다. 서버는 같은 데이터 디렉터리에서 여러 프로세스가 실행기를 소유하지 못하도록 파일 락을 사용한다.

NewUI는 별도 저장소이며 이번 에이전트 커밋에는 UI 변경을 포함하지 않는다. 추후 UI 변경을 반영하면 채팅 → 구성 확정 → 계획 확인·실행 순서로 사용할 수 있다. 현재 로컬에 보관된 UI로 수행한 브라우저 시험은 `verification.md`에 기록했다. 서버의 선택적 정적 파일 제공은 로컬 `ui/dist`가 있을 때만 활성화되며 API 실행의 필수 조건은 아니다.

## 6. GraphDB

YAML의 `graph.uri`, `graph.user`, `graph.database`와 `.env`의 `NEO4J_PASSWORD`를 설정한다. 접속이 실패하면 로컬 데이터로 조용히 대체하지 않는다. 설정이 없으면 SQLite 기반 개발 그래프를 사용하고 UI에는 mock 출처를 표시한다.

Neo4j에는 `ArmchemistNode(snapshot,node_id)`와 `ArmchemistEdge(edge_id,kind)`로 NewUI의 그래프를 격리 저장한다. 원래 labels와 properties를 보존한다. 기존 조직 GraphDB의 스키마를 자동 추정하거나 덮어쓰지 않는다. 실제 인벤토리를 NewUI `nodes/relationships` 형식으로 내보낸 뒤 가져온다. 이 명령은 ARMchemist의 `inventory` 스냅샷만 교체한다.

```bash
uv run --env-file .env python -m armchemist.cli import-inventory inventory.json
```

계획·확정 그래프·실행 이벤트는 `data_dir/agent.sqlite3`에 저장한다. 동작 완료를 DB의 재고 측정이나 화학 실험 성공으로 확대 해석하지 않는다.

## 7. 중단과 재시작

UI의 실행 중단은 취소 플래그를 설정한다. 실행 시간 대기/레일 폴링을 취소하고 팔·레일 정지를 각각 시도한다. 판단 요청 중에는 이미 팔이 정지 상태다. 정지 실패는 보고서의 `stop_errors`에 남긴다.

진행 중 서버가 재시작되면 실행은 `unknown`이 되고 자동 재개하지 않는다. 실제 물체·레일·팔 상태를 확인한 뒤 `POST /api/state/reconcile`에 `{"observed":true,"facts":{"left":"empty","right":"empty","cap":"closed"}}`처럼 실제 관측값을 보낸다. 서버가 팔 정지와 레일 상태를 다시 확인한다. 로봇 서버가 이전 실행을 여전히 소유하면 종료/재기동으로 먼저 정리해야 한다. 기존 실행 기록은 유지된다.

## 8. 실장비 검증 순서

1. 장비를 움직이지 않고 카메라/관절 토픽과 모델 관측 차원·이름을 확인한다.
2. 단일 ACT로 평균 시간 실행 → hold → 새 프레임 → 판정을 확인한다. 판단 중 팔·그리퍼가 유지되는지 측정한다.
3. 미완료 판정으로 전체 평균 시간만큼 재개되는지, 총 3회에서 중단되는지 확인한다.
4. 정지 후 카메라 끊김, 판정 API 지연, 사용자 취소 때 동작이 재개되지 않는지 확인한다.
5. 보정된 종료 자세 범위에서 레일 명령 ID·DONE·도착 스테이션을 확인한다.
6. 서로 다른 존의 ACT 시퀀스와 다음 모델 전환을 검증한다.

자동 테스트와 브라우저 E2E는 모의 장비 및 프로토콜 fixture로 수행한다. 실제 GPU 추론, 제어기의 hold 성능, 전체 파지 조건, 클라우드 판정 정확도는 이 절의 별도 검증 대상이다.
