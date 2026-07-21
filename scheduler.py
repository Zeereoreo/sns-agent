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
import os
import re
import sys
import time
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
    """상태 로드. 파일이 '없으면' 새 상태(정상). 파일이 '있는데 못 읽으면' 예외로 중단한다.
    (예전엔 손상/잠김도 빈 목록으로 처리해서, 다음 실행이 큐 전체를 재발행할 수 있었음.)
    잠깐의 잠금은 짧게 재시도한다."""
    if not STATE.exists():
        return {"published": [], "log": []}
    last = None
    for _ in range(4):
        try:
            data = json.loads(STATE.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or "published" not in data:
                raise ValueError("형식 오류(published 키 없음)")
            return data
        except Exception as e:
            last = e
            time.sleep(0.3)
    raise RuntimeError(
        f"publish_state.json 을 읽지 못했습니다({last}). 재발행 방지를 위해 중단합니다. "
        f"파일을 확인/복구하세요: {STATE}")


def _save_state(s: dict) -> None:
    """원자적 저장: 임시파일에 쓰고 os.replace 로 교체(reader 가 반쪽 파일을 보지 않음)."""
    STATE.parent.mkdir(exist_ok=True)
    tmp = STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, STATE)


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


LOCK = ROOT / "data" / ".publish.lock"
LOCK_STALE_SEC = 25 * 60   # 이보다 오래된 락은 죽은 프로세스로 간주


def run(dry_run: bool = True) -> None:
    """실제 발행은 락으로 중복 실행을 막는다(같은 초안 이중 발행 방지). dry-run 은 락 없음."""
    if dry_run:
        return _run(dry_run=True)
    LOCK.parent.mkdir(exist_ok=True)
    if LOCK.exists():
        try:
            age = time.time() - LOCK.stat().st_mtime
        except OSError:
            age = 0
        if age < LOCK_STALE_SEC:
            print(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} =====")
            print("다른 발행이 진행 중(락 존재). 이번 실행은 건너뜁니다.")
            return
        print("오래된 락 발견 — 죽은 프로세스로 보고 제거.")
    try:
        fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        print("락 획득 실패(경합) — 건너뜁니다.")
        return
    try:
        _run(dry_run=False)
    finally:
        try:
            LOCK.unlink()
        except OSError:
            pass


def _run(dry_run: bool = True) -> None:
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

    # 발행 전 SEO 게이트: 매 글을 점검해 품질을 추적한다(점수 낮으면 경고).
    seo_score = seo_grade = None
    try:
        import seo  # noqa: PLC0415
        sr = seo.score_draft(nxt)
        seo_score, seo_grade = sr["score"], sr["grade"]
        weak = [c["name"] for c in sr["checks"] if c["pts"] < c["max"] * 0.5]
        print(f"[SEO] {seo_grade} {seo_score}점" + (f" (약점: {', '.join(weak)})" if weak else ""))
        if seo_score < 70:
            print("  ⚠ SEO 점수 낮음 — 발행은 하되 개선 권장.")
    except Exception as e:
        print("[SEO] 점검 건너뜀:", e)

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
                     "seo_score": seo_score, "seo_grade": seo_grade,
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

        # 다음 발행 예정글의 경쟁 분석을 미리 준비(없을 때만). 실패해도 무시.
        try:
            import research  # noqa: PLC0415
            nx = research._next_draft()
            slug = None
            if nx:
                kw, _ = research._kw_from(nx)
                slug = re.sub(r"[^가-힣a-zA-Z0-9]+", "_", kw)[:40]
            if slug and not (ROOT / "data" / "research" / f"{slug}.json").exists():
                res = research.analyze(nx)
                # 경쟁글을 하나도 못 얻었으면(스크래핑 실패 가능성) 빈 결과를 캐시하지 않는다.
                if res.get("competitors") or res.get("length_benchmark"):
                    research.save(res)
                    print(f"[경쟁분석] 다음 글 '{kw}' 준비 완료")
                else:
                    print(f"[경쟁분석] '{kw}' 결과 없음 — 저장 안 함(다음 실행에서 재시도).")
        except Exception as e:
            print("경쟁 분석 건너뜀:", e)


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
