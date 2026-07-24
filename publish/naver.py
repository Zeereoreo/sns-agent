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
import base64
import random
import re
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
    "category_open": "[data-click-area='tpb*i.category']",  # 발행 레이어 카테고리 드롭다운
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
def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def _verify_published(page, blog_id: str, title: str) -> str | None:
    """방금 쓴 글이 '진짜' 블로그에 올라갔는지 확인하고 URL을 돌려준다.

    발행 버튼 클릭 성공 != 게시 성공. 홈 화면 본문 substring 매칭은 렌더 지연(오탐→중복발행)과
    제목 앞부분 충돌(오검증)에 취약하므로, 게시글 목록 API로 최신 글 제목을 정확히 대조한다.
    최대 4회 재시도(게시 반영에 몇 초 걸릴 수 있음).
    """
    want = _norm(title)
    if not want:
        return None
    home = f"https://m.blog.naver.com/{blog_id}"
    api = (f"https://m.blog.naver.com/api/blogs/{blog_id}/post-list"
           f"?categoryNo=0&itemCount=10&page=1&userId={blog_id}")
    try:                       # 컨텍스트/쿠키 확보(이게 없으면 API 403)
        page.goto(home, timeout=20000)
        page.wait_for_timeout(1500)
    except Exception:
        pass
    for attempt in range(4):
        try:
            r = page.request.get(api, headers={"referer": home}, timeout=15000)
            items = (r.json() or {}).get("result", {}).get("items", []) if r.ok else []
            for it in items:
                t = _norm(it.get("titleWithInspectMessage") or it.get("title") or "")
                # 정규화 제목이 서로 충분히 겹치면 동일 글로 본다(부분 잘림 대비 양방향 포함).
                if t and (t == want or want in t or t in want):
                    no = it.get("logNo")
                    return f"https://m.blog.naver.com/{blog_id}/{no}" if no else \
                        f"https://m.blog.naver.com/{blog_id}"
        except Exception as e:
            print(f"  발행 확인 API 오류(시도 {attempt + 1}):", e)
        page.wait_for_timeout(2500)

    # 폴백: 홈 화면 본문 대조(API 실패 시)
    try:
        page.goto(f"https://m.blog.naver.com/{blog_id}", timeout=20000)
        page.wait_for_timeout(2500)
        if want[:20] and want[:20] in _norm(page.inner_text("body")):
            return page.url
    except Exception as e:
        print("  발행 확인 폴백 오류:", e)
    return None


