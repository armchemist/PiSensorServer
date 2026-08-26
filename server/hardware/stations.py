"""레일 위 이름 붙은 지점들 — 시약존, 실험존 같은 것.

에이전트에게 mm 를 주면 LLM 이 숫자를 만들어낸다. 이름을 닫힌 집합으로 두면
잘못된 값은 이동이 아니라 404 가 되고, 환각해도 물리적으로 아무 일이 없다.
장비를 옮겨 실제 위치가 바뀌어도 이 표만 고치면 에이전트 쪽은 그대로다.

좌표는 점(point)이다. goto 하면 캐리지가 정확히 그 자리에 선다.

⚠️ mm 는 보정으로 정한 원점 기준이다. 다시 보정하면 원점이 달라질 수 있어서
저장된 좌표가 엉뚱한 곳을 가리키게 된다. 어느 보정 기준인지 함께 저장해 두고,
달라지면 stale 로 표시해 이동을 거부한다.
"""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
STATIONS_PATH = REPO_ROOT / "rail" / "stations.json"

# 이름은 URL 경로에 들어가므로 슬래시와 공백만 막는다. 한글은 그대로 쓴다.
NAME_RE = re.compile(r"^[^/\s][^/]{0,39}$")


class StationError(RuntimeError):
    """이름이 없거나, 중복이거나, 범위를 벗어남."""


class StationNotFound(StationError):
    """등록되지 않은 이름. 에이전트가 없는 이름을 부른 경우라 404 로 나간다."""


class StationStale(RuntimeError):
    """재보정 이후 좌표를 신뢰할 수 없음."""


class Stations:
    def __init__(self, path=STATIONS_PATH):
        self.path = path
        self._data = {"calibrated_at": None, "stations": {}}
        self._load()

    # --- 저장 ---

    def _load(self):
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        if isinstance(data, dict) and isinstance(data.get("stations"), dict):
            self._data = data

    def _save(self):
        try:
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False) + "\n")
            os.replace(tmp, self.path)
        except OSError:
            pass

    # --- 조회 ---

    def _stale(self, calibrated_at):
        """저장된 기준과 지금 보정 기준이 다르면 좌표를 믿을 수 없다."""
        stored = self._data.get("calibrated_at")
        if stored is None:
            # 보정 전에 등록한 경우. 보정이 생기면 기준이 달라진 것으로 본다.
            return calibrated_at is not None
        return stored != calibrated_at

    def list(self, stroke_mm, calibrated_at):
        stale = self._stale(calibrated_at)
        out = []
        for name, conf in sorted(
            self._data["stations"].items(), key=lambda kv: kv[1]["position_mm"]
        ):
            pos = conf["position_mm"]
            out.append({
                "name": name,
                "position_mm": pos,
                "out_of_range": not 0.0 <= pos <= stroke_mm,
                "stale": stale,
            })
        return {
            "stations": out,
            "stale": stale,
            "calibrated_at": self._data.get("calibrated_at"),
            "current_calibrated_at": calibrated_at,
        }

    def target_mm(self, name, stroke_mm, calibrated_at):
        """goto 대상 좌표. 신뢰할 수 없으면 예외."""
        conf = self._data["stations"].get(name)
        if conf is None:
            known = ", ".join(sorted(self._data["stations"])) or "(없음)"
            raise StationNotFound(f"'{name}' 은 등록되지 않았습니다. 등록된 것: {known}")

        if self._stale(calibrated_at):
            raise StationStale(
                "재보정 이후 스테이션 좌표를 신뢰할 수 없습니다. 다시 등록하거나, "
                "그대로 써도 된다면 /rail/stations/revalidate 를 부르세요."
            )

        pos = conf["position_mm"]
        if not 0.0 <= pos <= stroke_mm:
            raise StationError(
                f"'{name}' 의 좌표 {pos:.2f}mm 가 스트로크(0~{stroke_mm:.2f}mm) 밖입니다."
            )
        return pos

    # --- 수정 ---

    def add(self, name, position_mm, stroke_mm, calibrated_at, overwrite=False):
        if not NAME_RE.match(name or ""):
            raise StationError("이름은 1~40자, 공백과 '/' 는 쓸 수 없습니다.")
        if name in self._data["stations"] and not overwrite:
            raise StationError(f"'{name}' 은 이미 있습니다. 옮기려면 PATCH 를 쓰세요.")
        if not 0.0 <= position_mm <= stroke_mm:
            raise StationError(
                f"{position_mm:.2f}mm 는 스트로크(0~{stroke_mm:.2f}mm) 밖입니다."
            )

        # 등록하는 순간의 보정 기준을 표 전체에 박는다. 표 안에서 기준이 섞이면
        # 어느 것이 유효한지 알 수 없게 되므로 표 단위로 관리한다.
        if self._stale(calibrated_at) and self._data["stations"]:
            raise StationStale(
                "다른 보정 기준의 스테이션이 남아 있습니다. 전부 다시 등록하거나 "
                "/rail/stations/revalidate 로 지금 보정 기준을 인정하세요."
            )
        self._data["calibrated_at"] = calibrated_at
        self._data["stations"][name] = {
            "position_mm": round(float(position_mm), 3),
            "taught_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        }
        self._save()
        return {"name": name, **self._data["stations"][name]}

    def update(self, name, stroke_mm, calibrated_at, position_mm=None, new_name=None):
        conf = self._data["stations"].get(name)
        if conf is None:
            raise StationNotFound(f"'{name}' 은 등록되지 않았습니다.")
        if position_mm is not None:
            if not 0.0 <= position_mm <= stroke_mm:
                raise StationError(
                    f"{position_mm:.2f}mm 는 스트로크(0~{stroke_mm:.2f}mm) 밖입니다."
                )
            conf["position_mm"] = round(float(position_mm), 3)
            conf["taught_at"] = datetime.now(timezone.utc).astimezone().isoformat(
                timespec="seconds"
            )
            self._data["calibrated_at"] = calibrated_at
        if new_name and new_name != name:
            if not NAME_RE.match(new_name):
                raise StationError("이름은 1~40자, 공백과 '/' 는 쓸 수 없습니다.")
            if new_name in self._data["stations"]:
                raise StationError(f"'{new_name}' 은 이미 있습니다.")
            self._data["stations"][new_name] = self._data["stations"].pop(name)
            name = new_name
        self._save()
        return {"name": name, **self._data["stations"][name]}

    def remove(self, name):
        if name not in self._data["stations"]:
            raise StationNotFound(f"'{name}' 은 등록되지 않았습니다.")
        del self._data["stations"][name]
        self._save()
        return {"removed": name}

    def revalidate(self, calibrated_at):
        """재보정 후에도 좌표가 그대로 유효하다고 사용자가 확인해 준다."""
        self._data["calibrated_at"] = calibrated_at
        self._save()
        return {"calibrated_at": calibrated_at, "count": len(self._data["stations"])}


stations = Stations()
