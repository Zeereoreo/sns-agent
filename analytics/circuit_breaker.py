"""서킷브레이커 — 저품질/제재 신호를 감지하면 게시를 자동 중단하고 알린다.

감지 신호(Phase 3에서 구현):
- 검색 노출 순위가 급락(예: 21위 밖으로 밀림 = '3페이지 블로그')
- 방문자 급감 / 노출 소멸
신호 발생 시 scheduler가 게시를 멈추도록 상태 플래그를 세운다.
"""
from __future__ import annotations


def is_tripped() -> bool:
    """게시를 멈춰야 하는 상태이면 True."""
    raise NotImplementedError("Phase 3에서 구현 예정")
