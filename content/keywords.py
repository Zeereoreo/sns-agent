"""키워드 풀 — 사업 아이템 연관 롱테일 키워드를 관리하고 우선순위를 매긴다.

Phase 0에서 사용자가 제공하는 '사업 아이템 예시'를 바탕으로 시드 키워드를 구성하고,
성과 데이터(analytics.feedback)로 우선순위를 갱신한다.
"""
from __future__ import annotations


def next_keyword() -> str:
    """다음에 글을 쓸 키워드를 우선순위 기준으로 선택한다."""
    raise NotImplementedError("Phase 1에서 구현 예정")
