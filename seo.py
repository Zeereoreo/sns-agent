"""발행 전 SEO 품질 게이트 + 스코어러.

목적: 글을 쓸(발행할) 때마다 더 나은 방향으로 가도록, 발행 직전에 각 글을
네이버 검색 최적화 기준으로 점검·채점하고 고칠 수 있는 건 자동으로 고친다.
성과 데이터(metrics.json 순위)가 쌓이면 가중치를 그쪽으로 조정한다.

체크 항목(네이버 블로그 DIA/C-Rank 관점):
  - 제목에 대표 키워드 포함 / 제목 길이(모바일 잘림 방지)
  - 첫 문단에 대표 키워드(도입부 가중치)
  - 본문 분량(정보성 문서 선호)
  - 소제목(##) 수 / FAQ(Q&A) 유무
  - 이미지 슬롯 수 / 모든 이미지에 캡션(ALT)
  - 태그 개수 + 대표 키워드 토큰 포함
  - 기존 발행글과 제목 과중복 방지(자기잠식)

사용:
  python seo.py check            # 전체 초안 점수(낮은 순)
  python seo.py check <파일>     # 한 편 상세
  python seo.py fix              # 자동 수정 가능한 항목만 반영(태그 등)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from publish.draft_parser import parse_draft  # noqa: E402

DRAFTS = ROOT / "drafts"
STATE = ROOT / "data" / "publish_state.json"

# 각 항목 배점(합 100). 데이터 쌓이면 이 가중치를 순위 상관도로 조정.
WEIGHTS = {
    "title_kw": 16,      # 제목에 대표 키워드
    "title_len": 8,      # 제목 길이 적정
    "intro_kw": 14,      # 첫 문단에 대표 키워드
    "body_len": 10,      # 본문 분량
    "headings": 8,       # 소제목 수
    "faq": 10,           # FAQ 섹션
    "conversion": 6,     # 구매 판단을 돕는 구체성(수치·기준·실답변)
    "images": 10,        # 이미지 슬롯
    "captions": 8,       # 이미지 캡션(ALT)
    "tags": 6,           # 태그 개수
    "tag_kw": 4,         # 태그에 키워드 토큰
}
# 제목 정책(2026-07-29 전면 수정 — 근거: 원본 made-us 와 경쟁사 실측)
#   원본 made-us(누적 10만 방문, 구글 '개인방송 피켓' 1위)의 제목은 **70~107자(중앙 98)**
#   키워드 나열형이다. 구글 1위인 그 글 제목이 정확히 104자 나열형이었다.
#   경쟁사 linosgj 도 같은 형식. **우리만 19~26자로 짧게 쓰고, 우리만 노출이 0이다.**
#   기존 상한 42자는 '구글이 30자에서 자른다'는 이유로 우리가 건 제약이었는데,
#   잘려 보이는 것과 색인되는 것은 다르다. 절단 대비는 **앞 30자에 핵심**으로 해결한다.
TITLE_HEAD = 30      # 이 앞부분에 대표 키워드가 있어야 한다(구글 SERP 절단 구간)
TITLE_MAX = 100      # 네이버 제목 상한
TITLE_GOOD = 45      # 이보다 짧으면 키워드 커버리지가 아깝다(원본 중앙값 98)
TAG_MAX = 20         # 경쟁 상위 글 실측 상한(enrich_posts.TAG_MAX 와 같은 근거)
# 분량·이미지 규격(2026-07-31 수정 — 근거: 원본 made-us 90편 실측, 라운드 10)
#   색인이 정상이고 구글 1위인 원본은 **본문 3,143~3,933자 · 이미지 20~26장**이다.
#   우리 초안 중앙값은 1,533자 · 8장 — 원본의 43% / 40% 수준인데도 옛 기준
#   (BODY_GOOD 1500 · 이미지 3장 만점)에서는 전부 만점이 나왔다. 게이트가 얇은 글을
#   'A100' 이라고 말해 온 것이다(라운드 9·10 의 '게이트 자기교정'과 같은 종류의 결함).
#   **합격선(BODY_MIN·이미지 3장)은 올리지 않는다.** 기존 초안을 무더기 미달로 만들면
#   글자수를 채우려 지어내는 압력이 된다(CONV_SPEC_GOOD 6→4 교훈). 만점 기준만 실측에
#   맞춰 점수가 정직해지게 한다. 점수를 올리는 정직한 방법은 사례·실물 사진을 더 넣는 것.
BODY_MIN = 1000
BODY_GOOD = 3000     # 원본 실측 범위(3,143~3,933)의 하한
IMG_GOOD = 20        # 원본 실측 범위(20~26장)의 하한. 사진 풀 519장(a335·b28·c156)으로 가능


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def primary_keyword(text: str) -> str:
    m = re.search(r"타깃\s*검색키워드[^:]*:\s*(.+)", text)
    if not m:
        return ""
    return re.split(r"[,/·\n]", m.group(1).strip())[0].strip()


def _body_text(text: str) -> str:
    body = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    lines = []
    for ln in body.split("\n"):
        s = ln.strip()
        if not s or s.startswith("#") or s.startswith("[이미지") or s.startswith("👉"):
            continue
        lines.append(s)
    return " ".join(lines)


# 구매 판단을 돕는 구체 정보(치수·기간·가격대·수량)를 담은 문장 수 목표.
# 6 으로 뒀더니 실제 수치를 모르는 주제(간판 등)에서 **지어내라는 압력**이 됐다.
# 정직(가짜 데이터 금지)이 이 프로젝트의 상위 규칙이므로 4 로 낮춘다.
# 대신 회피 답변('알려주시면 안내') 감점은 그대로 둔다 — 그건 아는 걸 안 쓰는 것이라 별개다.
CONV_SPEC_GOOD = 4
# 답을 안 하고 문의로 떠넘기는 문장 — FAQ 를 이걸로만 채우면 읽는 사람이 못 고른다
_DODGE = re.compile(r"(알려주시면|문의\s*주시면|주시면)\s*[^.]{0,20}(안내|상담|확인)")


def _conversion_signals(text: str, body: str) -> tuple[int, int]:
    """(구체 수치 문장 수, 회피성 답변 수). 구매 결정을 돕는 정보가 실제로 있는지 본다."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n", body) if s.strip()]
    spec = sum(1 for s in sentences
               # '층'·'m'(미터)은 간판 글에서 가장 흔한 구체 정보인데 빠져 있었다.
               # 단위가 빠지면 실제로는 구체적인 글이 미달로 나와, 없는 수치를 더 넣게 만든다.
               if re.search(r"\d+\s*(cm|mm|m(?![a-zA-Z])|호|층|일|주|개월|년|만원|원|시간|분"
                            r"|W|V|%|장|개|kg|가지|자|도|단계|중|배|회|종|명|평|위)", s))
    dodge = len(_DODGE.findall(text))
    return spec, dodge


