"""효과 측정 지표 수집기.

에이전트가 실제로 성과를 내는지 증명하기 위한 시계열을 모은다:
  - 방문자: 블로그 홈의 오늘/전체 숫자를 매일 스냅샷 → 일별 추이
  - 키워드 순위: 발행글의 타깃 검색어를 네이버 블로그탭에서 검색해 우리 순위 기록

저장: data/metrics.json (날짜별 1레코드, 재실행 시 갱신)
검색량(월간 검색수)은 네이버 검색광고 API 키가 있어야 하므로 키가 있을 때만 채운다.

사용:
  python metrics.py collect          # 방문자 + (하루 1회) 키워드 순위 수집
  python metrics.py collect --ranks  # 순위도 강제 재수집
  python metrics.py show             # 최근 스냅샷 출력
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import config  # noqa: E402
from publish.draft_parser import parse_draft  # noqa: E402  (미사용이지만 초안 존재 확인용)

DRAFTS = ROOT / "drafts"
STATE = ROOT / "data" / "publish_state.json"
METRICS = ROOT / "data" / "metrics.json"

# 순위 집계에서 제외할 네비게이션/고정 링크의 가짜 blogId
_NOISE_IDS = {"MyBlog", "PostList", "PostView", "section", "search", "m",
              "GuestBook", "guestbook", "prologue"}


def _load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save(path: Path, obj) -> None:
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def primary_keyword(draft: Path) -> str:
    """초안 메타의 '타깃 검색키워드' 첫 구절을 대표 키워드로."""
    t = draft.read_text(encoding="utf-8")
    # '타깃 검색키워드(주력):' 처럼 괄호 주석이 붙는 경우가 있어 콜론까지 건너뛴다.
    m = re.search(r"타깃\s*검색키워드[^:]*:\s*(.+)", t)
    if not m:
        return ""
    return re.split(r"[,/·\n]", m.group(1).strip())[0].strip()


def published_keywords() -> dict[str, str]:
    """{초안파일명: 대표키워드} — 발행 완료된 글만."""
    state = _load(STATE, {"published": []})
    out: dict[str, str] = {}
    for name in state.get("published", []):
        p = DRAFTS / name
        if p.exists():
            kw = primary_keyword(p)
            if kw:
                out[name] = kw
    return out


# ---- 브라우저 사용 수집 (playwright 지연 임포트) ----

def _visitor_counts(page) -> dict:
    blog = config.NAVER_BLOG_ID or "made-us"
    page.goto(f"https://m.blog.naver.com/{blog}", timeout=30000)
    page.wait_for_timeout(2000)
    head = page.inner_text("body")[:150]
    m = re.search(r"오늘\s*([\d,]+).*?전체\s*([\d,]+)", head)
    if not m:
        return {}
    return {"today": int(m.group(1).replace(",", "")),
            "total": int(m.group(2).replace(",", ""))}


def _rank_of(page, keyword: str, blog: str) -> int | None:
    url = f"https://search.naver.com/search.naver?ssc=tab.blog.all&query={quote(keyword)}"
    try:
        page.goto(url, timeout=30000)
        page.wait_for_timeout(1200)
    except Exception:
        return None
    order = page.evaluate("""() => {
      const seen=new Set(), out=[];
      for (const a of document.querySelectorAll('a')) {
        const h=a.href||'';
        if(!/blog\\.naver\\.com|m\\.blog\\.naver\\.com/.test(h)) continue;
        const m=h.match(/blog\\.naver\\.com\\/([a-zA-Z0-9_-]+)/)||h.match(/blogId=([a-zA-Z0-9_-]+)/);
        if(!m) continue;
        const id=m[1];
        if(!seen.has(id)){ seen.add(id); out.push(id); }
      }
      return out;
    }""")
    order = [x for x in order if x not in _NOISE_IDS]
    return (order.index(blog) + 1) if blog in order else None


def collect(force_ranks: bool = False) -> dict:
    from playwright.sync_api import sync_playwright  # noqa: PLC0415
    from publish.browser import launch_context  # noqa: PLC0415

    blog = config.NAVER_BLOG_ID or "made-us"
    today = str(date.today())
    data = _load(METRICS, {"visitors": {}, "ranks": {}, "keywords": {}})
    kw_map = published_keywords()
    data["keywords"] = {k: {"kw": v} for k, v in kw_map.items()}

    need_ranks = force_ranks or today not in data.get("ranks", {})

    with sync_playwright() as p:
        ctx = launch_context(p, headed=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        vc = _visitor_counts(page)
        if vc:
            data.setdefault("visitors", {})[today] = vc
            print(f"[방문자] 오늘 {vc['today']} / 전체 {vc['total']}")

        if need_ranks and kw_map:
            ranks = {}
            for name, kw in kw_map.items():
                r = _rank_of(page, kw, blog)
                ranks[kw] = r
                print(f"[순위] '{kw}' -> {r if r else '30위권 밖'}")
            data.setdefault("ranks", {})[today] = ranks
        elif not need_ranks:
            print("[순위] 오늘 이미 수집됨(건너뜀). 강제: --ranks")

        ctx.close()

    _save(METRICS, data)
    return data


def show() -> None:
    data = _load(METRICS, {"visitors": {}, "ranks": {}})
    vs = data.get("visitors", {})
    print(f"방문자 스냅샷 {len(vs)}일치:")
    for d in sorted(vs)[-7:]:
        print(f"  {d}: 오늘 {vs[d].get('today')} / 전체 {vs[d].get('total')}")
    rk = data.get("ranks", {})
    if rk:
        latest = sorted(rk)[-1]
        print(f"\n최근 순위({latest}):")
        for kw, r in rk[latest].items():
            print(f"  {kw}: {r if r else '-'}")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    cp = sub.add_parser("collect")
    cp.add_argument("--ranks", action="store_true", help="순위 강제 재수집")
    sub.add_parser("show")
    a = ap.parse_args()
    if a.cmd == "collect":
        collect(force_ranks=a.ranks)
    else:
        show()


if __name__ == "__main__":
    main()
