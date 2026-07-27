"""made-us 원본 블로그에서 실물 사진을 수확한다.

사진 재고가 글 품질의 병목이다(글당 8~10장 쓰려면 계속 채워야 함).
수확물은 바로 풀에 넣지 않고 `drafts/photos/_harvest/` 에 모은 뒤,
**사람(또는 내가) 눈으로 보고 이름을 붙여서** 풀로 옮긴다.
— 2026-07-20 에 파일명만 보고 넣었다가 '피켓' 이름의 배터리 컷 11장이 섞여
   글에 엉뚱한 사진이 붙는 사고가 있었다. 그 재발을 막는 순서다.

사용:
  python harvest_photos.py --posts 15            # 최근 글 15편에서 수확
  python harvest_photos.py --posts 15 --min-px 600
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import config  # noqa: E402

OUT_DIR = ROOT / "drafts" / "photos" / "_harvest"
MANIFEST = OUT_DIR / "manifest.json"
SOURCE_BLOG = "made-us"   # 원본(클라이언트) 블로그


def _post_urls(page, blog: str, want: int) -> list[str]:
    """모바일 블로그 목록에서 글 URL 을 모은다(스크롤하며 lazy 목록 로딩)."""
    page.goto(f"https://m.blog.naver.com/{blog}", timeout=60000)
    page.wait_for_timeout(2500)
    urls: list[str] = []
    for _ in range(12):
        found = page.eval_on_selector_all(
            "a[href*='/PostView'], a[href^='/made-us/'], a[href*='logNo=']",
            "els => els.map(e => e.href)")
        for u in found:
            if re.search(r"logNo=\d+|/\d{9,}", u) and u not in urls:
                urls.append(u)
        if len(urls) >= want:
            break
        page.mouse.wheel(0, 3000)
        page.wait_for_timeout(1200)
    return urls[:want]


def _images_of(page, url: str, min_px: int) -> tuple[str, list[str]]:
    """글 1편에서 제목과 이미지 원본 URL 목록을 뽑는다.
    lazy-load 때문에 naturalWidth 로만 거르면 0장이 나온다 — data-lazy-src 까지 본다."""
    page.goto(url, timeout=60000)
    page.wait_for_timeout(2000)
    for _ in range(6):
        page.mouse.wheel(0, 2500)
        page.wait_for_timeout(700)
    title = (page.title() or "").strip()
    srcs = page.eval_on_selector_all(
        "img",
        """els => els.map(e => e.getAttribute('data-lazy-src')
                            || e.getAttribute('data-src')
                            || e.currentSrc || e.src || '')""")
    out = []
    for s in srcs:
        if not s or "pstatic.net" not in s:
            continue
        if any(x in s for x in ("profile", "ssl.pstatic.net/static", "blogpfthumb")):
            continue
        # 썸네일 파라미터를 원본 크기로 교체
        s = re.sub(r"\?type=w\d+.*$", "?type=w966", s)
        if s not in out:
            out.append(s)
    return title, out


def _download(url: str, dest: Path) -> bool:
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0",
                                    "Referer": "https://m.blog.naver.com/"})
        with urlopen(req, timeout=30) as r:  # noqa: S310
            data = r.read()
        if len(data) < 15000:      # 아이콘·장식컷 제외
            return False
        dest.write_bytes(data)
        return True
    except Exception as e:
        print("   다운로드 실패:", e)
        return False


def harvest(posts: int, min_px: int) -> None:
    from playwright.sync_api import sync_playwright  # noqa: PLC0415
    from publish.browser import launch_context  # noqa: PLC0415

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else {}
    seen_src = {v["src"] for v in manifest.values()}
    n_new = 0

    with sync_playwright() as p:
        ctx = launch_context(p, headed=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        urls = _post_urls(page, SOURCE_BLOG, posts)
        print(f"글 {len(urls)}편 발견")

        for i, u in enumerate(urls, 1):
            title, srcs = _images_of(page, u, min_px)
            print(f"[{i}/{len(urls)}] {title[:40]} — 이미지 {len(srcs)}장")
            for j, s in enumerate(srcs, 1):
                if s in seen_src:
                    continue
                name = f"h{len(manifest) + 1:03d}.jpg"
                if _download(s, OUT_DIR / name):
                    manifest[name] = {"src": s, "post": u, "title": title}
                    seen_src.add(s)
                    n_new += 1
        ctx.close()

    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n신규 {n_new}장 → {OUT_DIR}")
    print("다음: 눈으로 확인하고 '{a|b|c}_주제_NN.jpg' 로 이름 붙여 drafts/photos/ 로 옮길 것.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--posts", type=int, default=15)
    ap.add_argument("--min-px", type=int, default=600)
    a = ap.parse_args()
    if not config.NAVER_BLOG_ID:
        print("[경고] NAVER_BLOG_ID 미설정 — 세션 없이도 공개 글은 읽힙니다.")
    harvest(a.posts, a.min_px)
