"""운영 대시보드 (로컬 웹 UI).

발행 현황·큐·스케줄 작업 상태·이미지 풀을 한 화면에서 본다. 읽기 전용.

사용:
  python dashboard.py            # http://127.0.0.1:8765 열기
  python dashboard.py --port 9000
"""
from __future__ import annotations

import argparse
import csv
import html
import io
import json
import re
import socket
import subprocess
import sys
import threading
import webbrowser
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import config  # noqa: E402
import scheduler  # noqa: E402
from publish import images as imgmod  # noqa: E402
from publish.draft_parser import parse_draft  # noqa: E402

TASKS = ["SNS-Agent-1-Morning", "SNS-Agent-2-Noon", "SNS-Agent-3-Evening"]

# schtasks Last Result 코드 → 사람이 읽는 뜻
RESULT_MEANING = {
    "0": ("정상", "ok"),
    "267011": ("아직 실행된 적 없음", "warn"),
    "267009": ("실행 중", "ok"),
    "-2147020576": ("배터리 전원이라 실행 거부됨", "bad"),
    "1": ("스크립트 오류로 종료", "bad"),
}


def _run(cmd: list[str]) -> str:
    """schtasks 실행. 한국어 Windows 콘솔(cp949) 및 /xml 출력(UTF-16) 대응."""
    try:
        # pythonw(콘솔 없음)로 띄우면 stdin 핸들이 없어 schtasks 가 실패한다 → DEVNULL 필수.
        # CREATE_NO_WINDOW 는 호출할 때마다 콘솔 창이 깜빡이는 것을 막는다.
        raw = subprocess.run(
            cmd, capture_output=True, timeout=20,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except Exception:
        return ""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):   # /xml 은 UTF-16
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError:
            pass
    for enc in ("utf-8", "cp949", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return ""


# schtasks 의 텍스트 출력은 레이블이 로케일에 따라 한/영으로 바뀌어 파싱이 깨진다.
# PowerShell 개체의 속성명은 로케일과 무관하므로 JSON 으로 받아온다.
_PS_TASKS = """
$ErrorActionPreference='Stop'
Get-ScheduledTask -TaskName 'SNS-Agent-*' | ForEach-Object {
  $i = $_ | Get-ScheduledTaskInfo
  [pscustomobject]@{
    Name    = $_.TaskName
    State   = [string]$_.State
    Battery = [bool]$_.Settings.DisallowStartIfOnBatteries
    Next    = if ($i.NextRunTime) { $i.NextRunTime.ToString('yyyy-MM-dd HH:mm') } else { '-' }
    Last    = if ($i.LastRunTime) { $i.LastRunTime.ToString('yyyy-MM-dd HH:mm') } else { '-' }
    Result  = [string]$i.LastTaskResult
  }
} | ConvertTo-Json -Compress
"""


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _parse_multipart(body: bytes, boundary: str):
    """최소 multipart/form-data 파서 (Python 3.13+ 에서 cgi 모듈 제거됨).
    반환: (fields: {name: str}, files: [(filename, bytes), ...])"""
    sep = b"--" + boundary.encode()
    fields, files = {}, []
    for part in body.split(sep):
        if not part or part in (b"--\r\n", b"--", b"\r\n"):
            continue
        if b"\r\n\r\n" not in part:
            continue
        raw_head, data = part.split(b"\r\n\r\n", 1)
        data = data[:-2] if data.endswith(b"\r\n") else data   # 끝 CRLF 제거
        head = raw_head.decode("utf-8", "replace")
        disp = next((ln for ln in head.splitlines() if "content-disposition" in ln.lower()), "")
        m_name = re.search(r'name="([^"]*)"', disp)
        m_file = re.search(r'filename="([^"]*)"', disp)
        if not m_name:
            continue
        if m_file and m_file.group(1):
            files.append((m_file.group(1), data))
        else:
            fields[m_name.group(1)] = data.decode("utf-8", "replace").strip()
    return fields, files


def _norm_code(v) -> str:
    """작업 결과 코드를 부호 있는 32비트 문자열로 통일한다.

    schtasks 는 -2147020576, PowerShell 은 2147946720 으로 같은 값(0x800710E0)을
    다르게 준다. 한쪽만 매핑하면 조용히 '코드 xxxx' 로 떨어진다.
    """
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return str(v)
    if n > 0x7FFFFFFF:
        n -= 0x100000000
    return str(n)


def task_info() -> list[dict]:
    txt = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_TASKS])
    try:
        rows = json.loads(txt)
    except Exception:
        rows = []
    if isinstance(rows, dict):        # 작업이 1개면 배열이 아니라 개체로 온다
        rows = [rows]
    by_name = {r.get("Name"): r for r in rows}

    out = []
    for name in TASKS:
        r = by_name.get(name)
        if r is None:
            out.append({"name": name, "next": "-", "last": "-", "state": "-",
                        "result": "작업을 찾을 수 없음", "level": "bad",
                        "battery_block": False})
            continue
        battery_block = bool(r.get("Battery"))
        code = _norm_code(r.get("Result", ""))
        meaning, level = RESULT_MEANING.get(code, (f"코드 {code}", "warn"))
        # 과거 실패지만 원인 설정이 이미 고쳐졌다면 '해결됨'으로 낮춰 표시한다.
        if level == "bad" and code == "-2147020576" and not battery_block:
            meaning, level = f"{meaning} → 설정 수정됨, 다음 실행 대기", "warn"
        out.append({
            "name": name,
            "next": str(r.get("Next", "-")),
            "last": str(r.get("Last", "-")),
            "state": str(r.get("State", "-")),
            "result": meaning,
            "level": level,
            "battery_block": battery_block,
        })
    return out


def _metrics_view() -> dict:
    """data/metrics.json → 대시보드용 방문자 시계열 + 키워드 순위."""
    raw = _load_json(ROOT / "data" / "metrics.json",
                     {"visitors": {}, "ranks": {}, "keywords": {}})
    vis = raw.get("visitors", {})
    days = sorted(vis)
    series = []
    prev_total = None
    for d in days:
        total = vis[d].get("total")
        new = (total - prev_total) if (prev_total is not None and total is not None) else None
        if new is not None and new < 0:
            new = None      # 누적이 줄면 오파싱 → 가짜 음수 막대 대신 공백
        series.append({"date": d, "total": total, "today": vis[d].get("today"), "new": new})
        prev_total = total

    ranks = raw.get("ranks", {})
    rdays = sorted(ranks)
    latest = ranks.get(rdays[-1], {}) if rdays else {}
    prev = ranks.get(rdays[-2], {}) if len(rdays) >= 2 else {}
    kw_rows = []
    for kw, r in sorted(latest.items(), key=lambda kv: (kv[1] is None, kv[1] or 99)):
        pr = prev.get(kw)
        delta = None
        if isinstance(r, int) and isinstance(pr, int):
            delta = pr - r          # +면 순위 상승(숫자 작아짐)
        kw_rows.append({"kw": kw, "rank": r, "delta": delta})
    on_p1 = sum(1 for x in kw_rows if isinstance(x["rank"], int) and x["rank"] <= 10)

    return {
        "series": series,
        "cum_total": series[-1]["total"] if series else None,
        "today": series[-1]["today"] if series else None,
        "kw_rows": kw_rows,
        "kw_tracked": len(kw_rows),
        "kw_on_page1": on_p1,
        "rank_date": rdays[-1] if rdays else None,
    }


