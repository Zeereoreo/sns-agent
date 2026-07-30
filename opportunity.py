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
# ★방송 플랫폼 조합(2026-07-29 추가): 자동완성 수요는 0으로 나오지만 구글에는 실제
#   검색결과 1페이지가 있고 경쟁사(linosgj)가 거기를 먹고 있다. 수요 프록시가 못 보는 판이다.
#   순수 플랫폼명(아프리카TV·엑셀방송·치지직)은 수요 10이지만 '별풍선·주가·인방갤' 검색이라
#   우리가 1등 할 판이 아니다 → **조합어는 본문 타깃, 순수 플랫폼명은 태그**로 쓴다.
SEEDS = ["LED 간판", "네온사인", "아크릴 간판", "아크릴 메뉴판", "돌출 간판",
         "채널 간판", "술집 간판", "노래방 간판", "클럽 간판",
         "LED 피켓", "방송용 피켓", "무선 전광판", "응원 피켓",
         "엑셀방송 피켓", "아프리카TV 피켓", "BJ 피켓", "개인방송 피켓",
         "인터넷방송 피켓", "큰손 피켓", "시그니처 피켓", "LED 무선피켓"]

# 발행 글 태그에 넣을 방송 플랫폼·업계 용어. 경쟁 상위 글 태그 실측 빈도(2026-07-29,
# research.py 로 BJ/방송 키워드 7개 집계): 아프리카TV 13 · 엑셀방송 9 · 숲티비 6 ·
# SOOP 5 · 팬더TV 4 · 플렉스TV 4 · 띵라이브 4 · 큰손 2. 우리 17편 중 1편만 갖고 있었다.
PLATFORM_TAGS_CORE = ["#아프리카TV", "#엑셀방송", "#숲티비"]
PLATFORM_TAGS_ROTATE = ["#SOOP", "#팬더TV", "#플렉스TV", "#띵라이브", "#팝콘티비",
                        "#큰손", "#시그니처", "#치지직", "#인터넷방송"]

# ★ 제품 적합성 게이트 (GROWTH_LOOP.md 최우선 규칙)
# 자동완성은 동음이의어로 샌다 — '서포트' → 아치서포트(발 지지대)·핀서포트(건축 자재),
# '커피차' → 커피창고(카페 이름), '응원봉' → 기아 응원봉(야구).
# made-us 가 실제로 만드는 물건이 아닌 키워드는 아무리 수요가 커도 후보가 아니다.
# '굿즈'는 사용자가 명시적으로 요구한 카테고리이고 실제 우리 제품군이다
# (스트리머 굿즈 = 방송에 쓰는 LED 굿즈). 게이트에 없어서 스캔에서 계속 잘려나갔다.
_PRODUCT = ("피켓", "전광판", "네온", "간판", "아크릴", "사인", "버킷", "메뉴판", "굿즈")
# made-us 가 만들지 않는 것 / 명백한 오프타겟(수요가 커도 제외)
# '챌린지·기부'는 ALS 아이스버킷 챌린지 — 이름만 같고 제품이 아니다(2026-07-29 실제로
# 0.748 점 기회 키워드 1위로 올라왔다). '가방·스텐저그'도 우리가 만드는 LED 버킷이 아니다.
_NOT_OURS = ("응원봉", "슬로건", "커피차", "커피창고", "커피숍", "창업", "서포트",
             "시트지", "썬팅", "어닝", "현수막", "배너", "명함", "스티커",
             "챌린지", "기부", "가방", "스텐저그")


def is_our_product(kw: str) -> bool:
    """made-us 제품군 키워드인가. 제품 토큰이 있고 '안 만드는 것'이 아니어야 한다."""
    if any(x in kw for x in _NOT_OURS):
        return False
    return any(x in kw for x in _PRODUCT)


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


def _serp(page, kw: str, mobile: bool = True) -> list[dict]:
    """블로그탭 상위 결과의 (블로그ID, 제목).

    ★2026-07-30 부터 **모바일** 기준. 실측 유입 referrer 가 거의 전부
    `m.search.naver.com` 이었는데 우리는 PC SERP 로 경쟁을 재고 있었다.
    둘은 결과 수가 크게 다르다 — 같은 날 실측:
      vip피켓 모바일 **1건** vs PC 24건 · led 응원 피켓 3 vs 23 · 아크릴 메뉴판 4 vs 22.
    PC 로 '경쟁 24건'이라 포기한 키워드가 모바일에서는 비어 있었다.
    """
    host = "m.search.naver.com" if mobile else "search.naver.com"
    url = f"https://{host}/search.naver?ssc=tab.blog.all&query={quote(kw)}"
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