def score_draft(path: Path) -> dict:
    text = _read(path)
    d = parse_draft(path)
    kw = primary_keyword(text)
    kw_tokens = [w for w in re.split(r"\s+", kw) if len(w) > 1]
    title = d["title"]
    body = _body_text(text)
    first_para = next((ln.strip() for ln in re.sub(r"<!--.*?-->", "", text, flags=re.S).split("\n")
                       if ln.strip() and not ln.strip().startswith(("#", "[", "👉"))), "")

    n_img = sum(1 for b in d["blocks"] if b["kind"] == "image")
    n_cap = sum(1 for b in d["blocks"] if b["kind"] == "image" and (b.get("alt") or "").strip())
    n_head = sum(1 for b in d["blocks"] if b["kind"] == "heading")
    has_faq = bool(re.search(r"Q\.|자주 묻는|자주묻는", text))
    body_len = len(body.replace(" ", ""))

    checks = []

    def add(name, ok, detail, fixable=False, partial=None):
        pts = WEIGHTS[name] * (partial if partial is not None else (1 if ok else 0))
        checks.append({"name": name, "ok": ok, "detail": detail,
                       "pts": round(pts, 1), "max": WEIGHTS[name], "fixable": fixable})

    add("title_kw", bool(kw and (kw in title or sum(1 for t in kw_tokens if t in title) >= max(1, len(kw_tokens) - 1))),
        f"제목에 '{kw}' {'포함' if kw and kw in title else '부분/누락'}")
    # 제목은 **앞 30자에 대표 키워드**(절단 대비) + **전체 길이로 키워드 커버리지**.
    # 짧은 제목은 감점한다 — 이 판의 승자(원본 made-us·경쟁사 linosgj)는 전부 나열형 장문이다.
    head = title[:TITLE_HEAD]
    head_ok = bool(kw and (kw in head
                           or sum(1 for t in kw_tokens if t in head) >= max(1, len(kw_tokens) - 1)))
    if len(title) > TITLE_MAX:
        cover = 0.0                                    # 네이버 상한 초과 = 잘림
    elif len(title) >= TITLE_GOOD:
        cover = 1.0
    else:
        cover = 0.4 + 0.6 * (len(title) / TITLE_GOOD)  # 짧을수록 커버리지 손해
    add("title_len", head_ok and len(title) >= TITLE_GOOD,
        f"제목 {len(title)}자 (권장 {TITLE_GOOD}~{TITLE_MAX}) · 앞{TITLE_HEAD}자 키워드 "
        + ("있음" if head_ok else "없음"),
        partial=cover * (1.0 if head_ok else 0.5))
    add("intro_kw", bool(kw and (kw in first_para or sum(1 for t in kw_tokens if t in first_para) >= max(1, len(kw_tokens) - 1))),
        f"첫 문단 키워드 {'있음' if kw and kw in first_para else '부분/누락'}")
    add("body_len", body_len >= BODY_MIN, f"본문 {body_len}자",
        partial=min(1.0, body_len / BODY_GOOD))
    add("headings", n_head >= 3, f"소제목 {n_head}개", partial=min(1.0, n_head / 4))
    add("faq", has_faq, "FAQ 섹션 " + ("있음" if has_faq else "없음"))
    n_spec, n_dodge = _conversion_signals(text, body)
    conv = min(1.0, n_spec / CONV_SPEC_GOOD) * (0.5 if n_dodge else 1.0)
    add("conversion", n_spec >= CONV_SPEC_GOOD and not n_dodge,
        f"구체 수치 {n_spec}개" + (f" / 회피답변 {n_dodge}개" if n_dodge else ""),
        partial=conv)
    add("images", n_img >= 3, f"이미지 슬롯 {n_img}개 (원본 규격 {IMG_GOOD}장)",
        partial=min(1.0, n_img / IMG_GOOD))
    # 캡션은 첫 슬롯(대표=인포그래픽)만 초안 ALT 로 쓴다. 나머지 슬롯은 발행 시
    # 실제 삽입된 사진 파일명에서 만들어지므로(images.photo_caption) 여기서 미달로 보지 않는다.
    n_capable = min(n_img, n_cap + max(0, n_img - 1))
    add("captions", n_img > 0 and n_capable == n_img, f"캡션 {n_capable}/{n_img}",
        partial=(n_capable / n_img if n_img else 0))
    # 상한 10 은 어뷰징 방지용으로 우리가 임의로 건 것이었는데, 실측해 보니 네이버는
    # 그보다 많이 허용하고 같은 판의 상위 경쟁 글은 12~20개를 쓴다(2026-07-29).
    # 10 으로 막는 건 검색 노출면을 스스로 절반 버리는 것이라 8~20 으로 넓힌다.
    add("tags", 8 <= len(d["tags"]) <= TAG_MAX, f"태그 {len(d['tags'])}개", fixable=True)
    add("tag_kw", bool(kw_tokens and any(any(t in tag for t in kw_tokens) for tag in d["tags"])),
        "태그에 키워드 토큰 " + ("있음" if d["tags"] else "없음"), fixable=True)

    score = round(sum(c["pts"] for c in checks), 1)
    grade = "A" if score >= 85 else "B" if score >= 70 else "C" if score >= 55 else "D"
    return {"file": path.name, "title": title, "keyword": kw,
            "score": score, "grade": grade, "checks": checks,
            "body_len": body_len, "n_img": n_img,
            "competitor": _competitor_intel(kw, body + " " + title, body_len)}