def collect() -> dict:
    state = scheduler._load_state()
    drafts = scheduler._ordered_drafts()
    published = set(state["published"])
    today = str(date.today())

    try:
        import seo as seomod
    except Exception:
        seomod = None

    queue = []
    nxt_marked = False
    next_seo = None
    weak_drafts = []
    for i, p in enumerate(drafts, 1):
        done = p.name in published
        is_next = not done and not nxt_marked
        if is_next:
            nxt_marked = True
        try:
            title = parse_draft(p)["title"]
        except Exception:
            title = "(파싱 실패)"
        sc = None
        if seomod is not None:
            try:
                sr = seomod.score_draft(p)
                sc = {"score": sr["score"], "grade": sr["grade"],
                      "weak": [c["name"] for c in sr["checks"] if c["pts"] < c["max"] * 0.5]}
                if is_next:
                    next_seo = {"file": p.name, "title": title,
                                "competitor": sr.get("competitor"), **sc}
                if not done and sr["grade"] in ("C", "D"):
                    weak_drafts.append({"file": p.name, "score": sr["score"], "grade": sr["grade"]})
            except Exception:
                pass
        queue.append({"i": i, "name": p.name, "title": title, "seo": sc,
                      "status": "done" if done else ("next" if is_next else "wait")})

    infographics = imgmod._imgs(imgmod.IMG_DIR)
    inbox = imgmod._imgs(imgmod.INBOX_DIR)
    pool = [p for p in imgmod._imgs(imgmod.PHOTO_DIR) if p.parent == imgmod.PHOTO_DIR]

    shots = sorted((ROOT / "drafts" / "_debug").glob("*.png"),
                   key=lambda p: p.stat().st_mtime, reverse=True)[:6]

    # 가장 최근의 '실제' 실행이 실패로 끝났는지(테스트 실행은 제외)
    last_real = next((x for x in reversed(state["log"]) if not x.get("dry")), None)
    last_fail = last_real if (last_real and not last_real.get("ok")) else None

    remaining = len(drafts) - len(published)
    per_day = max(config.MAX_POSTS_PER_DAY, 1)

    return {
        "stamp": f"{datetime.now():%Y-%m-%d %H:%M}",
        "days_left": remaining // per_day,
        "last_fail": last_fail,
        "next_seo": next_seo,
        "weak_drafts": weak_drafts,
        "metrics": _metrics_view(),
        "blog": config.NAVER_BLOG_ID or "(미설정)",
        "max_per_day": config.MAX_POSTS_PER_DAY,
        "today_ok": sum(1 for e in state["log"] if e.get("date") == today and e.get("ok")),
        "total": len(drafts),
        "done": len(published),
        "queue": queue,
        "log": list(reversed(state["log"]))[:15],
        "images": {"info": len(infographics), "inbox": len(inbox), "pool": len(pool)},
        "shots": [s.name for s in shots],
        "tasks": task_info(),
    }


CSS = """
*{box-sizing:border-box}
body{margin:0;background:#0f1115;color:#e6e8eb;font:14px/1.55 "Malgun Gothic",system-ui,sans-serif}
a{color:#7cc4ff}
.wrap{max-width:1080px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:20px;margin:0 0 4px}
.sub{color:#8b93a1;font-size:13px;margin-bottom:22px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:26px}
.card{background:#171a21;border:1px solid #232833;border-radius:10px;padding:14px 16px}
.card .k{color:#8b93a1;font-size:12px;margin-bottom:6px}
.card .v{font-size:22px;font-weight:600}
.card .v small{font-size:13px;color:#8b93a1;font-weight:400}
h2{font-size:15px;margin:28px 0 10px;padding-bottom:8px;border-bottom:1px solid #232833}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:#8b93a1;font-weight:500;padding:7px 8px;border-bottom:1px solid #232833}
td{padding:7px 8px;border-bottom:1px solid #1b1f27}
tr:hover td{background:#151920}
.badge{display:inline-block;padding:2px 8px;border-radius:99px;font-size:11px;font-weight:600}
.ok{background:#12331f;color:#5ddb8f}
.bad{background:#3a1618;color:#ff8087}
.warn{background:#3a2e12;color:#f0c463}
.mut{background:#1e222b;color:#8b93a1}
.next{background:#152a3d;color:#7cc4ff}
.scroll{max-height:420px;overflow-y:auto;border:1px solid #232833;border-radius:10px}
.scroll table{font-size:12.5px}
.scroll th{position:sticky;top:0;background:#171a21}
.shots{display:flex;gap:10px;flex-wrap:wrap}
.shots figure{margin:0;width:170px}
.shots img{width:100%;border:1px solid #232833;border-radius:8px;display:block}
.shots figcaption{color:#8b93a1;font-size:11px;margin-top:5px;word-break:break-all}
.alert{background:#3a1618;border:1px solid #5b2327;color:#ffb3b8;padding:12px 14px;border-radius:10px;margin-bottom:20px}
.alert code{background:#00000040;padding:1px 5px;border-radius:4px}
.foot{color:#5d6675;font-size:12px;margin-top:34px}
.muted{color:#8b93a1;font-size:12.5px}
.panel{background:#171a21;border:1px solid #232833;border-radius:10px;padding:14px 16px;margin:10px 0}
.panel .ptit{color:#8b93a1;font-size:12px;margin-bottom:8px}
.chart{display:block;overflow:visible}
.chart .bval{fill:#c9d1dc;font-size:11px}
.chart .blab{fill:#6b7280;font-size:10px}
.top{display:flex;justify-content:space-between;align-items:flex-start;gap:16px}
#quit{background:#232833;color:#c9d1dc;border:1px solid #333b49;border-radius:8px;
 padding:7px 14px;font:inherit;font-size:12.5px;cursor:pointer}
#quit:hover{background:#3a1618;color:#ff8087;border-color:#5b2327}
.nav{display:flex;gap:2px;flex-wrap:wrap;margin:0 0 22px;border-bottom:1px solid #232833}
.nav a{padding:9px 15px;color:#8b93a1;text-decoration:none;font-size:13.5px;border-bottom:2px solid transparent;margin-bottom:-1px}
.nav a.on{color:#e6e8eb;border-bottom-color:#4a7dff;font-weight:600}
.nav a:hover{color:#e6e8eb}
.logbox{background:#0b0d11;border:1px solid #232833;border-radius:8px;padding:12px;font-size:12px;color:#9aa4b2;overflow-x:auto;white-space:pre-wrap;max-height:360px}
.preview{background:#171a21;border:1px solid #232833;border-radius:10px;padding:16px 20px}
.preview h4{margin:16px 0 6px;font-size:14px;color:#e6e8eb}
.preview p{margin:6px 0;color:#c4ccd6}
.imgslot{margin:10px 0;padding:10px 12px;border:1px dashed #333b49;border-radius:8px;color:#8b93a1;font-size:12.5px}
.phgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:10px;margin:8px 0 4px}
.ph{margin:0}
.ph img{width:100%;height:110px;object-fit:cover;border:1px solid #232833;border-radius:8px;display:block;background:#0b0d11}
.ph figcaption{color:#6b7280;font-size:10.5px;margin-top:4px;word-break:break-all;line-height:1.3}
.seg{font-size:13px;color:#8b93a1;margin:16px 0 4px}
.upbox{background:#141821;border:1px solid #2a3550;border-radius:10px;padding:16px 18px;margin:14px 0}
.upbox .ptit{color:#7cc4ff;font-size:13px;margin-bottom:10px;font-weight:600}
.upbox select,.upbox input[type=file]{margin:6px 10px 6px 0;color:#e6e8eb;background:#0f1115;border:1px solid #333b49;border-radius:6px;padding:6px 8px;font:inherit;font-size:12.5px}
.upbox button{background:#1d3a5f;color:#cfe4ff;border:1px solid #2a4d78;border-radius:7px;padding:7px 16px;font:inherit;font-size:13px;cursor:pointer}
.upbox button:hover{background:#264a75}
.okbar{background:#12331f;border:1px solid #245536;color:#5ddb8f;padding:11px 14px;border-radius:10px;margin-bottom:18px}
.ta{width:100%;background:#0f1115;color:#e6e8eb;border:1px solid #333b49;border-radius:8px;padding:10px 12px;font:inherit;font-size:13px;resize:vertical}
.panel button{background:#1d3a5f;color:#cfe4ff;border:1px solid #2a4d78;border-radius:7px;padding:7px 18px;font:inherit;font-size:13px;cursor:pointer;margin-right:10px}
.panel button:hover{background:#264a75}
.emph{background:#141a14;border:1px solid #2a4d2f;border-radius:8px;padding:10px 12px;margin:10px 0;color:#8fd6a0;font-size:13px}
.dlbar{background:#171a21;border:1px solid #232833;border-radius:8px;padding:10px 14px;margin:0 0 8px;font-size:13px}
.dl{font-size:12px;font-weight:400;margin-left:8px}
.dayrow td{background:#141821;color:#8fb2e0;font-weight:600;font-size:12px}
"""

STATUS_BADGE = {"done": ("발행됨", "ok"), "next": ("다음 차례", "next"), "wait": ("대기", "mut")}

_SEO_LABEL = {
    "title_kw": "제목 키워드", "title_len": "제목 길이", "intro_kw": "도입부 키워드",
    "body_len": "본문 분량", "headings": "소제목", "faq": "FAQ", "images": "이미지 수",
    "captions": "캡션", "tags": "태그 수", "tag_kw": "태그 키워드",
}

