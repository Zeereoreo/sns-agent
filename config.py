"""중앙 설정: .env에서 비밀값을 로드하고 전역 설정을 노출한다.

API 키 관리 방향:
- 실제 키는 .env 파일에만 두고 git에 올리지 않는다(.gitignore).
- .env.example 은 템플릿으로만 커밋한다.
- 시스템 환경변수(ANTHROPIC_API_KEY)가 있으면 그것도 자동 사용된다.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

# ----- Claude API -----
ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL: str = os.environ.get("CLAUDE_MODEL", "claude-opus-4-8")

# ----- 네이버 -----
NAVER_BLOG_ID: str = os.environ.get("NAVER_BLOG_ID", "")

# ----- 게시 안전장치 -----
MAX_POSTS_PER_DAY: int = int(os.environ.get("MAX_POSTS_PER_DAY", "1"))
PUBLISH_WINDOW_START: str = os.environ.get("PUBLISH_WINDOW_START", "08:00")
PUBLISH_WINDOW_END: str = os.environ.get("PUBLISH_WINDOW_END", "10:00")

# ----- 블로그 카테고리 (발행 레이어에서 선택; 블로그의 카테고리명과 정확히 일치해야 함) -----
SEGMENT_CATEGORY = {
    "a": "방송용 피켓·전광판",        # BJ/스트리머 방송 소품
    "b": "간판·네온사인·클럽 LED",   # 클럽/매장
    "c": "간판·네온사인·클럽 LED",
}

# ----- 경로 -----
DATA_DIR = ROOT / "data"
DRAFTS_DIR = ROOT / "drafts"
STORAGE_STATE = ROOT / "storage_state.json"  # 네이버 로그인 세션

# ----- 주요 강조 포인트 (운영자가 대시보드에서 편집) -----
EMPHASIS_FILE = DATA_DIR / "emphasis.json"


def load_emphasis() -> list[str]:
    """글마다 삽입할 핵심 셀링포인트 목록. 없으면 빈 리스트."""
    try:
        d = json.loads(EMPHASIS_FILE.read_text(encoding="utf-8"))
        return [s.strip() for s in d.get("points", []) if s and s.strip()][:6]
    except Exception:
        return []


def save_emphasis(points: list[str]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    clean = [s.strip() for s in points if s and s.strip()][:6]
    tmp = EMPHASIS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"points": clean}, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, EMPHASIS_FILE)


def require_api_key() -> str:
    """API 키가 없으면 친절한 오류를 낸다."""
    if not ANTHROPIC_API_KEY:
        raise RuntimeError(
            "ANTHROPIC_API_KEY가 설정되지 않았습니다. "
            ".env 파일을 만들고 키를 입력하세요 (.env.example 참고)."
        )
    return ANTHROPIC_API_KEY
