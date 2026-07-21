"""MADE-US 브랜드 인포그래픽 렌더 도구.

HTML(브랜드 스타일)을 venv playwright로 .card 요소만 device_scale_factor=2로
스크린샷 → drafts/images/*.png. 새 인포그래픽이 필요하면 SPECS 에 추가하고 실행.

사용:  python tools/make_infographics.py
(기존 파일은 덮어씀. drafts/images/ 는 gitignore 라 PNG 는 로컬 자산.)
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
from playwright.sync_api import sync_playwright

IMG = ROOT / "drafts" / "images"

CSS = """
*{margin:0;padding:0;box-sizing:border-box;font-family:'Malgun Gothic','맑은 고딕',system-ui,sans-serif}
body{background:#eef1f7;padding:40px}
.card{width:1120px;background:#fff;border:1px solid #e5e8f0;border-radius:24px;padding:44px 48px 40px}
.cat{color:#2f5fef;font-weight:700;font-size:19px;letter-spacing:-.3px}
.title{font-size:46px;font-weight:800;color:#1a1a2e;margin:10px 0 6px;letter-spacing:-1px}
.sub{font-size:21px;color:#8b93a1;margin-bottom:30px}
table{width:100%;border-collapse:collapse}
th{background:#2f5fef;color:#fff;font-size:20px;font-weight:700;padding:16px 22px;text-align:left}
th:first-child{border-radius:12px 0 0 12px}
th:last-child{border-radius:0 12px 12px 0;text-align:right}
td{padding:20px 22px;border-bottom:1px solid #eef0f5;font-size:20px;color:#4a5568;vertical-align:middle}
td.lbl{font-weight:700;color:#1a1a2e;width:32%}
td.tag{text-align:right;width:20%}
.pill{display:inline-block;background:#e8edff;color:#2f5fef;font-weight:700;font-size:17px;padding:7px 15px;border-radius:99px}
tr:last-child td{border-bottom:none}
b{color:#1a1a2e}
.foot{display:flex;justify-content:space-between;align-items:center;margin-top:26px;padding-top:8px}
.brand{font-size:22px;color:#1a1a2e}.brand b{font-weight:800}
.tag2{color:#aab0be;font-size:18px}
"""


def _html(cat, title, sub, headers, rows):
    ths = "".join(f"<th>{h}</th>" for h in headers)
    trs = ""
    for r in rows:
        trs += "<tr>"
        for i, c in enumerate(r):
            cls = "lbl" if i == 0 else ("tag" if i == len(r) - 1 else "")
            cell = f"<span class='pill'>{c}</span>" if cls == "tag" else c
            trs += f"<td class='{cls}'>{cell}</td>"
        trs += "</tr>"
    return (f"<!doctype html><html><head><meta charset='utf-8'><style>{CSS}</style></head>"
            f"<body><div class='card'><div class='cat'>MADE-US · {cat}</div>"
            f"<div class='title'>{title}</div><div class='sub'>{sub}</div>"
            f"<table><thead><tr>{ths}</tr></thead><tbody>{trs}</tbody></table>"
            "<div class='foot'><div class='brand'>메이드어스 · <b>commercial art studio</b></div>"
            "<div class='tag2'>디자인부터 제작·설치까지</div></div></div></body></html>")


# (파일명, 카테고리, 제목, 부제, 헤더, 행들) — 새 인포그래픽은 여기 추가
SPECS = [
    ("sign-cost.png", "간판 가이드", "간판 비용, 뭐가 좌우할까", "견적 전에 이 5가지를 확인하세요.",
     ["요인", "설명", "영향"],
     [["크기·면적", "클수록 소재·시공량 증가", "큼"],
      ["소재·발광 방식", "LED 채널 / 후광 / 아크릴 등", "큼"],
      ["글자 수·디자인", "복잡할수록 제작 공수 ↑", "중간"],
      ["설치 조건", "벽면·높이·전기 위치", "중간"],
      ["사후 관리(A/S)", "부분 수리·교체 기준", "작음"]]),
    ("bucket-care.png", "클럽 용품 가이드", "LED 아이스버킷 오래 쓰는 법",
     "매일 조금씩만 챙기면 연출이 오래갑니다.", ["단계", "관리 포인트", "주기"],
     [["사용 후", "물기 비우고 마른 천으로 닦기", "매일"],
      ["세척", "중성세제·부드러운 천 (수세미 X)", "수시"],
      ["전원부", "배선·연결부에 물 직접 닿지 않게", "항상"],
      ["보관", "완전 건조 후 눌림·긁힘 방지", "매일"],
      ["충전", "반충전 보관, 과충전·방전 피하기", "장기"]]),
    ("club-led-set.png", "클럽 용품 가이드", "테이블 연출 3요소",
     "버킷·트레이·사인을 조합하면 테이블이 무대가 됩니다.", ["요소", "역할", "연출"],
     [["LED 아이스버킷", "병을 담아 테이블 중앙에서 발광", "주인공"],
      ["LED 트레이·홀더", "병·잔·스파클러로 화려함 ↑", "볼거리"],
      ["LED 사인·피켓", "축하 문구·매장 로고로 순간 강조", "포인트"],
      ["커스텀 로고", "손님 사진마다 매장 홍보", "브랜딩"]]),
]


def main():
    IMG.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(device_scale_factor=2, viewport={"width": 1240, "height": 900})
        for fn, cat, title, sub, headers, rows in SPECS:
            pg.set_content(_html(cat, title, sub, headers, rows))
            pg.wait_for_timeout(400)
            pg.query_selector(".card").screenshot(path=str(IMG / fn))
            print("렌더:", fn)
        b.close()
    print("완료 →", IMG)


if __name__ == "__main__":
    main()