# scheduler 가 기록한 실패 사유 → 사람이 읽는 설명(+조치)
REASON_TEXT = {
    "dry_run": ("테스트 실행", "mut"),
    "review": ("반자동 모드", "mut"),
    "session_expired": ("네이버 세션 만료 — 재로그인 필요", "bad"),
    "title_failed": ("제목 입력 실패 — 에디터 선택자 확인", "bad"),
    "body_failed": ("본문 입력 실패 — 에디터 선택자 확인", "bad"),
    "publish_click_failed": ("발행 버튼 클릭 실패", "bad"),
    "not_found_after_publish": ("발행했으나 글이 확인되지 않음", "bad"),
}


def _reason(r) -> tuple[str, str]:
    if not r:
        return ("", "mut")
    if str(r).startswith("exception:"):
        return (str(r)[:80], "bad")
    return REASON_TEXT.get(str(r), (str(r), "warn"))


def _svg_bars(series: list, key: str, color: str) -> str:
    """방문자 시계열을 인라인 SVG 막대그래프로. 외부 라이브러리 없음(CSP 안전)."""
    pts = [(x["date"], x.get(key)) for x in series if isinstance(x.get(key), int)]
    if not pts:
        return "<p class='muted'>데이터가 아직 없습니다. 매 발행 시 자동으로 쌓입니다.</p>"
    vals = [v for _, v in pts]
    vmax = max(vals) or 1
    n = len(pts)
    W, H, pad = max(n * 42, 120), 150, 22
    bw = min(30, (W - pad) / n - 8)
    bars = []
    for i, (dt, v) in enumerate(pts):
        h = (v / vmax) * (H - pad - 18)
        x = pad + i * ((W - pad) / n)
        y = H - pad - h
        label = dt[5:]  # MM-DD
        bars.append(
            f'<rect x="{x:.0f}" y="{y:.0f}" width="{bw:.0f}" height="{h:.0f}" rx="3" fill="{color}"/>'
            f'<text x="{x + bw/2:.0f}" y="{y - 4:.0f}" text-anchor="middle" class="bval">{v}</text>'
            f'<text x="{x + bw/2:.0f}" y="{H - 6:.0f}" text-anchor="middle" class="blab">{label}</text>')
    return (f'<svg viewBox="0 0 {W:.0f} {H}" width="100%" height="{H}" '
            f'preserveAspectRatio="xMinYMid meet" class="chart">{"".join(bars)}</svg>')


def _rank_badge(rank, delta) -> str:
    if not isinstance(rank, int):
        return "<span class='badge mut'>30위권 밖</span>"
    cls = "ok" if rank <= 10 else ("warn" if rank <= 20 else "mut")
    arrow = ""
    if isinstance(delta, int) and delta != 0:
        arrow = (f" <span style='color:#5ddb8f'>▲{delta}</span>" if delta > 0
                 else f" <span style='color:#ff8087'>▼{-delta}</span>")
    return f"<span class='badge {cls}'>{rank}위</span>{arrow}"


NAV = [("/", "개요"), ("/analytics", "성과"), ("/growth", "성장엔진"), ("/posts", "발행"),
       ("/calendar", "캘린더"), ("/seo", "콘텐츠·SEO"), ("/images", "이미지"),
       ("/settings", "설정"), ("/diag", "진단"), ("/ops", "상태")]


def _log_tail(n: int = 40) -> str:
    f = ROOT / "data" / "scheduler.log"
    try:
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return "(로그 없음)"
    return "\n".join(lines[-n:])


def _csv_history() -> bytes:
    """발행 이력 CSV. 엑셀 한글 깨짐 방지 위해 UTF-8 BOM."""
    state = scheduler._load_state()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["날짜", "시각", "초안", "모드", "이미지", "SEO점수", "결과", "사유", "URL"])
    for x in state.get("log", []):
        w.writerow([x.get("date", ""), x.get("time", ""), x.get("draft", ""),
                    "dry-run" if x.get("dry") else "실제", x.get("images", ""),
                    x.get("seo_score", ""), "성공" if x.get("ok") else "미발행",
                    x.get("reason", "") or "", x.get("url", "") or ""])
    return b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")


def _csv_metrics() -> bytes:
    """방문자 + 키워드 순위 시계열 CSV."""
    m = _load_json(ROOT / "data" / "metrics.json", {})
    buf = io.StringIO()
    w = csv.writer(buf)
    # 방문자
    w.writerow(["[방문자]"])
    w.writerow(["날짜", "오늘", "전체"])
    for d in sorted(m.get("visitors", {})):
        v = m["visitors"][d]
        w.writerow([d, v.get("today", ""), v.get("total", "")])
    w.writerow([])
    # 키워드 순위(날짜 x 키워드)
    ranks = m.get("ranks", {})
    kws = sorted({k for day in ranks.values() for k in day})
    if kws:
        w.writerow(["[키워드 순위]"])
        w.writerow(["날짜"] + kws)
        for d in sorted(ranks):
            w.writerow([d] + [ranks[d].get(k, "") for k in kws])
    return b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")


def _load_research() -> dict:
    """data/research/*.json → {키워드: 분석}."""
    out = {}
    d = ROOT / "data" / "research"
    if d.exists():
        for f in d.glob("*.json"):
            try:
                r = json.loads(f.read_text(encoding="utf-8"))
                out[r.get("keyword", f.stem)] = r
            except Exception:
                pass
    return out


# ---------- 공통 HTML 조각 ----------

def _alerts_html(d) -> str:
    e = html.escape
    alerts = []
    blocked = [t["name"] for t in d["tasks"] if t.get("battery_block")]
    if blocked:
        alerts.append("배터리 전원에서 실행이 차단된 작업이 있습니다 — 노트북이 충전기에 꽂혀있지 "
                      f"않으면 발행되지 않습니다: <b>{e(', '.join(blocked))}</b>")
    sv = _load_json(ROOT / "data" / "metrics.json", {}).get("session")
    if sv and sv.get("ok") is False:
        alerts.append("<b>네이버 세션이 만료됐습니다</b> — 자동 발행이 실패합니다. 터미널에서 "
                      "<code>.venv\\Scripts\\python.exe -m publish.naver login</code> 실행 후 재로그인하세요.")
    if d.get("last_fail"):
        f = d["last_fail"]
        rtext, _ = _reason(f.get("reason"))
        alerts.append(f"마지막 실제 발행이 실패했습니다 — <b>{e(str(f.get('draft','')))}</b> "
                      f"({e(str(f.get('date','')))} {e(str(f.get('time','')))}): {e(rtext)}"
                      + ("<br>→ 터미널에서 <code>.\\.venv\\Scripts\\python.exe -m publish.naver login</code> 실행"
                         if f.get("reason") == "session_expired" else ""))
    for t in d["tasks"]:
        if t["level"] == "bad" and not t.get("battery_block"):
            alerts.append(f"작업 <b>{e(t['name'])}</b>: {e(t['result'])}")
    if d["total"] - d["done"] == 0:
        alerts.append("<b>큐가 비었습니다</b> — 더 이상 발행할 초안이 없습니다.")
    elif d["days_left"] <= 3:
        alerts.append(f"초안이 <b>{d['total'] - d['done']}편</b>({d['days_left']}일치)만 남았습니다 — 곧 발행이 멈춥니다.")
    if d["images"]["pool"] == 0 and d["images"]["inbox"] == 0:
        alerts.append("실물 사진 풀이 비어 있습니다 — 글마다 인포그래픽 1장만 삽입됩니다. "
                      "<code>drafts/photos/</code>에 사진을 넣으면 자동으로 섞여 들어갑니다.")
    for w in d.get("weak_drafts", []):
        alerts.append(f"SEO 점수 낮은 대기글: <b>{e(w['file'])}</b> ({w['grade']} {w['score']}점) — "
                      "발행 전 제목·본문·키워드 보강 권장")
    return "".join(f'<div class="alert">{a}</div>' for a in alerts)


def _kpi_cards(d) -> str:
    e = html.escape
    m = d.get("metrics", {})
    cum = m.get("cum_total")
    return f"""
    <div class="cards">
      <div class="card"><div class="k">오늘 발행</div><div class="v">{d['today_ok']} <small>/ {d['max_per_day']}</small></div></div>
      <div class="card"><div class="k">발행 완료</div><div class="v">{d['done']} <small>/ {d['total']}편</small></div></div>
      <div class="card"><div class="k">남은 초안</div><div class="v">{d['total'] - d['done']} <small>편 · 약 {d['days_left']}일치</small></div></div>
      <div class="card"><div class="k">누적 방문자</div><div class="v">{cum if cum is not None else '-'}</div></div>
      <div class="card"><div class="k">1페이지 노출</div><div class="v">{m.get('kw_on_page1', 0)} <small>/ {m.get('kw_tracked', 0)}개</small></div></div>
      <div class="card"><div class="k">대상 블로그</div><div class="v" style="font-size:16px">{e(d['blog'])}</div></div>
    </div>"""


