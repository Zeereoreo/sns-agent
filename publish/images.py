"""이미지 선택 로직.

우선순위: 인박스(새 사진) > 주제 매칭 인포그래픽 > 사진 풀(순환 재활용).
사진 풀이 비어 있어도 인포그래픽만으로 게시 가능.

폴더 구조:
  drafts/images/           내가 만든 인포그래픽(PNG)
  drafts/photos/           실물 사진 풀(사용자가 채움) — 자동 재활용
  drafts/photos/inbox/     새 사진(우선 사용, 사용 후 used로 이동)
  drafts/photos/used/      사용 완료 보관
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMG_DIR = ROOT / "drafts" / "images"
PHOTO_DIR = ROOT / "drafts" / "photos"
INBOX_DIR = PHOTO_DIR / "inbox"
USED_DIR = PHOTO_DIR / "used"
DATA_DIR = ROOT / "data"
ROT_FILE = DATA_DIR / "photo_rotation.json"
HARVEST_MANIFEST = PHOTO_DIR / "_harvest" / "manifest.json"

# 🔴 사진 풀 519장 중 493장(95%)이 **원본 블로그 made-us 에서 그대로 수확한 사진**이다
# (harvest_photos.py). 원본은 정상 색인·구글 상위인데 우리(made-us2)는 검색에서 통째로
# 빠져 있다. 즉 네이버가 보기에 우리는 '남의 블로그 이미지를 대량 재업로드하는 신생
# 블로그'다. 유사문서를 텍스트로만 재고(평균 8.5%, "중복 아님") **이미지는 한 번도 안 쟀다.**
#
# 실측(발행 로그의 신규 발행분 사진 투입량):
#   7/16~24  하루 4~10장 (글당 1.3~3.3장)  → 이 구간엔 검색 유입이 매일 있었다
#   7/27     12장 · 7/28 17장 (글당 6~8.5장) → **유입이 이 경계에서 0으로 붕괴**
#   8/01~03  하루 63~69장 (글당 23장)      → 회복을 기다리며 원인 후보를 3배로 키우고 있었다
# 상한 3 = 유입이 살아 있던 구간의 글당 실측 상한. '안전이 확인된 마지막 지점'이다.
# 인과는 확정이 아니라 정황(시점 일치 + 95% 복제)이라, 되돌릴 수 있게 상수 하나로 뒀다.
ORIGIN_MAX_PER_POST = 3

# 인박스(사용자가 직접 넣은 새 사진)는 이 상한에 걸리지 않는다 — 우리 사진이면
# 20장 규격을 그대로 채워도 복제 신호가 아니다. 회복 경로는 '새 사진'이다.
_origin_sizes: set[int] | None = None


def _origin_size_set() -> set[int]:
    """원본 블로그에서 수확한 사진의 바이트 크기 집합(풀 파일은 이름이 바뀌어 크기로 대조)."""
    global _origin_sizes
    if _origin_sizes is None:
        sizes: set[int] = set()
        try:
            names = json.loads(HARVEST_MANIFEST.read_text(encoding="utf-8"))
            for name in names:
                f = HARVEST_MANIFEST.parent / name
                if f.exists():
                    sizes.add(f.stat().st_size)
        except Exception:
            pass
        _origin_sizes = sizes
    return _origin_sizes


def is_origin_photo(p: Path) -> bool:
    """이 사진이 원본 블로그(made-us)에서 수확한 것인가."""
    try:
        return Path(p).stat().st_size in _origin_size_set()
    except OSError:
        return False

# 초안 코드 -> 대표 인포그래픽 파일명 (주제가 명확히 맞는 것만; 나머지는 세그먼트 기본)
INFOGRAPHIC_MAP = {
    "a02": "price-factors.png",
    "a03": "nickname-checklist.png",
    "a06": "platform-compare.png",
    "a08": "nickname-checklist.png",   # 크루 닉네임 → 닉네임 체크리스트
    "a11": "platform-compare.png",     # 쇼츠/틱톡 → 플랫폼 비교
    "c30": "process-flow.png",         # 간판 제작 과정 → 공정 플로우
    # 신규 제작 인포그래픽(2026-07-21)
    "c23": "sign-cost.png",            # 아크릴 간판 비용
    "c29": "sign-cost.png",            # 소상공인 간판 비용 절약
    "b24": "bucket-care.png",          # 아이스버킷 관리
    "b23": "club-led-set.png",         # 테이블 LED 조합
}
# 세그먼트 기본 인포그래픽(주제 매칭 없을 때)
SEGMENT_DEFAULT = {"a": "process-flow.png", "b": "bucket-compare.png", "c": "sign-compare.png"}

# 특정 소재 사진은 해당 주제 글에만 붙인다 — 무관 글에 배터리컷이 붙어
# 캡션과 사진이 어긋나는 사고 방지(2026-07-24 a17 발행에서 실제 발생).
SPECIAL_PHOTO = {"배터리": ("battery", "배터리")}   # 사진명 키워드 → 허용 초안명 키워드


def _photo_allowed(photo_name: str, draft_name: str) -> bool:
    for kw, allow in SPECIAL_PHOTO.items():
        if kw in photo_name and not any(a in draft_name for a in allow):
            return False
    return True


# a(BJ/스트리머) 글: 초안 파일명 토큰 → 주제에 맞는 사진명 키워드.
# 대표(첫 사진)가 글 주제와 맞는 네온 실물컷이 되게 한다(2026-07-24 사용자 지시:
# "주제에 맞게 섬네일" — 주 고객 = 비제이/스트리머).
A_PHOTO_THEME = [
    (("cheer",), ("하트", "큰손등장")),
    (("nickname", "crew"), ("곰돌이", "킹날개")),
    (("vip",), ("VVIP",)),                       # vip/vvip 둘 다 부분일치
    (("reaction", "bigfan"), ("큰손",)),
    (("bulk", "event"), ("드럼세트", "로얄")),
    (("signature", "price"), ("시그니처",)),
    (("battery",), ("배터리",)),
    (("design",), ("미키하트",)),
]


def _theme_photo(draft_name: str, pool: list[Path]) -> Path | None:
    """초안 주제에 맞는 사진 1장. 매칭 없으면 None(기존 순환 유지)."""
    name = draft_name.lower()
    for draft_kws, photo_kws in A_PHOTO_THEME:
        if any(k in name for k in draft_kws):
            for pk in photo_kws:
                for p in pool:
                    if pk in p.name:
                        return p
    return None


GROUP_MAX = 8      # 한 묶음 최대 장수


def _group_pool(pool: list[Path]) -> list[list[Path]]:
    """사진을 제품 묶음으로 나눈다. 그룹 표시가 없는 사진은 각자 1장짜리 묶음.

    수확 순번의 연속 구간(_gNN_)은 피켓처럼 한 제품을 여러 번 찍은 경우엔 그대로 '한 제품'이지만,
    간판은 원본 글이 여러 매장 사례를 연달아 올려서 한 구간에 75장·26장씩 들어간다
    (= 서로 다른 간판이 뒤섞임). 그래서 큰 묶음은 GROUP_MAX 장씩 잘라 인접한 것끼리만 쓰게 한다.
    """
    products: dict[str, list[Path]] = {}   # _pNN_ = 사람이 확인한 '같은 제품'
    seqs: dict[str, list[Path]] = {}       # _gNN_ = 수확 순번 연속(제품 단위 아님)
    singles: list[list[Path]] = []         # _x_ = 1~3장짜리 단품
    for p in sorted(pool, key=lambda x: x.name):
        seg_slug = p.name.split("_")[1]
        mp = re.search(r"_(p\d+)_", p.name)
        if mp:
            products.setdefault(f"{seg_slug}_{mp.group(1)}", []).append(p)
            continue
        if "_x_" in p.name:
            singles.append([p])
            continue
        mg = re.search(r"_(g\d+)_", p.name)
        seqs.setdefault(f"{seg_slug}_{mg.group(1)}" if mg else p.name, []).append(p)

    out: list[list[Path]] = [products[k] for k in sorted(products)]
    for k in sorted(seqs):                 # 아직 제품 라벨이 없는 세그먼트는 길이로 잘라 쓴다
        g = seqs[k]
        for i in range(0, len(g), GROUP_MAX):
            out.append(g[i:i + GROUP_MAX])
    return out + singles


_CAPTION_FORMS = ("{} 제작 사례", "{} 실물 컷", "{} 설치 예시", "{} 디테일 컷")


def photo_caption(path, idx: int = 0) -> str:
    """사진 파일명에서 캡션을 만든다: a_LED피켓_012.jpg → 'LED 피켓 제작 사례'.

    초안 슬롯에 캡션을 고정해두면 순환으로 뽑힌 실제 사진과 어긋난다
    (배터리 컷에 '피켓' 캡션이 붙던 사고). 캡션을 사진에서 파생하면 항상 맞는다.
    """
    stem = Path(path).stem
    parts = stem.split("_")
    if len(parts) < 2:
        return ""
    slug = parts[1]
    label = {
        "LED피켓": "LED 시그니처 피켓",
        "LED버킷": "LED 아이스버킷",
        "LED아크릴사인": "LED 아크릴 사인",
        "골드나무간판": "골드·우드 간판",
        "배터리셀": "피켓용 배터리",
    }.get(slug)
    if not label:
        return ""
    # 같은 캡션이 한 글에 여러 번 반복되지 않게 표현을 돌려쓴다.
    return _CAPTION_FORMS[idx % len(_CAPTION_FORMS)].format(label)


def _imgs(d: Path) -> list[Path]:
    out: list[Path] = []
    if d.exists():
        for ext in ("*.png", "*.jpg", "*.jpeg"):
            out += sorted(d.glob(ext))
    return out


def draft_code(draft_path) -> str:
    m = re.match(r"([abc]?\d+)", Path(draft_path).stem)
    return m.group(1) if m else Path(draft_path).stem[:3]


def _infographic_for(code: str) -> Path | None:
    fn = INFOGRAPHIC_MAP.get(code)
    if not fn:
        seg = code[0] if code and code[0] in "abc" else "a"
        fn = SEGMENT_DEFAULT.get(seg, "process-flow.png")
    p = IMG_DIR / fn
    if p.exists():
        return p
    # 폴백: 아무 인포그래픽
    any_info = _imgs(IMG_DIR)
    return any_info[0] if any_info else None


def _load_rot() -> int:
    try:
        return int(json.loads(ROT_FILE.read_text()).get("i", 0))
    except Exception:
        return 0


def _save_rot(i: int) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    ROT_FILE.write_text(json.dumps({"i": i}))


def pick_images(draft_path, n: int, advance: bool = True) -> tuple[list[Path], list[Path]]:
    """(삽입할 이미지 경로들, 소진한 인박스 사진들) 반환. n = 초안의 이미지 자리 수.
    advance=False 면 순환 인덱스를 저장하지 않는다(대시보드 미리보기용, 부작용 없음)."""
    n = max(int(n), 1)
    code = draft_code(draft_path)
    picks: list[Path] = []
    used_inbox: list[Path] = []

    # 1) 주제 매칭 인포그래픽(대표) 1장
    info = _infographic_for(code)
    if info:
        picks.append(info)

    # 2) 인박스 새 사진 우선 소진
    for p in _imgs(INBOX_DIR):
        if len(picks) >= n:
            break
        picks.append(p)
        used_inbox.append(p)

    # 3) 사진 풀 순환 재활용 — 같은 세그먼트(파일명 a_/b_/c_ 접두사) 사진을 우선한다.
    #    간판 글에 클럽 버킷 사진이 붙는 것을 막는다.
    draft_name = Path(draft_path).name
    pool = [p for p in _imgs(PHOTO_DIR)
            if p.parent == PHOTO_DIR and _photo_allowed(p.name, draft_name)]
    seg = code[0] if code and code[0] in "abc" else "a"
    same_seg = [p for p in pool if p.name.startswith(f"{seg}_")]
    pool = same_seg or pool
    if pool and len(picks) < n:
        # a 글은 주제 맞는 사진을 먼저(아래 스왑으로 대표가 됨), 나머지는 순환
        if seg == "a":
            theme = _theme_photo(draft_name, pool)
            if theme:
                picks.append(theme)
                pool = [p for p in pool if p != theme]
        # 한 글에는 '같은 제품' 사진이 들어가야 한다(2026-07-27 사용자 지시).
        # 파일명의 _gNN_ 은 원본 촬영 시퀀스의 연속 구간 = 같은 제품 묶음이다.
        need = n - len(picks)
        groups = _group_pool(pool)
        # 한 그룹으로 필요한 장수를 채울 수 있으면 그 그룹만 쓴다(한 글 = 한 제품).
        # 채울 만한 그룹이 없을 때만 여러 그룹을 이어 붙인다.
        enough = [g for g in groups if len(g) >= need]
        start = _load_rot()
        if enough:
            groups, gi = enough, start
        else:
            # 한 묶음으로 다 못 채운다(슬롯 20장 규격에서는 흔하다 — c 는 최대 묶음이 8장).
            # 이때 **큰 묶음부터** 쓴다. 앞쪽(본문) 슬롯이 한 제품으로 채워지고 남는 뒤쪽
            # (= 초안 끝 '실제 제작 사례' 갤러리)만 다른 제품이 된다(2026-07-31 사용자 결정).
            # 정렬하지 않으면 1장짜리 단품이 본문 첫 슬롯에 와서 본문부터 제품이 뒤섞인다.
            groups = sorted(groups, key=len, reverse=True)
            big = [g for g in groups if len(g) >= GROUP_MAX] or groups[:1]
            gi = start % len(big)          # 글마다 다른 제품이 대표가 되도록 큰 묶음 안에서만 회전
        used_groups = 0
        while len(picks) < n and used_groups < len(groups):
            for p in groups[gi % len(groups)]:
                if len(picks) >= n:
                    break
                picks.append(p)
            gi += 1
            used_groups += 1
        if advance:
            _save_rot(gi)

    # 썸네일 다양화: 세그먼트마다 같은 인포그래픽이 대표(첫 장)로 반복되는 것을 막는다.
    # 초안별 결정론적으로 '사진 우선'인 글은 실물 사진을 첫 장으로 올리고 인포그래픽은
    # 둘째 장으로 내린다(인포그래픽은 본문에 유지). 슬롯 2개 이상 + 첫 장이 인포그래픽 +
    # 둘째가 실물 사진일 때만.
    # 대표(첫 장)는 **항상 실물 사진**. 인포그래픽(흰 배경 도해)이 대표로 뜨면 블로그 목록
    # 썸네일이 표 이미지가 된다(2026-07-24 "썸네일이 왜 기존 블로그처럼 안 나오나" 지적).
    if n >= 2 and len(picks) >= 2 and picks[0].parent == IMG_DIR:
        real = next((i for i, p in enumerate(picks) if p.parent != IMG_DIR), None)
        if real is not None:
            picks[0], picks[real] = picks[real], picks[0]

    # 인포그래픽(흰 배경 도해)이 실물 사진들 사이에 끼면 톤이 튄다 → 맨 뒤로 보낸다.
    # 대표(첫 장)로 남아야 하는 경우(위 스왑이 없었던 글)는 그대로 둔다.
    picks = picks[:n]

    # 원본 블로그 출신 사진은 글당 ORIGIN_MAX_PER_POST 장까지(상단 주석의 실측 근거).
    # 인포그래픽(우리가 만든 것)과 인박스 새 사진은 제한하지 않는다 — 복제가 아니므로.
    # 남는 슬롯은 발행 쪽에서 그냥 건너뛴다(naver.py 의 `img_i < len(images)`).
    seen_origin = 0
    kept: list[Path] = []
    for p in picks:
        if p.parent == PHOTO_DIR and is_origin_photo(p):
            seen_origin += 1
            if seen_origin > ORIGIN_MAX_PER_POST:
                continue
        kept.append(p)
    picks = kept

    if len(picks) > 2 and picks[0].parent != IMG_DIR:
        info = [p for p in picks if p.parent == IMG_DIR]
        if info:
            picks = [p for p in picks if p.parent != IMG_DIR] + info

    return picks, used_inbox


def mark_inbox_used(paths: list[Path]) -> None:
    if not paths:
        return
    USED_DIR.mkdir(parents=True, exist_ok=True)
    for p in paths:
        try:
            Path(p).rename(USED_DIR / Path(p).name)
        except Exception:
            pass