def publish(draft_path: str, image_dir: str | None = None,
            image_paths: list | None = None,
            dry_run: bool = True, headed: bool = True, review: bool = False,
            category: str | None = None) -> dict:
    """결과를 dict 로 돌려준다: {ok, reason, images_inserted, url, title}.

    ok=True 는 '블로그 목록에서 글을 확인함' 을 뜻한다(클릭 성공이 아니라).
    """
    blog_id = config.NAVER_BLOG_ID or "made-us"
    data = parse_draft(draft_path)
    result = {"ok": False, "reason": None, "images_inserted": 0,
              "url": None, "title": data["title"]}
    if image_paths is not None:
        images = [Path(p) for p in image_paths]
    elif image_dir:
        images = sorted(Path(image_dir).glob("*.png")) + sorted(Path(image_dir).glob("*.jpg"))
    else:
        images = []

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
            result["reason"] = "session_expired"
            return result

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
            ctx.close()   # 제목 없는 글을 올리느니 중단한다
            result["reason"] = "title_failed"
            return result

        # 본문 입력
        emphasis = config.load_emphasis()   # 운영자 지정 핵심 강조 포인트
        emphasis_done = not emphasis
        try:
            page.locator(SEL["body"]).first.click()
            _pause()
            img_i = 0
            for blk in data["blocks"]:
                if blk["kind"] == "image":
                    # images 는 image_paths/image_dir 어느 쪽으로 받았든 채워져 있다.
                    # (예전엔 image_dir 을 조건으로 봐서 스케줄러 경로의 이미지가 통째로 누락됐음)
                    if img_i < len(images):
                        ok_img = _insert_image(page, images[img_i])
                        img_i += 1
                        # 캡션은 이미지가 실제로 삽입됐을 때만. (실패 시 고아 '▲ 캡션' 방지)
                        cap = (blk.get("alt") or "").strip()
                        if ok_img and cap:
                            page.keyboard.type(f"▲ {cap}", delay=random.randint(15, 40))
                            page.keyboard.press("Enter")
                            _pause(0.2, 0.5)
                    continue
                if blk["kind"] == "heading":
                    # 소제목은 굵게(가독성 + 강조 신호). Ctrl+B 토글로 감싼다.
                    page.keyboard.press("Control+B")
                    page.keyboard.type(blk["text"], delay=random.randint(15, 45))
                    page.keyboard.press("Control+B")
                    page.keyboard.press("Enter")
                    _pause(0.2, 0.5)
                    continue
                # 강조 포인트: CTA(👉) 직전에 굵게 삽입
                if not emphasis_done and blk["text"].startswith("👉"):
                    for pt in emphasis:
                        page.keyboard.press("Control+B")
                        page.keyboard.type(f"✅ {pt}", delay=random.randint(15, 40))
                        page.keyboard.press("Control+B")
                        page.keyboard.press("Enter")
                        _pause(0.15, 0.4)
                    page.keyboard.press("Enter")
                    emphasis_done = True
                page.keyboard.type(blk["text"], delay=random.randint(15, 45))
                page.keyboard.press("Enter")
                page.keyboard.press("Enter")
                _pause(0.2, 0.6)
            _shot(page, "03_body")
        except Exception as e:
            print("본문 입력 실패(선택자 보정 필요):", e)
            _shot(page, "03_body_FAIL")
            ctx.close()   # 본문 없는 글을 올리느니 중단한다
            result["reason"] = "body_failed"
            return result

        # 시도 횟수가 아니라 에디터에 실제로 들어간 이미지 수를 확인한다.
        try:
            inserted = page.locator(".se-component.se-image").count()
        except Exception:
            inserted = -1
        result["images_inserted"] = max(inserted, 0)
        if images and inserted == 0 and not dry_run:
            # 이미지가 계획됐는데 한 장도 안 들어감 = 반쪽 글. 발행하지 말고 중단(큐에 남겨 재시도).
            print(f"  ⚠ 이미지 {len(images)}장 계획했으나 0장 삽입 — 발행 중단(재시도).")
            _shot(page, "03_body_NOIMG")
            ctx.close()
            result["reason"] = "images_failed"
            return result
        if images and inserted == 0:
            print(f"  ⚠ 이미지 {len(images)}장을 넣으려 했으나 본문에 0장 — 삽입 실패")
        elif inserted >= 0 and inserted < len(images):
            print(f"  ⚠ 이미지 {len(images)}장 중 {inserted}장만 삽입됨")
        else:
            print(f"  이미지 삽입 확인: {inserted}장")

        # 발행 패널 → 태그 → 발행
        try:
            page.locator(SEL["publish_open"]).first.click(timeout=5000)
            _pause()
            _shot(page, "04_publish_panel")
            # 카테고리 선택(세그먼트→게시판). 실패해도 발행은 계속(기본 카테고리).
            if category:
                try:
                    btn = page.locator(SEL["category_open"]).first
                    if (btn.inner_text() or "").strip() != category:
                        btn.click(timeout=3000)
                        _pause(0.3, 0.7)
                        page.locator("label", has_text=category).first.click(timeout=3000)
                        _pause(0.2, 0.5)
                        now = (page.locator(SEL["category_open"]).first.inner_text() or "").strip()
                        if now == category:
                            print(f"  카테고리: {category}")
                        else:
                            print(f"  ⚠ 카테고리 선택 확인 실패(현재 '{now}') — 기본 카테고리로 발행")
                except Exception as e:
                    print("  ⚠ 카테고리 선택 실패(기본 카테고리로 발행):", e)
            tag_ok = 0
            for tag in data["tags"]:
                try:
                    page.locator(SEL["tag_input"]).first.fill(tag)
                    page.keyboard.press("Enter")
                    _pause(0.1, 0.3)
                    tag_ok += 1
                except Exception:
                    continue   # 한 태그 실패로 나머지를 버리지 않는다
            result["tags_added"] = tag_ok
            if data["tags"] and tag_ok < len(data["tags"]):
                print(f"  ⚠ 태그 {len(data['tags'])}개 중 {tag_ok}개만 입력됨")
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
            result["reason"] = "review"
            return result

        if dry_run:
            print("✅ dry-run: 발행 직전까지 진행. drafts/_debug/ 스크린샷을 확인하세요.")
            page.wait_for_timeout(2000)
            result["reason"] = "dry_run"
        else:
            try:
                page.locator(SEL["publish_confirm"]).first.click(timeout=5000)
                page.wait_for_timeout(4000)
                _shot(page, "06_published")
            except Exception as e:
                print("발행 버튼 실패(선택자 보정 필요):", e)
                _shot(page, "06_publish_FAIL")
                result["reason"] = "publish_click_failed"

            # 클릭 성공 여부와 무관하게, 블로그에 실제로 떴는지로 판정한다.
            url = _verify_published(page, blog_id, data["title"])
            if url:
                result["ok"] = True
                result["url"] = url
                print(f"🚀 발행 완료(확인됨). 이미지 {result['images_inserted']}장")
            else:
                result["reason"] = result["reason"] or "not_found_after_publish"
                print("‼ 발행했지만 블로그 목록에서 글을 찾지 못했습니다:", result["reason"])

        ctx.close()
        return result