def _tasks_table(d) -> str:
    e = html.escape
    trows = "".join(
        f"<tr><td>{e(t['name'])}</td><td>{e(t['next'])}</td><td>{e(t['last'])}</td>"
        f"<td><span class='badge {t['level']}'>{e(t['result'])}</span></td></tr>"
        for t in d["tasks"])
    return ("<table><tr><th>작업</th><th>다음 실행</th><th>마지막 실행</th>"
            f"<th>마지막 결과</th></tr>{trows}</table>")


def _queue_table(d, link=True) -> str:
    e = html.escape
    qrows = ""
    for q in d["queue"]:
        label, cls = STATUS_BADGE[q["status"]]
        seo = q.get("seo")
        sb = ""
        if seo:
            gc = {"A": "ok", "B": "next", "C": "warn", "D": "bad"}.get(seo["grade"], "mut")
            sb = f"<span class='badge {gc}'>{seo['grade']} {seo['score']}</span>"
        name = (f"<a href='/post?file={e(q['name'])}'>{e(q['name'])}</a>" if link
                else e(q["name"]))
        qrows += (f"<tr><td>{q['i']}</td><td><span class='badge {cls}'>{label}</span></td>"
                  f"<td>{name}</td><td>{e(q['title'])}</td><td>{sb}</td></tr>")
    return ("<div class=scroll><table><tr><th>#</th><th>상태</th><th>파일</th>"
            f"<th>제목</th><th>SEO</th></tr>{qrows}</table></div>")


def _log_table(d) -> str:
    e = html.escape
    lrows = ""
    for x in d["log"]:
        rtext, rcls = _reason(x.get("reason"))
        planned = x.get("planned_images")
        img = f"{x.get('images', 0)}장"
        if planned is not None and x.get("images", 0) < planned:
            img = f"<span class='badge warn'>{x.get('images',0)}/{planned}장</span>"
        title = f"<a href='{e(str(x['url']))}' target='_blank'>{e(str(x.get('draft','-')))}</a>" \
            if x.get("url") else e(str(x.get("draft", "-")))
        sg = x.get("seo_grade")
        if sg:
            gcls = {"A": "ok", "B": "next", "C": "warn", "D": "bad"}.get(sg, "mut")
            seo_cell = f"<span class='badge {gcls}'>{sg} {x.get('seo_score')}</span>"
        else:
            seo_cell = "-"
        lrows += (f"<tr><td>{e(str(x.get('date','-')))} {e(str(x.get('time','')))}</td>"
                  f"<td>{title}</td>"
                  f"<td>{'dry-run' if x.get('dry') else '실제'}</td><td>{img}</td><td>{seo_cell}</td>"
                  f"<td><span class='badge {'ok' if x.get('ok') else 'mut'}'>"
                  f"{'성공' if x.get('ok') else '미발행'}</span>"
                  + (f" <span class='badge {rcls}'>{e(rtext)}</span>" if rtext else "")
                  + "</td></tr>")
    lrows = lrows or "<tr><td colspan=6>기록 없음</td></tr>"
    return ("<table><tr><th>날짜</th><th>초안</th><th>모드</th><th>이미지</th>"
            f"<th>SEO</th><th>결과</th></tr>{lrows}</table>")


def _shots_html(d, shot_base) -> str:
    e = html.escape
    return "".join(
        f'<figure><img src="{shot_base}{e(s)}" alt="{e(s)}"><figcaption>{e(s)}</figcaption></figure>'
        for s in d["shots"]) or "<p class='muted'>스크린샷 없음</p>"


def _next_seo_panel(d) -> str:
    e = html.escape
    ns = d.get("next_seo")
    if not ns:
        return ""
    gcls = {"A": "ok", "B": "next", "C": "warn", "D": "bad"}.get(ns["grade"], "mut")
    weak = ("<div class='muted' style='margin-top:6px'>보강 포인트: "
            + ", ".join(_SEO_LABEL.get(w, w) for w in ns["weak"]) + "</div>") if ns.get("weak") else \
           "<div class='muted' style='margin-top:6px'>모든 항목 통과</div>"
    ci = ns.get("competitor")
    comp_html = ""
    if ci:
        lg = ci.get("length_gap")
        lentxt = ""
        if ci.get("length_benchmark"):
            lentxt = (f"경쟁 글 길이 중앙값 {ci['length_benchmark']}자"
                      + (f" · 우리가 {abs(lg)}자 {'짧음 → 보강 권장' if lg and lg > 0 else '김'}"
                         if lg else ""))
        miss = (" · 보강 후보: " + ", ".join(ci["missing_terms"])) if ci.get("missing_terms") else ""
        comp_html = f"<div class='muted' style='margin-top:6px'>경쟁 분석: {e(lentxt)}{e(miss)}</div>"
    return (f"<div class='panel'><div class='ptit'>다음 발행글 SEO 점검</div>"
            f"<div><span class='badge {gcls}'>{ns['grade']} {ns['score']}점</span> "
            f"&nbsp;{e(ns['title'])}</div>{weak}{comp_html}</div>")


def _metrics_section(d) -> str:
    e = html.escape
    m = d.get("metrics", {})
    total_bars = _svg_bars(m.get("series", []), "total", "#4a7dff")
    new_bars = _svg_bars(m.get("series", []), "new", "#5ddb8f")
    krows = "".join(
        f"<tr><td>{e(row['kw'])}</td><td>{_rank_badge(row['rank'], row['delta'])}</td></tr>"
        for row in m.get("kw_rows", []))
    krows = krows or "<tr><td colspan=2 class='muted'>발행글이 색인되면 순위가 표시됩니다</td></tr>"
    when = f" · {e(m['rank_date'])} 기준" if m.get("rank_date") else ""
    return f"""
<h2>방문자 추이</h2>
<div class="panel"><div class="ptit">일별 누적 방문자</div>{total_bars}</div>
<div class="panel"><div class="ptit">일별 신규 방문자(누적 증가분)</div>{new_bars}</div>
<h2>타깃 키워드 검색 순위 (네이버 블로그탭){when}</h2>
<table><tr><th>타깃 검색어</th><th>우리 글 순위 (전일 대비)</th></tr>{krows}</table>
<p class="muted">순위는 발행 후 자동 색인·수집됩니다. 검색량(월간 검색수)은 네이버 검색광고 API 키가 있으면 추가할 수 있습니다.</p>"""


# ---------- 페이지 ----------

def page_overview(d) -> str:
    return (_alerts_html(d) + _kpi_cards(d) + _next_seo_panel(d)
            + "<h2>다음 자동 실행</h2>" + _tasks_table(d))


_SLOT_TIMES = [(9, 7), (13, 23), (18, 41)]   # 작업 스케줄러 트리거(±0~9분 지터)


def _upcoming_slots(count: int, today_used: int, cap: int):
    """앞으로의 발행 예정 시각 count개. 오늘 남은 슬롯(미래 시각·상한 내)부터."""
    now = datetime.now()
    slots = []
    day = 0
    while len(slots) < count and day < 60:
        base = now.date() + timedelta(days=day)
        used_today = today_used if day == 0 else 0
        room = cap - used_today
        for h, mi in _SLOT_TIMES:
            if room <= 0:
                break
            dt = datetime(base.year, base.month, base.day, h, mi)
            if dt <= now:            # 이미 지난 시각은 건너뜀
                continue
            slots.append(dt)
            room -= 1
            if len(slots) >= count:
                break
        day += 1
    return slots


