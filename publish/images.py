"""이미지 선택 로직.

우선순위: 인박스(새 사진) > 주제 매칭 인포그래픽 > 사진 풀(순환 재활용).
사진 풀이 비어 있어도 인포그래픽만으로 게시 가능.

폴더 구조:
  drafts/images/           내가 만든 인포그래픽(PNG)
  drafts/photos/           실물 사진 풀(사용자가 채움) — 자동 재활용
  drafts/photos/inbox/     새 사진(우선 사용, 사용 후 used로 이동)
  drafts/photos/used/      사용 완료 보관
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMG_DIR = ROOT / "drafts" / "images"
PHOTO_DIR = ROOT / "drafts" / "photos"
INBOX_DIR = PHOTO_DIR / "inbox"
USED_DIR = PHOTO_DIR / "used"
DATA_DIR = ROOT / "data"
ROT_FILE = DATA_DIR / "photo_rotation.json"

# 초안 코드 -> 대표 인포그래픽 파일명
INFOGRAPHIC_MAP = {
    "a02": "price-factors.png",
    "a03": "nickname-checklist.png",
    "a06": "platform-compare.png",
}
# 세그먼트 기본 인포그래픽(주제 매칭 없을 때)
SEGMENT_DEFAULT = {"a": "process-flow.png", "b": "bucket-compare.png", "c": "sign-compare.png"}


def _imgs(d: Path) -> list[Path]:
    out: list[Path] = []
    if d.exists():
        for ext in ("*.png", "*.jpg", "*.jpeg"):
            out += sorted(d.glob(ext))
    return out


def draft_code(draft_path) -> str:
    m = re.match(r"([abc]?\d+)", Path(draft_path).stem)
    return m.group(1) if m else Path(draft_path).stem[:3]


def _infographic_for(code: str) -> Path | None:
    fn = INFOGRAPHIC_MAP.get(code)
    if not fn:
        seg = code[0] if code and code[0] in "abc" else "a"
        fn = SEGMENT_DEFAULT.get(seg, "process-flow.png")
    p = IMG_DIR / fn
    if p.exists():
        return p
    # 폴백: 아무 인포그래픽
    any_info = _imgs(IMG_DIR)
    return any_info[0] if any_info else None


def _load_rot() -> int:
    try:
        return int(json.loads(ROT_FILE.read_text()).get("i", 0))
    except Exception:
        return 0


def _save_rot(i: int) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    ROT_FILE.write_text(json.dumps({"i": i}))


def pick_images(draft_path, n: int) -> tuple[list[Path], list[Path]]:
    """(삽입할 이미지 경로들, 소진한 인박스 사진들) 반환. n = 초안의 이미지 자리 수."""
    n = max(int(n), 1)
    code = draft_code(draft_path)
    picks: list[Path] = []
    used_inbox: list[Path] = []

    # 1) 주제 매칭 인포그래픽(대표) 1장
    info = _infographic_for(code)
    if info:
        picks.append(info)

    # 2) 인박스 새 사진 우선 소진
    for p in _imgs(INBOX_DIR):
        if len(picks) >= n:
            break
        picks.append(p)
        used_inbox.append(p)

    # 3) 사진 풀 순환 재활용
    pool = [p for p in _imgs(PHOTO_DIR) if p.parent == PHOTO_DIR]
    if pool and len(picks) < n:
        start = _load_rot()
        steps = 0
        i = start
        while len(picks) < n and steps < len(pool):
            picks.append(pool[i % len(pool)])
            i += 1
            steps += 1
        _save_rot(i)

    return picks[:n], used_inbox


def mark_inbox_used(paths: list[Path]) -> None:
    if not paths:
        return
    USED_DIR.mkdir(parents=True, exist_ok=True)
    for p in paths:
        try:
            Path(p).rename(USED_DIR / Path(p).name)
        except Exception:
            pass
