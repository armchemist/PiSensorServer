# ACT 모델 위치

모델 선택·다운로드·실행의 기준은 [`../config/main.yaml`](../config/main.yaml)의 `actions`다. `catalog.json`은 그 고정 revision의 실제 `config.json`에서 확인한 관측/출력 형식이다. 설정을 변경하면 카탈로그도 함께 갱신한다.

기본 설치 경로는 `models/<ACT>/<40자리 revision>/<subfolder>/`이며 `config.json`, `model.safetensors`, `armchemist-model.json`을 둔다. 마지막 파일은 설치 도구가 기록하는 저장소·revision·subfolder 정보다. 가중치와 Hugging Face 토큰은 커밋하지 않는다.

| ACT | 저장소 | 체크포인트 | FPS | 평균 시간(초) |
|---|---|---|---:|---:|
| pickup_bottle1 | gc22-shin/pickup_bottle1_act | 30fps/070k | 30 | 15.0258 |
| pickdown_bottle1 | gc22-shin/pickdown_bottle1_act | 30fps/030k | 30 | 15.0208 |
| pickup_cylinder | Dominho/act-armchemist-pickup-cylinder-80k | 저장소 루트 | 15 | 10.4333 |
| pickdown_cylinder | gc22-shin/pickdown_cylinder_act | 30fps/100k | 30 | 18.0140 |
| open_cap | Dominho/act-armchemist-open-cap-100k | 저장소 루트 | 30 | 15.0044 |
| close_cap | gc22-shin/close_cap_act | 30fps/100k | 30 | 15.0142 |
| pour_bottle | gc22-shin/pour_bottle_act | 30fps_excl_ambiguous/100k | 30 | 10.0595 |
| pour_cylinder | gc22-shin/pour_cylinder_act | 30fps/100k | 30 | 15.0179 |
| setup_sensors | gc22-shin/setup_sensors_act | 30fps_excl_ambiguous/100k | 30 | 13.0173 |

체크포인트는 배포를 시작할 수 있게 고정한 후보이며 최적 성능을 보증하지 않는다. 모델 파일의 존재와 입력 차원은 확인했지만 가중치 전체 다운로드나 실제 GPU 실행은 이 개발 환경에서 수행하지 않았다.

로봇 PC의 저장소 루트에서 실행한다.

```bash
uv run --extra models python -m armchemist.cli download-models
uv run python -m armchemist.cli check-config --models
```

설치 도구는 고정 commit과 선택한 subfolder의 JSON/safetensors 파일만 받는다. 다른 체크포인트를 선택할 때 `model_revision`, `model_subfolder`, `policy_path`, `fps`를 함께 수정한다. ambiguous 데이터 포함 모델로 바꿀 경우 평균 시간도 해당 데이터 변형에 맞춘다. 단순 폴더 이름 변경으로 설치 revision 검사를 우회하지 않는다.

평균 시간은 [`duration-statistics.json`](../config/duration-statistics.json)의 원본 데이터셋 에피소드 프레임 수/FPS에서 계산했다. 학습 제외 에피소드를 적용한 평균이며 API가 제공하는 값이 아니다. 녹화 대기 구간이 포함되는지는 현장에서 확인한다.