def page_calendar(d) -> str:
    e = html.escape
    pending = [q for q in d["queue"] if q["status"] in ("next", "wait")]
    slots = _upcoming_slots(len(pending), d["today_ok"], max(d["max_per_day"], 1))
    rows, last_day = "", None
    for q, dt in zip(pending, slots):
        day = f"{dt:%Y-%m-%d (%a)}"
        if day != last_day:
            rows += (f"<tr class='dayrow'><td colspan=4>{e(day)}</td></tr>")
            last_day = day
        seo = q.get("seo") or {}
        sb = ""
        if seo:
            gc = {"A": "ok", "B": "next", "C": "warn", "D": "bad"}.get(seo["grade"], "mut")
            sb = f"<span class='badge {gc}'>{seo['grade']} {seo['score']}</span>"
        rows += (f"<tr><td>{dt:%H:%M}</td>"
                 f"<td><a href='/post?file={e(q['name'])}'>{e(q['name'])}</a></td>"
                 f"<td>{e(q['title'])}</td><td>{sb}</td></tr>")
    if not rows:
        rows = "<tr><td colspan=4 class='muted'>발행할 초안이 없습니다</td></tr>"
    note = (f"<p class='muted'>하루 {d['max_per_day']}편 · 발행 시각 09:07 / 13:23 / 18:41 "
            "(± 0~9분 랜덤). 오늘 남은 슬롯부터 순서대로 배정한 예상입니다 "
            "(실패 시 밀리며, 새 초안·상한 변경 시 달라집니다).</p>")
    return (f"<h2>발행 예정 캘린더 (남은 {len(pending)}편)</h2>{note}"
            f"<div class='scroll'><table><tr><th>시각</th><th>초안</th><th>제목</th>"
            f"<th>SEO</th></tr>{rows}</table></div>")


_refresh = {"running": False, "msg": ""}


def _refresh_metrics_bg():
    """방문자·순위·세션을 즉시 재수집(백그라운드 스레드)."""
    try:
        import metrics
        metrics.collect(force_ranks=True)
        _refresh["msg"] = "완료"
    except Exception as ex:
        _refresh["msg"] = f"실패: {ex}"
    finally:
        _refresh["running"] = False
        _cache["data"] = None


def page_growth(d) -> str:
    e = html.escape
    try:
        import growth
        segs = growth.segment_scores()
        w = growth.load_weights()
        q = growth.rank_queue()
        nxt = growth.next_draft()
        tlog = growth._load(growth.GLOG, {"tune": []}).get("tune", [])
    except Exception as ex:
        return f"<h2>성장 엔진</h2><p class='alert'>엔진 로드 실패: {e(str(ex))}</p>"

    seg_name = {"a": "방송 피켓", "b": "클럽·버킷", "c": "간판"}
    seg_cards = "".join(
        f"<div class='card'><div class='k'>{seg_name[s]} 성과</div>"
        f"<div class='v'>{segs[s]:.2f}</div></div>" for s in "abc")
    wrow = " · ".join(f"{k} {w[k]:.2f}" for k in growth.DEFAULT_WEIGHTS)

    qrows = ""
    for r in q[:15]:
        b = r["breakdown"]
        star = " ⭐" if r["name"] == nxt else ""
        qrows += (f"<tr><td>{r['score']:.3f}{star}</td><td>[{r['seg']}]</td>"
                  f"<td><a href='/post?file={e(r['name'])}'>{e(r['name'])}</a></td>"
                  f"<td class='muted'>{e(r['keyword'])}</td>"
                  f"<td class='muted'>세그{b['seg']} · SEO{b['seo']} · 탐색{b['explore']} · 폭{b['diversity']}</td></tr>")
    tune_html = ""
    if tlog:
        rows = "".join(
            f"<tr><td>{e(str(t.get('date','')))}</td><td>{t.get('samples','')}</td>"
            f"<td class='muted'>" + " · ".join(f"{k} {v}" for k, v in t.get('weights', {}).items())
            + "</td></tr>" for t in tlog[-8:])
        tune_html = ("<h2>자가 튜닝 이력</h2><table><tr><th>날짜</th><th>샘플</th>"
                     f"<th>보정된 가중치</th></tr>{rows}</table>")

    return (
        "<div class='dlbar'>이 엔진은 키워드 순위·방문자 데이터로 <b>어떤 글을 먼저 발행할지</b>를 "
        "정하고, 결과를 보고 스스로 가중치를 보정합니다. 없는 방문자를 만드는 게 아니라 "
        "<b>이길 수 있는 주제에 힘을 몰아주는</b> 최적화입니다.</div>"
        + "<h2>세그먼트 성과 (관측, 1=최고)</h2>"
        + f"<div class='cards'>{seg_cards}</div>"
        + f"<div class='panel'><div class='ptit'>현재 가중치 (자가 튜닝됨)</div><div>{wrow}</div>"
        + f"<div class='muted' style='margin-top:6px'>다음 추천 발행: <b>{e(str(nxt))}</b> "
        + "(⭐ 표시). 세그먼트 3연속은 자동 회피.</div></div>"
        + "<h2>발행 우선순위 (성장 점수순)</h2>"
        + "<div class=scroll><table><tr><th>점수</th><th>세그</th><th>초안</th>"
        + f"<th>키워드</th><th>점수 근거</th></tr>{qrows}</table></div>"
        + tune_html
        + "<p class='muted'>데이터가 쌓일수록 세그먼트 성과가 갈리고, 엔진이 잘 되는 주제를 "
        + "우선합니다. 지금은 발행글이 적어 SEO·폭 위주로 정렬됩니다.</p>")


def page_analytics(d) -> str:
    dl = ("<div class='dlbar'>클라이언트 보고용 내보내기: "
          "<a href='/export/metrics.csv'>방문자·순위 CSV</a> · "
          "<a href='/export/history.csv'>발행 이력 CSV</a> "
          "<span class='muted'>(엑셀에서 바로 열림)</span></div>")
    if _refresh["running"]:
        act = ("<div class='dlbar'>⏳ 지표 수집 중… 30~60초 걸립니다. "
               "잠시 후 새로고침하면 최신 순위가 반영됩니다.</div>")
    else:
        last = f" <span class='muted'>({_refresh['msg']})</span>" if _refresh["msg"] else ""
        act = ("<div class='dlbar'>지금 최신 데이터 가져오기: "
               "<form method='POST' action='/refresh-metrics' style='display:inline'>"
               f"<button type='submit'>순위·방문자 새로고침</button></form>{last}</div>")
    return _kpi_cards(d) + dl + act + _metrics_section(d)


def page_posts(d) -> str:
    return (f"<h2>발행 큐 ({d['done']}/{d['total']})</h2>" + _queue_table(d)
            + "<h2>최근 발행 기록 "
            + "<a class='dl' href='/export/history.csv'>⬇ CSV</a></h2>" + _log_table(d))


def page_seo(d) -> str:
    e = html.escape
    rows = ""
    for q in sorted(d["queue"], key=lambda x: (x.get("seo") or {}).get("score", 999)):
        seo = q.get("seo") or {}
        if not seo:
            continue
        gc = {"A": "ok", "B": "next", "C": "warn", "D": "bad"}.get(seo["grade"], "mut")
        weak = ", ".join(_SEO_LABEL.get(w, w) for w in seo.get("weak", [])) or "—"
        st = STATUS_BADGE[q["status"]][0]
        rows += (f"<tr><td><a href='/post?file={e(q['name'])}'>{e(q['name'])}</a></td>"
                 f"<td><span class='badge {gc}'>{seo['grade']} {seo['score']}</span></td>"
                 f"<td class='muted'>{st}</td><td class='muted'>{e(weak)}</td></tr>")
    # 경쟁 분석(저장된 research)
    comp = ""
    for kw, r in _load_research().items():
        gaps = ", ".join(r.get("gap_terms", [])) or "—"
        b = r.get("length_benchmark")
        comp += (f"<tr><td>{e(kw)}</td><td class='muted'>{len(r.get('competitors', []))}편</td>"
                 f"<td class='muted'>{b if b else '-'}자</td><td class='muted'>{e(gaps)}</td></tr>")
    comp = comp or "<tr><td colspan=4 class='muted'>아직 분석 데이터 없음(발행 시 자동 수집)</td></tr>"
    return (_next_seo_panel(d)
            + "<h2>전체 초안 SEO 점수 (낮은 순)</h2>"
            + "<div class=scroll><table><tr><th>초안</th><th>점수</th><th>상태</th>"
            + f"<th>보강 포인트</th></tr>{rows}</table></div>"
            + "<h2>경쟁 글 분석 (키워드별)</h2>"
            + "<table><tr><th>타깃 검색어</th><th>경쟁글</th><th>길이중앙값</th>"
            + f"<th>보강 후보 단어</th></tr>{comp}</table>")


def _session_view(d):
    return _load_json(ROOT / "data" / "metrics.json", {}).get("session")