def _competitor_intel(kw: str, our_text: str, our_len: int) -> dict | None:
    """data/research/<kw>.json 이 있으면 경쟁 대비 자문(점수엔 반영 안 함)."""
    slug = re.sub(r"[^가-힣a-zA-Z0-9]+", "_", kw)[:40]
    f = ROOT / "data" / "research" / f"{slug}.json"
    if not f.exists():
        return None
    try:
        r = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None
    # research 가 뽑은 보강후보 중, 지금 우리 글에 아직 없는 것만
    missing = [w for w in r.get("gap_terms", []) if w not in our_text]
    bench = r.get("length_benchmark")
    return {
        "length_benchmark": bench,
        "length_gap": (bench - our_len) if bench else None,   # +면 경쟁이 더 김
        "missing_terms": missing[:8],
        "competitors": len(r.get("competitors", [])),
    }


def _ordered():
    a = sorted(DRAFTS.glob("sample*.md")) + sorted(DRAFTS.glob("a*.md"))
    b = sorted(DRAFTS.glob("b*.md"))
    c = sorted(DRAFTS.glob("c*.md"))
    return a + b + c


def check_all() -> None:
    rows = [score_draft(p) for p in _ordered()]
    rows.sort(key=lambda r: r["score"])
    print(f"{'점수':>5} {'등급':>3}  {'파일':36} 약점")
    for r in rows:
        weak = ", ".join(c["name"] for c in r["checks"] if c["pts"] < c["max"] * 0.5)
        print(f"{r['score']:>5} {r['grade']:>3}  {r['file']:36} {weak}")
    avg = round(sum(r["score"] for r in rows) / len(rows), 1)
    print(f"\n평균 {avg}점 / {len(rows)}편")


