"""경쟁 글 분석 — 타깃 키워드로 상위 노출된 '다른' 블로그 글을 파악한다.

목적: 다음 글을 만들 때, 이미 상위에 있는 경쟁 글이 무엇을 다루는지 파악해
우리가 빠뜨린 소주제·단어를 반영한다. (내 글 made-us*는 제외)

수집(안정적인 것 위주):
  - 상위 경쟁 글 제목/블로그id (네이버 블로그탭)
  - 관련성 필터(키워드 토큰이 제목/본문에 있는 글만) — 호텔후기 등 오프토픽 제거
  - 상위 경쟁 글 본문 길이(벤치마크)
  - 경쟁 글에서 자주 쓰는 단어 중 우리 초안에 없는 것(= 보강 후보)
  - 자동완성(있으면). 니치 키워드는 대개 비어 있음.

저장: data/research/<slug>.json
사용:
  python research.py <초안파일 또는 키워드>   # 한 건 분석·저장·출력
  python research.py next                      # 다음 발행 예정글 키워드 분석
  python research.py all                        # 미발행 초안 전체(느림)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
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


def analyze(arg: str) -> dict:
    from playwright.sync_api import sync_playwright  # noqa: PLC0415
    from publish.browser import launch_context  # noqa: PLC0415

    keyword, draft = _kw_from(arg)
    kw_tokens = [w for w in re.split(r"\s+", keyword) if len(w) > 1]
    our_terms = set()
    our_len = None
    if draft:
        d = parse_draft(draft)
        our_body = " ".join(b.get("text", "") for b in d["blocks"])
        our_terms = set(_nouns(our_body + " " + d["title"]))
        our_len = len(our_body.replace(" ", ""))

    result = {"keyword": keyword, "draft": draft.name if draft else None,
              "competitors": [], "autocomplete": [], "gap_terms": [],
              "length_benchmark": None, "our_length": our_len}

    with sync_playwright() as p:
        ctx = launch_context(p, headed=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

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

        # 관련성 필터: 제목에 키워드 토큰이 하나라도 있는 '남의' 글
        comp = [x for x in posts if x["id"] not in OURS
                and any(tok in x["title"] for tok in kw_tokens)]

        lens, comp_terms = [], Counter()
        for x in comp[:MAX_COMPETITORS]:
            try:
                page.goto(f"https://m.blog.naver.com/{x['id']}/{x['no']}", timeout=25000)
                page.wait_for_timeout(1800)
                body = page.evaluate("""() => {
                  const c=document.querySelector('.se-main-container')||document.body;
                  return c.innerText || '';
                }""")
            except Exception:
                body = ""
            blen = len(re.sub(r"\s", "", body))
            x["length"] = blen
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

        ctx.close()

    return result


def _slug(kw: str) -> str:
    return re.sub(r"[^가-힣a-zA-Z0-9]+", "_", kw)[:40] or "kw"


def save(res: dict) -> Path:
    RDIR.mkdir(parents=True, exist_ok=True)
    f = RDIR / f"{_slug(res['keyword'])}.json"
    f.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return f


def _print(res: dict) -> None:
    print(f"\n키워드: {res['keyword']}  (초안: {res['draft']})")
    if res["autocomplete"]:
        print("자동완성:", res["autocomplete"])
    print(f"경쟁 글 {len(res['competitors'])}편:")
    for c in res["competitors"]:
        print(f"   {c['id']}  {c.get('length','?')}자  {c['title']}")
    b = res["length_benchmark"]
    if b:
        cmp = f"(우리 {res['our_length']}자)" if res["our_length"] else ""
        print(f"경쟁 본문 길이 중앙값: {b}자 {cmp}")
    if res["gap_terms"]:
        print("보강 후보 단어(경쟁글엔 자주, 우리 글엔 없음):", ", ".join(res["gap_terms"]))


def _next_draft() -> str | None:
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {"published": []}
    pub = set(state.get("published", []))
    a = sorted(DRAFTS.glob("sample*.md")) + sorted(DRAFTS.glob("a*.md"))
    order = a + sorted(DRAFTS.glob("b*.md")) + sorted(DRAFTS.glob("c*.md"))
    return next((p.name for p in order if p.name not in pub), None)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("target", help="초안파일 / 키워드 / 'next' / 'all'")
    a = ap.parse_args()

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
        targets = [a.target]

    for t in targets:
        res = analyze(t)
        f = save(res)
        _print(res)
        print("저장:", f)


if __name__ == "__main__":
    main()