def run_diagnostics(d) -> list[dict]:
    """운영 상태 일괄 점검. [{name, level(ok/warn/bad), detail, fix}]."""
    checks = []

    def add(name, level, detail, fix=""):
        checks.append({"name": name, "level": level, "detail": detail, "fix": fix})

    # 1) 대상 블로그 설정
    if config.NAVER_BLOG_ID:
        add("대상 블로그", "ok", config.NAVER_BLOG_ID)
    else:
        add("대상 블로그", "bad", "NAVER_BLOG_ID 미설정", ".env 에 NAVER_BLOG_ID 지정")

    # 2) 세션
    sv = _session_view(d)
    if sv is None:
        add("네이버 세션", "warn", "점검 데이터 없음(다음 수집 시 생성)")
    elif sv.get("ok"):
        add("네이버 세션", "ok", f"정상 ({sv.get('checked','')})")
    else:
        add("네이버 세션", "bad", "만료됨", "python -m publish.naver login")

    # 3) 작업 스케줄러
    bad_tasks = [t for t in d["tasks"] if t["level"] == "bad"]
    batt = [t["name"] for t in d["tasks"] if t.get("battery_block")]
    if batt:
        add("자동 실행 작업", "bad", f"배터리 차단: {', '.join(batt)}", "작업 설정에서 배터리 조건 해제")
    elif bad_tasks:
        add("자동 실행 작업", "warn", f"{len(bad_tasks)}개 경고 상태")
    else:
        add("자동 실행 작업", "ok", f"{len(d['tasks'])}개 정상")

    # 4) 초안 파싱
    fails, thin = [], []
    for p in scheduler._ordered_drafts():
        try:
            pd = parse_draft(p)
            if not pd["title"] or pd["title"] == "제목 없음":
                fails.append(p.name)
        except Exception:
            fails.append(p.name)
    if fails:
        add("초안 파싱", "bad", f"{len(fails)}개 실패: {', '.join(fails[:3])}")
    else:
        add("초안 파싱", "ok", f"{d['total']}편 정상")

    # 5) 큐 잔량
    left = d["total"] - d["done"]
    if left == 0:
        add("발행 큐", "bad", "비었음 — 발행 멈춤", "초안 추가 필요")
    elif d["days_left"] <= 3:
        add("발행 큐", "warn", f"{left}편(약 {d['days_left']}일치) 남음")
    else:
        add("발행 큐", "ok", f"{left}편(약 {d['days_left']}일치)")

    # 6) 이미지 풀(세그먼트별)
    img = d["images"]
    pool = [p for p in imgmod._imgs(imgmod.PHOTO_DIR) if p.parent == imgmod.PHOTO_DIR]
    segs = {s: sum(1 for p in pool if p.name[:2] == f"{s}_") for s in "abc"}
    missing = [s for s in "abc" if segs[s] == 0]
    if img["pool"] == 0 and img["inbox"] == 0:
        add("사진 풀", "warn", "비었음 — 인포그래픽만 삽입", "이미지 탭에서 업로드")
    elif missing:
        add("사진 풀", "warn", f"세그먼트 {','.join(missing)} 사진 없음 (a{segs['a']}/b{segs['b']}/c{segs['c']})")
    else:
        add("사진 풀", "ok", f"a{segs['a']} · b{segs['b']} · c{segs['c']} · 인박스{img['inbox']}")

    # 7) 상태 파일
    ok_state = (ROOT / "data" / "publish_state.json").exists()
    add("상태 파일", "ok" if ok_state else "warn",
        "publish_state.json 정상" if ok_state else "아직 없음(첫 발행 시 생성)")

    # 8) 강조 포인트(정보)
    emph = config.load_emphasis()
    add("강조 포인트", "ok" if emph else "warn",
        f"{len(emph)}개 설정됨" if emph else "미설정(선택)",
        "" if emph else "설정 탭에서 입력(선택)")

    return checks


def page_diag(d) -> str:
    e = html.escape
    checks = run_diagnostics(d)
    n_bad = sum(1 for c in checks if c["level"] == "bad")
    n_warn = sum(1 for c in checks if c["level"] == "warn")
    if n_bad:
        head = f"<div class='alert'>🔴 문제 {n_bad}건 — 조치가 필요합니다.</div>"
    elif n_warn:
        head = f"<div class='okbar' style='background:#3a2e12;border-color:#5c4a1e;color:#f0c463'>🟡 주의 {n_warn}건 · 심각 문제 없음</div>"
    else:
        head = "<div class='okbar'>✅ 모든 점검 통과 — 정상 가동 중</div>"
    rows = ""
    lbl = {"ok": ("정상", "ok"), "warn": ("주의", "warn"), "bad": ("문제", "bad")}
    for c in checks:
        t, cls = lbl[c["level"]]
        fix = f"<span class='muted'> · {e(c['fix'])}</span>" if c["fix"] else ""
        rows += (f"<tr><td>{e(c['name'])}</td><td><span class='badge {cls}'>{t}</span></td>"
                 f"<td>{e(c['detail'])}{fix}</td></tr>")
    return (head + "<h2>운영 진단</h2>"
            + f"<table><tr><th>항목</th><th>상태</th><th>상세</th></tr>{rows}</table>"
            + "<p class='muted'>이 페이지는 열 때마다 다시 점검합니다. 문제 항목의 조치를 따르세요.</p>")


def page_ops(d) -> str:
    e = html.escape
    last_ok = next((x for x in d["log"] if x.get("ok") and not x.get("dry")), None)
    sess = (f"마지막 발행 성공: {e(str(last_ok.get('date')))} {e(str(last_ok.get('time','')))}"
            if last_ok else "아직 성공 발행 없음")
    sv = _session_view(d)
    if sv:
        badge = ("<span class='badge ok'>세션 정상</span>" if sv.get("ok")
                 else "<span class='badge bad'>세션 만료 — 재로그인 필요</span>")
        sess_line = f"{badge} <span class='muted'>({e(str(sv.get('checked','')))} 점검)</span><br>"
    else:
        sess_line = "<span class='muted'>세션 점검 데이터 없음(다음 수집 시 생성)</span><br>"
    health = (f"<div class='panel'><div class='ptit'>세션·상태</div>"
              f"<div>{sess_line}{sess}</div>"
              f"<div class='muted' style='margin-top:4px'>세션 만료 시: "
              f"<code>.venv\\Scripts\\python.exe -m publish.naver login</code></div></div>")
    return (_alerts_html(d)
            + "<h2>자동 실행 스케줄</h2>" + _tasks_table(d)
            + "<h2>상태</h2>" + health
            + "<h2>마지막 실행 화면</h2><div class='shots'>" + _shots_html(d, "/shot/") + "</div>"
            + "<h2>실행 로그 (최근)</h2><pre class='logbox'>" + e(_log_tail(40)) + "</pre>")


def page_settings(d) -> str:
    e = html.escape
    pts = config.load_emphasis()
    txt = "\n".join(pts)
    times = "09:07 / 13:23 / 18:41 (± 0~9분 랜덤)"
    return f"""
<div class="panel"><div class="ptit">주요 강조 포인트</div>
<p class="muted" style="margin:0 0 8px">모든 글의 맺음말(연락처) 바로 위에 <b>✅ 굵은 줄</b>로 들어갑니다.
한 줄에 하나씩. 우리 사업의 핵심 셀링포인트를 적으세요(예: 당일 상담·견적 / 전국 제작·배송 / 10년 노하우). 최대 6개.</p>
<form method="POST" action="/save-emphasis">
  <textarea name="points" rows="7" class="ta" placeholder="당일 상담·견적 가능&#10;전국 제작·배송&#10;맞춤 디자인 무료 시안">{e(txt)}</textarea>
  <div style="margin-top:10px"><button type="submit">저장</button>
  <span class="muted">저장 즉시 다음 발행 글부터 반영됩니다.</span></div>
</form>
</div>
<h2>현재 운영 설정 (참고)</h2>
<table>
<tr><th>항목</th><th>값</th><th>바꾸는 곳</th></tr>
<tr><td>대상 블로그</td><td>{e(d['blog'])}</td><td class='muted'>.env NAVER_BLOG_ID</td></tr>
<tr><td>하루 발행 수</td><td>{d['max_per_day']}편</td><td class='muted'>.env MAX_POSTS_PER_DAY</td></tr>
<tr><td>발행 시각</td><td>{times}</td><td class='muted'>작업 스케줄러 SNS-Agent-1/2/3</td></tr>
</table>
<p class="muted">강조 포인트는 data/emphasis.json 에 저장됩니다. 비워두면 아무것도 삽입되지 않습니다.</p>
<div class="panel"><div class="ptit">실패 알림 (Windows 트레이)</div>
<p class="muted" style="margin:0 0 8px">발행이 실패하면(세션 만료·이미지 실패 등) 트레이 풍선 알림이 뜹니다.
아래 버튼으로 지금 알림이 정상 작동하는지 확인하세요.</p>
<form method="POST" action="/test-notify"><button type="submit">테스트 알림 보내기</button></form>
</div>"""


