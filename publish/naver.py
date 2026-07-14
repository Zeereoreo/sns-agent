"""네이버 블로그 게시 자동화 (스마트에디터 ONE).

사용법:
  # 1) 최초 1회: 사람이 직접 로그인 (비밀번호는 본인이 입력, 스크립트는 대기만)
  python -m publish.naver login

  # 2) 게시(먼저 dry-run: 발행 직전까지만, 스크린샷 확인)
  python -m publish.naver publish --draft drafts/a02_led-signature-picket-price.md \
      --images drafts/images --dry-run

  # 3) 실제 발행 (dry-run 빼기)
  python -m publish.naver publish --draft ... --images ...

주의:
- 로그인 자동화 금지(탐지 위험). 영속 프로필(user_data/) 재사용.
- 스마트에디터 DOM은 자주 바뀜 → 아래 SEL 선택자는 실제 화면에 맞춰 보정 필요(CALIBRATE).
- 각 단계 스크린샷이 drafts/_debug/ 에 저장됨 → 실패 시 원인 파악용.
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
from publish.browser import launch_context, DEBUG_DIR  # noqa: E402
from publish.draft_parser import parse_draft  # noqa: E402

# ── 선택자 (실제 스마트에디터 화면에 맞춰 보정) ──────────────────
WRITE_URL = "https://blog.naver.com/{blog_id}/postwrite"
SEL = {
    "editor_frame": "#mainFrame",                 # 스마트에디터 iframe
    "recover_cancel": "button:has-text('취소')",   # '작성 중 글 복구' 팝업 취소
    "title": ".se-section-documentTitle .se-text-paragraph",  # 제목 영역
    "body": ".se-section-text .se-text-paragraph",            # 본문 영역
    "img_button": "button.se-image-toolbar-button, button[data-name='image']",
    "img_input": "input[type='file']",
    "publish_open": "button:has-text('발행')",     # 상단 발행 패널 열기
    "tag_input": "#tag-input, input.tag_input",    # 태그 입력
    "publish_confirm": ".layer_btn_area button:has-text('발행'), button.confirm_btn",
}


def _pause(a=0.4, b=1.1):
    time.sleep(random.uniform(a, b))


def _shot(page, name: str):
    try:
        page.screenshot(path=str(DEBUG_DIR / f"{name}.png"))
    except Exception:
        pass


# ── 최초 1회 수동 로그인 ─────────────────────────────────────────
def login():
    with sync_playwright() as p:
        ctx = launch_context(p, headed=True)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://nid.naver.com/nidlogin.login")
        print("=" * 60)
        print(" 브라우저에서 네이버에 '직접' 로그인하세요 (2차 인증 포함).")
        print(" 로그인 완료 후, 이 창은 자동으로 닫히지 않습니다.")
        print(" 완료되면 이 터미널에서 Enter 를 누르세요.")
        print("=" * 60)
        try:
            input()
        except EOFError:
            page.wait_for_timeout(120000)  # 비대화형이면 2분 대기
        ctx.close()
        print("세션 저장 완료 (user_data/). 이제 publish 를 실행할 수 있어요.")


# ── 게시 ────────────────────────────────────────────────────────
def publish(draft_path: str, image_dir: str | None = None,
            dry_run: bool = True, headed: bool = True) -> None:
    blog_id = config.NAVER_BLOG_ID or "made-us"
    data = parse_draft(draft_path)
    images = []
    if image_dir:
        images = sorted(Path(image_dir).glob("*.png")) + sorted(Path(image_dir).glob("*.jpg"))

    print(f"[게시 준비] 제목: {data['title']}")
    print(f"  블록 {len(data['blocks'])}개 / 태그 {len(data['tags'])}개 / 이미지풀 {len(images)}장 / dry_run={dry_run}")

    with sync_playwright() as p:
        ctx = launch_context(p, headed=headed)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(WRITE_URL.format(blog_id=blog_id))
        page.wait_for_timeout(3000)
        _shot(page, "01_write_opened")

        frame = page.frame_locator(SEL["editor_frame"])

        # 로그인 안 됐으면 중단
        if "login" in page.url or "nidlogin" in page.url:
            print("‼ 로그인 세션이 없습니다. 먼저 `python -m publish.naver login` 실행하세요.")
            ctx.close()
            return

        # '작성 중 글 복구' 팝업 취소
        try:
            frame.locator(SEL["recover_cancel"]).first.click(timeout=3000)
        except Exception:
            pass
        _pause()

        # 제목 입력
        try:
            frame.locator(SEL["title"]).first.click()
            _pause()
            page.keyboard.type(data["title"], delay=random.randint(30, 90))
            _shot(page, "02_title")
        except Exception as e:
            print("제목 입력 실패(선택자 보정 필요):", e)
            _shot(page, "02_title_FAIL")

        # 본문 입력
        try:
            frame.locator(SEL["body"]).first.click()
            _pause()
            img_i = 0
            for blk in data["blocks"]:
                if blk["kind"] == "image":
                    if image_dir and img_i < len(images):
                        _insert_image(page, frame, images[img_i])
                        img_i += 1
                    continue
                page.keyboard.type(blk["text"], delay=random.randint(15, 45))
                page.keyboard.press("Enter")
                page.keyboard.press("Enter")
                _pause(0.2, 0.6)
            _shot(page, "03_body")
        except Exception as e:
            print("본문 입력 실패(선택자 보정 필요):", e)
            _shot(page, "03_body_FAIL")

        # 발행 패널 → 태그 → 발행
        try:
            page.locator(SEL["publish_open"]).first.click(timeout=5000)
            _pause()
            _shot(page, "04_publish_panel")
            for tag in data["tags"]:
                try:
                    page.locator(SEL["tag_input"]).first.fill(tag)
                    page.keyboard.press("Enter")
                    _pause(0.1, 0.3)
                except Exception:
                    break
            _shot(page, "05_tags")
        except Exception as e:
            print("발행 패널/태그 실패(선택자 보정 필요):", e)
            _shot(page, "04_publish_FAIL")

        if dry_run:
            print("✅ dry-run: 발행 직전까지 진행. drafts/_debug/ 스크린샷을 확인하세요.")
            page.wait_for_timeout(2000)
        else:
            try:
                page.locator(SEL["publish_confirm"]).first.click(timeout=5000)
                page.wait_for_timeout(3000)
                _shot(page, "06_published")
                print("🚀 발행 완료.")
            except Exception as e:
                print("발행 버튼 실패(선택자 보정 필요):", e)
                _shot(page, "06_publish_FAIL")

        ctx.close()


def _insert_image(page, frame, img_path: Path):
    """이미지 1장 삽입 (파일 선택 방식). 선택자 보정 필요."""
    try:
        with page.expect_file_chooser(timeout=5000) as fc:
            frame.locator(SEL["img_button"]).first.click()
        fc.value.set_files(str(img_path))
        page.wait_for_timeout(1500)
    except Exception as e:
        print(f"  이미지 삽입 실패({img_path.name}):", e)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login")
    pp = sub.add_parser("publish")
    pp.add_argument("--draft", required=True)
    pp.add_argument("--images", default=None)
    pp.add_argument("--dry-run", action="store_true")
    pp.add_argument("--headless", action="store_true")
    a = ap.parse_args()

    if a.cmd == "login":
        login()
    else:
        publish(a.draft, a.images, dry_run=a.dry_run, headed=not a.headless)


if __name__ == "__main__":
    main()
