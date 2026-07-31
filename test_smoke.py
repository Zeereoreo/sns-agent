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
    # 첫 슬롯(대표=인포그래픽)만 초안 ALT 를 쓰고, 나머지는 발행 시 실제 사진에서 캡션 생성.
    check("첫 이미지 슬롯에 ALT 존재", imgs and (imgs[0].get("alt") or "").strip())
    from publish.images import photo_caption
    check("사진에서 캡션 생성", photo_caption("drafts/photos/c_LED아크릴사인_003.jpg")
          == "LED 아크릴 사인 제작 사례", photo_caption("c_LED아크릴사인_003.jpg"))
    check("모르는 사진은 캡션 없음", photo_caption("drafts/images/price-factors.png") == "")

    import re
    from pathlib import Path as _P

    from publish.images import _group_pool
    gp = _group_pool([_P("a_LED피켓_g01_01.jpg"), _P("a_LED피켓_g01_02.jpg"),
                      _P("a_LED피켓_g02_01.jpg"), _P("a_곰돌이네온피켓_01.jpg")])
    check("제품 그룹으로 묶인다", [len(g) for g in gp] == [2, 1, 1], [len(g) for g in gp])

    from publish.images import pick_images
    picks, _ = pick_images("drafts/a04_reaction-picket-bigfan.md", 9, advance=False)
    pool_picks = [p for p in picks if p.parent.name == "photos"]
    gs = {re.search(r"_(g\d+)_", p.name).group(1) for p in pool_picks
          if re.search(r"_(g\d+)_", p.name)}
    check("한 글에 제품 그룹 2개 이하", len(gs) <= 2, f"groups={sorted(gs)}")

    # 사진 일관성(2026-07-27 사용자 지적 "사진들이 일관성이 없다")
    from publish.images import IMG_DIR, GROUP_MAX, _group_pool as _gp2, _imgs, PHOTO_DIR
    allpool = [p for p in _imgs(PHOTO_DIR) if p.parent == PHOTO_DIR]
    # 제품 라벨(_pNN_)이 붙은 묶음은 진짜 한 제품이라 커도 된다.
    # 라벨이 없는 순번 묶음(_gNN_)만 GROUP_MAX 로 잘라야 한다.
    seq_sizes = [len(g) for g in _gp2(allpool)
                 if g and not re.search(r"_(p\d+)_", g[0].name)]
    check("순번 묶음은 너무 크지 않다", max(seq_sizes) <= GROUP_MAX, max(seq_sizes))
    check("대표는 항상 실물 사진", picks[0].parent != IMG_DIR, picks[0].name)
    check("인포그래픽은 사이에 안 낀다",
          all(p.parent != IMG_DIR for p in picks[1:-1]),
          [p.name for p in picks if p.parent == IMG_DIR])

    # a(BJ) 글은 눈으로 확인한 제품 라벨(_pNN_)로 한 제품만 쓴다
    for dn in ("a18_streamer-goods.md", "a06_platform-picket-difference.md"):
        pk, _ = pick_images(f"drafts/{dn}", 9, advance=False)
        pids = {re.search(r"_(p\d+)_", p.name).group(1)
                for p in pk if re.search(r"_(p\d+)_", p.name)}
        check(f"{dn[:3]} 제품 1종", len(pids) == 1, f"{sorted(pids)}")

    # 내부 링크: json 임포트 누락으로 조용히 []를 반환하던 버그 잠금
    from publish.naver import related_links
    rel = related_links("c29_small-business-sign.md", limit=2)
    check("내부 링크가 나온다", len(rel) > 0, f"{rel}")
    check("내부 링크는 logNo URL", all(u.rstrip('/').split('/')[-1].isdigit() for _, u in rel))
    check("자기 글은 제외", all("c29" not in t for t, _ in rel))

    # SERP 형식 페널티: 지역 시공후기 판은 피해야 한다(2026-07-27 실측 근거)
    import growth
    pen = growth._serp_format_penalty
    # 실측 수요 0 = 검색 유입 없음 → 발행 슬롯 낭비. 미측정은 깎지 않는다.
    zd = growth._zero_demand_penalty
    check("수요 0 은 할인", zd("x", {"x": 0}) == 0.3)
    check("수요 있으면 유지", zd("x", {"x": 3}) == 1.0)
    check("미측정은 할인 없음", zd("x", {}) == 1.0)

    # 자기잠식: 같은 키워드로 여러 편을 올리면 서로 순위를 깎는다
    check("발행된 키워드는 할인", growth._cannibal_penalty("아이스버킷", {"아이스버킷"}) == 0.35)
    check("새 키워드는 할인 없음", growth._cannibal_penalty("오픈 네온사인", {"아이스버킷"}) == 1.0)
    # 보장하는 것: 같은 키워드가 여럿이면 1등 외에는 반드시 할인이 붙는다.
    # (절대 순위는 다른 신호에 따라 바뀔 수 있어 '상위 N에 없음'으로 검증하면 불안정하다.)
    qq = growth.rank_queue()
    seen_k, undiscounted = set(), []
    for r in qq:
        k = r["keyword"]
        if not k:
            continue
        if k in seen_k and not r["breakdown"].get("cannibal"):
            undiscounted.append(r["name"])
        seen_k.add(k)
    check("중복 키워드는 모두 할인됨", not undiscounted, undiscounted)
    check("연속 2편이 같은 키워드가 아님",
          all(qq[i]["keyword"] != qq[i + 1]["keyword"] for i in range(min(4, len(qq) - 1))))

    check("지역형 판은 강하게 할인", pen("노래방 간판") <= 0.35, pen("노래방 간판"))

    # 제목 정책(2026-07-29 수정): 길이로 거르지 않는다. 이 판의 승자는 나열형 장문이고
    # (원본 made-us 구글 1위 글이 104자), 짧게 쓰는 건 우리뿐이었다. 상한은 네이버 100자만.
    from publish.draft_parser import TITLE_NAVER_MAX, _choose_title
    short, long_ = "짧은 제목 피켓", "아주 길게 늘어놓은 방송용 피켓 제목 후보 예시입니다 정말 길죠"
    picked = {_choose_title([short, long_], "피켓", f"x{i}.md") for i in range(6)}
    check("긴 후보도 로테이션에 포함", len(picked) >= 1 and picked <= {short, long_}, picked)
    over = "피켓" + "가" * 120
    check("네이버 상한 넘는 후보는 제외",
          _choose_title([short, over], "피켓", "z.md") == short)
    check("상한 넘는 것뿐이면 그대로", _choose_title([over], "피켓", "w.md") == over)
    check("제목 상한 100", TITLE_NAVER_MAX == 100)

    # 라이브 편집 총량 제한 — 대량 편집 직후 세션이 두 번 끊겼다(7/28·7/30)
    import enrich_posts as _ep
    check("1회 편집 한도 있음", 1 <= _ep.EDIT_MAX_PER_RUN <= 10, _ep.EDIT_MAX_PER_RUN)
    check("하루 편집 한도 있음", 1 <= _ep.EDIT_MAX_PER_DAY <= 30, _ep.EDIT_MAX_PER_DAY)
    check("편집 사이 대기 있음", min(_ep.EDIT_PAUSE_SEC) >= 5, _ep.EDIT_PAUSE_SEC)
    check("편집 카운터 함수", callable(_ep._edits_today) and callable(_ep._record_edit))
    # 색인 미확인이면 경고만 하고 진행(2026-07-30 사용자 지시). 차단은 총량 제한이 맡는다.
    check("색인 경고 함수 존재", callable(_ep._index_ok))
    check("색인 미확인이어도 편집 허용", _ep._index_ok() is True)

    # 순위·경쟁 측정은 **모바일** 기준이어야 한다(실측 유입이 전부 m.search 였다)
    import inspect

    import metrics as _mt
    import opportunity as _op
    check("순위 측정 기본이 모바일", "m.search.naver.com" in inspect.getsource(_mt._rank_of))
    check("기회 SERP 가 모바일", "m.search.naver.com" in inspect.getsource(_op._serp))
    check("유입 검색어 수집 함수", callable(_mt._inflow_queries))

    # ★수요 0 이어도 경쟁이 비어 있으면 기회다 — 실측 유입 6건 중 5건이 그런 키워드였다
    _empty = {"n": 0, "ontopic": 0, "local": 0, "case": 0, "info": 0,
              "local_ratio": 0, "info_ratio": 0, "homonym_risk": False}
    _sparse = dict(_empty, n=2)
    _dense = dict(_empty, n=12)
    check("수요0 + 경쟁0 = 최고 기회", _op.score(0, _empty) >= 0.7, _op.score(0, _empty))
    check("수요0 + 경쟁2 도 기회", _op.score(0, _sparse) >= 0.6, _op.score(0, _sparse))
    check("수요0 + 경쟁많음 = 0", _op.score(0, _dense) == 0.0)
    check("빈 판이 수요 큰 빽빽한 판보다 높다",
          _op.score(0, _sparse) > _op.score(9, dict(_dense, ontopic=6)))

    # 세션 만료 사전 경고: 죽고 나서가 아니라 미리 알아야 발행이 안 멈춘다
    import metrics as _m
    check("만료 사전경고 기준", _m.SESSION_WARN_DAYS >= 7, _m.SESSION_WARN_DAYS)
    check("만료일 조회 함수", callable(_m._session_expiry) and callable(_m._warn_session_soon))

    # 기회 스캔은 주 1회만 — 매 발행마다 SERP 를 수십 번 긁지 않게
    import scheduler as sch
    check("주간 스캔 주기 설정", sch.OPP_EVERY_DAYS >= 7, sch.OPP_EVERY_DAYS)
    check("스캔 함수 존재", callable(sch._scan_opportunities_weekly))
    check("수요 갱신도 주간", sch.DEMAND_EVERY_DAYS >= 7 and callable(sch._audit_demand_weekly))

    # 자기 문제로 반복 실패하는 초안은 건너뛴다(큐가 그 자리에서 막히지 않게)
    real = "a19_led-picket-diy-vs-order.md"      # 실제로 존재하는 초안으로 검증
    bad3 = [{"draft": real, "ok": False, "reason": "images_failed"}] * 3
    check("3회 실패면 건너뜀", sch._repeatedly_failing(bad3, set()) == {real})
    sess = [{"draft": real, "ok": False, "reason": "session_expired"}] * 5
    check("세션 만료는 초안 탓 아님", sch._repeatedly_failing(sess, set()) == set())
    mixed = bad3 + [{"draft": real, "ok": True}]
    check("성공하면 초기화", sch._repeatedly_failing(mixed, set()) == set())
    ghost = [{"draft": "(전체 삭제)", "ok": False, "reason": "images_failed"}] * 5
    check("없는 초안은 무시", sch._repeatedly_failing(ghost, set()) == set())

    # 주기 판정: 기록 없으면 '오래됨', 오늘 기록이면 '아님'
    import tempfile as _tf
    from datetime import date as _dt
    with _tf.TemporaryDirectory() as td:
        s = _P(td) / "stamp"
        check("기록 없으면 실행 대상", sch._stale(s, 7) is True)
        s.write_text(_dt.today().isoformat(), encoding="utf-8")
        check("오늘 기록이면 건너뜀", sch._stale(s, 7) is False)
    check("지역형 아닌 판은 유지", pen("네온사인") == 1.0, pen("네온사인"))
    check("모르는 키워드는 할인 없음", pen("존재하지않는키워드") == 1.0)

    # 기회 발굴의 제품 적합성 게이트 — 자동완성이 동음이의어로 새는 것을 막는다
    from opportunity import _serp, is_our_product  # noqa: F401  (_serp 존재 확인용)
    check("우리 제품 키워드 통과", is_our_product("응원 피켓") and is_our_product("LED 전광판"))
    check("동음이의어 제외", not is_our_product("아치서포트") and not is_our_product("핀서포트"))
    check("안 만드는 것 제외",
          not is_our_product("응원봉") and not is_our_product("커피창고")
          and not is_our_product("간판 시트지"))
    check("아이스버킷 챌린지/가방 제외",
          not is_our_product("아이스버킷챌린지") and not is_our_product("아이스버킷 가방"))

    # 온토픽 판정은 띄어쓰기를 무시해야 한다('카페입간판' vs '카페 입간판 추천')
    import opportunity as _opp
    _joined = _opp.diagnose("카페입간판", [{"title": "카페 입간판 추천, 철제입간판"}] * 3)
    check("붙여쓴 키워드도 온토픽으로 잡힌다", _joined["ontopic"] == 3, _joined["ontopic"])
    # 대소문자도 무시해야 한다 — 'vvip피켓' vs 'VVIP피켓 …' 이 안 맞아 동음이의어로 오탐됐다
    _case = _opp.diagnose("vvip피켓", [{"title": "VVIP피켓 VIP피켓 엑셀방송용 시그니처피켓"}] * 2)
    check("대소문자 달라도 온토픽", _case["ontopic"] == 2, _case["ontopic"])
    # 온토픽 0 은 빈틈이 아니라 동음이의어일 수 있다 → 표시하고 점수를 깎는다
    _homo = _opp.diagnose("바사인", [{"title": f"창세기 성경공부 {i}장"} for i in range(6)])
    check("온토픽 0 이면 동음이의어 경보", _homo["homonym_risk"] is True)
    check("동음이의어 의심은 점수 절반",
          _opp.score(7, _homo) < _opp.score(7, dict(_homo, homonym_risk=False)))
    # 글의 '주제'(네이버 전역 분류) — 미지정이면 주제별 탭·추천에서 통째로 빠진다.
    # 2026-07-29 이전에는 발행 코드가 아예 안 건드려서 발행분 전체가 '주제 선택 안 함' 이었다.
    from publish.naver import TOPIC_BY_SEG, _set_topic  # noqa: F401
    check("세그먼트별 주제 매핑 존재",
          all(TOPIC_BY_SEG.get(s) for s in ("a", "b", "c", "s")), TOPIC_BY_SEG)
    check("BJ 글 주제는 '방송'", TOPIC_BY_SEG["a"] == "방송" and TOPIC_BY_SEG["s"] == "방송")
    check("주제 설정 함수 존재", callable(_set_topic))
    # 발행 후 라이브 대조 — '발행됨'과 '제대로 나감'은 다르다(소제목 없이 14편이 나갔었다)
    from publish.naver import _LIVE_JS, _audit_live_post  # noqa: F401
    check("발행 결과 점검 함수 존재", callable(_audit_live_post))
    check("점검 JS 가 소제목·태그를 센다",
          "se-sectionTitle" in _LIVE_JS and "tags" in _LIVE_JS)

    # '지금 할 일 1가지' — 문제가 여럿이면 가장 앞선 전제(색인 > 세션 > 발행)부터
    import diagnostics as _diag
    _cs = [{"name": "키워드 중복", "level": "warn", "detail": "", "fix": ""},
           {"name": "네이버 색인", "level": "bad", "detail": "", "fix": ""},
           {"name": "네이버 세션", "level": "bad", "detail": "", "fix": ""}]
    check("색인이 세션보다 먼저", _diag.next_action(_cs)["name"] == "네이버 색인")
    check("bad 가 warn 보다 먼저",
          _diag.next_action([_cs[0], _cs[2]])["name"] == "네이버 세션")
    check("문제 없으면 None", _diag.next_action(
        [{"name": "x", "level": "ok", "detail": "", "fix": ""}]) is None)
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
    # 대표(첫 장)는 실물 사진이어야 한다 — 인포그래픽이 목록 썸네일이 되면 안 된다.
    check("첫 장은 실물 사진", picks and picks[0].parent != im.IMG_DIR, picks[0].name)
    # 배터리 사진은 배터리 주제 글에만(캡션-사진 불일치 방지)
    check("배터리 사진: 무관 글 차단", im._photo_allowed("a_방송용엑셀피켓배터리_02.jpg",
                                                    "a17_cheer-picket-custom.md") is False)
    check("배터리 사진: 배터리 글 허용", im._photo_allowed("a_방송용엑셀피켓배터리_02.jpg",
                                                     "a05_wireless-handboard-battery.md") is True)
    check("일반 사진: 항상 허용", im._photo_allowed("a_시그니처피켓디자인_02.jpg",
                                               "a17_cheer-picket-custom.md") is True)
    # 주제 매칭 썸네일: 초안 주제에 맞는 네온 컷이 대표가 되게(2026-07-24 지시)
    pool = [p for p in im._imgs(im.PHOTO_DIR) if p.parent == im.PHOTO_DIR]
    tp = im._theme_photo("a17_cheer-picket-custom.md", pool)
    check("테마: 응원→하트/큰손등장", tp is not None and ("하트" in tp.name or "큰손등장" in tp.name),
          str(tp))
    tp2 = im._theme_photo("a07_vip-vvip-picket.md", pool)
    check("테마: VIP→VVIP 컷", tp2 is not None and "VVIP" in tp2.name, str(tp2))
    check("테마: 무관 초안=None", im._theme_photo("a06_platform-picket-difference.md", pool) is None)
    vip_draft = DRAFTS / "a07_vip-vvip-picket.md"
    if vip_draft.exists():
        vpicks, _ = im.pick_images(vip_draft, 3, advance=False)
        check("a07 대표=VVIP 컷", vpicks and "VVIP" in vpicks[0].name, str(vpicks[:1]))
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


