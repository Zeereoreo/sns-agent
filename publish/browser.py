"""네이버 자동화용 브라우저 세션.

핵심 원칙: 로그인은 자동화하지 않는다(탐지 위험 최상위).
사람이 최초 1회 로그인한 **영속 프로필(user_data/)** 을 재사용한다.
가능하면 실제 Chrome 채널을 써서 탐지를 줄이고, 없으면 번들 Chromium으로 폴백한다.

★프로필은 **한 번에 한 프로세스만** 열어야 한다(2026-07-30).
스케줄러 발행과 다른 스크립트가 같은 user_data 를 동시에 열면 크로미움이 프로필을
공유하지 못하고, 나중에 닫히는 쪽이 **쿠키 DB 를 빈 상태로 덮어써 로그인이 날아간다.**
실제로 13:30 발행과 dry-run 이 겹친 직후 NID_AUT/NID_SES 가 통째로 사라졌고
좀비 chrome 8개가 프로필을 잡고 있었다. 그래서 파일 잠금으로 직렬화한다.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
USER_DATA_DIR = ROOT / "user_data"          # .gitignore 됨 (로그인 세션 보관)
DEBUG_DIR = ROOT / "drafts" / "_debug"       # 단계별 스크린샷
LOCK_FILE = ROOT / "data" / ".browser-lock"
LOCK_WAIT_SEC = 180          # 다른 프로세스가 쓰는 중이면 이만큼 기다린다
LOCK_STALE_SEC = 900         # 이보다 오래된 잠금은 죽은 프로세스로 보고 회수


def _lock_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:                       # 신호 0 = 존재 확인만
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def acquire_profile_lock(wait_sec: int = LOCK_WAIT_SEC) -> bool:
    """프로필 사용권을 잡는다. 못 잡으면 False(호출측이 포기하거나 나중에 재시도)."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wait_sec
    while True:
        try:
            fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()} {time.time():.0f}".encode())
            os.close(fd)
            return True
        except FileExistsError:
            pass
        # 잠금 주인이 죽었거나 너무 오래됐으면 회수
        try:
            raw = LOCK_FILE.read_text(encoding="utf-8").split()
            pid, ts = int(raw[0]), float(raw[1])
        except Exception:
            pid, ts = -1, 0.0
        if not _lock_alive(pid) or (time.time() - ts) > LOCK_STALE_SEC:
            try:
                LOCK_FILE.unlink()
                continue
            except OSError:
                pass
        if time.monotonic() >= deadline:
            print(f"[브라우저] 프로필이 다른 프로세스(pid {pid})에서 사용 중 — 건너뜁니다.")
            return False
        time.sleep(3)


def release_profile_lock() -> None:
    try:
        LOCK_FILE.unlink()
    except OSError:
        pass


def launch_context(p, headed: bool = True):
    """영속 컨텍스트를 연다. (p = sync_playwright() 인스턴스)

    프로필 잠금을 잡고 열며, 컨텍스트를 닫을 때 잠금을 푼다.
    잠금을 못 잡으면 **프로필을 건드리지 않고** RuntimeError 로 알린다 —
    억지로 열면 쿠키가 날아간다.
    """
    if not acquire_profile_lock():
        raise RuntimeError("브라우저 프로필 사용 중(동시 실행). 잠시 후 다시 실행하세요.")

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
        try:
            ctx = p.chromium.launch_persistent_context(channel="chrome", **common)
        except Exception:
            ctx = p.chromium.launch_persistent_context(**common)
    except Exception:
        release_profile_lock()
        raise
    # 클립보드 붙여넣기(이미지 자동 삽입)용 권한
    try:
        ctx.grant_permissions(["clipboard-read", "clipboard-write"],
                              origin="https://blog.naver.com")
    except Exception:
        pass

    # close() 될 때 잠금을 반드시 푼다(호출부가 여기저기라 래핑이 가장 안전하다)
    _orig_close = ctx.close

    def _close(*a, **kw):
        try:
            return _orig_close(*a, **kw)
        finally:
            release_profile_lock()

    ctx.close = _close
    return ctx
