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
    "topic_open": "a[data-click-area='tpb*i.subject']",     # 발행 레이어 '주제'(네이버 전역 분류)
}

# 세그먼트 → 네이버 '주제'. 카테고리(우리 게시판)와 달리 **네이버 전역 분류**라,
# 미지정이면 주제별 탭·추천 경로에서 통째로 빠진다.
# 2026-07-29 확인: 발행 19편이 전부 '주제 선택 안 함' 이었다(코드가 아예 안 건드렸다).
# 참고로 색인이 잘 되는 원본 블로그 made-us 는 '인테리어·DIY' 를 쓴다.
TOPIC_BY_SEG = {
    "a": "방송",             # BJ/스트리머 방송 소품
    "b": "비즈니스·경제",     # 클럽·주류 브랜드 POSM(B2B)
    "c": "인테리어·DIY",      # 간판·네온사인·아크릴 사인
    "s": "방송",             # sample_bj-picket-guide (a 와 같은 BJ 글)
}


def _set_topic(page, draft_name: str) -> None:
    """글의 '주제'를 세그먼트에 맞게 지정한다. 실패해도 발행은 계속한다."""
    topic = TOPIC_BY_SEG.get((draft_name or "s")[0])
    if not topic:
        return
    try:
        btn = page.locator(SEL["topic_open"]).first
        if topic in (btn.inner_text() or ""):
            return
        btn.click(timeout=3000)
        _pause(0.3, 0.7)
        page.get_by_text(topic, exact=True).first.click(timeout=3000)
        _pause(0.2, 0.5)
        # 선택 후 확인 버튼이 있으면 누른다(레이어 구현에 따라 없을 수도)
        for sel in ("button:has-text('확인')", ".btn_confirm"):
            try:
                page.locator(sel).first.click(timeout=1200)
                break
            except Exception:
                continue
        _pause(0.2, 0.4)
        now = (page.locator(SEL["topic_open"]).first.inner_text() or "").strip()
        print(f"  주제: {now}" if topic in now else f"  ⚠ 주제 설정 확인 실패(현재 '{now}')")
    except Exception as e:
        print("  ⚠ 주제 설정 실패(주제 없이 발행):", e)


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
        # 눌렀다고 켜진 게 아니다 — 실제 체크 상태를 확인해서 안 켜졌으면 크게 알린다.
        keep_on = None
        try:
            keep_on = page.evaluate("""() => {
                const el = document.querySelector('#keep, input[name=nvlong], input[id*=keep]');
                return el ? (el.checked || el.value === 'on') : null;
            }""")
        except Exception:
            pass
        print("=" * 60)
        print(" 열린 크롬 창에서 네이버(made-us2)에 직접 로그인하세요. (2차 인증 포함)")
        if keep_on:
            print(" ※ '로그인 상태 유지' 자동으로 켜뒀습니다.")
        else:
            print(" ⚠ '로그인 상태 유지'를 자동으로 못 켰습니다 — **직접 켜고** 로그인하세요.")
            print("   안 켜면 인증 쿠키가 세션쿠키로 저장돼 하루 안에 발행이 전부 실패합니다.")
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
        # 저장 검증: 쿠키가 '있는지'가 아니라 **영속 쿠키인지**(만료 시각이 미래인지) 본다.
        # 세션 쿠키(expires=-1)면 브라우저가 닫히는 순간 사라져 다음 발행이 통째로 실패한다.
        # 2026-07-28: '있는지'만 확인하고 OK 를 띄웠다가 하루 만에 NID_AUT/NID_SES 가 사라져
        # 예약 발행이 전부 죽었다. 그래서 만료 시각까지 확인하고 출력한다.
        persisted = False
        detail = ""
        if saved:
            c2 = launch_context(p, headed=False)
            try:
                auth = [c for c in c2.cookies()
                        if c.get("name") in ("NID_AUT", "NID_SES")
                        and "naver" in (c.get("domain") or "")]
                lasting = [c for c in auth if (c.get("expires") or -1) > 0]
                persisted = len(lasting) >= 2
                if auth:
                    import datetime
                    detail = " / ".join(
                        f"{c['name']}="
                        + ("세션쿠키" if (c.get("expires") or -1) <= 0 else
                           datetime.datetime.fromtimestamp(c["expires"]).strftime("%Y-%m-%d"))
                        for c in auth)
            finally:
                c2.close()
    if persisted:
        print(f"[OK] 로그인 세션 저장·검증 완료 — 만료: {detail}")
    elif saved:
        print(f"[!] 로그인은 됐으나 **영속 저장 안 됨**({detail or '인증 쿠키 없음'}).")
        print("    로그인 창의 '로그인 상태 유지'를 켜고 다시 로그인하세요. "
              "이대로 두면 하루 안에 발행이 다시 실패합니다.")
    else:
        print("[!] 로그인 감지 안 됨 - 다시 실행해 로그인해 주세요.")


