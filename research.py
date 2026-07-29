"""경쟁 글 분석 — 타깃 키워드로 상위 노출된 '다른' 블로그 글을 파악한다.

목적: 다음 글을 만들 때, 이미 상위에 있는 경쟁 글이 무엇을 다루는지 파악해
우리가 빠뜨린 소주제·단어를 반영한다. (내 글 made-us*는 제외)

수집(안정적인 것 위주):
  - 상위 경쟁 글 제목/블로그id/**순위** (네이버 블로그탭)
  - 관련성 필터(키워드 토큰이 제목/본문에 있는 글만) — 호텔후기 등 오프토픽 제거
  - 경쟁 글의 **실측 지표**: 본문 길이·이미지 수·소제목 수·태그·발행일
  - 경쟁 글에서 자주 쓰는 단어 중 우리 초안에 없는 것(= 보강 후보)
  - 자동완성(있으면). 니치 키워드는 대개 비어 있음.
  - **구글 SERP**(--google): 네이버 밖에서 누가 이기고 있는지

왜 지표까지 재나(2026-07-29 실측):
  '엑셀방송 피켓' 구글 2위 경쟁글(linosgj)은 본문 474자·소제목 0개짜리 짧은 글인데,
  우리 글은 2,247자·소제목 8개인데도 노출이 없었다. 즉 **길이로 지는 게 아니다.**
  차이는 제목·태그에 업계 용어(BJ·아프리카TV·엑셀방송·클럽)를 박았느냐였다.
  길이만 비교하면 이걸 못 본다 → 태그·제목·이미지까지 저장해 비교한다.

저장: data/research/<slug>.json (스냅샷을 날짜별로 누적 — 판이 바뀌는 걸 본다)
사용:
  python research.py <초안파일 또는 키워드>   # 한 건 분석·저장·출력
  python research.py next                      # 다음 발행 예정글 키워드 분석
  python research.py all                        # 미발행 초안 전체(느림)
  python research.py <키워드> --google         # 구글 SERP 도 수집(창이 뜬다)
  python research.py bench <키워드>            # 저장된 1등 vs 우리 비교표
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from statistics import median
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import config  # noqa: E402
from publish.draft_parser import parse_draft  # noqa: E402

DRAFTS = ROOT / "drafts"
STATE = ROOT / "data" / "publish_state.json"
RDIR = ROOT / "data" / "research"
OURS = {"made-us", "made-us2", "MyBlog"}
MAX_COMPETITORS = 5      # 본문까지 볼 경쟁 글 수
STOP = set("제작 그리고 하지만 그러나 우리 여기 이런 저런 그런 정도 경우 관련 통해 위해 대한 "
           "가장 매우 다양 다양한 사용 사용하 있습니다 합니다 됩니다 입니다 때문 이번 오늘 오늘도 "
           "블로그 포스팅 안녕하세요 문의 상담 바로 많은 것은 화면을 정말 진짜 하나 부분 모두 각각 "
           "생각 시작 준비 확인 소개 니다 세요 어요 아요 해서 하는 하고 되는 있는 없는 같은 다른 "
           "때문에 그램 이제 한번 다시 먼저 이후 이전 조금 아주 역시 물론 특히 바로가기".split())
# 흔한 조사/어미 꼬리 — 토큰 끝에서 제거해 정규화(화면을->화면, 조명이->조명)
_JOSA = ("으로", "에서", "에게", "까지", "부터", "이나", "이라", "라고", "고요",
         "을", "를", "이", "가", "은", "는", "에", "의", "도", "로", "와", "과", "만", "요", "죠")


def _kw_from(arg: str) -> tuple[str, Path | None]:
    """인자가 초안파일이면 (대표키워드, 경로), 아니면 (키워드, None)."""
    p = DRAFTS / arg if (DRAFTS / arg).exists() else Path(arg)
    if p.exists() and p.suffix == ".md":
        t = p.read_text(encoding="utf-8")
        m = re.search(r"타깃\s*검색키워드[^:]*:\s*(.+)", t)
        kw = re.split(r"[,/·\n]", m.group(1).strip())[0].strip() if m else p.stem
        return kw, p
    return arg, None


def _strip_josa(w: str) -> str:
    for j in _JOSA:
        if len(w) > len(j) + 1 and w.endswith(j):
            return w[: -len(j)]
    return w


# 동사/형용사 활용형으로 끝나는 토큰(명사 아님) 배제
_VERBISH = re.compile(r"(습니다|합니다|해요|하게|하는|해주|주셨|셨|였|웠|드립|드려|해서|하고|해도|"
                      r"됩니|되어|보다|주는|나요|까요|어요|아요)$")


def _nouns(text: str) -> set[str]:
    """한글 2~6자 토큰 추출(간이 명사후보). 조사 꼬리 정규화 + 불용어/동사형 제거. 집합 반환."""
    out = set()
    for w in re.findall(r"[가-힣]{2,6}", text):
        w = _strip_josa(w)
        if len(w) >= 2 and w not in STOP and not _VERBISH.search(w):
            out.add(w)
    return out


# 경쟁 글에서 실제로 뽑히는 값들(2026-07-29 실측 확인).
# 갤러리 1개에 사진 7장이 들어가므로 se-image 컴포넌트 수와 img 태그 수를 따로 센다.
_POST_JS = """() => {
  const c = document.querySelector('.se-main-container') || document.body;
  const txt = c.innerText || '';
  const tags = Array.from(document.querySelectorAll('a'))
      .map(a => (a.innerText || '').trim())
      .filter(t => /^#\\S/.test(t)).slice(0, 20);
  const d = document.querySelector('.se_publishDate, .blog_date, .date, time');
  return {
    length: txt.replace(/\\s/g, '').length,
    images: c.querySelectorAll('img').length,
    image_blocks: c.querySelectorAll('.se-component.se-image').length,
    headings: c.querySelectorAll('.se-component.se-sectionTitle').length,
    quotes: c.querySelectorAll('.se-component.se-quotation').length,
    videos: c.querySelectorAll('.se-component.se-video, video, iframe').length,
    tags: tags,
    published: d ? (d.innerText || d.getAttribute('datetime') || '').trim() : '',
    body: txt,
  };
}"""


def _serp_google(page, keyword: str, limit: int = 10) -> list[dict]:
    """구글 상위 결과. **headed 브라우저에서만 동작**한다.

    headless 로 열면 구글이 결과 대신 6KB 짜리 차단 페이지를 준다(2026-07-29 실측).
    실패하면 빈 리스트 — 0건을 '경쟁 없음'으로 오해하지 않도록 호출부에서 구분할 것.
    """
    try:
        page.goto(f"https://www.google.com/search?q={quote(keyword)}&hl=ko&gl=kr&num=20",
                  timeout=30000)
        page.wait_for_timeout(1800)
    except Exception:
        return []
    rows = page.evaluate("""() => {
        const out = [];
        for (const a of document.querySelectorAll('a[href^="http"]')) {
            const h = a.getAttribute('href');
            if (!h || h.includes('google.com')) continue;
            const t = a.querySelector('h3');
            if (!t) continue;
            out.push({url: h, title: (t.innerText || '').slice(0, 70)});
        }
        return out;
    }""")
    seen, out = set(), []
    for r in rows:
        if r["url"] in seen:
            continue
        seen.add(r["url"])
        host = re.sub(r"^https?://(www\.)?", "", r["url"]).split("/")[0]
        m = re.search(r"blog\.naver\.com/(?:PostView\.naver\?blogId=)?([a-zA-Z0-9_-]+)", r["url"])
        out.append({"rank": len(out) + 1, "url": r["url"], "title": r["title"],
                    "host": host, "blog_id": m.group(1) if m else None})
        if len(out) >= limit:
            break
    return out


def _analyze_on(page, arg: str, google: bool) -> dict:
    keyword, draft = _kw_from(arg)
    kw_tokens = [w for w in re.split(r"\s+", keyword) if len(w) > 1]
    our_terms = set()
    our_len = None
    our = None
    if draft:
        d = parse_draft(draft)
        our_body = " ".join(b.get("text", "") for b in d["blocks"])
        our_terms = set(_nouns(our_body + " " + d["title"]))
        our_len = len(our_body.replace(" ", ""))
        our = {"title": d["title"], "length": our_len, "tags": d["tags"],
               "images": sum(1 for b in d["blocks"] if b["kind"] == "image"),
               "headings": sum(1 for b in d["blocks"] if b["kind"] == "heading")}

    result = {"keyword": keyword, "draft": draft.name if draft else None,
              "date": str(date.today()),
              "competitors": [], "autocomplete": [], "gap_terms": [],
              "length_benchmark": None, "our_length": our_len, "ours": our,
              "google": None}

    # 자동완성(있으면)
    try:
        r = page.request.get(
            f"https://ac.search.naver.com/nx/ac?q={quote(keyword)}&con=0&frm=nv&ans=2"
            f"&r_format=json&st=100",
            headers={"referer": "https://search.naver.com/"}, timeout=12000)
        j = json.loads(r.text())
        result["autocomplete"] = [it[0] for grp in j.get("items", []) for it in grp][:10]
    except Exception:
        pass

    # 상위 블로그 글
    page.goto(f"https://search.naver.com/search.naver?ssc=tab.blog.all&query={quote(keyword)}",
              timeout=30000)
    page.wait_for_timeout(1500)
    posts = page.evaluate("""() => {
      const out=[], seen=new Set();
      for(const a of document.querySelectorAll('a')){
        const m=(a.href||'').match(/blog\\.naver\\.com\\/([a-zA-Z0-9_-]+)\\/(\\d+)/);
        if(!m) continue;
        const key=m[1]+'/'+m[2]; if(seen.has(key)) continue; seen.add(key);
        const t=(a.innerText||'').trim().split('\\n')[0];
        if(t.length>=8) out.push({id:m[1], no:m[2], title:t.slice(0,50)});
      }
      return out.slice(0,15);
    }""")
    # SERP 순위를 붙인다 — '1등이 무엇을 했나'를 봐야 따라잡을 기준이 생긴다.
    for i, x in enumerate(posts, 1):
        x["rank"] = i
    result["serp_total"] = len(posts)

    # 관련성 필터: 제목에 키워드 토큰이 하나라도 있는 '남의' 글
    comp = [x for x in posts if x["id"] not in OURS
            and any(tok in x["title"] for tok in kw_tokens)]

    lens, comp_terms = [], Counter()
    for x in comp[:MAX_COMPETITORS]:
        try:
            page.goto(f"https://m.blog.naver.com/{x['id']}/{x['no']}", timeout=25000)
            page.wait_for_timeout(1800)
            m = page.evaluate(_POST_JS)
        except Exception:
            m = None
        body = (m or {}).get("body", "")
        blen = (m or {}).get("length", 0)
        x["length"] = blen
        # 길이만 재면 '왜 지는지'를 못 본다 — 제목·태그·구성까지 같이 저장한다.
        for k in ("images", "image_blocks", "headings", "quotes", "videos",
                  "tags", "published"):
            x[k] = (m or {}).get(k)
        if blen > 300:
            lens.append(blen)
            comp_terms.update(_nouns(body))
        result["competitors"].append(x)

    if lens:
        result["length_benchmark"] = int(median(lens))
    # 경쟁 글이 자주 쓰는데 우리 글엔 없는 단어(키워드 토큰 제외)
    gaps = [(w, c) for w, c in comp_terms.most_common(40)
            if c >= 2 and w not in our_terms and w not in kw_tokens and len(w) >= 2]
    result["gap_terms"] = [w for w, _ in gaps[:12]]
    # 경쟁 글이 실제로 쓰는 태그(빈도순) — 우리가 안 쓰는 것이 곧 빈틈이다.
    tag_cnt = Counter(t.lstrip("#") for x in result["competitors"]
                      for t in (x.get("tags") or []))
    our_tags = {t.replace(" ", "") for t in (our or {}).get("tags", [])}
    result["competitor_tags"] = [{"tag": t, "n": n} for t, n in tag_cnt.most_common(20)]
    result["missing_tags"] = [t for t, n in tag_cnt.most_common(20)
                              if n >= 2 and t not in our_tags][:10]

    if google:
        result["google"] = _serp_google(page, keyword)

    return result


def analyze(arg: str, google: bool = False) -> dict:
    return analyze_many([arg], google)[0]


def analyze_many(args: list[str], google: bool = False) -> list[dict]:
    """여러 키워드를 **브라우저 한 번**으로 처리한다.

    키워드마다 컨텍스트를 새로 열면 구글 수집(headed) 때 창이 그만큼 떴다 사라진다.
    """
    from playwright.sync_api import sync_playwright  # noqa: PLC0415
    from publish.browser import launch_context  # noqa: PLC0415

    out = []
    with sync_playwright() as p:
        # 구글은 headless 를 차단한다 — 구글까지 볼 때만 창을 띄운다.
        ctx = launch_context(p, headed=google)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        for arg in args:
            try:
                out.append(_analyze_on(page, arg, google))
            except Exception as e:
                print(f"[실패] {arg}: {type(e).__name__}: {e}")
        ctx.close()
    return out


def _slug(kw: str) -> str:
    return re.sub(r"[^가-힣a-zA-Z0-9]+", "_", kw)[:40] or "kw"


SNAP_KEEP = 12   # 키워드당 보관할 스냅샷 수(주 1회면 3개월치)


def save(res: dict) -> Path:
    """최신 결과를 저장하되, 과거 스냅샷을 함께 남긴다.

    덮어쓰기만 하면 '판이 어떻게 바뀌었나'(1등이 교체됐나, 우리가 올라왔나)를 못 본다.
    비교에 쓰는 값만 추려 담아 파일이 무한정 커지지 않게 한다.
    """
    RDIR.mkdir(parents=True, exist_ok=True)
    f = RDIR / f"{_slug(res['keyword'])}.json"

    snaps = []
    if f.exists():
        try:
            snaps = json.loads(f.read_text(encoding="utf-8")).get("snapshots", [])
        except Exception:
            snaps = []
    snap = {"date": res.get("date"),
            "top": [{k: c.get(k) for k in ("rank", "id", "title", "length",
                                           "images", "headings", "tags")}
                    for c in res["competitors"][:3]],
            "ours": res.get("ours"),
            "google_top": [{k: g.get(k) for k in ("rank", "host", "title", "blog_id")}
                           for g in (res.get("google") or [])[:5]]}
    snaps = [s for s in snaps if s.get("date") != snap["date"]] + [snap]
    res["snapshots"] = snaps[-SNAP_KEEP:]

    f.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return f


def _print(res: dict) -> None:
    print(f"\n키워드: {res['keyword']}  (초안: {res['draft']})")
    if res["autocomplete"]:
        print("자동완성:", res["autocomplete"])
    print(f"경쟁 글 {len(res['competitors'])}편:")
    for c in res["competitors"]:
        print(f"  #{c.get('rank','?'):<2} {c['id'][:16]:18} {c.get('length','?'):>5}자 "
              f"이미지{c.get('images') or 0:>3} 소제목{c.get('headings') or 0:>2} "
              f"태그{len(c.get('tags') or []):>2}  {c['title'][:34]}")
    b = res["length_benchmark"]
    if b:
        cmp = f"(우리 {res['our_length']}자)" if res["our_length"] else ""
        print(f"경쟁 본문 길이 중앙값: {b}자 {cmp}")
    if res.get("missing_tags"):
        print("경쟁이 쓰는데 우리엔 없는 태그:", ", ".join("#" + t for t in res["missing_tags"]))
    if res["gap_terms"]:
        print("보강 후보 단어(경쟁글엔 자주, 우리 글엔 없음):", ", ".join(res["gap_terms"]))
    if res.get("google"):
        print("구글 상위:")
        for g in res["google"][:8]:
            mark = " ★네이버블로그" if "blog.naver.com" in g["url"] else ""
            print(f"  #{g['rank']:<2} {g['host'][:28]:30} {g['title'][:40]}{mark}")


def bench(keyword: str) -> None:
    """저장된 벤치마크로 '1등과 우리가 무엇이 다른가'를 표로 본다."""
    f = RDIR / f"{_slug(keyword)}.json"
    if not f.exists():
        print(f"'{keyword}' 벤치마크가 없습니다 — 먼저 research.py '{keyword}' 실행")
        return
    res = json.loads(f.read_text(encoding="utf-8"))
    comps = res.get("competitors") or []
    if not comps:
        print(f"'{keyword}': 경쟁 글이 수집되지 않았습니다(니치 키워드일 수 있음).")
        return
    top = comps[0]
    our = res.get("ours")

    print(f"=== '{keyword}' 벤치마크 ({res.get('date','?')} 수집) ===")
    print(f"네이버 1등: {top['id']}  {top['title']}")
    rows = [("본문 길이", top.get("length"), (our or {}).get("length")),
            ("이미지", top.get("images"), (our or {}).get("images")),
            ("소제목", top.get("headings"), (our or {}).get("headings")),
            ("태그 수", len(top.get("tags") or []), len((our or {}).get("tags") or []))]
    print(f"\n{'항목':10} {'1등':>8} {'우리':>8}   차이")
    for name, a, b in rows:
        if a is None or b is None:
            print(f"{name:10} {str(a):>8} {str(b):>8}   -")
            continue
        d = b - a
        print(f"{name:10} {a:>8} {b:>8}   {d:+}")
    if top.get("tags"):
        print(f"\n1등 태그: {' '.join(top['tags'])}")
    if our and our.get("tags"):
        print(f"우리 태그: {' '.join('#' + t for t in our['tags'])}")
    if res.get("missing_tags"):
        print(f"\n▶ 우리에게 없는 경쟁 태그: {', '.join('#' + t for t in res['missing_tags'])}")
    if res.get("gap_terms"):
        print(f"▶ 보강 후보 단어: {', '.join(res['gap_terms'])}")

    g = res.get("google") or []
    if g:
        print(f"\n구글 상위 {len(g)}건:")
        for x in g[:8]:
            mark = " ★네이버블로그" if "blog.naver.com" in x["url"] else ""
            print(f"  #{x['rank']:<2} {x['host'][:28]:30} {x['title'][:38]}{mark}")

    snaps = res.get("snapshots") or []
    if len(snaps) > 1:
        print(f"\n스냅샷 {len(snaps)}회 — 1등 변화:")
        for s in snaps[-5:]:
            t = (s.get("top") or [{}])[0]
            print(f"  {s.get('date')}  {t.get('id','?'):16} {str(t.get('length','?')):>6}자")


def _next_draft() -> str | None:
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {"published": []}
    pub = set(state.get("published", []))
    a = sorted(DRAFTS.glob("sample*.md")) + sorted(DRAFTS.glob("a*.md"))
    order = a + sorted(DRAFTS.glob("b*.md")) + sorted(DRAFTS.glob("c*.md"))
    return next((p.name for p in order if p.name not in pub), None)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("target", help="초안파일 / 키워드 / 'next' / 'all' / 'bench'")
    ap.add_argument("keyword", nargs="?", help="bench 에서 볼 키워드")
    ap.add_argument("--google", action="store_true",
                    help="구글 SERP 도 수집(브라우저 창이 뜬다 — 구글이 headless 를 차단)")
    a = ap.parse_args()

    if a.target == "bench":
        if not a.keyword:
            print("사용: python research.py bench <키워드>")
            return
        bench(a.keyword)
        return

    if a.target == "next":
        nx = _next_draft()
        if not nx:
            print("다음 발행 예정글 없음")
            return
        targets = [nx]
    elif a.target == "all":
        state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {"published": []}
        pub = set(state.get("published", []))
        targets = [p.name for p in sorted(DRAFTS.glob("[abc]*.md")) if p.name not in pub]
    else:
        # 쉼표로 여러 키워드를 한 번에(브라우저 한 번만 뜬다)
        targets = [s.strip() for s in a.target.split(",") if s.strip()]

    for res in analyze_many(targets, google=a.google):
        f = save(res)
        _print(res)
        print("저장:", f)


if __name__ == "__main__":
    main()
