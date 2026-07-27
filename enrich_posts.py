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
    """이미 발행된 글의 태그를 보강한다(네이버 상한 10개).

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
        room = 10 - len(cur)
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
    ap.add_argument("--only", default=None, help="초안 접두사(예: c22)")
    ap.add_argument("--target", type=int, default=9)
    ap.add_argument("--dry-run", action="store_true")
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
        if a.add_tags:
            tags = [t.strip() for t in a.add_tags.split(",") if t.strip()]
            for d, p in items:
                r = add_tags(page, d, p, log_map, tags, a.dry_run)
                mark = "OK " if r["ok"] else "FAIL"
                print(f"[{mark}] {d[:32]:34} +{r['added']}개  {r['reason'] or ''}")
            ctx.close()
            return
        for d, p in items:
            r = enrich(page, d, p, log_map, a.target, a.dry_run)
            mark = "OK " if r["ok"] else "FAIL"
            print(f"[{mark}] {d[:32]:34} {r['before']} → {r['after']}  {r['reason'] or ''}")
        ctx.close()


if __name__ == "__main__":
    main()
