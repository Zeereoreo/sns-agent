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
import os
import re
import sys
from datetime import date, datetime
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
    # 파일이 없으면 default(정상 시작). 있는데 못 읽으면 예외 → 기존 히스토리를
    # 빈값으로 덮어써 날리는 것을 막는다(수집만 이번에 건너뜀).
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _save(path: Path, obj) -> None:
    """원자적 저장(임시파일 → os.replace)."""
    path.parent.mkdir(exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


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


def _rank_of(page, keyword: str, blog: str, mobile: bool = True) -> tuple[int | None, int]:
    """(순위, SERP에서 확인된 블로그 결과 수) 반환.
    결과 수가 0이면 스크래핑 실패/차단 가능성 → 호출측이 '순위 이탈'로 오기록하지 않는다.

    ★기본을 **모바일**로 둔다(2026-07-30). 실측 유입 referrer 가 거의 전부
    `m.search.naver.com` 이었는데 우리는 PC SERP 로만 순위를 재고 있었다 —
    모바일과 PC 는 결과가 다르므로 **엉뚱한 화면을 보고 순위를 판단**하고 있었다.
    """
    host = "m.search.naver.com" if mobile else "search.naver.com"
    url = f"https://{host}/search.naver?ssc=tab.blog.all&query={quote(keyword)}"
    try:
        page.goto(url, timeout=30000)
        page.wait_for_timeout(1500)
    except Exception:
        return None, 0
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
    rank = (order.index(blog) + 1) if blog in order else None
    return rank, len(order)


def _search_inflow(page, blog: str) -> dict | None:
    """네이버 크리에이터 어드바이저에서 **검색 유입수**를 가져온다.

    방문자 누계는 이웃·우리 자신·직접 유입이 섞여 실제 성장을 못 보여준다.
    성장 목표는 '검색으로 들어오는 사람'이고, 그 수치는 이 API 에만 있다.
    2026-07-30 에 처음 재보니 7/21~27 매일 있었고(7일 합 13명, 7/26 정점 5명)
    **7/28 부터 0** 이었다 — 내 대량 편집 경계와 일치. 이걸 안 재고 있었다.

    반환 {YYYY-MM-DD: {"cv":조회, "uv":순방문, "search":검색유입}} (직전 2일).
    SPA 전용 헤더가 필요해 page.request 로는 403 이라 **페이지 안 fetch** 로 부른다.
    """
    try:
        page.goto(f"https://creator-advisor.naver.com/naver_blog/{blog}", timeout=35000)
        page.wait_for_timeout(3500)
        body = page.evaluate(
            """async (url) => {
                const r = await fetch(url, {credentials: 'include'});
                return {s: r.status, b: await r.text()};
            }""",
            f"https://creator-advisor.naver.com/api/v6/home/yesterday-summary"
            f"?channelId={blog}&service=naver_blog&date={date.today().isoformat()}")
        if body.get("s") != 200:
            return None
        d = json.loads(body["b"])["data"]
        out = {}
        for i, day in enumerate(d.get("date") or []):
            out[day] = {"cv": d["cv"]["cv"][i], "uv": d["cv"]["uv"][i],
                        "search": d["searchInflow"]["searchInflow"][i]}
        return out or None
    except Exception:
        return None


def _inflow_queries(page, blog: str) -> dict | None:
    """**실제로 사람들이 무엇을 검색해서 들어왔는지.** 우리가 가진 유일한 정답지다.

    2026-07-30 에 처음 봤다. 열흘 동안 자동완성 '수요 프록시'로 키워드를 고르면서
    정작 실측 유입어를 안 보고 있었다. 실제 유입어는 이랬다:
      7/21 LED피켓 · 7/23 led 피켓 제작/아크릴 메뉴판 · 7/26 vip피켓·led 응원 피켓·
      휴대용led응원피켓 · 7/27 led 응원 피켓·비제이 전광판 구매
    → 거의 전부 **모바일 검색(m.search.naver.com)** 이고 **BJ/피켓 계열**이다.
      자동완성 프록시는 '휴대용led응원피켓' 같은 롱테일을 아예 못 잡는다.

    API 를 직접 부르면 403 이다(x-ca-sig 서명 헤더 필요) → **SPA 응답을 가로챈다.**
    반환 {YYYY-MM-DD: [{q, ratio}]}.
    """
    got: dict[str, list] = {}

    def on_resp(r):
        if "/api/v6/inflow-analysis/referrer-query-rank" not in r.url:
            return
        try:
            rows = json.loads(r.text()).get("data") or []
        except Exception:
            return
        for row in rows:
            top = [{"q": t.get("searchQuery"), "ratio": round(t.get("ratio", 0), 3)}
                   for t in (row.get("topN") or []) if t.get("searchQuery")]
            if top:
                got[row.get("date")] = top

    page.on("response", on_resp)
    try:
        page.goto(f"https://creator-advisor.naver.com/naver_blog/{blog}"
                  "/inflow-analysis#by-rq-count", timeout=45000)
        page.wait_for_timeout(6000)
        # 며칠치를 더 훑는다(하루만 보면 표본이 1~2건이라 아무것도 못 읽는다)
        for _ in range(6):
            try:
                page.get_by_text("이전 기간 조회", exact=False).first.click(timeout=3500)
                page.wait_for_timeout(2500)
            except Exception:
                break
    except Exception:
        pass
    finally:
        try:
            page.remove_listener("response", on_resp)
        except Exception:
            pass
    return got or None


def _indexed_count(page, blog: str, titles: list[str]) -> dict | None:
    """발행글 제목을 그대로 검색해 **네이버가 우리 글을 색인했는지** 본다.

    2026-07-29 에 made-us2 가 2주·18편 동안 **한 편도 색인되지 않은 것**을 뒤늦게
    발견했다. 순위·키워드·태그는 전부 색인이 됐다는 전제 위에서만 의미가 있는데,
    그 전제를 아무도 확인하지 않고 있었다. 제목 정확 검색은 색인만 돼 있으면
    거의 반드시 잡히므로, 색인 여부의 가장 싼 시험지다.

    반환 {sampled, found, titles_found}. 수집 자체가 실패하면 None(0 으로 적지 않는다).
    """
    if not titles:
        return None
    found, checked = [], 0
    for t in titles:
        try:
            page.goto("https://search.naver.com/search.naver?ssc=tab.blog.all"
                      f"&query={quote(t)}", timeout=30000)
            page.wait_for_timeout(1500)
            ids = page.evaluate("""() => {
              const out=[];
              for (const a of document.querySelectorAll('a[href*="blog.naver.com"]')) {
                const m=a.href.match(/blog\\.naver\\.com\\/([a-zA-Z0-9_-]+)/);
                if(m) out.push(m[1]);
              }
              return Array.from(new Set(out));
            }""")
        except Exception:
            continue
        if not ids:          # SERP 가 비었으면 스크래핑 실패 — 표본에서 뺀다
            continue
        checked += 1
        if blog in ids:
            found.append(t)
    if not checked:
        return None
    return {"sampled": checked, "found": len(found), "titles_found": found}


def _session_ok(page, blog: str) -> bool | None:
    """write 페이지가 로그인으로 튕기지 않으면 세션 유효. 판단 불가 시 None."""
    try:
        page.goto(f"https://blog.naver.com/{blog}/postwrite", timeout=30000)
        page.wait_for_timeout(2500)
        url = page.url
    except Exception:
        return None
    if "nidlogin" in url or "nid.naver.com" in url:
        return False
    if "postwrite" in url or f"/{blog}" in url:
        return True
    return None


SESSION_WARN_DAYS = 7


def _session_expiry(ctx) -> str | None:
    """인증 쿠키(NID_AUT/NID_SES)의 만료일. 영속이 아니면 None.

    세션이 죽고 나서야 아는 구조라 2026-07-25~28 에 발행 4일을 날렸다.
    만료일을 미리 알면 죽기 전에 갱신할 수 있다.
    """
    try:
        exps = [c.get("expires") for c in ctx.cookies()
                if c.get("name") in ("NID_AUT", "NID_SES")
                and "naver" in (c.get("domain") or "")
                and (c.get("expires") or -1) > 0]
        if not exps:
            return None
        return date.fromtimestamp(min(exps)).isoformat()
    except Exception:
        return None


def _warn_session_soon(expires: str, days_left: int) -> None:
    """만료가 가까우면 미리 알린다(죽고 나서가 아니라)."""
    try:
        import notify  # noqa: PLC0415
        notify.notify("SNS Agent 세션 만료 임박",
                      f"네이버 세션이 {days_left}일 뒤({expires}) 만료됩니다 — 미리 재로그인하세요.")
        notify.write_alert(
            f"네이버 세션이 {days_left}일 뒤 만료됩니다(만료일 {expires}).\n"
            f"아직 발행은 되지만, 만료되면 예약 발행이 전부 실패합니다.\n\n"
            f"  cd {ROOT}\n"
            f"  .\\.venv\\Scripts\\python.exe publish\\naver.py login\n\n"
            f"로그인 창에서 '로그인 상태 유지'를 반드시 체크하세요.\n")
    except Exception as e:
        print("만료 임박 알림 실패:", e)


def _external_indexed(page, blog: str) -> int | None:
    """네이버 밖 검색엔진에 우리 글이 몇 편이나 색인됐는지.

    구글은 자동 조회를 차단하므로(검색 결과 대신 차단 페이지를 준다) 같은 웹 크롤 기반인
    DuckDuckGo(빙 인덱스)로 대신 잰다. **구글 순위가 아니라 '색인 여부' 프록시**다.
    실패하면 None — 0 으로 적어 가짜 하락을 만들지 않는다.

    DDG 는 봇 차단을 **HTTP 200 + 캡차 페이지**로 준다("Select all squares containing a duck").
    그대로 세면 0 편이 나와 '색인 안 됨'으로 읽히는 가짜 데이터가 된다 → 차단 페이지는 None.
    """
    try:
        q = quote(f"site:blog.naver.com {blog}")
        r = page.request.get(f"https://html.duckduckgo.com/html/?q={q}",
                             headers={"user-agent": "Mozilla/5.0"}, timeout=20000)
        if not r.ok:
            return None
        html = r.text()
        if "anomaly" in html or "error-lite@duckduckgo.com" in html:
            return None
        return len(set(re.findall(rf"blog\.naver\.com/{re.escape(blog)}/(\d{{6,}})", html)))
    except Exception:
        return None


def _recent_titles(n: int = 3) -> list[str]:
    """색인 시험용 표본 제목 — **오래된 글 위주 + 최신 1편**(중복 제거).

    최신 3편만 재던 것을 2026-07-31 에 고쳤다. 새 글은 건강한 블로그에서도 색인까지
    며칠 걸리므로, 최신만 보면 '아직 안 됨'을 '색인에서 빠짐'으로 오독한다.
    그 오독이 편집 게이트·발행 상한까지 좌우하므로 표본을 양끝으로 벌린다.
    (같은 날 실측: made-us2 는 7/22 글까지 전부 검색에 없다 — 넓은 표본이라야 보인다.)
    """
    state = _load(ROOT / "data" / "publish_state.json", {})
    titles: list[str] = []
    for e in state.get("log", []):          # log 는 오래된 것부터
        if e.get("ok") and not e.get("dry") and e.get("title"):
            if e["title"] not in titles:
                titles.append(e["title"])
    if len(titles) <= n:
        return titles
    return titles[:n - 1] + titles[-1:]     # 오래된 n-1 편 + 최신 1편


def _warn_not_indexed(today: str, sampled: int) -> None:
    """색인 0 — 발행을 아무리 해도 검색 유입이 안 생기는 상태라 즉시 알린다."""
    try:
        import notify  # noqa: PLC0415
        notify.notify("SNS Agent 색인 안 됨",
                      f"발행글 {sampled}편을 제목으로 검색해도 우리 블로그가 안 나옵니다.")
        notify.write_alert(
            f"네이버 색인이 확인되지 않습니다({today} 점검).\n"
            f"발행글 {sampled}편의 제목을 그대로 검색해도 우리 블로그가 나오지 않습니다.\n\n"
            f"색인이 안 되면 키워드·태그·순위 작업은 전부 효과가 없습니다.\n"
            f"블로그 활동성(이웃·댓글)과 개설 경과 기간을 확인하세요.\n")
    except Exception as e:
        print("색인 경고 실패:", e)


def _warn_session_expired(today: str) -> None:
    """세션 만료를 즉시 알린다(트레이 풍선 + 바탕화면 경고). 발행 실패를 기다리지 않는다."""
    try:
        import notify  # noqa: PLC0415
        notify.notify("SNS Agent 세션 만료",
                      "네이버 세션이 만료됐습니다 — 재로그인 전까지 발행이 안 됩니다.")
        notify.write_alert(
            f"네이버 세션이 만료됐습니다({today} 점검).\n"
            f"재로그인 전까지 예약 발행이 전부 실패합니다.\n\n"
            f"  cd {ROOT}\n"
            f"  .\\.venv\\Scripts\\python.exe publish\\naver.py login\n\n"
            f"로그인 창에서 '로그인 상태 유지'를 반드시 체크하세요.\n"
            f"발행이 정상화되면 이 파일은 자동으로 사라집니다.\n")
    except Exception as e:
        print("세션 만료 알림 실패:", e)


def collect(force_ranks: bool = False) -> dict:
    from playwright.sync_api import sync_playwright  # noqa: PLC0415
    from publish.browser import launch_context  # noqa: PLC0415

    blog = config.NAVER_BLOG_ID or "made-us"
    if not config.NAVER_BLOG_ID:
        print(f"[경고] NAVER_BLOG_ID 미설정 — '{blog}' 로 가정. .env 확인(테스트=made-us2).")
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

        # 발행글 대표 키워드 + 운영자가 대시보드에서 추가한 키워드(초안이 없어도 추적한다).
        targets = list(kw_map.items())
        for kw in config.load_keywords():
            if kw not in kw_map.values():
                targets.append((f"(직접 추가) {kw}", kw))

        # 🔴 **실제로 사람이 들어온 검색어**를 순위 추적에 자동으로 넣는다(2026-08-10).
        # 그동안 이게 빠져 있었다: 실측으로 순위에 든 9개 중 유입이 온 건 2개뿐이고,
        # 반대로 실측 유입어 8개 중 순위를 추적하던 것도 2개뿐이었다.
        # 즉 **검색하는 사람이 없는 판에서 1위를 재고, 정작 사람이 오는 말은 몇 위인지
        # 모르고 있었다.** 유입어는 수요 프록시가 0으로 보는 말이라 자동완성으로는 영영
        # 발견되지 않는다 — 유일한 정답지이므로 나올 때마다 추적 목록에 들어가야 한다.
        # 긴 유입어는 **짧게 끊어서도** 같이 잰다(2026-08-10 사용자 지적).
        # 순위는 정확히 그 문자열로만 재기 때문에 `엑셀led 판넬 피켓 제작` 1위가
        # `판넬 피켓` 순위를 뜻하지 않는다(우리 데이터에도 `vip피켓`과 `VIP 피켓`이
        # 각각 따로 잡힌다 — 띄어쓰기만 달라도 다른 판이다).
        # 3어절 이상이면 앞 2어절·뒤 2어절을 함께 추적해 어느 조각이 실제 판인지 본다.
        _seen = {kw for _, kw in targets}
        for _qs in (data.get("inflow_queries") or {}).values():
            for _q in _qs:
                _kw = (_q.get("q") or "").strip()
                if not _kw:
                    continue
                _cands = [_kw]
                _parts = _kw.split()
                if len(_parts) >= 3:
                    _cands += [" ".join(_parts[:2]), " ".join(_parts[-2:])]
                for _c in _cands:
                    if _c not in _seen:
                        _seen.add(_c)
                        targets.append((f"(실측 유입) {_c}", _c))

        if need_ranks and targets:
            ranks, failures = {}, 0
            for name, kw in targets:
                r, n_results = _rank_of(page, kw, blog)
                if r is None and n_results == 0:
                    # 🔴 2026-08-06 실측: 하루 41건 중 11건(27%)이 여기서 버려지고 있었다.
                    # 연속 요청이 몰릴 때 나는 일시적 실패라 **한 번 쉬었다 재시도**하면
                    # 상당수가 살아난다. 버려진 키워드는 승산 신호에서 통째로 빠지고
                    # 그만큼 큐 순서가 부정확해진다.
                    page.wait_for_timeout(2500)
                    r, n_results = _rank_of(page, kw, blog)
                if r is None and n_results == 0:
                    # 재시도해도 비었으면 스크래핑 실패/차단. '이탈'로 기록하지 않고 건너뜀.
                    failures += 1
                    print(f"[순위] '{kw}' -> 수집 실패(재시도 후에도 SERP 비어있음), 기록 안 함")
                    continue
                ranks[kw] = r
                print(f"[순위] '{kw}' -> {r if r else '30위권 밖'}")
            # 하나라도 실제로 수집됐을 때만 기록. 전부 실패면 그날 순위를 안 써서
            # 가짜 '전면 이탈'을 막는다(다음 실행에서 재시도).
            if ranks:
                data.setdefault("ranks", {})[today] = ranks
            else:
                print(f"[순위] 전건 수집 실패({failures}) — 오늘 순위 기록 보류.")
        elif not need_ranks:
            print("[순위] 오늘 이미 수집됨(건너뜀). 강제: --ranks")

        # ★검색 유입(하루 1회) — 방문자 누계가 아니라 **이것**이 성장 지표다.
        if need_ranks:
            si = _search_inflow(page, blog)
            if si:
                data.setdefault("search_inflow", {}).update(si)
                latest = sorted(si)[-1]
                print(f"[검색유입] {latest} {si[latest]['search']}명 "
                      f"(조회 {si[latest]['cv']})")
            else:
                print("[검색유입] 수집 실패 — 기록 보류")

            # ★실제 유입 검색어 — 우리가 가진 유일한 정답지
            iq = _inflow_queries(page, blog)
            if iq:
                data.setdefault("inflow_queries", {}).update(iq)
                allq = [t["q"] for v in iq.values() for t in v]
                print(f"[유입검색어] {len(iq)}일치 · {', '.join(dict.fromkeys(allq))[:90]}")
            else:
                print("[유입검색어] 수집 실패 — 기록 보류")

        # ★네이버 색인 여부(하루 1회). 순위보다 앞선 전제 — 색인이 0 이면 순위는 없다.
        if need_ranks:
            titles = _recent_titles(3)
            idx = _indexed_count(page, blog, titles)
            if idx is not None:
                idx["checked"] = f"{today} {datetime.now():%H:%M}"
                data["index_status"] = idx
                print(f"[색인] 제목검색 {idx['found']}/{idx['sampled']}편 확인")
                if idx["found"] == 0:
                    _warn_not_indexed(today, idx["sampled"])
            else:
                print("[색인] 수집 실패 — 기록 보류")

        # 외부 검색엔진 색인 수(하루 1회) — 네이버 밖 유입 가능성을 추적
        if need_ranks:
            n_idx = _external_indexed(page, blog)
            if n_idx is not None:
                data["external_index"] = {"count": n_idx, "engine": "duckduckgo(bing)",
                                          "checked": f"{today} {datetime.now():%H:%M}"}
                print(f"[외부색인] 검색엔진에 잡힌 글 {n_idx}편(구글 순위 아님, 색인 프록시)")
            else:
                print("[외부색인] 수집 실패 — 기록 보류")

        # 세션 점검은 매 실행. 만료를 '다음 발행이 실패하기 전에' 알린다.
        # (하루 1회만 하던 때는 07-27 09:14 에 만료를 기록해두고도 아무도 몰랐다.)
        so = _session_ok(page, blog)
        if so is not None:
            exp = _session_expiry(ctx)
            data["session"] = {"ok": so, "checked": f"{today} {datetime.now():%H:%M}",
                               "expires": exp}
            print(f"[세션] {'정상' if so else '만료 — 재로그인 필요'}"
                  + (f" / 쿠키 만료 {exp}" if exp else " / 영속 쿠키 아님(곧 죽음)"))
            if not so:
                _warn_session_expired(today)
            elif exp:
                left = (date.fromisoformat(exp) - date.today()).days
                if left <= SESSION_WARN_DAYS:
                    print(f"[세션] ⚠ {left}일 뒤 만료 — 미리 알림")
                    _warn_session_soon(exp, left)
            else:
                # 로그인은 됐지만 세션쿠키라 하루 안에 죽는다 — 이걸 미리 잡아야 한다
                print("[세션] ⚠ 영속 쿠키 없음 — '로그인 상태 유지' 없이 로그인된 상태")
                _warn_session_soon("영속 아님", 0)

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
