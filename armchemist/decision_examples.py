"""Load reviewed, bundled reference images without runtime network downloads."""
import base64
import copy
import hashlib
import json
import re
from pathlib import Path

from .registry import CAPABILITIES


class DecisionExamples:
    def __init__(self, manifest_path):
        path = Path(manifest_path).resolve()
        raw = path.read_bytes()
        manifest = json.loads(raw)
        if manifest.get("schema_version") != 1:
            raise ValueError("지원하지 않는 판정 예시 manifest")
        self.fingerprint = hashlib.sha256(raw).hexdigest()
        self.actions = {}
        seen = set()
        for group in manifest["actions"]:
            name = group["action_id"]
            if name not in CAPABILITIES or name in self.actions:
                raise ValueError("중복 또는 미등록 ACT 판정 예시")
            question = group["question"]
            if not isinstance(question, str) or not question.strip() or not group["examples"]:
                raise ValueError(f"{name}: 질문과 예시가 필요합니다")
            content = []
            image_count = 0
            for example in group["examples"]:
                if example["id"] in seen or example["label"] not in ("complete", "incomplete"):
                    raise ValueError("판정 예시는 중복 없는 complete/incomplete만 허용합니다")
                seen.add(example["id"])
                if example.get("review_status") != "approved" or not example["images"]:
                    raise ValueError("승인된 이미지 예시가 필요합니다")
                content.append({"type": "input_text", "text":
                                f"[참조 예시 {example['id']}] 정답: {example['label']}. "
                                f"설명: {example['description']}"})
                for frame in example["images"]:
                    relative = Path(frame["path"])
                    image_path = (path.parent / relative).resolve()
                    if relative.is_absolute() or not image_path.is_relative_to(path.parent):
                        raise ValueError("판정 예시 이미지는 manifest 디렉터리 내부 경로여야 합니다")
                    if image_path.suffix.lower() != ".jpg":
                        raise ValueError("판정 예시는 JPEG 파일이어야 합니다")
                    data = image_path.read_bytes()
                    if not data.startswith(b"\xff\xd8\xff") or len(data) > 5_000_000:
                        raise ValueError("잘못된 JPEG 판정 예시")
                    if not re.fullmatch(r"[0-9a-f]{64}", frame["sha256"]) \
                            or hashlib.sha256(data).hexdigest() != frame["sha256"]:
                        raise ValueError(f"판정 예시 이미지 해시 불일치: {relative}")
                    content.extend([
                        {"type": "input_text", "text": f"참조 카메라(데이터셋 이름): {frame['camera']}"},
                        {"type": "input_image", "image_url": "data:image/jpeg;base64," +
                         base64.b64encode(data).decode("ascii")},
                    ])
                    image_count += 1
            # Leave ample room for current camera images in the Decisions request.
            if image_count > 32:
                raise ValueError(f"{name}: 참조 이미지는 최대 32장입니다")
            self.actions[name] = {"question": question, "content": content}

    def for_action(self, name):
        return copy.deepcopy(self.actions.get(name))