def _nospace(s: str) -> str:
    # 대소문자도 무시한다 — 'vvip피켓' 이 'VVIP피켓 VIP피켓…' 제목과 안 맞아
    # 동음이의어로 오탐됐다(2026-07-30). LED/led, VIP/vip 가 섞여 쓰이는 판이다.
    return re.sub(r"\s+", "", s).casefold()


def diagnose(kw: str, items: list[dict]) -> dict:
    """상위 글 형식을 진단해 '우리가 들어갈 자리가 있는가'를 본다."""
    toks = [t for t in re.split(r"\s+", kw) if len(t) > 1]
    kwn = _nospace(kw)
    n = len(items) or 1
    # 온토픽 판정은 **띄어쓰기를 무시**해야 한다. 붙여쓴 키워드('카페입간판')를 띄어쓴
    # 제목('카페 입간판 추천')과 그냥 비교하면 전부 불일치로 나온다 — 실제로 카페입간판이
    # 온토픽 0/10 으로 잡혀 기회점수 0.806(2위)까지 올라갔었다(2026-07-29).
    # 붙여쓰기 변형을 자동 생성하는 기능과 겹쳐 상위 점수가 통째로 오염돼 있었다.
    ontopic = sum(1 for it in items
                  if kwn in _nospace(it["title"])
                  or all(t.casefold() in it["title"].casefold() for t in toks))
    local = sum(1 for it in items if _LOCAL.search(it["title"]))
    case = sum(1 for it in items if _CASE.search(it["title"]))
    info = sum(1 for it in items if _INFO.search(it["title"]))
    return {"n": len(items), "ontopic": ontopic, "local": local, "case": case, "info": info,
            "local_ratio": round(local / n, 2), "info_ratio": round(info / n, 2),
            # 온토픽이 하나도 없으면 '빈틈'이 아니라 **다른 뜻**일 때가 많다.
            # 실제 예: '바사인' 상위=성경 바사(페르시아) / '룸사인' 상위=호텔 객실 후기.
            "homonym_risk": bool(ontopic == 0 and len(items) >= 5)}


SPARSE_N = 3        # 모바일 블로그탭 경쟁이 이 수 이하면 '비어 있는 판'


