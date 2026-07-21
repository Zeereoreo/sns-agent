"""무인 발행 스케줄러.

Windows 작업 스케줄러가 하루 N회(예: 09/13/18시) 이 스크립트의 `run`을 호출한다.
각 실행: 큐에서 다음 미발행 초안 1편 → 이미지 선택 → 발행 → 상태 기록.
하루 상한(config.MAX_POSTS_PER_DAY) 준수. 세그먼트 A/B/C 인터리브로 주제 분산.

사용:
  python scheduler.py run            # 실제 1편 발행
  python scheduler.py run --dry-run  # 테스트(발행 안 함)
  python scheduler.py status         # 진행 현황
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from itertools import zip_longest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# UTF-8 콘솔
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import config  # noqa: E402
from publish import images as imgmod  # noqa: E402
from publish.draft_parser import parse_draft  # noqa: E402
# publish.naver 는 playwright 를 끌어온다. status/대시보드는 브라우저가 필요 없으므로
# run() 안에서만 늦게 임포트한다.

DRAFTS = ROOT / "drafts"
STATE = ROOT / "data" / "publish_state.json"


def _load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {"published": [], "log": []}


def _save_state(s: dict) -> None:
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")


def _ordered_drafts() -> list[Path]:
    """세그먼트 A/B/C 인터리브 (샘플 포함). 주제 분산."""
    a = sorted(DRAFTS.glob("sample*.md")) + sorted(DRAFTS.glob("a*.md"))
    b = sorted(DRAFTS.glob("b*.md"))
    c = sorted(DRAFTS.glob("c*.md"))
    out: list[Path] = []
    for trio in zip_longest(a, b, c):
        out.extend(p for p in trio if p is not None)
    return out


def _image_slots(draft: Path) -> int:
    try:
        d = parse_draft(draft)
        return sum(1 for blk in d["blocks"] if blk["kind"] == "image")
    except Exception:
        return 1


def run(dry_run: bool = True) -> None:
    # 로그 구분선은 여기서 찍는다(배치 echo 로 찍으면 cp949 라 UTF-8 로그와 섞임).
    print(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} =====")
    s = _load_state()
    today = str(date.today())
    published = set(s["published"])
    today_ok = sum(1 for e in s["log"] if e.get("date") == today and e.get("ok"))

    if not dry_run and today_ok >= config.MAX_POSTS_PER_DAY:
        print(f"오늘 발행 상한({config.MAX_POSTS_PER_DAY}) 도달. 종료.")
        return

    nxt = next((p for p in _ordered_drafts() if p.name not in published), None)
    if nxt is None:
        print("발행할 초안 없음(큐가 비었습니다).")
        return

    n = _image_slots(nxt)
    picks, used_inbox = imgmod.pick_images(nxt, n)
    print(f"[스케줄러] 대상: {nxt.name} | 이미지 {len(picks)}장 | dry_run={dry_run}")

    from publish import naver  # noqa: PLC0415 (playwright 지연 임포트)

    ok = False
    reason = None
    res: dict = {}
    try:
        res = naver.publish(str(nxt), image_paths=[str(x) for x in picks],
                            dry_run=dry_run, headed=False) or {}
        ok = bool(res.get("ok"))
        reason = res.get("reason")
    except Exception as e:
        print("발행 중 오류:", e)
        reason = f"exception: {e}"

    # 발행이 확인된 경우에만 큐에서 뺀다. 확인 안 되면 다음 실행에서 다시 시도.
    if ok:
        s["published"].append(nxt.name)
        imgmod.mark_inbox_used(used_inbox)
    s["log"].append({"date": today, "time": datetime.now().strftime("%H:%M"),
                     "draft": nxt.name, "ok": ok, "dry": dry_run,
                     "images": res.get("images_inserted", 0),
                     "planned_images": len(picks),
                     "reason": reason, "url": res.get("url")})
    _save_state(s)
    if ok:
        print("완료.", res.get("url") or "")
    elif dry_run:
        print("dry-run 완료.")
    else:
        print(f"발행 실패({reason}) — 초안은 큐에 남겨둡니다. 다음 실행에서 재시도.")
        if reason == "session_expired":
            print("  → 세션 만료입니다. `python -m publish.naver login` 으로 다시 로그인하세요.")

    # 효과 지표 수집(방문자 매회, 키워드 순위는 하루 1회). 발행 성공/실패와 무관, 실패해도 무시.
    if not dry_run:
        try:
            import metrics  # noqa: PLC0415
            metrics.collect()
        except Exception as e:
            print("지표 수집 건너뜀:", e)


def status() -> None:
    s = _load_state()
    alld = _ordered_drafts()
    pub = set(s["published"])
    print(f"전체 초안: {len(alld)}  |  발행됨: {len(pub)}  |  남음: {len(alld) - len(pub)}")
    today = str(date.today())
    print(f"오늘 발행: {sum(1 for e in s['log'] if e.get('date') == today and e.get('ok'))} / {config.MAX_POSTS_PER_DAY}")
    nxt = next((p.name for p in alld if p.name not in pub), None)
    print(f"다음 대상: {nxt}")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    rp = sub.add_parser("run")
    rp.add_argument("--dry-run", action="store_true")
    sub.add_parser("status")
    a = ap.parse_args()
    if a.cmd == "run":
        run(dry_run=a.dry_run)
    else:
        status()


if __name__ == "__main__":
    main()
