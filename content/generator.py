"""초안 생성 — 키워드를 받아 SEO 구조의 블로그 글 초안을 만든다 (Claude API).

Phase 1에서 구현. config.require_api_key()로 키를 가져와 anthropic 클라이언트 사용.
반환 구조 예: {"title": str, "body": str, "tags": list[str], "images_needed": int}
"""
from __future__ import annotations


def generate_draft(keyword: str) -> dict:
    """키워드 -> 초안 dict. (제목/본문 1,500자+/태그/필요 이미지 수)"""
    raise NotImplementedError("Phase 1에서 구현 예정")
