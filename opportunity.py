"""기회 키워드 발굴 — '수요 있고 우리가 이길 수 있는' 키워드를 발행 전에 찾는다.

지금까지의 한계:
  demand.py 는 **이미 가진 초안의 키워드만** 잰다(새 기회를 못 찾음).
  자동완성 수는 '검색 활동 유무'만 알려주고 **경쟁이 어떤 글로 채워져 있는지**는 모른다.
  그래서 발행하고 순위를 실측한 뒤에야 못 이겼다는 걸 알았다.

2026-07-27 실측으로 드러난 진짜 원인:
  '노래방 간판'(수요6) 상위 10개는 전부 "부산/목동/용산/수원/대전/창원 + 시공 후기" 형식이었다.
  우리 글이 짧아서 진 게 아니라 **검색 의도(지역 시공 사례)와 형식이 달라서** 진 것이다.
  지역 시공 후기는 실제 시공지를 모르면 정직하게 쓸 수 없다 → 그런 키워드는 피해야 한다.

그래서 이 도구는 키워드마다 SERP 상위 10개의 **형식**을 진단한다.
  - 지역형 비율이 높다 → 우리가 정직하게 못 쓰는 판. 회피.
  - 정보형(가격·비용·종류·방법·차이) 이 섞여 있다 → 우리 형식이 통하는 판. 진입.
  - 온토픽 글이 적다 → 그 키워드를 정조준한 글이 없다. 빈틈.

사용:
  python opportunity.py scan            # 시드에서 후보 발굴 → 수요 → SERP 진단
  python opportunity.py scan --seeds "LED 간판,네온사인"
  python opportunity.py report          # 저장된 결과 다시 보기
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

OUT = ROOT / "data" / "opportunities.json"

# made-us 제품·손님에 맞는 시드만(적합성 게이트 — GROWTH_LOOP.md)
SEEDS = ["LED 간판", "네온사인", "아크릴 간판", "아크릴 메뉴판", "돌출 간판",
         "채널 간판", "술집 간판", "노래방 간판", "클럽 간판",
         "LED 피켓", "방송용 피켓", "무선 전광판", "응원 피켓"]

# 상위 글 제목 형식 분류
_LOCAL = re.compile(
    r"(서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충북|충남|전북|전남|경북|경남|제주"
    r"|수원|성남|용인|고양|창원|마산|천안|청주|전주|포항|김해|평택|구미|안산|안양|부천"
    r"|강남|홍대|건대|신촌|목동|용산|잠실|분당|일산)")
_INFO = re.compile(r"(가격|비용|견적|종류|방법|추천|차이|비교|고르|선택|후회|주의|체크|정리|가이드)")
_CASE = re.compile(r"(후기|사례|시공|현장|작업기)")


def _demand(page, kw: str) -> tuple[int, list[str]]:
    url = (f"https://ac.search.naver.com/nx/ac?q={quote(kw)}&con=0&frm=nv&ans=2"
           f"&r_format=json&st=100")
    try:
        r = page.request.get(url, headers={"referer": "https://search.naver.com/"},
                             timeout=12000)
        j = json.loads(r.text())
        sug = [it[0] for grp in j.get("items", []) for it in grp]
        return len(sug), sug
    except Exception:
        return -1, []


def _serp(page, kw: str) -> list[dict]:
    """블로그탭 상위 결과의 (블로그ID, 제목)."""
    url = f"https://search.naver.com/search.naver?ssc=tab.blog.all&query={quote(kw)}"
    try:
        page.goto(url, timeout=30000)
        page.wait_for_timeout(1400)
    except Exception:
        return []
    return page.evaluate("""() => {
        const out = [], seen = new Set();
        for (const a of document.querySelectorAll('a[href*="blog.naver.com"]')) {
            const m = a.href.match(/blog\\.naver\\.com\\/([a-zA-Z0-9_-]+)\\/(\\d+)/);
            if (!m) continue;
            const key = m[1] + '/' + m[2];
            if (seen.has(key)) continue;
            const title = (a.innerText || '').split('\\n')[0].trim();
            if (title.length < 6) continue;
            seen.add(key);
            out.push({id: m[1], title: title.slice(0, 60)});
            if (out.length >= 10) break;
        }
        return out;
    }""")


def diagnose(kw: str, items: list[dict]) -> dict:
    """상위 글 형식을 진단해 '우리가 들어갈 자리가 있는가'를 본다."""
    toks = [t for t in re.split(r"\s+", kw) if len(t) > 1]
    n = len(items) or 1
    ontopic = sum(1 for it in items if all(t in it["title"] for t in toks))
    local = sum(1 for it in items if _LOCAL.search(it["title"]))
    case = sum(1 for it in items if _CASE.search(it["title"]))
    info = sum(1 for it in items if _INFO.search(it["title"]))
    return {"n": len(items), "ontopic": ontopic, "local": local, "case": case, "info": info,
            "local_ratio": round(local / n, 2), "info_ratio": round(info / n, 2)}


def score(demand: int, d: dict) -> float:
    """기회 점수. 수요가 있어야 하고, 지역 시공 후기 판이면 깎고, 빈틈이 있으면 올린다."""
    if demand <= 0 or d["n"] == 0:
        return 0.0
    gap = 1.0 - (d["ontopic"] / max(d["n"], 1))       # 정조준 글이 적을수록 빈틈
    fit = 1.0 - d["local_ratio"]                      # 지역형이 많을수록 우리 자리 없음
    bonus = 1.0 + 0.3 * d["info_ratio"]               # 정보형이 통하는 판이면 가산
    return round(min(demand, 10) / 10 * (0.35 + 0.65 * gap) * fit * bonus, 3)


def scan(seeds: list[str], max_candidates: int) -> None:
    from playwright.sync_api import sync_playwright  # noqa: PLC0415

    from publish.browser import launch_context  # noqa: PLC0415

    with sync_playwright() as p:
        ctx = launch_context(p, headed=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        # 1) 시드 → 자동완성으로 후보 확장
        cands: dict[str, int] = {}
        for s in seeds:
            n, sug = _demand(page, s)
            if n > 0:
                cands[s] = n
            for w in sug:
                w = w.strip()
                if 3 <= len(w) <= 20 and w not in cands:
                    cands[w] = -1
        print(f"후보 {len(cands)}개 발굴(시드 {len(seeds)})")

        # 2) 수요 측정(자동완성 API — 가볍다)
        for kw in list(cands):
            if cands[kw] == -1:
                cands[kw] = _demand(page, kw)[0]
        alive = {k: v for k, v in cands.items() if v >= 2}
        print(f"수요 2 이상: {len(alive)}개")

        # 3) 수요 상위만 SERP 진단(무겁다)
        top = sorted(alive.items(), key=lambda kv: -kv[1])[:max_candidates]
        rows = []
        for i, (kw, dem) in enumerate(top, 1):
            items = _serp(page, kw)
            d = diagnose(kw, items)
            sc = score(dem, d)
            rows.append({"keyword": kw, "demand": dem, **d, "score": sc,
                         "top_titles": [it["title"] for it in items[:3]]})
            print(f"  [{i:>2}/{len(top)}] {kw[:20]:22} 수요{dem:>3} "
                  f"온토픽{d['ontopic']}/{d['n']} 지역{d['local']} 정보{d['info']} → {sc}")
        ctx.close()

    rows.sort(key=lambda r: -r["score"])
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    _report(rows)


def _report(rows: list[dict]) -> None:
    print("\n===== 기회 키워드 상위 =====")
    print(f"{'점수':>6} {'수요':>4} {'온토픽':>6} {'지역':>4}  키워드")
    for r in rows[:15]:
        print(f"{r['score']:>6} {r['demand']:>4} {r['ontopic']:>3}/{r['n']:<2} "
              f"{r['local']:>4}  {r['keyword']}")
    print("\n※ 지역 수가 크면 '지역+시공후기' 판 — 실제 시공지를 모르면 정직하게 못 쓴다(회피).")
    print("※ 온토픽이 적을수록 그 키워드를 정조준한 글이 없다는 뜻(빈틈).")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sc = sub.add_parser("scan")
    sc.add_argument("--seeds", default=None, help="쉼표 구분(기본: 제품 시드)")
    sc.add_argument("--max", type=int, default=20, help="SERP 진단할 후보 수")
    sub.add_parser("report")
    a = ap.parse_args()

    if a.cmd == "report":
        if not OUT.exists():
            print("아직 scan 결과가 없습니다.")
            return
        _report(json.loads(OUT.read_text(encoding="utf-8")))
        return
    seeds = [s.strip() for s in a.seeds.split(",")] if a.seeds else SEEDS
    scan(seeds, a.max)


if __name__ == "__main__":
    main()
