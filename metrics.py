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


def _rank_of(page, keyword: str, blog: str) -> tuple[int | None, int]:
    """(순위, SERP에서 확인된 블로그 결과 수) 반환.
    결과 수가 0이면 스크래핑 실패/차단 가능성 → 호출측이 '순위 이탈'로 오기록하지 않는다."""
    url = f"https://search.naver.com/search.naver?ssc=tab.blog.all&query={quote(keyword)}"
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

        if need_ranks and kw_map:
            ranks, failures = {}, 0
            for name, kw in kw_map.items():
                r, n_results = _rank_of(page, kw, blog)
                if r is None and n_results == 0:
                    # SERP가 비어있음 = 스크래핑 실패/차단. '이탈'로 기록하지 않고 건너뜀.
                    failures += 1
                    print(f"[순위] '{kw}' -> 수집 실패(SERP 비어있음), 기록 안 함")
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