_SEG_NAME = {"a": "방송 피켓", "b": "클럽·버킷", "c": "간판"}


def _photo_grid(files, base="/photo/") -> str:
    e = html.escape
    if not files:
        return "<p class='muted'>없음</p>"
    cells = "".join(
        f"<figure class='ph'><img src='{base}{e(f.name)}' loading='lazy' alt='{e(f.name)}'>"
        f"<figcaption>{e(f.name)}</figcaption></figure>" for f in files)
    return f"<div class='phgrid'>{cells}</div>"


def page_images(d) -> str:
    e = html.escape
    pool = [p for p in imgmod._imgs(imgmod.PHOTO_DIR) if p.parent == imgmod.PHOTO_DIR]
    inbox = imgmod._imgs(imgmod.INBOX_DIR)
    infos = [p for p in imgmod._imgs(imgmod.IMG_DIR)]
    by_seg = {"a": [], "b": [], "c": [], "?": []}
    for p in pool:
        by_seg.get(p.name[0] if p.name[:1] in "abc" else "?", by_seg["?"]).append(p)

    guide = f"""
<div class="panel"><div class="ptit">새 사진 추가하는 법</div>
<ol style="margin:6px 0 0;padding-left:20px;line-height:1.9">
<li><b>여기서 바로 업로드</b> — 아래 업로드 박스에 사진을 끌어다 놓거나 선택하세요.
    세그먼트를 고르면 파일명 앞에 <code>a_/b_/c_</code>가 붙어 해당 주제 글에만 쓰입니다.</li>
<li>업로드한 사진은 <b>인박스</b>로 들어가 <b>다음 발행 글에 우선</b> 사용되고, 쓰인 뒤 보관됩니다.</li>
<li>폴더로 직접 넣어도 됩니다 — 우선순위 사진: <code>drafts/photos/inbox/</code>,
    평소 재활용 풀: <code>drafts/photos/</code></li>
</ol>
<div class="muted" style="margin-top:8px">세그먼트: a=방송 피켓 · b=클럽/버킷 · c=간판. 원본 실물 사진만(AI 생성 제품컷 금지).</div>
</div>

<form class="upbox" method="POST" action="/upload" enctype="multipart/form-data">
  <div class="ptit">사진 업로드 → 인박스(다음 글 우선 사용)</div>
  <label>세그먼트(주제):
    <select name="segment">
      <option value="">자동(파일명 유지)</option>
      <option value="a">a · 방송 피켓</option>
      <option value="b">b · 클럽·버킷</option>
      <option value="c">c · 간판</option>
    </select>
  </label>
  <input type="file" name="file" accept="image/*" multiple required>
  <button type="submit">업로드</button>
  <div class="muted" style="margin-top:6px">여러 장 선택 가능. 업로드 후 이 페이지로 돌아옵니다.</div>
</form>"""

    inbox_html = (f"<h2>인박스 — 다음 글에 우선 사용 ({len(inbox)}장)</h2>"
                  + _photo_grid(inbox, "/photo/"))
    seg_html = ""
    for s in ("a", "b", "c", "?"):
        fs = by_seg[s]
        if not fs:
            continue
        seg_html += f"<h3 class='seg'>{_SEG_NAME.get(s, '미분류')} · {len(fs)}장</h3>" + _photo_grid(fs)
    info_html = ("<h2>강조 인포그래픽 (자동 생성, 글마다 1장)</h2>"
                 + _photo_grid(infos, "/photo/"))
    return (guide
            + inbox_html
            + f"<h2>사진 풀 — 순환 재활용 ({len(pool)}장)</h2>" + seg_html
            + info_html)


def page_post_detail(d, fname: str) -> str:
    e = html.escape
    fname = Path(fname).name
    p = ROOT / "drafts" / fname
    if p.suffix != ".md" or not p.is_file():
        return "<h2>초안을 찾을 수 없습니다</h2><p><a href='/posts'>← 발행</a></p>"
    try:
        parsed = parse_draft(p)
    except Exception as ex:
        return f"<h2>파싱 실패</h2><p class='muted'>{e(str(ex))}</p>"
    try:
        import seo as seomod
        sr = seomod.score_draft(p)
    except Exception:
        sr = None
    # 이 글에 실제로 들어갈 이미지 미리보기(회전 인덱스 건드리지 않음)
    n_slot = sum(1 for b in parsed["blocks"] if b["kind"] == "image")
    imgs_html = ""
    try:
        picks, _ = imgmod.pick_images(p, n_slot, advance=False)
        cells = ""
        for pth in picks:
            kind = "인포그래픽" if pth.parent == imgmod.IMG_DIR else (
                "인박스" if pth.parent == imgmod.INBOX_DIR else "사진풀")
            cells += (f"<figure class='ph'><img src='/photo/{e(pth.name)}' loading='lazy'>"
                      f"<figcaption>{kind}<br>{e(pth.name)}</figcaption></figure>")
        imgs_html = (f"<h2>이 글에 들어갈 이미지 ({len(picks)}/{n_slot}장)</h2>"
                     f"<div class='phgrid'>{cells}</div>"
                     "<p class='muted'>인박스에 새 사진을 올리면 여기 우선 반영됩니다 "
                     "(<a href='/images'>이미지 관리</a>).</p>")
    except Exception:
        pass

    emphasis = config.load_emphasis()
    emph_html = ("<div class='emph'>" + "<br>".join(f"✅ {e(p)}" for p in emphasis) + "</div>") if emphasis else ""
    body = ""
    for b in parsed["blocks"]:
        if b["kind"] == "heading":
            body += f"<h4>{e(b['text'])}</h4>"
        elif b["kind"] == "image":
            body += f"<div class='imgslot'>🖼 이미지 — {e(b.get('alt') or '(캡션 없음)')}</div>"
        else:
            if b["text"].startswith("👉") and emph_html:
                body += emph_html            # CTA 직전에 강조 포인트(발행 시와 동일 위치)
                emph_html = ""
            body += f"<p>{e(b['text'])}</p>"
    body += emph_html   # CTA 가 없던 경우 맨 끝에
    tags = " ".join(f"#{e(t)}" for t in parsed["tags"])
    seo_html = ""
    if sr:
        gc = {"A": "ok", "B": "next", "C": "warn", "D": "bad"}.get(sr["grade"], "mut")
        checks = "".join(
            f"<tr><td>{_SEO_LABEL.get(c['name'], c['name'])}</td>"
            f"<td>{c['pts']}/{c['max']}</td><td class='muted'>{e(c['detail'])}</td></tr>"
            for c in sr["checks"])
        seo_html = (f"<h2>SEO <span class='badge {gc}'>{sr['grade']} {sr['score']}점</span></h2>"
                    f"<table><tr><th>항목</th><th>점수</th><th>상세</th></tr>{checks}</table>")
    return (f"<p><a href='/posts'>← 발행</a></p><h2>{e(parsed['title'])}</h2>"
            f"<div class='muted'>{e(fname)} · 태그: {tags}</div>"
            f"{seo_html}{imgs_html}<h2>본문 미리보기</h2><div class='preview'>{body}</div>")


PAGES = {
    "/": ("개요", page_overview),
    "/analytics": ("성과", page_analytics),
    "/growth": ("성장엔진", page_growth),
    "/posts": ("발행", page_posts),
    "/calendar": ("캘린더", page_calendar),
    "/seo": ("콘텐츠·SEO", page_seo),
    "/images": ("이미지", page_images),
    "/settings": ("설정", page_settings),
    "/diag": ("진단", page_diag),
    "/ops": ("상태", page_ops),
}


def _nav(active: str) -> str:
    return "<div class='nav'>" + "".join(
        f"<a href='{path}' class='{'on' if path == active else ''}'>{label}</a>"
        for path, label in NAV) + "</div>"


