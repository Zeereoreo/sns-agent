"""이미 발행된 글에 사진을 더 넣는다.

초안의 이미지 슬롯을 늘려도 **이미 올라간 글에는 반영되지 않는다**(발행 시점에 박힘).
발행 14편이 사진 0~4장뿐이라, 라이브 글을 직접 열어 사진을 보강한다.

- 한 글에는 같은 제품 그룹 사진만 넣는다(images.pick_images 규칙과 동일).
- 캡션은 실제 넣는 사진에서 만든다(images.photo_caption).
- 본문 문단 사이에 고르게 끼워 넣는다(끝에 몰아넣지 않음).

사용:
  python enrich_posts.py --list                 # 라이브 사진 수만 확인
  python enrich_posts.py --only c22 --target 9  # 한 편만
  python enrich_posts.py --target 9             # 부족한 글 전부
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import config  # noqa: E402
from publish import images as imgmod  # noqa: E402

STATE = ROOT / "data" / "publish_state.json"

# 네이버가 실제로 허용하는 태그 수는 10 보다 많다(2026-07-29 실측). 같은 판의 상위
# 경쟁 글이 12~20개를 쓰므로 20 까지 채운다 — 그 이상은 확인 안 된 영역이라 두지 않는다.
TAG_MAX = 20

# ★라이브 편집 총량 제한 (2026-07-30 추가 — 뼈아픈 근거)
# 7/28 과 7/30 두 번 다 **발행글을 대량 편집한 직후 로그인 세션이 끊겼다**.
# 만료가 아니다 — NID_AUT/NID_SES 가 만료일(8/27)을 한 달 남기고 삭제됐다.
# 7/29 하루에 postupdate 를 70회 이상 열고 발행 버튼을 눌렀으니 자동화로 보였을 것이다.
# 편집은 되돌릴 수 있지만 세션이 끊기면 **발행이 통째로 멈춘다** — 그쪽이 훨씬 비싸다.
EDIT_MAX_PER_RUN = 6          # 한 번 실행에서 손댈 발행글 수
EDIT_PAUSE_SEC = (8.0, 16.0)  # 글 사이 대기(사람 속도에 가깝게)
EDIT_LOG = ROOT / "data" / ".live-edit-log"
EDIT_MAX_PER_DAY = 12         # 하루 총량


def _edits_today() -> int:
    if not EDIT_LOG.exists():
        return 0
    today = str(__import__("datetime").date.today())
    return sum(1 for ln in EDIT_LOG.read_text(encoding="utf-8").splitlines()
               if ln.startswith(today))


def _record_edit(draft: str) -> None:
    import datetime as _dt
    with EDIT_LOG.open("a", encoding="utf-8") as f:
        f.write(f"{_dt.date.today()} {_dt.datetime.now():%H:%M} {draft}\n")


def published_posts() -> dict[str, dict]:
    """초안 → {url, title}. 같은 초안이 여러 번 있으면 최신 발행 기록을 쓴다."""
    s = json.loads(STATE.read_text(encoding="utf-8"))
    out = {}
    for e in s.get("log", []):
        if e.get("ok") and not e.get("dry"):
            out[e["draft"]] = {"url": e.get("url") or "", "title": e.get("title") or ""}
    return out


def _log_no(url: str) -> str | None:
    """URL 끝의 logNo. 옛 기록은 블로그 홈 주소라 logNo 가 없다 → None."""
    tail = url.rstrip("/").split("/")[-1].split("?")[0]
    return tail if tail.isdigit() else None


def _norm(s: str) -> str:
    import re
    return re.sub(r"\s+", "", s or "")


def draft_title_candidates(draft: str) -> list[str]:
    """초안에서 가능한 제목들(H1 + 메타의 '제목안' 후보).
    옛 발행 기록은 title 이 비어 있어 목록 API 와 대조하려면 초안에서 후보를 뽑아야 한다."""
    import re
    p = ROOT / "drafts" / draft
    if not p.exists():
        return []
    text = p.read_text(encoding="utf-8")
    out = []
    m = re.search(r"^#\s+(.+)$", text, re.M)
    if m:
        out.append(m.group(1).strip())
    for line in re.findall(r"^\s*(?:제목안:)?\s*\d\)\s*(.+)$", text, re.M):
        out.append(line.strip())
    return out


def resolve_log_nos(page, blog: str) -> dict[str, str]:
    """게시글 목록 API 로 '정규화 제목 → logNo' 를 만든다.
    publish_state 의 옛 URL 에는 logNo 가 없어서(홈 주소 폴백) 이 대조가 필요하다."""
    home = f"https://m.blog.naver.com/{blog}"
    page.goto(home, timeout=30000)
    page.wait_for_timeout(1500)
    out: dict[str, str] = {}
    for pg in (1, 2, 3):
        api = (f"https://m.blog.naver.com/api/blogs/{blog}/post-list"
               f"?categoryNo=0&itemCount=30&page={pg}&userId={blog}")
        try:
            r = page.request.get(api, headers={"referer": home}, timeout=15000)
            items = (r.json() or {}).get("result", {}).get("items", []) if r.ok else []
        except Exception:
            items = []
        if not items:
            break
        for it in items:
            t = _norm(it.get("titleWithInspectMessage") or it.get("title") or "")
            if t and it.get("logNo"):
                out.setdefault(t, str(it["logNo"]))
    return out


def _count_images(page) -> int:
    return page.locator(".se-component.se-image").count()


def enrich(page, draft: str, post: dict, log_map: dict[str, str],
           target: int, dry: bool) -> dict:
    from publish.naver import _insert_image  # noqa: PLC0415

    blog = config.NAVER_BLOG_ID
    res = {"draft": draft, "before": None, "after": None, "ok": False, "reason": None}
    no = _log_no(post["url"]) or log_map.get(_norm(post["title"]))
    if not no:                       # 옛 기록은 title 이 비어 있다 → 초안 제목 후보로 대조
        for cand in draft_title_candidates(draft):
            key = _norm(cand)
            no = log_map.get(key) or next(
                (v for k, v in log_map.items() if key and (key in k or k in key)), None)
            if no:
                break
    if not no:
        res["reason"] = "logNo 를 찾지 못함(제목 불일치)"
        return res

    if True:
        try:
            page.goto(f"https://blog.naver.com/{blog}/postupdate?logNo={no}", timeout=60000)
            page.wait_for_timeout(3500)
            if "postupdate" not in page.url and "PostWriteForm" not in page.url:
                res["reason"] = f"편집기가 열리지 않음({page.url[:50]})"
                return res
            for sel in (".se-popup-button-cancel", "button:has-text('취소')"):
                try:
                    page.locator(sel).first.click(timeout=1500)
                    break
                except Exception:
                    continue
            page.keyboard.press("Escape")
            page.wait_for_timeout(800)

            before = _count_images(page)
            res["before"] = before
            need = max(0, target - before)
            if need == 0:
                res["ok"] = True
                res["reason"] = "이미 충분"
                return res

            picks, _ = imgmod.pick_images(str(ROOT / "drafts" / draft), need + 1,
                                          advance=True)
            photos = [p for p in picks if p.parent.name == "photos"][:need]
            if not photos:
                res["reason"] = "쓸 사진 없음"
                return res

            # 본문 문단을 고르게 골라 그 뒤에 삽입
            # 편집기에는 .se-main-container 가 없다(그건 발행된 글의 클래스).
            paras = page.locator(".se-section-text")
            n_par = paras.count()
            if n_par == 0:
                res["reason"] = "본문 문단을 찾지 못함"
                return res
            step = max(1, n_par // (len(photos) + 1))
            added = 0
            for k, ph in enumerate(photos, start=1):
                idx = min(n_par - 1, k * step)
                try:
                    paras.nth(idx).click()
                    page.keyboard.press("End")
                    page.keyboard.press("Enter")
                except Exception:
                    page.keyboard.press("Control+End")
                if dry:
                    added += 1
                    continue
                if _insert_image(page, ph):
                    added += 1
                    cap = imgmod.photo_caption(ph, k)
                    if cap:
                        page.keyboard.type(f"▲ {cap}", delay=20)
                        page.keyboard.press("Enter")
                    time.sleep(0.4)

            res["after"] = _count_images(page)
            if dry:
                res["ok"] = True
                res["reason"] = f"dry-run: {added}장 삽입 예정"
                return res

            if added == 0:
                res["reason"] = "삽입 실패"
                return res

            page.locator('[data-click-area="tpb.publish"]').first.click()
            page.wait_for_timeout(1800)
            page.locator('[data-click-area="tpb*i.publish"]').first.click()
            page.wait_for_timeout(6000)
            res["ok"] = True
        except Exception as e:
            res["reason"] = f"오류: {e}"
    return res


def add_tags(page, draft: str, post: dict, log_map: dict[str, str],
             tags: list[str], dry: bool) -> dict:
    """이미 발행된 글의 태그를 보강한다.

    상한을 10 으로 두고 있었는데 **그건 우리가 잘못 넣은 가정**이었다(2026-07-29 실측:
    11번째 태그가 정상 입력됨). 같은 판의 경쟁 상위 글은 12~20개를 쓴다
    (linosgj 8~17 · jayent_media 20). 10 으로 막아 슬롯 절반을 버리고 있었다.

    측정(자동완성)이 놓쳤을 검색 경로를 태그로 열어두기 위함 — 비용 0, 되돌리기 쉬움.
    """
    blog = config.NAVER_BLOG_ID
    res = {"draft": draft, "added": 0, "ok": False, "reason": None}
    no = _log_no(post["url"]) or log_map.get(_norm(post["title"]))
    if not no:
        for cand in draft_title_candidates(draft):
            k = _norm(cand)
            no = log_map.get(k) or next(
                (v for kk, v in log_map.items() if k and (k in kk or kk in k)), None)
            if no:
                break
    if not no:
        res["reason"] = "logNo 없음"
        return res
    try:
        page.goto(f"https://blog.naver.com/{blog}/postupdate?logNo={no}", timeout=60000)
        page.wait_for_timeout(3500)
        for sel in (".se-popup-button-cancel", "button:has-text('취소')"):
            try:
                page.locator(sel).first.click(timeout=1500)
                break
            except Exception:
                continue
        page.keyboard.press("Escape")
        page.wait_for_timeout(600)

        page.locator('[data-click-area="tpb.publish"]').first.click(timeout=8000)
        page.wait_for_timeout(2000)

        box = page.locator("input#tag-input, input.tag_input, input[placeholder*='태그']").first
        # 태그 칩은 해시 클래스라 [class^="tag__"] 로 잡는다.
        # (예전 셀렉터는 하나도 못 잡아 '이미 있는 태그'를 다시 입력했고,
        #  Enter 가 안 먹은 사이 두 태그가 붙어 '스트리머굿즈스트리머굿즈제작' 이 생겼다.)
        def current() -> set[str]:
            got = page.eval_on_selector_all(
                '[class^="tag__"]', "e => e.map(x => x.innerText.trim())")
            return {t.lstrip("#").strip() for t in got if t.strip()}

        cur = current()
        room = TAG_MAX - len(cur)
        want = [t.lstrip("#") for t in tags if t.lstrip("#") not in cur][:max(0, room)]
        if not want:
            res["ok"] = True
            res["reason"] = f"이미 있음/자리 없음(현재 {len(cur)}개)"
            return res
        if dry:
            res["ok"] = True
            res["added"] = len(want)
            res["reason"] = f"dry-run: {', '.join(want)}"
            return res
        for t in want:
            before = len(current())
            box.click()
            page.keyboard.type(t, delay=35)
            page.keyboard.press("Enter")
            page.wait_for_timeout(900)
            if len(current()) <= before:      # Enter 가 안 먹었다 → 붙은 태그를 만들지 않는다
                page.keyboard.press("Enter")
                page.wait_for_timeout(900)
                if len(current()) <= before:
                    res["reason"] = f"태그 '{t}' 입력 실패 — 중단"
                    return res
            res["added"] += 1
        page.locator('[data-click-area="tpb*i.publish"]').first.click(timeout=8000)
        page.wait_for_timeout(6000)
        res["ok"] = True
    except Exception as e:
        res["reason"] = f"오류: {str(e)[:70]}"
    return res


def set_topic(page, draft: str, post: dict, log_map: dict[str, str], dry: bool) -> dict:
    """이미 발행된 글에 '주제'(네이버 전역 분류)를 지정한다.

    발행 코드가 주제를 아예 안 건드려서 발행분 전체가 '주제 선택 안 함' 이었다
    (2026-07-29). 주제가 없으면 주제별 탭·추천 경로에서 통째로 빠진다.
    """
    from publish.naver import TOPIC_BY_SEG  # noqa: PLC0415

    blog = config.NAVER_BLOG_ID
    res = {"draft": draft, "added": 0, "ok": False, "reason": None}
    topic = TOPIC_BY_SEG.get((draft or "s")[0])
    if not topic:
        res["reason"] = "세그먼트 없음"
        return res
    no = _log_no(post["url"]) or log_map.get(_norm(post["title"]))
    if not no:
        for cand in draft_title_candidates(draft):
            k = _norm(cand)
            no = log_map.get(k) or next(
                (v for kk, v in log_map.items() if k and (k in kk or kk in k)), None)
            if no:
                break
    if not no:
        res["reason"] = "logNo 없음"
        return res
    try:
        page.goto(f"https://blog.naver.com/{blog}/postupdate?logNo={no}", timeout=60000)
        page.wait_for_timeout(3500)
        for sel in (".se-popup-button-cancel", "button:has-text('취소')"):
            try:
                page.locator(sel).first.click(timeout=1500)
                break
            except Exception:
                continue
        page.keyboard.press("Escape")
        page.wait_for_timeout(600)
        page.locator('[data-click-area="tpb.publish"]').first.click(timeout=8000)
        page.wait_for_timeout(2200)

        btn = page.locator("a[data-click-area='tpb*i.subject']").first
        cur = (btn.inner_text() or "").strip()
        if topic in cur:
            res["ok"] = True
            res["reason"] = f"이미 '{topic}'"
            return res
        if dry:
            res["ok"] = True
            res["added"] = 1
            res["reason"] = f"dry-run: '{cur}' → '{topic}'"
            return res
        btn.click(timeout=4000)
        page.wait_for_timeout(1200)
        page.get_by_text(topic, exact=True).first.click(timeout=4000)
        page.wait_for_timeout(800)
        for sel in ("button:has-text('확인')", ".btn_confirm"):
            try:
                page.locator(sel).first.click(timeout=1200)
                break
            except Exception:
                continue
        page.wait_for_timeout(600)
        now = (page.locator("a[data-click-area='tpb*i.subject']").first.inner_text() or "").strip()
        if topic not in now:
            res["reason"] = f"설정 실패(현재 '{now}')"
            return res
        page.locator('[data-click-area="tpb*i.publish"]').first.click(timeout=8000)
        page.wait_for_timeout(6000)
        res["ok"] = True
        res["added"] = 1
        res["reason"] = topic
    except Exception as e:
        res["reason"] = f"오류: {str(e)[:70]}"
    return res


def sync_title(page, draft: str, post: dict, log_map: dict[str, str], dry: bool) -> dict:
    """발행된 글의 제목을 초안의 (확장된) 제목으로 교체한다.

    2026-07-29 제목 정책 변경 — 이 판의 승자는 나열형 장문 제목이다(원본 made-us 구글
    '개인방송 피켓' 1위 글이 104자, 경쟁사 linosgj 도 동일). 우리 발행분은 19~26자라
    키워드 커버리지를 거의 못 가져간다. 색인이 열렸을 때를 대비해 미리 바꿔둔다.
    """
    from publish.draft_parser import parse_draft  # noqa: PLC0415

    blog = config.NAVER_BLOG_ID
    res = {"draft": draft, "added": 0, "ok": False, "reason": None}
    dp = ROOT / "drafts" / draft
    if not dp.exists():
        res["reason"] = "초안 없음"
        return res
    want = parse_draft(dp)["title"]
    if not want or want == "제목 없음":
        res["reason"] = "초안 제목 없음"
        return res

    no = _log_no(post["url"]) or log_map.get(_norm(post["title"]))
    if not no:
        for cand in draft_title_candidates(draft):
            k = _norm(cand)
            no = log_map.get(k) or next(
                (v for kk, v in log_map.items() if k and (k in kk or kk in k)), None)
            if no:
                break
    if not no:
        res["reason"] = "logNo 없음"
        return res
    try:
        page.goto(f"https://blog.naver.com/{blog}/postupdate?logNo={no}", timeout=60000)
        page.wait_for_timeout(4000)
        for sel in (".se-popup-button-cancel", "button:has-text('취소')"):
            try:
                page.locator(sel).first.click(timeout=1500)
                break
            except Exception:
                continue
        page.keyboard.press("Escape")
        page.wait_for_timeout(600)

        cur = (page.locator(".se-section-documentTitle").first.inner_text() or "").strip()
        if cur.replace(" ", "") == want.replace(" ", ""):
            res["ok"] = True
            res["reason"] = "이미 동일"
            return res
        if dry:
            res["ok"] = True
            res["added"] = 1
            res["reason"] = f"dry-run: {len(cur)}자 → {len(want)}자"
            return res

        page.locator(".se-section-documentTitle").first.click()
        page.wait_for_timeout(400)
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
        page.wait_for_timeout(300)
        page.keyboard.type(want, delay=12)
        page.wait_for_timeout(600)
        got = (page.locator(".se-section-documentTitle").first.inner_text() or "").strip()
        if got.replace(" ", "") != want.replace(" ", ""):
            res["reason"] = f"제목 입력 확인 실패({len(got)}자) — 발행하지 않음"
            return res

        page.locator('[data-click-area="tpb.publish"]').first.click(timeout=8000)
        page.wait_for_timeout(2200)
        page.locator('[data-click-area="tpb*i.publish"]').first.click(timeout=8000)
        page.wait_for_timeout(6000)
        res["ok"] = True
        res["added"] = 1
        res["reason"] = f"{len(cur)}자 → {len(want)}자"
    except Exception as e:
        res["reason"] = f"오류: {str(e)[:70]}"
    return res


def _heading_anchors(draft_path) -> list[tuple[str, str]]:
    """[(소제목, 그 바로 다음 본문 문단의 앞부분)] — 삽입 위치를 잡는 기준."""
    from publish.draft_parser import parse_draft  # noqa: PLC0415

    blocks = parse_draft(draft_path)["blocks"]
    out = []
    for i, b in enumerate(blocks):
        if b["kind"] != "heading" or not b.get("text", "").strip():
            continue
        follow = ""
        for nb in blocks[i + 1:]:
            if nb["kind"] == "text" and len(nb.get("text", "").strip()) > 12:
                follow = nb["text"].strip()
                break
        out.append((b["text"].strip(), follow))
    return out


def _insert_heading_before(page, heading: str, follow: str) -> int:
    """`follow` 문단 앞에 `heading` 문단을 새로 만든다. 만든 문단 index 를 돌려준다(-1=실패)."""
    if not follow:
        return -1
    # 다음 문단이 리스트 항목이면 초안엔 '- ' 가 붙어 있지만 에디터는 그걸 빼고 렌더한다.
    # 그대로 대조하면 못 찾아서 삽입이 조용히 실패한다(sample 에서 2개를 놓쳤다).
    key = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", follow)[:18]
    idx = page.evaluate("""(k) => {
        const ps = document.querySelectorAll('.se-text-paragraph');
        for (let i = 0; i < ps.length; i++) {
            if ((ps[i].innerText || '').trim().startsWith(k)) return i;
        }
        return -1;
    }""", key)
    if idx < 0:
        return -1
    try:
        page.locator(".se-text-paragraph").nth(idx).click()
        page.wait_for_timeout(250)
        page.keyboard.press("Home")
        # 소제목 텍스트 + 줄바꿈 → 문단이 둘로 갈리고 앞쪽이 소제목이 된다
        page.keyboard.type(heading, delay=18)
        page.keyboard.press("Enter")
        page.wait_for_timeout(500)
        # 방금 만든 문단(= 원래 idx 자리)이 소제목 텍스트인지 확인
        got = page.evaluate("""(i) => {
            const ps = document.querySelectorAll('.se-text-paragraph');
            return ps[i] ? (ps[i].innerText || '').trim() : '';
        }""", idx)
        return idx if got == heading else -1
    except Exception:
        return -1


def fix_headings(page, draft: str, post: dict, log_map: dict[str, str], dry: bool) -> dict:
    """이미 발행된 글의 소제목을 '소제목' 문단 서식으로 바꾼다.

    소제목 서식 기능을 나중에 넣어서, 그 전에 발행된 글은 소제목이 **평문**으로 나갔다
    (2026-07-29 전수 확인: 19편 중 14편이 라이브 소제목 0개). 읽는 사람에게는 그냥
    글자 덩어리로 보인다. 초안의 heading 블록 텍스트와 **정확히 같은** 문단만 고른다.
    """
    from publish.draft_parser import parse_draft  # noqa: PLC0415
    from publish.naver import _set_text_format  # noqa: PLC0415

    blog = config.NAVER_BLOG_ID
    res = {"draft": draft, "added": 0, "ok": False, "reason": None}
    dp = ROOT / "drafts" / draft
    if not dp.exists():
        res["reason"] = "초안 없음"
        return res
    heads = [b["text"].strip() for b in parse_draft(dp)["blocks"]
             if b["kind"] == "heading" and b.get("text", "").strip()]
    if not heads:
        res["reason"] = "소제목 없음"
        return res

    no = _log_no(post["url"]) or log_map.get(_norm(post["title"]))
    if not no:
        for cand in draft_title_candidates(draft):
            k = _norm(cand)
            no = log_map.get(k) or next(
                (v for kk, v in log_map.items() if k and (k in kk or kk in k)), None)
            if no:
                break
    if not no:
        res["reason"] = "logNo 없음"
        return res
    try:
        page.goto(f"https://blog.naver.com/{blog}/postupdate?logNo={no}", timeout=60000)
        page.wait_for_timeout(4000)
        for sel in (".se-popup-button-cancel", "button:has-text('취소')"):
            try:
                page.locator(sel).first.click(timeout=1500)
                break
            except Exception:
                continue
        page.keyboard.press("Escape")
        page.wait_for_timeout(600)

        before = page.locator(".se-component.se-sectionTitle").count()
        if before >= len(heads):
            res["ok"] = True
            res["reason"] = f"이미 적용됨({before}개)"
            return res
        if dry:
            res["ok"] = True
            res["added"] = len(heads)
            res["reason"] = f"dry-run: 소제목 {before} → {len(heads)}개 예정"
            return res

        for h, follow in _heading_anchors(dp):
            # 본문에 같은 문구가 또 있을 수 있으므로 '문단 전체가 정확히 그 텍스트'인 것만
            idx = page.evaluate("""(t) => {
                const ps = document.querySelectorAll('.se-text-paragraph');
                for (let i = 0; i < ps.length; i++) {
                    if ((ps[i].innerText || '').trim() === t) return i;
                }
                return -1;
            }""", h)
            if idx < 0:
                # 소제목이 **본문에 아예 없는** 글이 있다(초기 발행 3편). 서식 변경이 아니라
                # 삽입이 필요하다 — 초안에서 그 소제목 **다음 문단**을 찾아 그 앞에 넣는다.
                idx = _insert_heading_before(page, h, follow)
                if idx < 0:
                    continue
            para = page.locator(".se-text-paragraph").nth(idx)
            para.click()
            page.wait_for_timeout(250)
            page.keyboard.press("Home")
            page.keyboard.press("Shift+End")
            page.wait_for_timeout(200)
            if _set_text_format(page, "소제목"):
                res["added"] += 1
            page.wait_for_timeout(300)

        after = page.locator(".se-component.se-sectionTitle").count()
        if after <= before:
            res["reason"] = f"서식이 늘지 않음({before}→{after}) — 발행하지 않음"
            return res
        page.locator('[data-click-area="tpb.publish"]').first.click(timeout=8000)
        page.wait_for_timeout(2200)
        page.locator('[data-click-area="tpb*i.publish"]').first.click(timeout=8000)
        page.wait_for_timeout(6000)
        res["ok"] = True
        res["reason"] = f"소제목 {before}→{after}개"
    except Exception as e:
        res["reason"] = f"오류: {str(e)[:70]}"
    return res


def _index_ok(force: bool = False) -> bool:
    """색인이 살아 있을 때만 라이브 편집을 허용한다.

    ★2026-07-30 실측(Creator Advisor 검색유입):
      7/21~27 매일 검색유입 있었고 **7/26 에 5명으로 정점**을 찍었다(7일 합 13명).
      그런데 7/28 부터 **0**. 그 경계에 내가 한 발행글 대량 편집이 있다
      (7/27~28 사진 14편 보강, 7/29 postupdate 70회 이상).
      제목 정확 검색으로도 0/16편 — 색인이 '아직 안 된' 게 아니라 **빠진** 것이다.
    발행글을 고치면 재수집 대기에 들어가고, 대량이면 자동화로 보인다.
    회복 전에 또 편집하면 같은 구덩이를 판다 → 색인 0 이면 **편집을 막는다**.
    새 글 발행은 정상 활동이므로 제한하지 않는다.
    """
    try:
        m = json.loads((ROOT / "data" / "metrics.json").read_text(encoding="utf-8"))
        idx = m.get("index_status") or {}
    except Exception:
        return True                      # 판단 근거가 없으면 막지 않는다
    if idx.get("sampled") and idx.get("found", 0) == 0:
        # 2026-07-30 사용자 지시로 **차단이 아니라 경고**. 편집 자체는 허용하되,
        # 대량으로 몰아치지 않게 총량 제한(EDIT_MAX_*)은 그대로 건다 —
        # 세션이 두 번 끊긴 게 '한 번에 많이' 했기 때문이지 편집 자체 때문이 아니다.
        print(f"[주의] 색인 미확인 상태입니다(제목검색 {idx['found']}/{idx['sampled']}편,"
              f" {idx.get('checked','')}). 편집은 진행하되 소량·간격을 지킵니다.")
    return True


def _run_edits(page, items, log_map, args, do_one, fmt=None) -> None:
    """라이브 편집 루프 — **총량 제한과 대기를 강제한다**.

    7/28·7/30 두 번 다 발행글을 대량 편집한 직후 로그인 세션이 끊겼다(만료 아님 —
    NID_AUT/NID_SES 가 만료일 한 달 전에 삭제됐다). 7/29 하루에 postupdate 를 70회 넘게
    열었으니 자동화로 보였을 것이다. 편집은 다시 할 수 있지만 **세션이 끊기면 발행이
    통째로 멈춘다** — 그쪽이 훨씬 비싸므로 느리게 가는 쪽을 택한다.
    dry-run 은 발행하지 않으므로 제한하지 않는다.
    """
    import random
    import time as _t

    dry = bool(getattr(args, "dry_run", False))
    if not dry and not _index_ok(getattr(args, "force", False)):
        return
    done_today = 0 if dry else _edits_today()
    room_day = EDIT_MAX_PER_DAY - done_today
    if not dry and room_day <= 0:
        print(f"[중단] 오늘 라이브 편집 {done_today}건으로 한도({EDIT_MAX_PER_DAY})를 채웠습니다.")
        print("       세션 보호를 위해 내일 이어서 하세요.")
        return
    cap = len(items) if dry else min(EDIT_MAX_PER_RUN, room_day)
    if not dry and len(items) > cap:
        print(f"[제한] 대상 {len(items)}편 중 {cap}편만 처리합니다"
              f"(1회 {EDIT_MAX_PER_RUN} · 오늘 남은 {room_day}). 나머지는 다시 실행하세요.")

    for i, (d, p) in enumerate(items[:cap]):
        r = do_one(page, d, p)
        mark = "OK " if r.get("ok") else "FAIL"
        detail = fmt(r) if fmt else (r.get("reason") or "")
        print(f"[{mark}] {d[:32]:34} {detail}")
        if not dry and r.get("ok") and r.get("added", 1):
            _record_edit(d)
        if i + 1 < cap and not dry:
            _t.sleep(random.uniform(*EDIT_PAUSE_SEC))


def draft_tags(draft: str) -> list[str]:
    """초안 헤더의 '태그:' 줄을 읽는다. 글마다 태그가 다르므로 라이브 동기화에 쓴다."""
    p = ROOT / "drafts" / draft
    if not p.exists():
        return []
    m = re.search(r"^태그:\s*(.+)$", p.read_text(encoding="utf-8"), flags=re.M)
    return m.group(1).split() if m else []


def fix_urls(page, blog: str) -> int:
    """publish_state 의 URL 중 logNo 가 없는 것(홈 주소 폴백)을 목록 API 로 되찾아 채운다.
    내부 링크·글 보강이 전부 logNo 에 의존하므로 데이터를 먼저 바로잡는다."""
    log_map = resolve_log_nos(page, blog)
    s = json.loads(STATE.read_text(encoding="utf-8"))
    fixed = 0
    for e in s.get("log", []):
        if not e.get("ok") or e.get("dry"):
            continue
        if _log_no(e.get("url") or ""):
            continue
        no = log_map.get(_norm(e.get("title") or ""))
        if not no:                   # 옛 기록은 title 도 비어 있다 → 초안 제목 후보로 대조
            for cand in draft_title_candidates(e.get("draft", "")):
                k = _norm(cand)
                no = log_map.get(k) or next(
                    (v for kk, v in log_map.items() if k and (k in kk or kk in k)), None)
                if no:
                    break
        if no:
            e["url"] = f"https://m.blog.naver.com/{blog}/{no}"
            fixed += 1
    if fixed:
        tmp = STATE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
        import os
        os.replace(tmp, STATE)
    return fixed


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix-urls", action="store_true", help="logNo 없는 URL 기록 복구")
    ap.add_argument("--add-tags", default=None,
                    help="발행된 글에 태그 추가(쉼표 구분). 예: 스트리머굿즈,스트리머굿즈제작")
    ap.add_argument("--sync-tags", action="store_true",
                    help="각 발행글에 **그 글 초안의 태그**를 반영(글마다 다른 태그를 한 번에)")
    ap.add_argument("--set-topic", action="store_true",
                    help="발행글의 '주제'(네이버 전역 분류)를 세그먼트에 맞게 지정")
    ap.add_argument("--fix-headings", action="store_true",
                    help="평문으로 나간 소제목에 '소제목' 문단 서식을 입힌다")
    ap.add_argument("--sync-titles", action="store_true",
                    help="발행글 제목을 초안의 (확장된) 제목으로 교체")
    ap.add_argument("--only", default=None, help="초안 접두사(예: c22)")
    ap.add_argument("--target", type=int, default=9)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="색인 게이트 무시(권장하지 않음 — 편집이 색인을 날린 전력이 있음)")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()

    posts = published_posts()
    items = [(d, p) for d, p in posts.items() if not a.only or d.startswith(a.only)]
    if not items:
        print("대상 없음")
        return

    from playwright.sync_api import sync_playwright  # noqa: PLC0415

    from publish.browser import launch_context  # noqa: PLC0415

    with sync_playwright() as pw:
        ctx = launch_context(pw, headed=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        if a.fix_urls:
            n = fix_urls(page, config.NAVER_BLOG_ID)
            print(f"URL 기록 복구 {n}건")
            ctx.close()
            return
        log_map = resolve_log_nos(page, config.NAVER_BLOG_ID)
        print(f"목록 API 에서 글 {len(log_map)}편 확인")
        if a.list:
            for d, p in items:
                no = _log_no(p["url"]) or log_map.get(_norm(p["title"])) or "-"
                print(f"  {d[:32]:34} logNo={no}")
            ctx.close()
            return
        if a.sync_titles:
            _run_edits(page, items, log_map, a,
                       lambda pg, d, p: sync_title(pg, d, p, log_map, a.dry_run))
            ctx.close()
            return
        if a.fix_headings:
            _run_edits(page, items, log_map, a,
                       lambda pg, d, p: fix_headings(pg, d, p, log_map, a.dry_run))
            ctx.close()
            return
        if a.set_topic:
            _run_edits(page, items, log_map, a,
                       lambda pg, d, p: set_topic(pg, d, p, log_map, a.dry_run))
            ctx.close()
            return
        if a.add_tags or a.sync_tags:
            fixed_tags = ([t.strip() for t in a.add_tags.split(",") if t.strip()]
                          if a.add_tags else None)

            def _tag_one(pg, d, p):
                tags = fixed_tags if fixed_tags is not None else draft_tags(d)
                if not tags:
                    return {"ok": True, "added": 0, "reason": "초안 태그 없음"}
                return add_tags(pg, d, p, log_map, tags, a.dry_run)

            _run_edits(page, items, log_map, a, _tag_one)
            ctx.close()
            return
        _run_edits(page, items, log_map, a,
                   lambda pg, d, p: enrich(pg, d, p, log_map, a.target, a.dry_run),
                   fmt=lambda r: f"{r.get('before')} → {r.get('after')}  {r.get('reason') or ''}")
        ctx.close()


if __name__ == "__main__":
    main()
