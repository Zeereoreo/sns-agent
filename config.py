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


# ----- 운영자가 직접 넣는 타깃 키워드 (대시보드 '키워드' 탭) -----
# 자동완성 수요 프록시는 롱테일을 못 잡는다(실측 유입어 '휴대용led응원피켓'을 0으로 봤다).
# 사람이 아는 키워드를 직접 넣을 통로가 필요하다. 여기 넣은 키워드는
# 수요 측정·순위 추적·경쟁 스캔 대상에 들어가고, 성장엔진이 우선 타깃으로 가산한다.
KEYWORDS_FILE = DATA_DIR / "keywords_user.json"
KEYWORDS_MAX = 30


def load_keywords() -> list[str]:
    """운영자가 추가한 타깃 키워드. 없으면 빈 리스트."""
    try:
        d = json.loads(KEYWORDS_FILE.read_text(encoding="utf-8"))
        return [s.strip() for s in d.get("keywords", []) if s and s.strip()][:KEYWORDS_MAX]
    except Exception:
        return []


def save_keywords(keywords: list[str]) -> None:
    """중복·공백을 정리해 저장(입력 순서 유지)."""
    DATA_DIR.mkdir(exist_ok=True)
    clean: list[str] = []
    for s in keywords:
        s = " ".join((s or "").split())
        if s and s not in clean:
            clean.append(s)
    tmp = KEYWORDS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"keywords": clean[:KEYWORDS_MAX]}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    os.replace(tmp, KEYWORDS_FILE)


def require_api_key() -> str:
    """API 키가 없으면 친절한 오류를 낸다."""
    if not ANTHROPIC_API_KEY:
        raise RuntimeError(
            "ANTHROPIC_API_KEY가 설정되지 않았습니다. "
            ".env 파일을 만들고 키를 입력하세요 (.env.example 참고)."
        )
    return ANTHROPIC_API_KEY
