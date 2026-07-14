"""네이버 자동화용 브라우저 세션.

핵심 원칙: 로그인은 자동화하지 않는다(탐지 위험 최상위).
사람이 최초 1회 로그인한 **영속 프로필(user_data/)** 을 재사용한다.
가능하면 실제 Chrome 채널을 써서 탐지를 줄이고, 없으면 번들 Chromium으로 폴백한다.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
USER_DATA_DIR = ROOT / "user_data"          # .gitignore 됨 (로그인 세션 보관)
DEBUG_DIR = ROOT / "drafts" / "_debug"       # 단계별 스크린샷


def launch_context(p, headed: bool = True):
    """영속 컨텍스트를 연다. (p = sync_playwright() 인스턴스)"""
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)

    common = dict(
        user_data_dir=str(USER_DATA_DIR),
        headless=not headed,
        args=["--disable-blink-features=AutomationControlled"],
        viewport={"width": 1280, "height": 900},
        locale="ko-KR",
    )
    # 실제 Chrome 우선(탐지 회피), 미설치 시 번들 Chromium
    try:
        return p.chromium.launch_persistent_context(channel="chrome", **common)
    except Exception:
        return p.chromium.launch_persistent_context(**common)
