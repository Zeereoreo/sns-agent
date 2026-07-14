"""중앙 설정: .env에서 비밀값을 로드하고 전역 설정을 노출한다.

API 키 관리 방향:
- 실제 키는 .env 파일에만 두고 git에 올리지 않는다(.gitignore).
- .env.example 은 템플릿으로만 커밋한다.
- 시스템 환경변수(ANTHROPIC_API_KEY)가 있으면 그것도 자동 사용된다.
"""
from __future__ import annotations

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

# ----- 경로 -----
DATA_DIR = ROOT / "data"
DRAFTS_DIR = ROOT / "drafts"
STORAGE_STATE = ROOT / "storage_state.json"  # 네이버 로그인 세션


def require_api_key() -> str:
    """API 키가 없으면 친절한 오류를 낸다."""
    if not ANTHROPIC_API_KEY:
        raise RuntimeError(
            "ANTHROPIC_API_KEY가 설정되지 않았습니다. "
            ".env 파일을 만들고 키를 입력하세요 (.env.example 참고)."
        )
    return ANTHROPIC_API_KEY
