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

ALERT_AFTER = 2   # 연속 실패가 이 횟수 이상이면 바탕화면 경고를 남긴다


FAIL_SKIP_AFTER = 3
# 초안과 무관한 실패 이유 — 이건 그 초안 탓이 아니므로 카운트하지 않는다
_NOT_DRAFT_FAULT = {"session_expired", "dry_run", None, ""}


def _repeatedly_failing(log: list, published: set) -> set:
    """자기 문제로 FAIL_SKIP_AFTER 회 이상 연속 실패한 초안 집합.

    세션 만료처럼 전체가 막히는 원인은 제외한다. 그런 걸 초안 탓으로 세면
    멀쩡한 초안이 통째로 건너뛰어진다. 이미지 삽입 실패·본문 실패·예외만 센다.
    성공하면 카운트가 초기화된다.
    """
    streak: dict[str, int] = {}
    for e in log:
        if e.get("dry"):
            continue
        name = e.get("draft")
        if not name:
            continue
        if e.get("ok"):
            streak[name] = 0
            continue
        if str(e.get("reason")) in _NOT_DRAFT_FAULT:
            continue
        streak[name] = streak.get(name, 0) + 1
    # 실제로 존재하는 초안만 — 옛 로그에는 '(전체 삭제)' 같은 항목이 남아 있다
    return {n for n, c in streak.items()
            if c >= FAIL_SKIP_AFTER and n not in published and (DRAFTS / n).exists()}


def _consecutive_failures(log: list) -> int:
    """마지막 실제 실행부터 거꾸로 센 연속 실패 횟수(dry-run 은 제외)."""
    n = 0
    for e in reversed(log):
        if e.get("dry"):
            continue
        if e.get("ok"):
            break
        n += 1
    return n


