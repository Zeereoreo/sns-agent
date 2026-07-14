"""성과 수집 — 방문자/유입키워드/검색노출순위를 모아 SQLite에 저장한다.

데이터 소스(Phase 3에서 구현): 네이버 블로그 통계 + (선택)Google Analytics.
저장 위치: config.DATA_DIR / 'stats.sqlite'
"""
from __future__ import annotations


def collect() -> None:
    """오늘자 지표를 수집해 저장한다."""
    raise NotImplementedError("Phase 3에서 구현 예정")
