"""방문자 성장 최적화 엔진.

우리가 통제할 수 있는 레버는 "무엇을, 어떤 순서로 발행하는가"다. 이 엔진은
매일 쌓이는 데이터(키워드 순위·방문자 추이)로 각 미발행 초안의 '기대 성과'를
점수화해, 성과가 좋은 주제/세그먼트를 먼저 발행하도록 큐를 재정렬한다.
그리고 자신의 과거 결정이 실제로 좋은 순위를 냈는지 평가해 가중치를 스스로 보정한다.

정직한 한계: 알고리즘이 없는 방문자를 만들지 못한다. "이길 수 있는 주제에 힘을
몰아주고, 안 되는 주제는 뒤로 미루는" 최적화다. 데이터가 쌓일수록 똑똑해진다.

구성:
  - segment_scores(): 세그먼트별 관측 성과(발행글 평균 순위 기반)
  - rank_queue(): 미발행 초안 우선순위 점수 + 설명
  - next_draft(): 다음 발행 초안(동일 세그먼트 3연속 방지)
  - evaluate_and_tune(): 과거 결정 평가 → 가중치 자가 보정 + 로그
  - report(): 사람이 읽는 요약

CLI:
  python growth.py plan       # 현재 우선순위 + 이유
  python growth.py tune       # 자가 평가·가중치 보정
  python growth.py report     # 성장 리포트
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

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
METRICS = ROOT / "data" / "metrics.json"
WEIGHTS_FILE = ROOT / "data" / "growth_weights.json"
GLOG = ROOT / "data" / "growth_log.json"

DEFAULT_WEIGHTS = {"demand": 0.35, "seg": 0.20, "seo": 0.20, "diversity": 0.15, "explore": 0.10}
MAX_RANK = 30  # 이 순위 밖은 최하로 취급
DEMAND_CACHE = ROOT / "data" / "demand_cache.json"
RESEARCH_DIR = ROOT / "data" / "research"


# ---------- 데이터 로드 ----------

def _load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save(path, obj):
    path.parent.mkdir(exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def load_weights() -> dict:
    w = _load(WEIGHTS_FILE, dict(DEFAULT_WEIGHTS))
    # 누락 키 보정 + 정규화
    for k, v in DEFAULT_WEIGHTS.items():
        w.setdefault(k, v)
    s = sum(w[k] for k in DEFAULT_WEIGHTS) or 1.0
    return {k: w[k] / s for k in DEFAULT_WEIGHTS}


def primary_keyword(path: Path) -> str:
    t = path.read_text(encoding="utf-8")
    m = re.search(r"타깃\s*검색키워드[^:]*:\s*(.+)", t)
    return re.split(r"[,/·\n]", m.group(1).strip())[0].strip() if m else ""


def _segment(name: str) -> str:
    return name[0] if name[:1] in "abc" else "a"


def _ordered_drafts() -> list[Path]:
    a = sorted(DRAFTS.glob("sample*.md")) + sorted(DRAFTS.glob("a*.md"))
    return a + sorted(DRAFTS.glob("b*.md")) + sorted(DRAFTS.glob("c*.md"))


# ---------- 관측 성과 ----------

def _latest_ranks() -> dict:
    ranks = _load(METRICS, {}).get("ranks", {})
    if not ranks:
        return {}
    return ranks[sorted(ranks)[-1]]


def segment_scores() -> dict:
    """세그먼트별 관측 성과 0~1(높을수록 잘됨). 발행글 순위를 '검색수요로 가중'한 평균.

    정직 교정: 수요 0 키워드에서 1위여도 방문자는 0이다. 경쟁 없는 죽은 키워드의
    쉬운 1위를 '성과'로 세면 엔진이 그쪽으로 쏠린다. 그래서 각 발행글의 순위 성과를
    그 키워드의 검색수요로 가중한다(수요0=가중0=성과 미반영). 수요 있는 발행글이
    아직 없으면 근거 부족이므로 중립 0.5.
    """
    state = _load(STATE, {"published": []})
    latest = _latest_ranks()
    dmap = _demand_map()
    per_seg: dict[str, list[tuple[float, float]]] = {"a": [], "b": [], "c": []}
    for name in state.get("published", []):
        p = DRAFTS / name
        if not p.exists():
            continue
        kw = primary_keyword(p)
        r = latest.get(kw)
        seg = _segment(name)
        if isinstance(r, int):
            rank_s = max(0.0, min(1.0, 1 - (r - 1) / (MAX_RANK - 1)))
            weight = _demand_score(kw, dmap)  # 수요=성과 가중치. 수요0이면 반영 0.
            per_seg[seg].append((rank_s, weight))
        # 순위 미측정(키워드가 아직 metrics 에 없음)은 페널티 아님 → 건너뜀(중립).
    out = {}
    for seg in "abc":
        pairs = per_seg[seg]
        wsum = sum(wt for _, wt in pairs)
        if not pairs or wsum <= 0:
            out[seg] = 0.5  # 수요 있는 발행글이 아직 없음 → 근거 부족, 중립
        else:
            out[seg] = sum(rs * wt for rs, wt in pairs) / wsum
    return out


# ---------- 방문자 연결(정직: 검색어별 귀속은 네이버가 API로 안 줌 → 전체 추이·상관만) ----------

def visitor_trend() -> dict:
    """전체 방문자 시계열의 최근 추세. {days, latest_total, recent_new_per_day, label}."""
    vis = _load(METRICS, {}).get("visitors", {})
    days = sorted(vis)
    totals = [vis[d].get("total") for d in days if isinstance(vis[d].get("total"), int)]
    out = {"days": len(totals), "latest_total": totals[-1] if totals else None,
           "recent_new_per_day": None, "label": "데이터 축적 중",
           "as_of": days[-1] if days else None, "lag": None}
    if len(totals) >= 3:
        span = min(7, len(totals) - 1)
        new = (totals[-1] - totals[-1 - span]) / span
        out["recent_new_per_day"] = round(new, 1)
        out["label"] = "상승" if new > 0.5 else ("하락" if new < -0.5 else "보합")
    # 인덱스로만 계산해서 수집이 멈춰도 같은 '최근'을 반복한다. 2026-08-13 에는 나흘
    # 묵은 데이터로 '상승 +1.7/일'이라고 말하고 있었다. 며칠 기준인지 함께 돌려주고,
    # 끊긴 동안은 추세를 주장하지 않는다(자가 튜닝도 이 label 로 판단한다).
    if days:
        out["lag"] = (date.today() - date.fromisoformat(days[-1])).days
        if out["lag"] >= 2:
            out["label"] = f"알 수 없음(수집 {out['lag']}일 끊김)"
    return out


def rank_visitor_signal() -> dict:
    """'1페이지 노출 키워드 수'와 '방문자 수'가 함께 움직이는지(피어슨 상관).
    순위 최적화가 실제 방문자로 이어지는지 평가하는 지표. 데이터 3일+ 필요."""
    m = _load(METRICS, {})
    ranks, vis = m.get("ranks", {}), m.get("visitors", {})
    xs, ys = [], []
    for d in sorted(set(ranks) & set(vis)):
        p1 = sum(1 for r in ranks[d].values() if isinstance(r, int) and r <= 10)
        t = vis[d].get("total")
        if isinstance(t, int):
            xs.append(p1)
            ys.append(t)
    if len(xs) < 3:
        return {"points": len(xs), "corr": None, "note": "데이터 축적 중(3일+ 필요)"}
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs) ** 0.5
    vy = sum((y - my) ** 2 for y in ys) ** 0.5
    corr = (cov / (vx * vy)) if vx and vy else None
    return {"points": n, "corr": round(corr, 2) if corr is not None else None,
            "note": "1페이지 노출↑ 이 방문자↑ 로 이어지는 정도"}


# ---------- 초안 점수화 ----------

def _seo_score(path: Path) -> float:
    try:
        import seo
        return seo.score_draft(path)["score"] / 100.0
    except Exception:
        return 0.8


def _demand_map() -> dict:
    """data/demand_cache.json — {키워드: 자동완성수}. demand.py audit 로 갱신."""
    return _load(DEMAND_CACHE, {})


# 손님 적합성 우선순위 (사용자 확정 2026-07-23: BJ/스트리머 > 엔터/야간 > 일반상가).
# 수요·승산이 같아도 우선순위 손님에 맞는 글을 먼저 발행하도록 총점에 곱한다.
# 일반상가는 배제가 아니라 감점('그런 경우도 있음'). 튜닝 가능한 키워드 분류.
_FIT_PRIORITY = ("피켓", "전광판", "방송", "시그니처", "응원", "조공", "스트리머", "비제이")
_FIT_ENTER = ("네온", "클럽", "유흥", "노래방", "술집", "포차", "이자카야", "호프", "아이스버킷")
_FIT_OFFTARGET = ("약국", "병원", "학원", "네일", "미용실", "카페 간판", "상가 간판",
                  "간판 교체", "매장 간판", "프랜차이즈", "간판 제작", "간판 디자인",
                  "돌출간판", "아크릴 메뉴판", "아크릴 간판")


def _fit_multiplier(kw: str) -> float:
    """손님 우선순위 반영. BJ/스트리머(방송소품)=1.0, 엔터/야간=0.92, 일반상가=0.6, 미분류=0.85."""
    if any(t in kw for t in _FIT_PRIORITY):
        return 1.0
    if any(t in kw for t in _FIT_ENTER):
        return 0.92
    if any(t in kw for t in _FIT_OFFTARGET):
        return 0.6
    return 0.85


def _demand_score(kw: str, dmap: dict) -> float:
    """검색 수요 0~1(자동완성 5개 이상이면 1.0). 방문자 직결 신호."""
    n = dmap.get(kw)
    if n is None or n < 0:
        return 0.3          # 미측정: 중립 이하
    return min(1.0, n / 5.0)


def _draft_body_len(path: Path) -> int | None:
    """초안 본문 공백제외 글자수(research 벤치마크와 동일 기준). 실패 시 None."""
    try:
        d = parse_draft(path)
        body = " ".join(b.get("text", "") for b in d["blocks"])
        return len(re.sub(r"\s", "", body))
    except Exception:
        return None


def _research_slug(kw: str) -> str:
    return re.sub(r"[^가-힣a-zA-Z0-9]+", "_", kw)[:40] or "kw"


def _research_opportunity(kw: str, path: Path) -> float:
    """경쟁 리서치 기반 '승산 기회' 0~1. 신생 블로그가 이기는 길은 상위 경쟁글보다
    '더 깊게' 쓰는 것 — 우리 글이 경쟁 길이 벤치마크보다 충분히 깊으면 이길 수 있는
    신호다. 자동완성 수요가 낮아도 경쟁을 이길 수 있는 중간꼬리(예: 네일샵 간판 비용)를
    엔진이 저평가하지 않게 한다.
    리서치 없거나(모름) 우리가 경쟁보다 확실히 깊지 않으면 0(기여 없음 → 기존 demand 그대로).
    research.py 의 제목필터는 의미적 온토픽까지는 못 재므로, '경쟁 대비 우리 깊이'라는
    직접 측정 가능한 신호만 쓴다(정직).
    """
    rf = RESEARCH_DIR / f"{_research_slug(kw)}.json"
    if not rf.exists():
        return 0.0
    bench = _load(rf, {}).get("length_benchmark")
    if not bench:
        return 0.0
    our_len = _draft_body_len(path)
    if our_len is None:
        return 0.0
    ratio = our_len / bench
    if ratio < 1.2:            # 경쟁보다 확실히 깊지 않으면 기회로 안 침
        return 0.0
    return max(0.6, min(1.0, ratio / 1.8))   # 1.2배→0.67, 1.8배+→1.0


def _opportunity_allowed(kw: str, dmap: dict) -> bool:
    """경쟁깊이 기회 승격 허용 여부. 실측 수요 0 키워드는 경쟁을 이겨도 방문자가
    0이므로(수요0 1위 함정) 승격 금지. 미측정(None)은 아직 모름 → 허용."""
    return dmap.get(kw) != 0


def _research_penalty(kw: str, path: Path) -> float:
    """경쟁 상위글이 우리보다 확실히 깊으면(길이비<0.9) 승산 낮음 → demand 할인(<1).
    신생 블로그는 더 깊게 못 쓰면 깊은 경쟁을 못 이긴다. _research_opportunity 의 대칭
    (깊으면 우대, 얕으면 할인). 리서치 없거나 비등/우세면 1.0(영향 없음)."""
    rf = RESEARCH_DIR / f"{_research_slug(kw)}.json"
    if not rf.exists():
        return 1.0
    bench = _load(rf, {}).get("length_benchmark")
    if not bench:
        return 1.0
    our_len = _draft_body_len(path)
    if our_len is None:
        return 1.0
    ratio = our_len / bench
    if ratio >= 0.9:
        return 1.0
    return max(0.5, ratio)     # 0.9→0.9, 0.67→0.67, 0.5이하→하한 0.5


OPPORTUNITIES = ROOT / "data" / "opportunities.json"


def _serp_format_penalty(kw: str) -> float:
    """SERP 상위가 '지역 + 시공 후기'로 채워진 키워드는 우리가 이길 수 없다 → 할인.

    2026-07-27 실측: '노래방 간판'(수요6) 상위 10개 중 7개가 부산·목동·용산·수원·대전·창원
    같은 **지역 시공 후기**였다. 우리 c38 이 30위 밖인 이유는 글이 부실해서가 아니라
    검색 의도(내 지역 시공 사례)와 형식이 달라서였다. 실제 시공지를 모르는 우리는
    그 형식을 정직하게 쓸 수 없으므로 아예 피하는 게 맞다.
    `opportunity.py scan` 이 만든 data/opportunities.json 을 근거로 쓴다(없으면 할인 없음).
    """
    rows = _load(OPPORTUNITIES, [])
    if not isinstance(rows, list):
        return 1.0
    for r in rows:
        if r.get("keyword") != kw:
            continue
        n = r.get("n") or 0
        if n < 5:                      # 표본이 적으면 판단 보류
            return 1.0
        ratio = (r.get("local") or 0) / n
        if ratio >= 0.6:
            return 0.3                 # 지역 시공후기 판 — 사실상 진입 불가
        if ratio >= 0.35:
            return 0.7
        return 1.0
    return 1.0


def _winnability(kw: str, latest: dict) -> float:
    """승산 보정 0~1. 수요(자동완성)는 검색활동만 재고 '우리가 이길 수 있는지'는
    못 잰다. 그래서 우리 실측 순위를 승산 신호로 쓴다:
      - 이미 상위(≤10위) 노출 = 승산 입증 → 1.0
      - 측정했는데 30위 밖(None)/저순위(>10) = 신생 블로그가 못 이기는 키워드 → 할인
      - 미측정 = 아직 기회(발행해서 확인) → 할인 없음 1.0
    head term(아이스버킷·네온사인 등)은 수요는 높아도 30위 밖으로 드러나 여기서 걸린다.
    """
    fmt = _serp_format_penalty(kw)
    if kw not in latest:
        return 1.0 * fmt
    r = latest[kw]
    if isinstance(r, int) and r <= 10:
        return 1.0
    return 0.35 * fmt


def _opp_row(kw: str) -> dict | None:
    """opportunity 스캔에 저장된 그 키워드의 진단 행."""
    try:
        import opportunity  # noqa: PLC0415
        for r in _load(opportunity.OUT, []):
            if r.get("keyword") == kw:
                return r
    except Exception:
        pass
    return None


def _sparse_field(kw: str) -> bool:
    """모바일 블로그탭 경쟁이 비어 있는 판인가(opportunity 스캔 결과 기준)."""
    r = _opp_row(kw)
    if not r:
        return False
    import opportunity  # noqa: PLC0415
    return r.get("n", 99) <= opportunity.SPARSE_N and not r.get("homonym_risk")


def _sparse_score(kw: str) -> float:
    """경쟁이 비어 있는 판의 기회 점수(없으면 0 = 기여 없음)."""
    r = _opp_row(kw)
    return float(r.get("score", 0.0)) if r and _sparse_field(kw) else 0.0


def _proven_inflow_score(kw: str) -> float:
    """**실제로 사람이 그 말로 검색해 들어온 적이 있으면** 수요 프록시보다 이걸 믿는다.

    자동완성 제안 수는 '검색 활동이 있나'의 프록시일 뿐인데, 우리는 그걸 유일한
    수요 신호로 써 왔다. 그러면서 정작 **실측 유입어**(Creator Advisor 유입분석)는
    엔진이 한 번도 보지 않았다 — 우리가 가진 유일한 정답지인데.
    7/26~27 유입어 4개 중 3개(휴대용led응원피켓·vip피켓·비제이 전광판 구매)를
    자동완성은 0으로 본다. 프록시 0 = 발행 후순위 = 실제로 들어오던 판을 스스로 버렸다.

    표본이 작으므로(하루 1~5명) 1.0 이 아니라 0.9 를 **하한**으로만 준다.
    """
    if not kw:
        return 0.0
    try:
        m = _load(ROOT / "data" / "metrics.json", {})
        qs = {q.get("q", "").replace(" ", "").lower()
              for rows in (m.get("inflow_queries") or {}).values() for q in rows}
    except Exception:
        return 0.0
    return 0.9 if kw.replace(" ", "").lower() in qs else 0.0


def _user_priority(kw: str) -> float:
    """운영자가 대시보드 '키워드' 탭에 직접 넣은 키워드면 가산한다.

    엔진이 쓰는 수요·경쟁 신호는 전부 프록시다. 현장을 아는 사람이 "이건 된다"고
    집어넣은 키워드는 그 프록시들보다 나은 정보일 때가 많다(실측 유입어
    '휴대용led응원피켓'을 자동완성은 0으로 봤다). 그래서 **배제가 아니라 가산**으로 둔다.
    """
    if not kw:
        return 1.0
    try:
        import config  # noqa: PLC0415
        picks = [k.replace(" ", "").lower() for k in config.load_keywords()]
    except Exception:
        return 1.0
    return 1.25 if kw.replace(" ", "").lower() in picks else 1.0


NO_BLOG_PENALTY = 0.2
# 근거(2026-08-13 실측): 그 키워드의 모바일 SERP 에 **블로그 영역 자체가 없다**.
# 결과가 쇼핑·플레이스·광고뿐이라 1위를 해도 유입이 0이다. 수요 0(0.3)보다 확실한
# 신호다 — 수요는 자동완성 프록시지만 이건 직접 관측이다. 0 이 아니라 0.2 인 이유:
# 네이버 SERP 구성은 바뀔 수 있고 관측이 며칠치뿐이라, 완전히 죽이지 않고 밀어만 둔다.


def _no_blog_penalty(kw: str) -> float:
    """블로그 노출 자리가 없는 판이면 1위를 해도 방문자가 생기지 않는다.

    2026-08-13 까지 우리는 이 상태를 '수집 실패'로 버렸고, 그래서 해당 키워드는
    '미측정 = 기회'로 full 점수를 받아 큐를 잘못된 판으로 밀고 있었다.
    (b 초안 7편이 공유하는 '아이스버킷'이 바로 여기 해당한다.)
    """
    m = _load(METRICS, {})
    nb = m.get("serp_no_blog", {})
    if not nb or not kw:
        return 1.0
    if not any(kw in (nb.get(d) or []) for d in sorted(nb)[-3:]):
        return 1.0
    # 같은 기간에 순위가 잡힌 적이 있으면 판은 있는 것이다(관측 흔들림 방어).
    ranks = m.get("ranks", {})
    for d in sorted(ranks)[-3:]:
        if isinstance((ranks.get(d) or {}).get(kw), int):
            return 1.0
    return NO_BLOG_PENALTY


def _zero_demand_penalty(kw: str, dmap: dict) -> float:
    """검색 수요가 **실측 0**이면 총점을 깎는다 — 단 경쟁이 비어 있으면 깎지 않는다.

    2026-07-28 실측: b19(LED 폭죽 트레이)·b21(클럽 조형물) 같은 수요 0 초안이 큐 #4·#5 였다.
    수요 0 + 경쟁 빽빽 = 발행 슬롯 낭비가 맞다.

    ★그런데 2026-07-30 유입 실측에서 정반대 사례가 나왔다. **실제 유입 6건 중 5건이
    자동완성 수요 0~4 인 BJ 키워드**였다(vip피켓·led 응원 피켓·비제이 전광판 구매).
    그 판은 모바일 블로그탭 경쟁이 0~3건으로 비어 있어서, 검색량이 적어도 그 소량이
    전부 우리에게 왔다. 반대로 수요 9였던 '카페입간판'은 유입 0.
    → **수요가 아니라 '수요 0 × 경쟁 빽빽'이 나쁜 조합**이다. 경쟁이 비면 면제한다.
    (이 페널티가 a 세그먼트를 큐 바닥에 묶어두고 있었다 — BJ 쿼터로 겨우 발행되던 것.)
    """
    n = dmap.get(kw)
    if not (isinstance(n, (int, float)) and n == 0):
        return 1.0                      # 수요 있음 / 미측정 → 깎지 않음
    return 1.0 if _sparse_field(kw) else 0.3


def _cannibal_penalty(kw: str, published_kws: set, ranked: dict | None = None) -> float:
    """같은 타깃 키워드로 이미 발행한 글이 있으면 할인(자기잠식 방지).

    2026-07-28 실측: 초안 44편이 35종 키워드를 쓰는데 **'아이스버킷' 하나에 7편**이 몰려 있었다.
    같은 키워드로 여러 편을 올리면 네이버가 어느 글을 대표로 볼지 흐려져 서로 순위를 깎아먹는다.
    이미 그 키워드로 발행한 글이 있으면 새 글보다 **그 글을 키우는 게** 낫다.

    ★단, **죽은 글은 잠식하지 않는다**(2026-08-13). 전수조사에서 발행 48편 중 29편이
    색인에서 빠져 있었다(7/22~8/04 = 2/34편). 그 글들은 검색에 존재하지 않으므로
    깎을 자리도 없다 — 경쟁 상대가 아니라 시체다. 그런데 이 페널티가 걸려서
    **죽은 글을 되살리는 새 글이 구조적으로 큐 바닥에 묶였다**(a35 닉네임 피켓 제작).
    판정은 순위로 한다: 그 키워드로 우리가 순위에 잡히면 살아 있는 글이 있는 것이고,
    30위 밖이면 잠식할 자리 자체가 없다.
    """
    if not (kw and kw in published_kws):
        return 1.0
    # 키가 있고 값이 null 이어야 '실측 30위 밖'이다. 키가 아예 없으면 수집 실패이므로
    # 판단 근거가 없고, 그때는 보수적으로 잠식을 유지한다.
    if ranked is not None and kw in ranked and not isinstance(ranked[kw], int):
        return 1.0          # 발행은 했지만 그 키워드로 순위가 없다 = 잠식 대상 아님
    return 0.35


def rank_queue() -> list[dict]:
    """미발행 초안 전부를 우선순위 점수와 함께 정렬해 반환(설명 포함)."""
    w = load_weights()
    state = _load(STATE, {"published": []})
    published = set(state.get("published", []))
    segs = segment_scores()
    dmap = _demand_map()
    latest = _latest_ranks()

    # 세그먼트별 발행 수(explore: 적게 발행된 세그먼트 우대)
    pub_per_seg = {"a": 0, "b": 0, "c": 0}
    for n in published:
        pub_per_seg[_segment(n)] += 1
    max_pub = max(pub_per_seg.values()) or 1

    # diversity: 미발행 세그먼트별 잔량이 많을수록 약간 우대(폭 유지)
    unpub = [p for p in _ordered_drafts() if p.name not in published]
    rem_per_seg = {"a": 0, "b": 0, "c": 0}
    for p in unpub:
        rem_per_seg[_segment(p.name)] += 1
    max_rem = max(rem_per_seg.values()) or 1

    # 이미 발행한 글들의 타깃 키워드 — 같은 키워드를 또 쓰면 서로 잡아먹는다
    published_kws = set()
    for n in published:
        pp = DRAFTS / n
        if pp.exists():
            k = primary_keyword(pp)
            if k:
                published_kws.add(k)

    rows = []
    for p in unpub:
        seg = _segment(p.name)
        kw = primary_keyword(p)
        seg_s = segs[seg]
        seo_s = _seo_score(p)
        # 검색 수요 × 승산 보정(못 이긴다고 드러난 head 는 깎임) × 경쟁깊이 페널티
        # (경쟁이 우리보다 깊으면 할인). 단, 리서치로 '경쟁보다 깊게 썼다'가 확인되면
        # 저수요여도 기회 점수로 끌어올림(대칭 보정).
        demand_s = (_demand_score(kw, dmap) * _winnability(kw, latest)
                    * _research_penalty(kw, p))
        if _opportunity_allowed(kw, dmap):
            demand_s = max(demand_s, _research_opportunity(kw, p))
        # ★모바일 경쟁이 비어 있는 판은 자동완성 수요와 무관하게 기회다(2026-07-30 실측).
        # opportunity.score 가 이미 그 판정을 담고 있으므로 하한으로 쓴다.
        demand_s = max(demand_s, _sparse_score(kw))
        # ★실측 유입어는 프록시보다 위다 — 그 말로 사람이 실제로 들어온 적이 있다.
        demand_s = max(demand_s, _proven_inflow_score(kw))
        explore = 1 - pub_per_seg[seg] / max_pub      # 적게 발행된 세그먼트 ↑
        diversity = rem_per_seg[seg] / max_rem         # 잔량 많은 세그먼트 ↑
        base = (w["demand"] * demand_s + w["seg"] * seg_s + w["seo"] * seo_s
                + w["explore"] * explore + w["diversity"] * diversity)
        fit = _fit_multiplier(kw)                      # 손님 우선순위(BJ/스트리머 우선)
        cann = _cannibal_penalty(kw, published_kws, latest)   # 중복 발행 방지(죽은 글은 제외)
        zero = _zero_demand_penalty(kw, dmap)          # 실측 수요 0 = 검색 유입 없음
        noblog = _no_blog_penalty(kw)                  # SERP 에 블로그 자리가 없는 판
        pick = _user_priority(kw)                      # 운영자가 직접 넣은 타깃 키워드
        total = base * fit * cann * zero * noblog * pick
        rows.append({
            "name": p.name, "seg": seg, "keyword": kw,
            "score": round(total, 4),
            "breakdown": {"demand": round(demand_s, 2), "seg": round(seg_s, 2),
                          "seo": round(seo_s, 2), "explore": round(explore, 2),
                          "diversity": round(diversity, 2), "fit": round(fit, 2),
                          "pick": round(pick, 2), "noblog": round(noblog, 2)},
        })
    rows.sort(key=lambda r: r["score"], reverse=True)
    # 큐 안에서도 같은 키워드가 겹치면 1등만 남기고 나머지는 할인한다.
    # (아직 아무것도 발행 안 한 키워드라도 두 편을 연달아 올리면 똑같이 자기잠식이다.)
    seen_kw: set = set()
    for r in rows:
        k = r["keyword"]
        if not k:
            continue
        if k in seen_kw:
            r["score"] = round(r["score"] * 0.35, 4)
            r["breakdown"]["cannibal"] = 0.35
        else:
            seen_kw.add(k)
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows


def next_draft(recent_segments: list[str] | None = None) -> str | None:
    """최고 우선순위 초안. 단, 같은 세그먼트 3연속 발행은 피한다(폭 유지)."""
    q = rank_queue()
    if not q:
        return None
    recent = recent_segments or _recent_published_segments(2)
    for r in q:
        # 직전 2개가 모두 같은 세그먼트면 그 세그먼트는 건너뛴다
        if len(recent) >= 2 and recent[-1] == recent[-2] == r["seg"]:
            continue
        return r["name"]
    return q[0]["name"]   # 전부 걸리면 그냥 1등


def _recent_published_segments(n: int) -> list[str]:
    log = _load(STATE, {"log": []}).get("log", [])
    segs = [_segment(e["draft"]) for e in log
            if e.get("ok") and not e.get("dry") and e.get("draft")]
    return segs[-n:]


# ---------- 자가 평가·튜닝 ----------

def evaluate_and_tune(apply: bool = True) -> dict:
    """과거 발행 결정을 관측 순위로 평가해 가중치를 미세 보정한다.

    아이디어: 발행글마다 '결정 당시 가장 크게 작용한 신호'와 '실제 성과(순위)'를 대응.
    좋은 성과(순위≤5)를 낸 결정에서 큰 신호였던 항목의 가중치를 소폭↑, 나쁜 성과는 소폭↓.
    데이터가 적으면 거의 움직이지 않는다(안전).
    """
    w = load_weights()
    state = _load(STATE, {"published": []})
    latest = _latest_ranks()
    segs = segment_scores()
    dmap = _demand_map()

    adjust = {k: 0.0 for k in DEFAULT_WEIGHTS}
    samples = 0
    for name in state.get("published", []):
        p = DRAFTS / name
        if not p.exists():
            continue
        kw = primary_keyword(p)
        r = latest.get(kw)
        if not isinstance(r, int):
            continue
        samples += 1
        # 정직 교정: 순위가 좋아도(대개 1위) 수요0이면 방문자 0 → '좋은 성과'가 아니다.
        # 이 결정의 교훈은 "수요 신호를 더 봐야 한다"이므로 demand 가중치를 올린다.
        if _demand_score(kw, dmap) <= 0:
            adjust["demand"] += 0.02
            continue
        good = r <= 5           # 수요 있는 키워드에서의 1페이지 상단만 진짜 성과
        seg = _segment(name)
        # 결정 당시 신호값(근사: 현재 세그먼트점수/ SEO)
        signals = {"seg": segs[seg], "seo": _seo_score(p),
                   "explore": 0.5, "diversity": 0.5}
        # 가장 큰 신호를 좋은/나쁜 성과에 따라 소폭 조정
        top = max(signals, key=signals.get)
        adjust[top] += (0.02 if good else -0.02)

    # 방문자 반영(정직·보수적): 순위는 좋은데 방문자가 여러 날 '하락'이면
    # exploit(세그먼트)만으론 안 되는 것 → 탐색/폭을 조금 올려 다른 주제를 시도.
    # 데이터 5일+ 있고 뚜렷한 하락일 때만 작동(노이즈 방지).
    vt = visitor_trend()
    if vt["days"] >= 5 and vt["label"] == "하락":
        adjust["explore"] += 0.02
        adjust["diversity"] += 0.02
        adjust["seg"] -= 0.02

    new = {k: max(0.05, w[k] + adjust[k]) for k in DEFAULT_WEIGHTS}
    s = sum(new.values())
    new = {k: round(new[k] / s, 4) for k in new}
    result = {"samples": samples, "old": w, "new": new, "adjust": adjust,
              "visitor_trend": vt["label"]}
    if apply and samples > 0:
        _save(WEIGHTS_FILE, new)
        log = _load(GLOG, {"tune": []})
        log.setdefault("tune", []).append(
            {"date": str(date.today()), "samples": samples, "weights": new})
        _save(GLOG, log)
    return result


# ---------- 리포트 ----------

def report() -> str:
    segs = segment_scores()
    w = load_weights()
    q = rank_queue()
    vt = visitor_trend()
    rv = rank_visitor_signal()
    lines = ["===== 방문자 성장 엔진 리포트 =====",
             f"세그먼트 성과(관측): a={segs['a']:.2f} b={segs['b']:.2f} c={segs['c']:.2f} (1=최고)",
             f"방문자 추세: {vt['label']}"
             + (f" (최근 +{vt['recent_new_per_day']}/일, 누적 {vt['latest_total']})"
                if vt['recent_new_per_day'] is not None else f" (누적 {vt['latest_total']})")
             + (f"  ※{vt['as_of']} 기준" if vt.get("lag") else ""),
             f"순위→방문자 상관: {rv['corr'] if rv['corr'] is not None else rv['note']}",
             f"현재 가중치: " + " ".join(f"{k}={w[k]:.2f}" for k in DEFAULT_WEIGHTS),
             f"다음 추천 발행: {next_draft()}",
             "우선순위 상위 5:"]
    for r in q[:5]:
        b = r["breakdown"]
        lines.append(f"  {r['score']:.3f}  [{r['seg']}] {r['name']}  "
                     f"(seg{b['seg']}/seo{b['seo']}/exp{b['explore']}/div{b['diversity']})")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["plan", "tune", "report"], nargs="?", default="report")
    a = ap.parse_args()
    if a.cmd == "tune":
        r = evaluate_and_tune()
        print(f"자가 튜닝: 샘플 {r['samples']}개")
        print("  이전:", r["old"])
        print("  이후:", r["new"])
    elif a.cmd == "plan":
        for r in rank_queue()[:10]:
            print(f"{r['score']:.3f} [{r['seg']}] {r['name']}  {r['keyword']}")
    else:
        print(report())


if __name__ == "__main__":
    main()