def _update_alert_file(log: list) -> None:
    """연속 실패가 쌓이면 바탕화면 경고를 남기고, 발행이 복구되면 치운다."""
    import notify  # noqa: PLC0415
    fails = _consecutive_failures(log)
    if fails < ALERT_AFTER:
        notify.clear_alert()
        return
    last = next(e for e in reversed(log) if not e.get("dry"))
    notify.write_alert(
        f"SNS Agent 발행이 {fails}회 연속 실패했습니다.\n"
        f"마지막 시도: {last.get('date')} {last.get('time')}  "
        f"이유: {last.get('reason')}\n\n"
        f"세션 만료면 아래로 다시 로그인하세요('로그인 상태 유지' 체크 필수).\n"
        f"  cd {ROOT}\n"
        f"  .\\.venv\\Scripts\\python.exe publish\\naver.py login\n\n"
        f"발행이 복구되면 이 파일은 자동으로 사라집니다.\n")


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

    # 특정 초안이 자기 문제로 계속 실패하면 큐가 그 자리에서 막힌다(무한 재시도).
    # 세션 만료처럼 초안과 무관한 실패는 제외하고, 그 초안 고유 실패만 센다.
    blocked = _repeatedly_failing(s["log"], published)
    if blocked:
        print(f"[건너뜀] 자기 문제로 {FAIL_SKIP_AFTER}회 이상 실패한 초안: {', '.join(blocked)}")

    # 발행 순서 = 성장 엔진이 성과 데이터로 정한 우선순위. 실패 시 기존 인터리브로 폴백.
    nxt = None
    try:
        import growth  # noqa: PLC0415
        pick = growth.next_draft()          # 세그먼트 3연속 회피 로직 포함
        if pick in blocked:                 # 막힌 초안이면 그다음 후보로
            pick = next((r["name"] for r in growth.rank_queue()
                         if r["name"] not in blocked and (DRAFTS / r["name"]).exists()), None)
        if pick and pick not in published:
            nxt = DRAFTS / pick
            print(f"[성장엔진] 다음 발행 선택: {pick}")
    except Exception as e:
        print("성장엔진 건너뜀(인터리브로 폴백):", e)
    if nxt is None:
        nxt = next((p for p in _ordered_drafts() if p.name not in published), None)
    if nxt is None:
        print("발행할 초안 없음(큐가 비었습니다).")
        return

    # BJ/스트리머 위주 목록(2026-07-24 사용자 지시): 하루 최소 2편은 a(방송소품).
    # 남은 슬롯을 전부 a 로 채워야만 쿼터가 차는 시점부터 개입(그 전엔 엔진 자유).
    # a 초안이 소진되면 자동 해제. a 안에서의 순서는 성장엔진 rank_queue 를 따른다.
    if not nxt.name.startswith("a"):
        today_a = sum(1 for e in s["log"] if e.get("date") == today and e.get("ok")
                      and str(e.get("draft", "")).startswith("a"))
        slots_left = max(1, config.MAX_POSTS_PER_DAY - today_ok)
        need_a = max(0, 2 - today_a)
        if need_a >= slots_left:
            try:
                import growth  # noqa: PLC0415
                a_pick = next((r["name"] for r in growth.rank_queue()
                               if r["name"].startswith("a")
                               and (DRAFTS / r["name"]).exists()), None)
                if a_pick:
                    print(f"[BJ 쿼터] 오늘 a {today_a}편/슬롯 {slots_left} → {a_pick} 로 교체")
                    nxt = DRAFTS / a_pick
            except Exception as e:
                print("[BJ 쿼터] 건너뜀:", e)

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
                            dry_run=dry_run, headed=False,
                            category=config.SEGMENT_CATEGORY.get(nxt.name[:1])) or {}
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
                     "title": res.get("title"),   # 발행에 쓰인 제목(A/B 로테이션 관찰용)
                     # 라이브 글 실측(이미지·태그·소제목·본문). '발행됨'과 '제대로 나감'은 다르다.
                     "audit": res.get("audit"),
                     "reason": reason, "url": res.get("url")})
    _save_state(s)
    if not dry_run:
        _update_alert_file(s["log"])
    if ok:
        print("완료.", res.get("url") or "")
    elif dry_run:
        print("dry-run 완료.")
    else:
        print(f"발행 실패({reason}) — 초안은 큐에 남겨둡니다. 다음 실행에서 재시도.")
        if reason == "session_expired":
            print("  → 세션 만료입니다. `python -m publish.naver login` 으로 다시 로그인하세요.")
        # 조용한 실패를 즉시 알린다(Windows 트레이 알림).
        try:
            import notify  # noqa: PLC0415
            msg = {"session_expired": "네이버 세션 만료 — 재로그인 필요",
                   "images_failed": "이미지 삽입 실패로 발행 중단",
                   "not_found_after_publish": "발행했으나 글이 확인되지 않음"
                   }.get(str(reason), f"발행 실패: {reason}")
            notify.notify(f"SNS Agent 발행 실패 ({nxt.name})", msg)
        except Exception:
            pass

    # 효과 지표 수집(방문자 매회, 키워드 순위는 하루 1회). 발행 성공/실패와 무관, 실패해도 무시.
    if not dry_run:
        try:
            import metrics  # noqa: PLC0415
            metrics.collect()
        except Exception as e:
            print("지표 수집 건너뜀:", e)

        # 성장 엔진 자가 튜닝: 최신 순위로 과거 결정을 평가해 가중치 보정.
        try:
            import growth  # noqa: PLC0415
            r = growth.evaluate_and_tune()
            if r["samples"]:
                print(f"[성장엔진] 자가 튜닝(샘플 {r['samples']}) → {r['new']}")
        except Exception as e:
            print("성장엔진 튜닝 건너뜀:", e)

        # 성장엔진 우선순위 상위 후보의 경쟁 분석을 미리 준비(리서치 없는 것 최대 2편).
        # 경쟁깊이 승산 신호(growth._research_opportunity/_research_penalty)가 실제로 작동하려면
        # '발행될 초안'의 키워드에 경쟁 데이터가 있어야 한다. (예전엔 인터리브 순서의 엉뚱한
        # 초안을 리서치했다 — 실제 발행은 성장엔진 점수순인데.)
        try:
            import growth  # noqa: PLC0415
            import research  # noqa: PLC0415
            RDIR = ROOT / "data" / "research"
            done = 0
            for row in growth.rank_queue():
                if done >= 2:
                    break
                kw = row.get("keyword")
                if not kw or (RDIR / f"{research._slug(kw)}.json").exists():
                    continue
                res = research.analyze(row["name"])
                # 경쟁글을 하나도 못 얻었으면(스크래핑 실패 가능성) 빈 결과를 캐시하지 않는다.
                if res.get("competitors") or res.get("length_benchmark"):
                    research.save(res)
                    print(f"[경쟁분석] 상위 후보 '{kw}' 준비 완료")
                    done += 1
                else:
                    print(f"[경쟁분석] '{kw}' 결과 없음 — 저장 안 함(다음 실행에서 재시도).")
        except Exception as e:
            print("경쟁 분석 건너뜀:", e)

        # 수요 캐시 갱신(주 1회) — 초안 키워드가 바뀌면 캐시가 낡는다.
        # 낡은 캐시는 '수요 0인데 큐 상위' 같은 잘못된 발행 순서를 만든다.
        try:
            _audit_demand_weekly()
        except Exception as e:
            print("수요 갱신 건너뜀:", e)

        # 기회 키워드 재스캔(주 1회) — 새 기회를 사람이 손으로 찾지 않아도 되게.
        # SERP 형식은 시간이 지나면 바뀐다(경쟁 글이 새로 올라오거나 빠짐).
        try:
            _scan_opportunities_weekly()
        except Exception as e:
            print("기회 스캔 건너뜀:", e)

        # 라이브 전수 대조(주 1회) — 발행된 글이 아직도 계획대로인지.
        try:
            _audit_live_weekly()
        except Exception as e:
            print("라이브 감사 건너뜀:", e)