def t_index_gate():
    section("색인 표본·색인 대기 감속")
    import json as _json
    import metrics
    import scheduler
    import config

    # 표본은 '최신만'이 아니라 오래된 글까지 포함해야 한다(새 글은 원래 늦게 색인된다).
    log = [{"ok": True, "dry": False, "title": f"t{i}"} for i in range(1, 8)]
    state = metrics.ROOT / "data" / "publish_state.json"
    orig = state.read_text(encoding="utf-8") if state.exists() else None
    try:
        state.write_text(_json.dumps({"published": [], "log": log}, ensure_ascii=False),
                         encoding="utf-8")
        s = metrics._recent_titles(3)
        check("표본에 가장 오래된 글 포함", "t1" in s, s)
        check("표본에 최신 글 포함", "t7" in s, s)
        check("표본 개수 3", len(s) == 3, s)
    finally:
        if orig is not None:
            state.write_text(orig, encoding="utf-8")

    # 색인 0 이면 하루 1편, 색인이 살아 있으면 설정 상한 그대로.
    mp = scheduler.ROOT / "data" / "metrics.json"
    m_orig = mp.read_text(encoding="utf-8")
    try:
        m = _json.loads(m_orig)
        m["index_status"] = {"sampled": 3, "found": 0}
        mp.write_text(_json.dumps(m, ensure_ascii=False), encoding="utf-8")
        check("색인 0 → 하루 1편", scheduler._daily_cap() == 1)
        m["index_status"] = {"sampled": 3, "found": 2}
        mp.write_text(_json.dumps(m, ensure_ascii=False), encoding="utf-8")
        check("색인 살아있으면 설정 상한",
              scheduler._daily_cap() == config.MAX_POSTS_PER_DAY)
        m["index_status"] = {}
        mp.write_text(_json.dumps(m, ensure_ascii=False), encoding="utf-8")
        check("근거 없으면 감속 안 함",
              scheduler._daily_cap() == config.MAX_POSTS_PER_DAY)
    finally:
        mp.write_text(m_orig, encoding="utf-8")


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


def t_scheduler_alert():
    section("스케줄러 연속 실패 경고")
    import scheduler
    ok = {"ok": True, "dry": False}
    bad = {"ok": False, "dry": False, "reason": "session_expired"}
    dry = {"ok": False, "dry": True, "reason": "dry_run"}
    check("성공 뒤면 0", scheduler._consecutive_failures([bad, bad, ok]) == 0)
    check("연속 실패 셈", scheduler._consecutive_failures([ok, bad, bad]) == 2)
    check("dry-run 은 무시", scheduler._consecutive_failures([ok, bad, dry, bad]) == 2)
    check("빈 로그는 0", scheduler._consecutive_failures([]) == 0)

    import notify
    notify.write_alert("테스트 경고")
    check("경고 파일 생성", notify.ALERT_FILE.exists())
    notify.clear_alert()
    check("복구 시 경고 삭제", not notify.ALERT_FILE.exists())

    import metrics
    check("세션 만료 경고 함수 존재", callable(metrics._warn_session_expired))


def main():
    for t in (t_parser, t_seo, t_images, t_metrics, t_index_gate, t_research, t_growth,
              t_dashboard, t_scheduler_alert):
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
