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

# 한국어 Windows 콘솔(cp949) 출력 깨짐/크래시 방지
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
from publish.browser import launch_context, DEBUG_DIR  # noqa: E402
from publish.draft_parser import parse_draft  # noqa: E402

# ── 선택자 (실제 스마트에디터 화면에 맞춰 보정) ──────────────────
WRITE_URL = "https://blog.naver.com/{blog_id}/postwrite"
# 스마트에디터 ONE (iframe 없음, 페이지에 직접). 2026-07 실사 보정.
SEL = {
    "recover_cancel": ".se-popup-alert-confirm .se-popup-button-cancel, .se-popup-button-cancel",  # 복구 팝업(취소=새로작성)
    "title": ".se-section-documentTitle",          # 제목 영역(클릭 후 타이핑)
    "body": ".se-section-text",                    # 본문 영역
    "img_button": ".se-image-toolbar-button, button[data-name='image']",
    "img_input": "input[type='file']",
    "publish_open": "[data-click-area='tpb.publish']",   # 상단 초록 '발행' 버튼
    "tag_input": "input#tag-input, input.tag_input, input[placeholder*='태그']",
    "publish_confirm": "[data-click-area='tpb*i.publish'], .confirm_btn__WEaBq",  # 레이어 최종 발행
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
        # '로그인 상태 유지' 자동 체크 (안 하면 NID_AUT/NID_SES가 세션쿠키라 저장 안 됨)
        page.wait_for_timeout(1000)
        for sel in ("label[for='keep']", "#keep", "text=로그인 상태 유지"):
            try:
                page.locator(sel).first.click(timeout=2000)
                break
            except Exception:
                continue
        print("=" * 60)
        print(" 열린 크롬 창에서 네이버(made-us2)에 직접 로그인하세요. (2차 인증 포함)")
        print(" ※ '로그인 상태 유지'가 켜져 있는지 확인하세요(자동으로 켜뒀습니다).")
        print(" 로그인되면 자동 감지해 세션을 저장하고 창을 닫습니다. (최대 6분 대기)")
        print(" 로그인 끝날 때까지 창을 닫지 마세요.")
        print("=" * 60)
        saved = False
        for _ in range(180):  # 180 x 2s = 6분
            try:
                names = {c.get("name") for c in ctx.cookies()
                         if "naver" in (c.get("domain") or "")}
            except Exception:
                names = set()
            if names & {"NID_AUT", "NID_SES"}:
                saved = True
                break
            try:
                page.wait_for_timeout(2000)
            except Exception:
                break
        if saved:
            page.wait_for_timeout(1500)  # 쿠키 flush 여유
        ctx.close()
        # 저장 검증: 새 컨텍스트로 열어 NID_AUT/NID_SES 가 디스크에 남았는지 확인
        persisted = False
        if saved:
            c2 = launch_context(p, headed=False)
            try:
                names = {c.get("name") for c in c2.cookies()
                         if "naver" in (c.get("domain") or "")}
                persisted = bool({"NID_AUT", "NID_SES"} & names)
            finally:
                c2.close()
    if persisted:
        print("[OK] 로그인 세션 저장·검증 완료 (user_data/). publish 실행 가능.")
    elif saved:
        print("[!] 로그인은 됐으나 세션 미저장 - 로그인 창의 '로그인 상태 유지'를 켜고 다시 로그인하세요.")
    else:
        print("[!] 로그인 감지 안 됨 - 다시 실행해 로그인해 주세요.")


# ── 게시 ────────────────────────────────────────────────────────
def publish(draft_path: str, image_dir: str | None = None,
            dry_run: bool = True, headed: bool = True, review: bool = False) -> None:
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

        # 로그인 안 됐으면 중단
        if "login" in page.url or "nidlogin" in page.url:
            print("‼ 로그인 세션이 없습니다. 먼저 `python -m publish.naver login` 실행하세요.")
            ctx.close()
            return

        # '작성 중 글 복구' 확인 팝업 먼저 닫기(취소=새로 작성) — 클릭을 가로막음
        page.wait_for_timeout(1200)
        try:
            page.locator(SEL["recover_cancel"]).first.click(timeout=3000)
            page.wait_for_timeout(600)
        except Exception:
            pass

        # 도움말/툴팁 오버레이 닫기
        for _ in range(3):
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
        _pause()

        # 제목 입력
        try:
            page.locator(SEL["title"]).first.click()
            _pause()
            page.keyboard.type(data["title"], delay=random.randint(30, 90))
            _shot(page, "02_title")
        except Exception as e:
            print("제목 입력 실패(선택자 보정 필요):", e)
            _shot(page, "02_title_FAIL")

        # 본문 입력
        try:
            page.locator(SEL["body"]).first.click()
            _pause()
            img_i = 0
            for blk in data["blocks"]:
                if blk["kind"] == "image":
                    if image_dir and img_i < len(images):
                        _insert_image(page, images[img_i])
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

        if review:
            # 발행 설정 패널 닫기(이미지는 본문에 직접 넣어야 하므로)
            for _ in range(2):
                page.keyboard.press("Escape")
                page.wait_for_timeout(300)
            print("=" * 60)
            print(" 제목·본문·태그 입력 완료! 이 창에서 직접:")
            print("  1) 원하는 이미지를 본문에 드래그드롭으로 넣고")
            print("  2) 검토 후 오른쪽 위 초록 '발행' 클릭 (태그는 이미 입력됨)")
            print(" 완료 후 창을 닫으면 종료됩니다. (최대 15분 대기)")
            print("=" * 60)
            try:
                page.wait_for_event("close", timeout=900000)
            except Exception:
                pass
            try:
                ctx.close()
            except Exception:
                pass
            return

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


def _insert_image(page, img_path: Path):
    """이미지 1장 삽입 (파일 선택 방식)."""
    try:
        with page.expect_file_chooser(timeout=5000) as fc:
            page.locator(SEL["img_button"]).first.click()
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
    pp.add_argument("--review", action="store_true",
                    help="제목·본문·태그 채우고 멈춤 → 사람이 이미지 넣고 발행")
    a = ap.parse_args()

    if a.cmd == "login":
        login()
    else:
        publish(a.draft, a.images, dry_run=a.dry_run,
                headed=(a.review or not a.headless), review=a.review)


if __name__ == "__main__":
    main()