OPP_STAMP = ROOT / "data" / ".opportunity-last-scan"
OPP_EVERY_DAYS = 7
DEMAND_STAMP = ROOT / "data" / ".demand-last-audit"
DEMAND_EVERY_DAYS = 7
LIVE_STAMP = ROOT / "data" / ".live-last-audit"
LIVE_EVERY_DAYS = 7
LIVE_REPORT = ROOT / "data" / "live_audit.json"


def _stale(stamp: Path, days: int) -> bool:
    """마지막 실행이 days 보다 오래됐는가(기록이 없으면 오래된 것으로 본다)."""
    if not stamp.exists():
        return True
    try:
        return (date.today() - date.fromisoformat(stamp.read_text(encoding="utf-8").strip())
                ).days >= days
    except Exception:
        return True


def _audit_demand_weekly() -> None:
    """초안 키워드의 검색 수요를 다시 잰다. 캐시가 낡으면 발행 순서가 틀어진다."""
    if not _stale(DEMAND_STAMP, DEMAND_EVERY_DAYS):
        return
    import demand  # noqa: PLC0415
    print("[수요] 주간 갱신 시작")
    demand.audit()
    DEMAND_STAMP.write_text(date.today().isoformat(), encoding="utf-8")


def _audit_live_weekly() -> None:
    """발행된 글이 **아직도** 계획대로인지 주 1회 전수 대조한다.

    발행 직후 점검(publish._audit_live_post)은 그 순간만 본다. 나중에 네이버 렌더링이
    바뀌거나 우리가 소급 수정을 잘못하면 조용히 어긋난다. 실제로 소제목이 평문으로
    나간 14편을 9일 뒤에야 발견했다(2026-07-29). 결과는 data/live_audit.json 에 남겨
    대시보드 진단이 읽는다.
    """
    if not _stale(LIVE_STAMP, LIVE_EVERY_DAYS):
        return
    import json as _json  # noqa: PLC0415
    import re as _re  # noqa: PLC0415

    from playwright.sync_api import sync_playwright  # noqa: PLC0415

    from publish.browser import launch_context  # noqa: PLC0415
    from publish.draft_parser import parse_draft  # noqa: PLC0415
    from publish.naver import _audit_live_post  # noqa: PLC0415

    print("[라이브 감사] 주간 전수 대조 시작")
    s = _load_state()
    last = {}
    for e in s.get("log", []):
        if e.get("ok") and not e.get("dry") and e.get("draft"):
            last[e["draft"]] = e

    rows = []
    with sync_playwright() as p:
        ctx = launch_context(p, headed=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        for name, e in last.items():
            dp = DRAFTS / name
            if not dp.exists() or not _re.search(r"/(\d{6,})", e.get("url") or ""):
                continue
            d = parse_draft(dp)
            r = _audit_live_post(page, e["url"], {
                "images": e.get("planned_images") or 0,
                "tags": len(d["tags"]),
                "headings": sum(1 for b in d["blocks"] if b["kind"] == "heading"),
            })
            if r.get("issues"):
                rows.append({"draft": name, "url": e["url"], "issues": r["issues"]})
        ctx.close()

    LIVE_REPORT.write_text(_json.dumps(
        {"checked": date.today().isoformat(), "total": len(last), "bad": rows},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[라이브 감사] {len(last)}편 중 문제 {len(rows)}편")
    LIVE_STAMP.write_text(date.today().isoformat(), encoding="utf-8")


def _scan_opportunities_weekly() -> None:
    """마지막 스캔이 OPP_EVERY_DAYS 보다 오래됐으면 기회 키워드를 다시 훑는다."""
    if not _stale(OPP_STAMP, OPP_EVERY_DAYS):
        return
    import opportunity  # noqa: PLC0415
    print("[기회] 주간 스캔 시작(제품 시드 기준)")
    opportunity.scan(opportunity.SEEDS, max_candidates=12)
    OPP_STAMP.write_text(date.today().isoformat(), encoding="utf-8")


def status() -> None:
    s = _load_state()
    alld = _ordered_drafts()
    pub = set(s["published"])
    print(f"전체 초안: {len(alld)}  |  발행됨: {len(pub)}  |  남음: {len(alld) - len(pub)}")
    today = str(date.today())
    print(f"오늘 발행: {sum(1 for e in s['log'] if e.get('date') == today and e.get('ok'))} / {config.MAX_POSTS_PER_DAY}")
    # 다음 대상 = 실제 발행에 쓰는 성장엔진 선택(폴백: 인터리브)
    nxt = None
    try:
        import growth  # noqa: PLC0415
        nxt = growth.next_draft()
    except Exception:
        pass
    if not nxt:
        nxt = next((p.name for p in alld if p.name not in pub), None)
    print(f"다음 대상: {nxt}  (성장엔진 우선순위)")


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
