"""ArUco 마커로 '카메라에 보이는 시약이 목표 시약인가' 를 본다. OpenCV 만 쓴다.

파지 성공 판정이 아니다. ID 가 맞아도 병을 제대로 집었다는 뜻은 아니므로 결과는 '시약 일치 확인' 으로만 쓴다.

판정 (목표 ID = T, 한 프레임에 보이는 ID 집합 = S):
    S 가 비어 있음      → NOT_DETECTED (연속 카운트 리셋)
    |S| ≥ 2             → AMBIGUOUS    (어느 병을 집었는지 모른다. T 가 섞여 있어도 성공 아님)
    |S| = 1             → 같은 ID 가 confirm_frames 번 연속이면 확정: ID == T → MATCH, 아니면 MISMATCH
    timeout_s 안에 확정 못 하면 마지막 상태(NOT_DETECTED / AMBIGUOUS)로 끝난다.

ponytail: 마커 하나만 보여주는 검증 모드. 여러 병이 동시에 보이는 환경이면 운영자가 대상 마커를
고르는 UI(또는 가장 큰 마커 선택)를 Verifier.observe 의 S 필터로 추가할 것.
"""

import json
import time
from pathlib import Path

import cv2
import numpy as np

CONFIG_PATH = Path(__file__).with_name("reagents.json")
DEFAULTS = {"dictionary": "DICT_4X4_50", "marker_size_mm": 20, "confirm_frames": 3,
            "timeout_s": 20, "stale_after_s": 3, "reagents": {}}
RESULTS = ("MATCH", "MISMATCH", "NOT_DETECTED", "AMBIGUOUS")


def load_config(path=CONFIG_PATH):
    cfg = {**DEFAULTS, **json.loads(Path(path).read_text(encoding="utf-8"))}
    cfg["reagents"] = {int(k): v for k, v in cfg["reagents"].items()}
    return cfg


class Detector:
    def __init__(self, dictionary="DICT_4X4_50"):
        self.dictionary = dictionary
        self._dict = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, dictionary))
        self._det = cv2.aruco.ArucoDetector(self._dict, cv2.aruco.DetectorParameters())

    def detect(self, jpeg):
        """JPEG 바이트 → {"ids", "markers", "size", "ms"}. 디코드 실패면 ids 빈 리스트."""
        t = time.perf_counter()
        gray = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_GRAYSCALE)
        if gray is None:
            return {"ids": [], "markers": [], "size": None, "ms": 0.0, "error": "JPEG 디코드 실패"}
        corners, ids, _ = self._det.detectMarkers(gray)
        markers = []
        for c, i in zip(corners, ids.ravel().tolist() if ids is not None else []):
            pts = c.reshape(4, 2)
            side = float(np.mean(np.linalg.norm(pts - np.roll(pts, -1, axis=0), axis=1)))
            markers.append({"id": int(i), "corners": pts.round(1).tolist(), "side_px": round(side, 1)})
        markers.sort(key=lambda m: -m["side_px"])
        return {"ids": sorted({m["id"] for m in markers}), "markers": markers,
                "size": [gray.shape[1], gray.shape[0]],
                "ms": round((time.perf_counter() - t) * 1000, 1)}

    def marker_image(self, marker_id, side_px=400):
        return cv2.aruco.generateImageMarker(self._dict, marker_id, side_px)