def check_one(name: str) -> None:
    p = DRAFTS / name if (DRAFTS / name).exists() else Path(name)
    r = score_draft(p)
    print(f"[{r['grade']}] {r['score']}점 — {r['file']}")
    print(f"  제목: {r['title']}  (키워드: {r['keyword']})")
    for c in r["checks"]:
        mark = "OK" if c["pts"] >= c["max"] * 0.99 else ("~ " if c["pts"] > 0 else "X ")
        print(f"  [{mark}] {c['name']:10} {c['pts']:>4}/{c['max']:<3} {c['detail']}")
    ci = r.get("competitor")
    if ci:
        print(f"  [경쟁] 상위 {ci['competitors']}편 / 길이 중앙값 {ci['length_benchmark']}자"
              + (f" (우리가 {abs(ci['length_gap'])}자 {'짧음' if ci['length_gap'] > 0 else '김'})"
                 if ci.get("length_gap") else ""))
        if ci["missing_terms"]:
            print(f"         보강 후보: {', '.join(ci['missing_terms'])}")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    cp = sub.add_parser("check")
    cp.add_argument("file", nargs="?")
    sub.add_parser("fix")
    a = ap.parse_args()
    if a.cmd == "check":
        check_one(a.file) if a.file else check_all()
    else:
        print("fix: 아직 미구현(다음 단계)")


if __name__ == "__main__":
    main()