def layout(active: str, body: str, live: bool, stamp: str, title="개요") -> str:
    refresh = '<meta http-equiv="refresh" content="30">' if live else ""
    sub = ("30초마다 자동 새로고침 · 읽기 전용" if live
           else f"고정 스냅샷 ({stamp} 기준)")
    quit_btn = ("""<button id="quit" onclick="fetch('/quit',{method:'POST'})
      .then(()=>document.body.innerHTML='<div class=wrap><h1>종료했습니다</h1>'
      +'<div class=sub>이 탭은 닫으셔도 됩니다.</div></div>')">■ 종료</button>""" if live else "")
    nav = _nav(active) if live else ""
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<title>SNS Agent · {title}</title>{refresh}
<style>{CSS}</style></head><body><div class="wrap">
<div class="top"><div><h1>SNS Agent 대시보드</h1><div class="sub">{sub}</div></div>{quit_btn}</div>
{nav}{body}
<div class="foot">상태 파일: data/publish_state.json · 지표: data/metrics.json</div>
</div></body></html>"""


def render(d: dict, shot_base: str = "/shot/", page: str = "/", query: dict | None = None) -> str:
    """페이지 라우터. page 경로에 맞는 본문을 만들어 공통 레이아웃으로 감싼다."""
    live = shot_base == "/shot/"
    if page == "/post":
        fname = (query or {}).get("file", "")
        body = page_post_detail(d, fname)
        return layout("/posts", body, live, d["stamp"], title="글 미리보기")
    label, fn = PAGES.get(page, PAGES["/"])
    body = fn(d)
    if page == "/images" and query:
        if query.get("ok"):
            body = (f"<div class='okbar'>✅ 사진 {html.escape(query['ok'])}장을 인박스에 올렸습니다 "
                    "— 다음 발행 글부터 우선 사용됩니다.</div>") + body
        elif query.get("err"):
            body = "<div class='alert'>업로드 실패 — 이미지 파일인지 확인하세요.</div>" + body
    if page == "/settings" and query:
        if query.get("ok"):
            body = "<div class='okbar'>✅ 강조 포인트를 저장했습니다 — 다음 발행 글부터 반영됩니다.</div>" + body
        elif query.get("err"):
            body = "<div class='alert'>저장 실패</div>" + body
        elif query.get("notify"):
            body = "<div class='okbar'>🔔 테스트 알림을 보냈습니다 — 트레이(오른쪽 아래)를 확인하세요.</div>" + body
    return layout(page if page in PAGES else "/", body, live, d["stamp"], title=label)


_cache = {"t": 0.0, "data": None}


def cached_collect(ttl: float = 8.0) -> dict:
    """collect() 는 무겁다(schtasks + 30편 채점). 짧은 TTL 로 페이지 전환을 빠르게."""
    import time as _t
    now = _t.time()
    if _cache["data"] is None or now - _cache["t"] > ttl:
        _cache["data"] = collect()
        _cache["t"] = now
    return _cache["data"]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if self.path.startswith("/shot/"):
            name = Path(self.path[len("/shot/"):]).name  # 경로 이탈 방지
            f = ROOT / "drafts" / "_debug" / name
            if f.suffix.lower() == ".png" and f.is_file():
                self._send(f.read_bytes(), "image/png")
            else:
                self.send_error(404)
            return
        if self.path.startswith("/photo/"):
            name = Path(unquote(urlparse(self.path).path[len("/photo/"):])).name
            # 사진 풀 / 인박스 / 인포그래픽 중 존재하는 곳에서 서빙(경로 이탈 방지)
            for base in (imgmod.PHOTO_DIR, imgmod.INBOX_DIR, imgmod.IMG_DIR):
                f = base / name
                if f.is_file() and f.suffix.lower() in (".png", ".jpg", ".jpeg"):
                    ct = "image/png" if f.suffix.lower() == ".png" else "image/jpeg"
                    self._send(f.read_bytes(), ct)
                    return
            self.send_error(404)
            return
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path in ("/export/history.csv", "/export/metrics.csv"):
            data = _csv_history() if "history" in path else _csv_metrics()
            fname = "publish_history.csv" if "history" in path else "metrics.csv"
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/api":
            self._send(json.dumps(cached_collect(), ensure_ascii=False).encode(),
                       "application/json; charset=utf-8")
            return
        if path == "/" or path in PAGES or path == "/post":
            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            html_out = render(cached_collect(), page=path, query=query)
            self._send(html_out.encode(), "text/html; charset=utf-8")
            return
        self.send_error(404)

    def do_POST(self):  # noqa: N802
        if self.path == "/quit":
            self._send(b"bye", "text/plain")
            # 핸들러 안에서 shutdown() 하면 교착되므로 별도 스레드에서 종료한다.
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if self.path == "/upload":
            self._handle_upload()
            return
        if self.path == "/save-emphasis":
            self._handle_save_emphasis()
            return
        if self.path == "/refresh-metrics":
            if not _refresh["running"]:
                _refresh["running"] = True
                _refresh["msg"] = ""
                threading.Thread(target=_refresh_metrics_bg, daemon=True).start()
            self._redirect("/analytics?refresh=1")
            return
        if self.path == "/test-notify":
            try:
                import notify
                notify.notify("SNS Agent 테스트 알림", "알림이 정상 작동합니다. 발행 실패 시 이렇게 표시됩니다.")
            except Exception:
                pass
            self._redirect("/settings?notify=1")
            return
        self.send_error(404)

    def _handle_save_emphasis(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if 0 < length < 100_000 else b""
        form = {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace")).items()}
        points = [ln.strip() for ln in form.get("points", "").splitlines() if ln.strip()]
        try:
            config.save_emphasis(points)
            _cache["data"] = None
            self._redirect("/settings?ok=1")
        except Exception:
            self._redirect("/settings?err=1")

    def _handle_upload(self):
        ctype = self.headers.get("Content-Type", "")
        length = int(self.headers.get("Content-Length", 0) or 0)
        if "multipart/form-data" not in ctype or length <= 0 or length > 60_000_000:
            self._redirect("/images?err=bad")
            return
        boundary = ctype.split("boundary=", 1)[-1].strip().strip('"')
        body = self.rfile.read(length)
        try:
            fields, files = _parse_multipart(body, boundary)
        except Exception:
            self._redirect("/images?err=parse")
            return
        seg = (fields.get("segment") or "").strip().lower()
        seg = seg if seg in ("a", "b", "c") else ""
        imgmod.INBOX_DIR.mkdir(parents=True, exist_ok=True)
        saved = 0
        for filename, data in files:
            ext = Path(filename).suffix.lower()
            if ext not in (".png", ".jpg", ".jpeg") or len(data) < 1000:
                continue
            stem = Path(filename).name
            if seg and not stem[:2] == f"{seg}_":
                stem = f"{seg}_{stem}"
            dest = imgmod.INBOX_DIR / stem
            i = 1
            while dest.exists():   # 이름 충돌 회피
                dest = imgmod.INBOX_DIR / f"{Path(stem).stem}_{i}{ext}"
                i += 1
            dest.write_bytes(data)
            saved += 1
        _cache["data"] = None      # 캐시 무효화(업로드 즉시 반영)
        self._redirect(f"/images?ok={saved}")

    def _redirect(self, to: str):
        self.send_response(303)
        self.send_header("Location", to)
        self.end_headers()

    def _send(self, body: bytes, ctype: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # 콘솔 조용히
        pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--snapshot", action="store_true",
                    help="서버 없이 data/dashboard.html 파일 하나로 저장")
    a = ap.parse_args()

    if a.snapshot:
        out = ROOT / "data" / "dashboard.html"
        out.parent.mkdir(exist_ok=True)
        # 스크린샷은 이 파일 기준 상대경로로 참조(브라우저에서 file:// 로 바로 열림)
        out.write_text(render(collect(), shot_base="../drafts/_debug/"), encoding="utf-8")
        print(f"저장: {out}")
        if not a.no_open:
            webbrowser.open(out.as_uri())
        return

    url = f"http://127.0.0.1:{a.port}"

    # 이미 떠 있으면 두 번 띄우지 않고 그 탭만 열어준다(아이콘 두 번 눌러도 안전).
    with socket.socket() as s:
        s.settimeout(0.4)
        if s.connect_ex(("127.0.0.1", a.port)) == 0:
            print(f"이미 실행 중입니다: {url}")
            if not a.no_open:
                webbrowser.open(url)
            return

    print(f"대시보드: {url}  (웹페이지의 '종료' 버튼 또는 Ctrl+C 로 종료)")
    if not a.no_open:
        webbrowser.open(url)
    HTTPServer(("127.0.0.1", a.port), Handler).serve_forever()
    print("종료했습니다.")


if __name__ == "__main__":
    main()