def _insert_image(page, img_path: Path) -> bool:
    """클립보드 붙여넣기로 이미지 삽입. 실제로 컴포넌트가 늘었는지 확인해 성공/실패를 반환한다.

    커서는 본문 입력 흐름상 이미 본문에 있으므로 재클릭하지 않고 현재 위치에 붙여넣는다.
    """
    try:
        before = page.locator(".se-component.se-image").count()
    except Exception:
        before = 0
    try:
        b64 = base64.b64encode(img_path.read_bytes()).decode()
        mime = "image/jpeg" if img_path.suffix.lower() in (".jpg", ".jpeg") else "image/png"
        # 클립보드 API 는 image/png 만 쓰기 허용한다(JPEG 는 NotAllowedError).
        # 사진 풀이 대부분 .jpg 라 canvas 로 PNG 변환해서 붙여넣는다.
        page.evaluate(
            """async ([b64, mime]) => {
                const res = await fetch('data:' + mime + ';base64,' + b64);
                let blob = await res.blob();
                if (blob.type !== 'image/png') {
                    const bmp = await createImageBitmap(blob);
                    const cv = document.createElement('canvas');
                    cv.width = bmp.width; cv.height = bmp.height;
                    cv.getContext('2d').drawImage(bmp, 0, 0);
                    blob = await new Promise(r => cv.toBlob(r, 'image/png'));
                }
                await navigator.clipboard.write([new ClipboardItem({'image/png': blob})]);
            }""", [b64, mime])
        page.keyboard.press("Control+V")
        page.wait_for_timeout(random.randint(1800, 2800))  # 업로드·삽입 대기
        after = page.locator(".se-component.se-image").count()
        if after <= before:
            print(f"  이미지 삽입 확인 실패({img_path.name}): 컴포넌트 증가 없음")
            return False
        return True
    except Exception as e:
        print(f"  이미지 삽입 실패({img_path.name}):", e)
        return False


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