def score(demand: int, d: dict) -> float:
    """기회 점수. 수요가 있어야 하고, 지역 시공 후기 판이면 깎고, 빈틈이 있으면 올린다.

    ★2026-07-30 대수정 — '수요 0 이면 기회 0' 이 최대 오류였다.
    자동완성 수요는 **프록시일 뿐**인데 그걸 관문으로 써서, 실제로 유입을 만든 키워드를
    전부 0점으로 버리고 있었다. 실측 유입어(vip피켓·led 응원 피켓·비제이 전광판 구매)는
    자동완성 수요가 0~4 였다. 그리고 이 판은 모바일 블로그탭 경쟁이 0~3건으로 비어 있다
    (BJ/피켓 27개 중 21개). 경쟁이 없으면 검색량이 적어도 **그 소량이 전부 우리 것**이다.
    → 수요 0 이어도 **경쟁이 비어 있으면 기회로 인정**한다.
    """
    sparse = d["n"] <= SPARSE_N and not d.get("homonym_risk")
    if demand <= 0:
        if sparse:
            # 비어 있는 판: 경쟁이 적을수록 높게(0건이 최고). 0.60~0.75.
            # 이 대역을 수요 있는 판보다 위에 두는 근거 — **실측 유입 6건 중 5건이
            # 이런 '수요 0 · 경쟁 희소' 키워드에서 나왔다**(vip피켓·led 응원 피켓 등).
            # 반면 자동완성 수요 9였던 '카페입간판'은 유입 0이었다.
            # n==0 을 '기회 없음'으로 보면 안 된다 — 'LED피켓'은 지금 블로그 결과가 0건인데
            # 7/21 에 그 키워드로 실제 유입이 있었다. 네이버가 쿼리별로 블로그 섹션 노출을
            # 동적으로 정할 뿐, 자리가 비어 있다는 뜻이다.
            return round(0.75 - 0.05 * d["n"], 3)
        return 0.0
    gap = 1.0 - (d["ontopic"] / max(d["n"], 1))       # 정조준 글이 적을수록 빈틈
    fit = 1.0 - d["local_ratio"]                      # 지역형이 많을수록 우리 자리 없음
    bonus = 1.0 + 0.3 * d["info_ratio"]               # 정보형이 통하는 판이면 가산
    s = min(demand, 10) / 10 * (0.35 + 0.65 * gap) * fit * bonus
    if sparse:
        s = max(s, 0.75 - 0.05 * d["n"])   # 수요까지 있는 빈 판은 최소한 빈 판만큼은 준다
    # 온토픽 0 은 최대 gap 을 받아 점수가 제일 높게 나오는데, 그 중 상당수가 동음이의어다.
    # 확신할 수 없으니 지우지는 않고 절반으로 깎아 '검토 대상'으로 남긴다(제목을 봐야 안다).
    if d.get("homonym_risk"):
        s *= 0.5
    return round(s, 3)


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
            # ★시드는 수요와 무관하게 무조건 후보다(2026-07-30 수정).
            # 'if n > 0' 때문에 수요 0 인 시드가 후보에 들지도 못했다 — 그런데 실측 유입을
            # 만든 키워드가 정확히 그런 것들이다(vip피켓·리액션 피켓·비제이 전광판 구매).
            cands[s] = max(n, 0)
            for w in sug:
                w = w.strip()
                if 3 <= len(w) <= 20 and w not in cands:
                    cands[w] = -1
        # ★띄어쓰기 변형을 자동으로 같이 잰다.
        # 같은 뜻인데 경쟁 밀도가 완전히 다르다(2026-07-28 실측):
        #   '메뉴판 제작' 온토픽 8/10 vs '메뉴판제작' 0/10
        #   '입간판' 10/10 vs '입간판제작' 1/10 · '아크릴 간판' 9/10 vs '아크릴간판' 4/10
        # 붙여쓴 형태를 빠뜨리면 최대 빈틈을 놓친다.
        for w in list(cands):
            joined = w.replace(" ", "")
            if joined != w and 3 <= len(joined) <= 20 and joined not in cands:
                cands[joined] = -1

        dropped = [k for k in cands if not is_our_product(k)]
        for k in dropped:
            del cands[k]
        print(f"후보 {len(cands)}개 발굴(시드 {len(seeds)}) — 제품 무관 {len(dropped)}개 제외")
        if dropped:
            print(f"  제외 예: {', '.join(dropped[:8])}")

        # 2) 수요 측정(자동완성 API — 가볍다)
        for kw in list(cands):
            if cands[kw] == -1:
                cands[kw] = _demand(page, kw)[0]
        # ★수요 2 미만을 여기서 버리면 안 된다(2026-07-30 수정).
        # 실측 유입을 만든 키워드(vip피켓·비제이 전광판 구매 등)는 자동완성 수요가 0 이라
        # 이 필터에서 전부 잘려나가 **SERP 진단조차 못 받고** 있었다.
        # 시드는 무조건 진단하고, 확장 후보는 수요 상위로 채운다.
        seed_set = [k for k in cands if k in seeds]
        rest = sorted(((k, v) for k, v in cands.items() if k not in seed_set),
                      key=lambda kv: -kv[1])
        top = [(k, cands[k]) for k in seed_set] + rest
        top = top[:max_candidates]
        print(f"진단 대상 {len(top)}개(시드 {len(seed_set)} + 확장 {len(top) - len(seed_set)})")
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

    # 스캔 결과는 **누적**한다. 세그먼트별로 나눠 돌리므로 덮어쓰면 이전 진단이 사라진다
    # (실제로 BJ 스캔이 간판 스캔을 지워 엔진의 지역형 페널티가 풀린 적 있음).
    merged: dict[str, dict] = {}
    if OUT.exists():
        try:
            for r in json.loads(OUT.read_text(encoding="utf-8")):
                merged[r["keyword"]] = r
        except Exception:
            pass
    for r in rows:
        merged[r["keyword"]] = r
    out = sorted(merged.values(), key=lambda r: -r["score"])
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n누적 저장: {len(out)}개 키워드 (이번 스캔 {len(rows)}개)")
    _report(rows)