# ── 게시 ────────────────────────────────────────────────────────
def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def related_links(draft_name: str, limit: int = 2) -> list[tuple[str, str]]:
    """같은 세그먼트의 최근 발행 글 (제목, URL) 목록. 자기 글과 logNo 없는 기록은 제외."""
    import json  # noqa: PLC0415
    state = Path(__file__).resolve().parent.parent / "data" / "publish_state.json"
    try:
        s = json.loads(state.read_text(encoding="utf-8"))
    except Exception as e:
        print("  내부 링크: 상태파일을 읽지 못함:", e)   # 조용히 빈 값 반환하지 않는다
        return []
    seg = draft_name[:1]
    out: list[tuple[str, str]] = []
    seen = set()
    for e in reversed(s.get("log", [])):
        if not e.get("ok") or e.get("dry") or e.get("draft") == draft_name:
            continue
        url, title = e.get("url") or "", e.get("title") or ""
        if not title or not url.rstrip("/").split("/")[-1].isdigit():
            continue
        if not e.get("draft", "").startswith(seg) or e["draft"] in seen:
            continue
        seen.add(e["draft"])
        out.append((title, url))
        if len(out) >= limit:
            break
    return out


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
            # 이미지가 슬롯보다 적으면 앞에서부터 채우지 않고 **글 전체에 고르게** 배치한다.
            # (원본 사진 제한 뒤 삽입 4~5장 / 슬롯 7~10개 — 앞머리에만 몰리면 후반이 텍스트 벽)
            from publish.images import spread_slots  # noqa: PLC0415
            _slots = [i for i, b in enumerate(data["blocks"]) if b["kind"] == "image"]
            _use = spread_slots(_slots, len(images))
            img_i = 0
            for _bi, blk in enumerate(data["blocks"]):
                if blk["kind"] == "image":
                    # images 는 image_paths/image_dir 어느 쪽으로 받았든 채워져 있다.
                    # (예전엔 image_dir 을 조건으로 봐서 스케줄러 경로의 이미지가 통째로 누락됐음)
                    # 고르게 배치: 선택되지 않은 슬롯은 건너뛴다(단, 반드시 continue —
                    # 여기서 안 걸러내면 이미지 블록이 아래 heading/text 분기로 샌다).
                    if _bi in _use and img_i < len(images):
                        ok_img = _insert_image(page, images[img_i])
                        img_i += 1
                        # 캡션은 이미지가 실제로 삽입됐을 때만. (실패 시 고아 '▲ 캡션' 방지)
                        # 초안에 캡션이 없으면 실제 삽입된 사진에서 만든다(항상 사진과 일치).
                        cap = (blk.get("alt") or "").strip()
                        if not cap:
                            from publish.images import photo_caption  # noqa: PLC0415
                            cap = photo_caption(images[img_i - 1], img_i - 1)
                        if ok_img and cap:
                            page.keyboard.type(f"▲ {cap}", delay=random.randint(15, 40))
                            page.keyboard.press("Enter")
                            _pause(0.2, 0.5)
                    continue
                if blk["kind"] == "heading":
                    # 에디터의 '소제목' 문단 서식을 쓴다 → se-sectionTitle 컴포넌트로 들어가
                    # 발행 HTML 에서 heading 태그가 된다(검색엔진이 글 구조를 읽는 신호).
                    # 굵게(Ctrl+B)는 <b> 라서 구조 신호가 안 된다 — 실패 시에만 폴백.
                    if _set_text_format(page, "소제목"):
                        page.keyboard.type(blk["text"], delay=random.randint(15, 45))
                        page.keyboard.press("Enter")
                        _set_text_format(page, "본문")
                    else:
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

        # 내부 링크 — 검색엔진이 글끼리의 관계를 읽는 통로이자 체류 시간에도 도움.
        # 같은 세그먼트의 이전 발행 글 2편을 본문 끝에 링크한다.
        try:
            rel = related_links(Path(draft_path).name, limit=2)
            if rel and not review:
                page.keyboard.press("Enter")
                page.keyboard.type("함께 보면 좋은 글", delay=random.randint(15, 35))
                page.keyboard.press("Enter")
                for title, url in rel:
                    page.keyboard.type(f"· {title}", delay=random.randint(10, 25))
                    page.keyboard.press("Enter")
                    page.keyboard.type(url, delay=random.randint(8, 18))
                    page.keyboard.press("Enter")
                    _pause(0.2, 0.5)
                print(f"  내부 링크 {len(rel)}개 삽입")
        except Exception as e:
            print("  내부 링크 건너뜀:", e)

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
            _set_topic(page, Path(draft_path).name)
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
                # '떴다'와 '제대로 떴다'는 다르다 — 라이브 글을 열어 계획과 대조한다.
                result["audit"] = _audit_live_post(page, url, {
                    "images": len(image_paths or []),
                    "tags": len(data["tags"]),
                    "headings": sum(1 for b in data["blocks"] if b["kind"] == "heading"),
                })
            else:
                result["reason"] = result["reason"] or "not_found_after_publish"
                print("‼ 발행했지만 블로그 목록에서 글을 찾지 못했습니다:", result["reason"])

        ctx.close()
        return result


