"""피드백 루프 — 성과가 좋은 키워드/포맷의 가중치를 올려 다음 주제 선정에 반영한다.

collect.py가 쌓은 지표를 읽어 content.keywords의 우선순위를 갱신한다. Phase 3에서 구현.
"""
from __future__ import annotations


def update_weights() -> None:
    """성과 기반으로 키워드/포맷 가중치를 갱신한다."""
    raise NotImplementedError("Phase 3에서 구현 예정")