def _report(rows: list[dict]) -> None:
    print("\n===== 기회 키워드 상위 =====")
    print(f"{'점수':>6} {'수요':>4} {'온토픽':>6} {'지역':>4}  키워드")
    for r in rows[:15]:
        warn = "  ⚠동음이의어?" if r.get("homonym_risk") else ""
        print(f"{r['score']:>6} {r['demand']:>4} {r['ontopic']:>3}/{r['n']:<2} "
              f"{r['local']:>4}  {r['keyword']}{warn}")
    print("\n※ 지역 수가 크면 '지역+시공후기' 판 — 실제 시공지를 모르면 정직하게 못 쓴다(회피).")
    print("※ 온토픽이 적을수록 그 키워드를 정조준한 글이 없다는 뜻(빈틈).")
    risky = [r for r in rows[:15] if r.get("homonym_risk")]
    if risky:
        print("\n⚠ 온토픽 0 — '빈틈'이 아니라 다른 뜻일 수 있다. 상위 제목을 직접 볼 것:")
        for r in risky[:5]:
            print(f"   {r['keyword']}: {' / '.join(r.get('top_titles', [])[:2])}")


def retarget() -> None:
    """승산 없는 초안을 찾아 '어떤 키워드로 바꾸면 되는지' 후보를 제안한다.

    콘텐츠를 아무리 다듬어도 못 이기는 판이 있다 — 지역 시공후기 판, 실측 수요 0,
    같은 키워드 중복. 이런 초안은 **글이 아니라 타깃을 바꿔야** 산다.
    사람이 매번 판단하지 않아도 되게 목록으로 뽑아준다.
    """
    import re as _re

    import growth  # noqa: PLC0415

    rows = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else []
    opp = {r["keyword"]: r for r in rows}
    dmap = growth._demand_map()
    q = growth.rank_queue()

    # 이미 발행된 키워드(중복 판정용)
    pub_kw = set()
    state = json.loads((ROOT / "data" / "publish_state.json").read_text(encoding="utf-8"))
    for n in state.get("published", []):
        p = ROOT / "drafts" / n
        if p.exists():
            m = _re.search(r"타깃\s*검색키워드[^:]*:\s*(.+)", p.read_text(encoding="utf-8"))
            if m:
                pub_kw.add(_re.split(r"[,/·\n]", m.group(1).strip())[0].strip())

    stuck = []
    for r in q:
        kw, reasons = r["keyword"], []
        o = opp.get(kw)
        if o and o.get("n", 0) >= 5 and (o.get("local", 0) / o["n"]) >= 0.6:
            reasons.append(f"지역 시공후기 판({o['local']}/{o['n']})")
        # a(BJ/스트리머)는 수요 0 이 정상이다 — 검색 유입이 아니라 진열·전환용이고
        # 스케줄러 BJ 쿼터로 발행된다(사용자 최우선 세그먼트). 리타겟 대상이 아니다.
        if dmap.get(kw) == 0 and r["seg"] != "a":
            reasons.append("실측 수요 0")
        if kw in pub_kw:
            reasons.append("이미 같은 키워드로 발행함")
        if reasons:
            stuck.append((r["name"], kw, reasons))

    # 제안 후보: 기회점수 상위 & 아직 아무 초안도 안 쓰는 키워드
    # ★제품 게이트를 여기서도 건다 — 게이트 도입 전에 저장된 동음이의어(아치서포트·커피차 등)가
    #   파일에 남아 있어서, 읽을 때도 걸러야 엉뚱한 주제를 제안하지 않는다.
    used = {r["keyword"] for r in q} | pub_kw
    free = [r for r in rows
            if r["keyword"] not in used and r.get("score", 0) > 0.3
            and is_our_product(r["keyword"])]
    free.sort(key=lambda r: -r["score"])

    print(f"=== 타깃을 바꿔야 할 초안 {len(stuck)}편 ===")
    for name, kw, reasons in stuck:
        print(f"  {name[:30]:32} '{kw}' — {', '.join(reasons)}")
    print(f"\n=== 비어 있는 기회 키워드 상위 {min(8, len(free))}개 ===")
    for r in free[:8]:
        print(f"  {r['score']:>5}  수요{r['demand']:>3}  온토픽{r['ontopic']}/{r['n']}  {r['keyword']}")
    if not free:
        print("  (없음 — opportunity.py scan 으로 후보를 더 발굴하세요)")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sc = sub.add_parser("scan")
    sc.add_argument("--seeds", default=None, help="쉼표 구분(기본: 제품 시드)")
    sc.add_argument("--max", type=int, default=20, help="SERP 진단할 후보 수")
    sub.add_parser("report")
    sub.add_parser("retarget")
    a = ap.parse_args()

    if a.cmd == "retarget":
        retarget()
        return
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