def annotate(jpeg, detection, target_id=None, quality=80):
    """검출 박스와 ID 를 그린 JPEG. 목표 ID 는 초록, 다른 ID 는 빨강, 목표 없으면 노랑."""
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return jpeg
    for m in detection.get("markers", []):
        pts = np.array(m["corners"], np.int32)
        if target_id is None:
            color = (0, 200, 255)
        else:
            color = (0, 200, 0) if m["id"] == target_id else (0, 0, 255)
        cv2.polylines(img, [pts], True, color, 2)
        x, y = pts[0]
        cv2.putText(img, f"id {m['id']} {m['side_px']:.0f}px", (int(x), int(y) - 6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
    ok, out = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return out.tobytes() if ok else jpeg


class Verifier:
    """목표 시약 하나에 대한 확인 세션. 프레임마다 observe(), 주기적으로 tick() 을 부른다."""

    def __init__(self, reagents, confirm_frames=3, timeout_s=20.0):
        self.reagents = reagents
        self.confirm_frames = confirm_frames
        self.timeout_s = timeout_s
        self.active = False
        self.target_id = None
        self.device_id = None
        self.started_at = None
        self.live = None  # 마지막 프레임 기준: NOT_DETECTED / AMBIGUOUS / CONFIRMING (STALE 은 snapshot 에서)
        self.observed_ids = []
        self.streak_id = None
        self.streak = 0
        self.frames = 0
        self.result = None  # 확정 결과. 다음 start() 까지 남는다.

    def name(self, marker_id):
        return self.reagents.get(marker_id, f"ID {marker_id}")

    def start(self, target_id, device_id):
        self.__init__(self.reagents, self.confirm_frames, self.timeout_s)
        self.active = True
        self.target_id = int(target_id)
        self.device_id = device_id
        self.started_at = time.time()
        self.live = "NOT_DETECTED"

    def stop(self, reason="운영자 중단"):
        if self.active and self.result is None:
            status = "AMBIGUOUS" if self.live == "AMBIGUOUS" else "NOT_DETECTED"
            self.result = self._make_result(status, reason)
        self.active = False

    def observe(self, detection):
        if not self.active:
            return
        ids = detection["ids"]
        self.frames += 1
        self.observed_ids = ids
        if not ids:
            self.live, self.streak_id, self.streak = "NOT_DETECTED", None, 0
        elif len(ids) > 1:
            self.live, self.streak_id, self.streak = "AMBIGUOUS", None, 0
        else:
            i = ids[0]
            self.streak = self.streak + 1 if i == self.streak_id else 1
            self.streak_id = i
            self.live = "CONFIRMING"
            if self.streak >= self.confirm_frames:
                self.result = self._make_result("MATCH" if i == self.target_id else "MISMATCH",
                                                f"{self.streak} 프레임 연속 검출", observed_id=i)
                self.active = False

    def tick(self):
        """시간 초과 검사."""
        if self.active and time.time() - self.started_at > self.timeout_s:
            status = "AMBIGUOUS" if self.live == "AMBIGUOUS" else "NOT_DETECTED"
            self.result = self._make_result(status, f"{self.timeout_s:.0f}초 안에 확정 못 함")
            self.active = False

    def _make_result(self, status, note, observed_id=None):
        assert status in RESULTS
        return {"status": status, "target_id": self.target_id, "target_name": self.name(self.target_id),
                "observed_id": observed_id,
                "observed_name": self.name(observed_id) if observed_id is not None else None,
                "frames": self.frames, "note": note, "decided_at": time.time(), "device_id": self.device_id}

    def snapshot(self, frame_age_s=None, stale_after_s=3.0):
        """frame_age_s: 그 폰의 마지막 프레임 경과 초 (프레임이 없으면 None). 오래됐으면 live=STALE."""
        live = self.live
        if self.active and (frame_age_s is None or frame_age_s > stale_after_s):
            live = "STALE"
        return {"active": self.active, "target_id": self.target_id,
                "target_name": self.name(self.target_id) if self.target_id is not None else None,
                "device_id": self.device_id, "live": live, "observed_ids": self.observed_ids,
                "streak": self.streak, "confirm_frames": self.confirm_frames, "frames": self.frames,
                "elapsed_s": round(time.time() - self.started_at, 1) if self.started_at else None,
                "timeout_s": self.timeout_s, "result": self.result}


def head_state_for(snap):
    """검증 스냅샷 → 폰 얼굴 (state, message). None 이면 보낼 것 없음. 바뀐 것만 보내는 건 호출자가 한다."""
    name = snap["target_name"]
    if snap["active"]:
        return {"STALE": ("error", "카메라 영상이 끊겼어요"),
                "NOT_DETECTED": ("working", f"{name} 마커를 찾고 있어요"),
                "AMBIGUOUS": ("thinking", "마커가 여러 개 보여요. 하나만 보여 주세요"),
                "CONFIRMING": ("working", f"{name} 시약을 확인하고 있어요"),
                }[snap["live"]]
    r = snap["result"]
    if r is None:
        return None
    return {"MATCH": ("done", f"시약을 확인했어요 ({r['target_name']})"),
            "MISMATCH": ("danger", f"선택한 시약이 다릅니다 ({r['observed_name']} ≠ {r['target_name']})"),
            "NOT_DETECTED": ("error", f"{r['target_name']} 마커를 찾지 못했어요"),
            "AMBIGUOUS": ("error", "마커가 여러 개 보여 판단할 수 없어요"),
            }[r["status"]]
