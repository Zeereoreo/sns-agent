"""초안 끝에 '실제 제작 사례' 갤러리 섹션을 붙여 이미지 슬롯을 원본 규격까지 채운다.

왜 필요한가(2026-07-31 라운드 15):
  색인이 정상이고 구글 1위인 원본 made-us 는 글당 이미지 **20~26장**인데 우리는 **8장**이다.
  그런데 20장을 '한 제품' 사진만으로는 채울 수 없다 — 제품 묶음 최대가 a 19장·c 8장·b 6장이라
  20장 이상인 단일 묶음이 하나도 없다(실측).
  본문에 다른 제품을 섞는 건 금지다(2026-07-27 사용자 지시: 한 글에는 같은 제품 사진.
  a17 발행에서 배터리컷에 피켓 캡션이 붙는 사고가 실제로 났다).

  그래서 **본문은 한 제품 그대로 두고 글 끝에 사례 갤러리를 따로 둔다**(2026-07-31 사용자 결정).
  갤러리에 붙는 사진은 pick_images 가 다음 묶음에서 이어 뽑고, 캡션은 images.photo_caption 이
  파일명에서 만들므로 사진과 어긋나지 않는다.

사용:
  python tools/add_gallery.py                      # 미리보기(변경 없음)
  python tools/add_gallery.py --apply              # 적용
  python tools/add_gallery.py --apply --only a21   # 특정 초안만
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import seo  # noqa: E402

DRAFTS = ROOT / "drafts"
MARKER = "[이미지]"
HEAD = "## 실제 제작 사례"
INTRO = "아래는 실제로 제작해 납품한 결과물입니다. 크기·색·설치 방식이 어떻게 달라지는지 함께 보세요."

# b(버킷)는 사진 풀이 28장뿐이라 한 글에 20장을 넣으면 풀을 통째로 소진해
# 모든 b 글이 같은 사진으로 채워진다. 풀이 넉넉한 세그먼트에만 적용한다.
SKIP_SEGMENTS = ("b",)


def _insert_at(text: str) -> int:
    """갤러리를 넣을 위치(문자 인덱스). 마무리 > 문의줄 > 태그줄 > 맨끝 순."""
    for anchor in ("## 마무리", "👉"):
        i = text.find(anchor)
        if i >= 0:
            return i
    for ln in text.splitlines():
        if ln.startswith("#") and not ln.startswith("##") and " " not in ln.strip():
            return text.find(ln)          # 태그 줄
    return len(text)


def add_gallery(path: Path, target: int | None = None, apply: bool = False) -> int:
    """부족한 슬롯 수만큼 갤러리를 넣고 '늘어난 슬롯 수'를 반환. 이미 있거나 충분하면 0."""
    target = target or seo.IMG_GOOD
    text = path.read_text(encoding="utf-8")
    if HEAD in text:
        return 0
    need = target - text.count(MARKER)
    if need <= 0:
        return 0
    block = f"\n{HEAD}\n{INTRO}\n\n" + "\n\n".join([MARKER] * need) + "\n\n"
    i = _insert_at(text)
    if apply:
        path.write_text(text[:i] + block + text[i:], encoding="utf-8")
    return need


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="실제로 파일을 고친다(기본은 미리보기)")
    ap.add_argument("--only", default="", help="초안 이름 접두사(예: a21)")
    ap.add_argument("--target", type=int, default=None,
                    help=f"목표 이미지 수(기본 seo.IMG_GOOD={seo.IMG_GOOD})")
    a = ap.parse_args()

    total = done = 0
    for p in sorted(DRAFTS.glob("*.md")):
        if a.only and not p.name.startswith(a.only):
            continue
        # 실제 발행 대상만 손댄다(scheduler._ordered 와 같은 규칙). drafts/ 에는
        # keyword-map-and-30-titles.md 같은 메모 파일도 있는데 발행되지 않는다.
        if not p.name.startswith(("sample", "a", "b", "c")):
            continue
        if p.name[0] in SKIP_SEGMENTS:
            continue
        before = p.read_text(encoding="utf-8").count(MARKER)
        n = add_gallery(p, target=a.target, apply=a.apply)
        if n:
            done += 1
            total += n
            print(f"  {p.stem[:44]:<44} 이미지 {before} → {before + n} (+{n})")
    verb = "적용" if a.apply else "미리보기"
    print(f"[{verb}] 초안 {done}편 · 슬롯 +{total}")
    if not a.apply and done:
        print("실제로 넣으려면 --apply 를 붙이세요.")


if __name__ == "__main__":
    main()
