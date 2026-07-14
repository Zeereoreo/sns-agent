"""품질 게이트 — 초안이 게시 기준을 만족하는지 자동 검사한다.

검사 항목(Phase 1에서 구현):
- 글자수 1,500자 이상
- 중복도(기존 게시물과의 유사도) 상한
- 홍보밀도(홍보성 문장 비율) 상한  <- 저품질 회피 핵심
- 금칙어 / 가독성
미달 시 폐기하고 재생성을 요청한다.
"""
from __future__ import annotations


def check(draft: dict) -> tuple[bool, list[str]]:
    """(통과여부, 실패사유목록) 반환."""
    raise NotImplementedError("Phase 1에서 구현 예정")
