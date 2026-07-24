"""스모크 테스트 — 브라우저/네트워크 없이 순수 로직만 검증한다.

지금까지 조용히 프로덕션에 나갔던 버그들을 회귀 방지로 잠근다:
  - 소제목(##)이 파서에서 통째로 누락되던 버그
  - 이미지 조건을 image_dir 로 보던(스케줄러 경로 이미지 누락) 버그
  - metrics 키워드에 '(주력):' 접두사가 섞이던 버그
  - schtasks 결과코드 부호(2147946720 vs -2147020576) 정규화
실행:  .venv\Scripts\python.exe test_smoke.py   (종료코드 0=성공)
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

_passed = _failed = 0


def check(name, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  [OK] {name}")
    else:
        _failed += 1
        print(f"  [FAIL] {name}  {detail}")


def section(t):
    print(f"\n== {t} ==")


DRAFTS = ROOT / "drafts"
B13 = DRAFTS / "b13_led-icebucket-case.md"
SAMPLE = DRAFTS / "sample_bj-picket-guide.md"


def t_parser():
    section("draft_parser (소제목 누락 회귀 방지)")
    from publish.draft_parser import parse_draft
    d = parse_draft(B13)
    heads = [b for b in d["blocks"] if b["kind"] == "heading"]
    imgs = [b for b in d["blocks"] if b["kind"] == "image"]
    check("소제목이 파싱된다(>0)", len(heads) > 0, f"heading={len(heads)}")
    check("이미지 블록에 ALT 존재", imgs and all((b.get("alt") or "").strip() for b in imgs))
    check("제목이 있다", d["title"] and d["title"] != "제목 없음")
    check("태그 5개 이상", len(d["tags"]) >= 5, f"tags={len(d['tags'])}")
    check("본문 text 블록이 # 로 시작하지 않음",
          all(not b["text"].startswith("#") for b in d["blocks"] if b["kind"] == "text"))


def t_seo():
    section("seo.score_draft")
    import seo
    r = seo.score_draft(B13)
    check("점수 0~100", 0 <= r["score"] <= 100, str(r["score"]))
    check("등급 ABCD", r["grade"] in ("A", "B", "C", "D"))
    names = {c["name"] for c in r["checks"]}
    check("10개 항목 모두 존재", names >= set(seo.WEIGHTS), names ^ set(seo.WEIGHTS))
    hc = next(c for c in r["checks"] if c["name"] == "headings")
    check("headings 항목 점수>0 (소제목 반영)", hc["pts"] > 0, str(hc["pts"]))
    check("좋은 글은 B 이상", r["score"] >= 70, str(r["score"]))


def t_images():
    section("images.pick_images (세그먼트 매칭)")
    from publish import images as im
    picks, _ = im.pick_images(B13, 4)
    check("요청 수만큼 선택", len(picks) == 4, str(len(picks)))
    check("첫 장은 인포그래픽", picks and picks[0].parent == im.IMG_DIR)
    # 배터리 사진은 배터리 주제 글에만(캡션-사진 불일치 방지)
    check("배터리 사진: 무관 글 차단", im._photo_allowed("a_방송용엑셀피켓배터리_02.jpg",
                                                    "a17_cheer-picket-custom.md") is False)
    check("배터리 사진: 배터리 글 허용", im._photo_allowed("a_방송용엑셀피켓배터리_02.jpg",
                                                     "a05_wireless-handboard-battery.md") is True)
    check("일반 사진: 항상 허용", im._photo_allowed("a_시그니처피켓디자인_02.jpg",
                                               "a17_cheer-picket-custom.md") is True)
    # a(BJ/스트리머) 초안은 실물 피켓 사진이 대표(첫 장) — 썸네일 지시(2026-07-24)
    a_drafts = sorted(DRAFTS.glob("a*.md"))
    if a_drafts:
        apicks, _ = im.pick_images(a_drafts[0], 3, advance=False)
        check("a 초안 첫 장은 실물 사진(BJ 썸네일)",
              len(apicks) >= 2 and apicks[0].parent == im.PHOTO_DIR,
              str(apicks[:2]))
    photos = [p for p in picks if p.parent == im.PHOTO_DIR]
    if photos:
        seg_ok = all(p.name.startswith("b_") for p in photos)
        check("사진은 같은 세그먼트(b_)", seg_ok, [p.name for p in photos])
    else:
        check("사진 풀 접근", True)


def t_metrics():
    section("metrics.primary_keyword ((주력) 접두사 제거)")
    import metrics
    kw = metrics.primary_keyword(SAMPLE)   # Path 를 받는다(파일 읽음)
    check("키워드에 '(주력)' 없음", "주력" not in kw, kw)
    check("키워드에 ':' 없음", ":" not in kw, kw)
    check("키워드 비어있지 않음", bool(kw.strip()), kw)


def t_research():
    section("research 텍스트 정규화")
    import research
    check("조사 제거: 화면을→화면", research._strip_josa("화면을") == "화면")
    check("조사 제거: 조명이→조명", research._strip_josa("조명이") == "조명")
    nn = research._nouns("무선 아이스버킷을 노력합니다 다양하게 로고")
    check("동사형 제외: 노력합니다 없음", "노력합니다" not in nn, nn)
    check("명사 유지: 로고 있음", "로고" in nn, nn)


def t_growth():
    section("growth 승산/기회 로직")
    import growth
    # _winnability: 미측정=1.0, 30위밖(None)/저순위=할인, 상위(≤10)=1.0
    check("winnability 미측정=1.0", growth._winnability("없는키워드zzz", {}) == 1.0)
    check("winnability 30위밖(None)=할인", growth._winnability("x", {"x": None}) < 1.0)
    check("winnability 상위(≤10)=1.0", growth._winnability("x", {"x": 3}) == 1.0)
    check("winnability 저순위(>10)=할인", growth._winnability("x", {"x": 25}) < 1.0)
    # _research_opportunity: 리서치 없으면 0(기여 없음)
    check("research 기회 없는 키워드=0", growth._research_opportunity("리서치없음zzz", SAMPLE) == 0.0)
    # 기회 승격 게이트: 실측 수요 0 이면 경쟁을 이겨도 방문자 0 → 승격 금지
    check("opportunity 실측수요0=금지", growth._opportunity_allowed("x", {"x": 0}) is False)
    check("opportunity 수요있음=허용", growth._opportunity_allowed("x", {"x": 3}) is True)
    check("opportunity 미측정=허용", growth._opportunity_allowed("x", {}) is True)
    check("research 페널티 없는 키워드=1.0", growth._research_penalty("리서치없음zzz", SAMPLE) == 1.0)
    # 손님 적합성: BJ/스트리머(피켓·전광판)=1.0 > 엔터(노래방)=0.92 > 일반상가(상가 간판)=0.6
    check("fit BJ 우선(응원 피켓 제작)=1.0", growth._fit_multiplier("응원 피켓 제작") == 1.0)
    check("fit 엔터(노래방 간판) 중간", 0.9 <= growth._fit_multiplier("노래방 간판") < 1.0)
    check("fit 일반상가(상가 간판) 감점", growth._fit_multiplier("상가 간판") < 0.7)
    check("fit BJ > 일반상가", growth._fit_multiplier("전광판 제작") > growth._fit_multiplier("카페 간판"))
    check("demand_score 0~1", 0.0 <= growth._demand_score("아무거나", {}) <= 1.0)
    # segment_scores: a/b/c 키 + 0~1
    segs = growth.segment_scores()
    check("segment_scores 키/범위", set(segs) == set("abc") and all(0 <= v <= 1 for v in segs.values()))
    # rank_queue: 내림차순 정렬 + demand 0~1
    q = growth.rank_queue()
    check("rank_queue 비어있지 않음", len(q) > 0)
    check("rank_queue 내림차순", all(q[i]["score"] >= q[i + 1]["score"] for i in range(len(q) - 1)))
    check("rank_queue demand 0~1", all(0 <= r["breakdown"]["demand"] <= 1 for r in q))


def t_dashboard():
    section("dashboard 유틸")
    import dashboard
    check("결과코드 부호 정규화", dashboard._norm_code(2147946720) == "-2147020576",
          dashboard._norm_code(2147946720))
    check("정상코드 0 유지", dashboard._norm_code("0") == "0")
    mv = dashboard._metrics_view()
    check("_metrics_view 키 존재", {"series", "kw_rows", "kw_on_page1"} <= set(mv))


def main():
    for t in (t_parser, t_seo, t_images, t_metrics, t_research, t_growth, t_dashboard):
        try:
            t()
        except Exception:
            global _failed
            _failed += 1
            print(f"  [ERROR] {t.__name__}\n{traceback.format_exc()}")
    print(f"\n결과: {_passed} 통과 / {_failed} 실패")
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()
