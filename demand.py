"""검색 수요 진단 — 우리 초안 키워드를 실제로 사람들이 검색하는지 측정한다.

'방문자 없음'의 근본 원인 진단용. 순위 1위여도 검색량 0이면 방문자는 0이다.
절대 검색량은 네이버 검색광고 API 키가 필요하므로, **자동완성 제안 수**를 수요 프록시로
쓴다(네이버는 검색 활동이 있는 단어에만 자동완성을 준다). 0=거의 죽은 키워드.

사용:
  python demand.py audit      # 전체 초안 키워드 수요 진단(낮은 순)
  python demand.py <키워드>   # 한 키워드 자동완성 확인
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

DRAFTS = ROOT / "drafts"
CACHE = ROOT / "data" / "demand_cache.json"


def _primary_kw(path: Path) -> str:
    t = path.read_text(encoding="utf-8")
    m = re.search(r"타깃\s*검색키워드[^:]*:\s*(.+)", t)
    return re.split(r"[,/·\n]", m.group(1).strip())[0].strip() if m else ""


def keyword_demand(page, kw: str) -> tuple[int, list[str]]:
    """(자동완성 제안 수, 샘플). 제안이 많을수록 검색 수요가 크다."""
    url = (f"https://ac.search.naver.com/nx/ac?q={quote(kw)}&con=0&frm=nv&ans=2"
           f"&r_format=json&st=100")
    try:
        r = page.request.get(url, headers={"referer": "https://search.naver.com/"}, timeout=12000)
        j = json.loads(r.text())
        sug = [it[0] for grp in j.get("items", []) for it in grp]
        return len(sug), sug[:6]
    except Exception:
        return -1, []


def audit() -> None:
    from playwright.sync_api import sync_playwright
    from publish.browser import launch_context

    files = (sorted(DRAFTS.glob("sample*.md")) + sorted(DRAFTS.glob("a*.md"))
             + sorted(DRAFTS.glob("b*.md")) + sorted(DRAFTS.glob("c*.md")))
    rows = []
    cache = {}
    with sync_playwright() as p:
        ctx = launch_context(p, headed=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        for f in files:
            kw = _primary_kw(f)
            if not kw:
                continue
            n, sug = keyword_demand(page, kw)
            rows.append((n, f.name, kw, sug))
            cache[kw] = n
        ctx.close()

    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")

    rows.sort(key=lambda r: r[0])
    dead = sum(1 for r in rows if r[0] == 0)
    live = sum(1 for r in rows if r[0] > 0)
    print(f"{'수요':>4}  {'초안':34} 키워드")
    for n, name, kw, sug in rows:
        mark = "💀" if n == 0 else ("🔥" if n >= 5 else "·")
        print(f"{n:>4} {mark} {name:32} {kw}")
    print(f"\n검색 수요 있음 {live}편 · 거의 없음(0) {dead}편 / 총 {len(rows)}편")
    print("→ 수요 0 키워드는 1위여도 방문자가 안 생긴다. 넓고 검색되는 키워드로 전환 필요.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("target", nargs="?", default="audit")
    a = ap.parse_args()
    if a.target == "audit":
        audit()
    else:
        from playwright.sync_api import sync_playwright
        from publish.browser import launch_context
        with sync_playwright() as p:
            ctx = launch_context(p, headed=False)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            n, sug = keyword_demand(page, a.target)
            print(f"'{a.target}' 자동완성 {n}개: {', '.join(sug)}")
            ctx.close()


if __name__ == "__main__":
    main()
