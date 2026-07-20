"""운영 대시보드 (로컬 웹 UI).

발행 현황·큐·스케줄 작업 상태·이미지 풀을 한 화면에서 본다. 읽기 전용.

사용:
  python dashboard.py            # http://127.0.0.1:8765 열기
  python dashboard.py --port 9000
"""
from __future__ import annotations

import argparse
import html
import json
import socket
import subprocess
import sys
import threading
import webbrowser
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

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


def collect() -> dict:
    state = scheduler._load_state()
    drafts = scheduler._ordered_drafts()
    published = set(state["published"])
    today = str(date.today())

    queue = []
    nxt_marked = False
    for i, p in enumerate(drafts, 1):
        done = p.name in published
        is_next = not done and not nxt_marked
        if is_next:
            nxt_marked = True
        try:
            title = parse_draft(p)["title"]
        except Exception:
            title = "(파싱 실패)"
        queue.append({"i": i, "name": p.name, "title": title,
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
.top{display:flex;justify-content:space-between;align-items:flex-start;gap:16px}
#quit{background:#232833;color:#c9d1dc;border:1px solid #333b49;border-radius:8px;
 padding:7px 14px;font:inherit;font-size:12.5px;cursor:pointer}
#quit:hover{background:#3a1618;color:#ff8087;border-color:#5b2327}
"""

STATUS_BADGE = {"done": ("발행됨", "ok"), "next": ("다음 차례", "next"), "wait": ("대기", "mut")}

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


def render(d: dict, shot_base: str = "/shot/") -> str:
    """shot_base: 서버 모드는 '/shot/', 스냅샷 파일 모드는 상대경로."""
    e = html.escape
    alerts = []
    blocked = [t["name"] for t in d["tasks"] if t.get("battery_block")]
    if blocked:
        alerts.append("배터리 전원에서 실행이 차단된 작업이 있습니다 — 노트북이 충전기에 꽂혀있지 "
                      f"않으면 발행되지 않습니다: <b>{e(', '.join(blocked))}</b>")
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
    alert_html = "".join(f'<div class="alert">{a}</div>' for a in alerts)

    cards = f"""
    <div class="cards">
      <div class="card"><div class="k">오늘 발행</div><div class="v">{d['today_ok']} <small>/ {d['max_per_day']}</small></div></div>
      <div class="card"><div class="k">발행 완료</div><div class="v">{d['done']} <small>/ {d['total']}편</small></div></div>
      <div class="card"><div class="k">남은 초안</div><div class="v">{d['total'] - d['done']} <small>편 · 약 {d['days_left']}일치</small></div></div>
      <div class="card"><div class="k">대상 블로그</div><div class="v" style="font-size:16px">{e(d['blog'])}</div></div>
      <div class="card"><div class="k">이미지</div><div class="v" style="font-size:15px">인포 {d['images']['info']} · 인박스 {d['images']['inbox']} · 풀 {d['images']['pool']}</div></div>
    </div>"""

    trows = "".join(
        f"<tr><td>{e(t['name'])}</td><td>{e(t['next'])}</td><td>{e(t['last'])}</td>"
        f"<td><span class='badge {t['level']}'>{e(t['result'])}</span></td></tr>"
        for t in d["tasks"])

    qrows = ""
    for q in d["queue"]:
        label, cls = STATUS_BADGE[q["status"]]
        qrows += (f"<tr><td>{q['i']}</td><td><span class='badge {cls}'>{label}</span></td>"
                  f"<td>{e(q['name'])}</td><td>{e(q['title'])}</td></tr>")

    lrows = ""
    for x in d["log"]:
        rtext, rcls = _reason(x.get("reason"))
        planned = x.get("planned_images")
        img = f"{x.get('images', 0)}장"
        if planned is not None and x.get("images", 0) < planned:
            img = f"<span class='badge warn'>{x.get('images',0)}/{planned}장</span>"
        title = f"<a href='{e(str(x['url']))}' target='_blank'>{e(str(x.get('draft','-')))}</a>" \
            if x.get("url") else e(str(x.get("draft", "-")))
        lrows += (f"<tr><td>{e(str(x.get('date','-')))} {e(str(x.get('time','')))}</td>"
                  f"<td>{title}</td>"
                  f"<td>{'dry-run' if x.get('dry') else '실제'}</td><td>{img}</td>"
                  f"<td><span class='badge {'ok' if x.get('ok') else 'mut'}'>"
                  f"{'성공' if x.get('ok') else '미발행'}</span>"
                  + (f" <span class='badge {rcls}'>{e(rtext)}</span>" if rtext else "")
                  + "</td></tr>")
    lrows = lrows or "<tr><td colspan=5>기록 없음</td></tr>"

    shots = "".join(
        f'<figure><img src="{shot_base}{e(s)}" alt="{e(s)}"><figcaption>{e(s)}</figcaption></figure>'
        for s in d["shots"]) or "<p style='color:#8b93a1'>스크린샷 없음</p>"

    live = shot_base == "/shot/"
    refresh = '<meta http-equiv="refresh" content="30">' if live else ""
    sub = ("30초마다 자동 새로고침 · 읽기 전용" if live
           else f"고정 스냅샷 ({d['stamp']} 기준) · 최신화하려면 snapshot 다시 실행")
    # 콘솔 창 없이 띄우면 Ctrl+C 로 못 끄므로 UI 에 종료 버튼을 둔다.
    quit_btn = ("""<button id="quit" onclick="fetch('/quit',{method:'POST'})
      .then(()=>document.body.innerHTML='<div class=wrap><h1>종료했습니다</h1>'
      +'<div class=sub>이 탭은 닫으셔도 됩니다.</div></div>')">■ 종료</button>""" if live else "")

    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<title>SNS Agent 대시보드</title>{refresh}
<style>{CSS}</style></head><body><div class="wrap">
<div class="top"><div>
<h1>SNS Agent 대시보드</h1>
<div class="sub">{sub}</div>
</div>{quit_btn}</div>
{alert_html}{cards}
<h2>자동 실행 스케줄</h2>
<table><tr><th>작업</th><th>다음 실행</th><th>마지막 실행</th><th>마지막 결과</th></tr>{trows}</table>
<h2>발행 큐 ({d['done']}/{d['total']})</h2>
<div class="scroll"><table><tr><th>#</th><th>상태</th><th>파일</th><th>제목</th></tr>{qrows}</table></div>
<h2>최근 발행 기록</h2>
<table><tr><th>날짜</th><th>초안</th><th>모드</th><th>이미지</th><th>결과</th></tr>{lrows}</table>
<h2>마지막 실행 화면</h2>
<div class="shots">{shots}</div>
<div class="foot">scheduler.py 상태 파일: data/publish_state.json</div>
</div></body></html>"""


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
        if self.path.rstrip("/") in ("", "/api"):
            if self.path.rstrip("/") == "/api":
                self._send(json.dumps(collect(), ensure_ascii=False).encode(),
                           "application/json; charset=utf-8")
            else:
                self._send(render(collect()).encode(), "text/html; charset=utf-8")
            return
        self.send_error(404)

    def do_POST(self):  # noqa: N802
        if self.path == "/quit":
            self._send(b"bye", "text/plain")
            # 핸들러 안에서 shutdown() 하면 교착되므로 별도 스레드에서 종료한다.
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        self.send_error(404)

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
