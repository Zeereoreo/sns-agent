"""네이버 블로그 게시 — Playwright로 스마트에디터 ONE을 직접 조작한다.

핵심 원칙(계정 정지/저품질 회피):
- 로그인 자동화 금지. 최초 1회 사람이 로그인한 세션(config.STORAGE_STATE)을 재사용한다.
- HTML 주입 금지. 실제 타이핑을 시뮬레이션한다(에디터가 주입을 거부함).
- 하루 1건, 오전 8~10시 랜덤 시각, 사람 같은 입력 속도.
- 기기 2차인증이 뜨면 자동화를 멈추고 사람에게 알린다.

Phase 2에서 구현.
"""
from __future__ import annotations


def save_login_session() -> None:
    """최초 1회: 브라우저를 띄워 사람이 로그인 -> storage_state 저장."""
    raise NotImplementedError("Phase 2에서 구현 예정")


def publish(draft: dict, image_paths: list[str]) -> str:
    """초안+이미지를 게시하고 게시글 URL을 반환한다."""
    raise NotImplementedError("Phase 2에서 구현 예정")