_LIVE_JS = """() => {
  const c = document.querySelector('.se-main-container') || document.body;
  return {
    length: (c.innerText || '').replace(/\\s/g, '').length,
    images: c.querySelectorAll('img').length,
    headings: c.querySelectorAll('.se-component.se-sectionTitle').length,
    tags: Array.from(document.querySelectorAll('a'))
            .map(a => (a.innerText || '').trim())
            .filter(t => /^#\\S/.test(t)).length,
  };
}"""


def _audit_live_post(page, url: str, expect: dict) -> dict:
    """발행된 글을 **실제로 열어** 계획대로 나갔는지 대조한다.

    '발행 성공'은 지금까지 *버튼을 눌렀다*는 뜻이었지 *결과가 맞다*는 뜻이 아니었다.
    그래서 소제목 서식이 안 들어간 채 14편이 나간 것을 9일 뒤에야 알았다(2026-07-29).
    여기서 어긋나면 로그에 남겨 다음 주기에 잡을 수 있게 한다.
    """
    out = {"issues": []}
    try:
        page.goto(url, timeout=30000)
        page.wait_for_timeout(2200)
        # m.blog 은 태그를 '+N' 버튼 뒤에 숨긴다 — 펼쳐야 다 세어진다.
        try:
            more = page.locator("button, a").filter(has_text=re.compile(r"^\+\d+$"))
            if more.count():
                more.first.click(timeout=2000)
                page.wait_for_timeout(500)
        except Exception:
            pass
        live = page.evaluate(_LIVE_JS)
    except Exception as e:
        out["issues"].append(f"확인 실패: {str(e)[:50]}")
        return out
    out.update(live)
    if expect.get("images") and live["images"] < expect["images"]:
        out["issues"].append(f"이미지 {live['images']}/{expect['images']}")
    if expect.get("tags") and live["tags"] < expect["tags"]:
        out["issues"].append(f"태그 {live['tags']}/{expect['tags']}")
    if expect.get("headings") and live["headings"] < expect["headings"]:
        # 0 일 때만 잡으면 '5/7 처럼 일부만 들어간' 글을 놓친다(실제로 놓쳤다).
        tail = " (평문으로 나감)" if live["headings"] == 0 else ""
        out["issues"].append(f"소제목 서식 {live['headings']}/{expect['headings']}{tail}")
    if live["length"] < 600:
        out["issues"].append(f"본문 {live['length']}자 — 잘렸을 수 있음")
    if out["issues"]:
        print("  ⚠ 발행 결과 점검:", " · ".join(out["issues"]))
    else:
        print(f"  발행 결과 점검 OK (이미지 {live['images']} · 태그 {live['tags']} "
              f"· 소제목 {live['headings']} · {live['length']}자)")
    return out


def _set_text_format(page, label: str) -> bool:
    """에디터 툴바의 '문단 서식'을 바꾼다(본문 / 소제목 / 인용구).

    본문 텍스트에 같은 글자가 있어도 오클릭하지 않도록 툴바 버튼으로 범위를 좁힌다.
    """
    try:
        page.locator('[data-name="text-format"]').first.click(timeout=4000)
        page.wait_for_timeout(350)
        page.locator(f'button.se-toolbar-option-text-button:has-text("{label}")') \
            .first.click(timeout=4000)
        page.wait_for_timeout(300)
        return True
    except Exception as e:
        print(f"  문단 서식('{label}') 적용 실패:", str(e)[:70])
        return False


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
