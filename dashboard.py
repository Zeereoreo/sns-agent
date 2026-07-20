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
import re
import subprocess
import sys
import webbrowser
from datetime import date
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
        raw = subprocess.run(cmd, capture_output=True, timeout=20).stdout
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


def task_info() -> list[dict]:
    out = []
    for name in TASKS:
        txt = _run(["schtasks", "/query", "/tn", name, "/v", "/fo", "LIST"])

        def grab(label: str) -> str:
            m = re.search(rf"^{label}:\s*(.+)$", txt, re.M)
            return m.group(1).strip() if m else "-"

        xml = _run(["schtasks", "/query", "/tn", name, "/xml", "ONE"])
        battery_block = "<DisallowStartIfOnBatteries>true" in xml

        code = grab("Last Result")
        meaning, level = RESULT_MEANING.get(code, (f"코드 {code}", "warn"))
        # 과거 실패지만 원인 설정이 이미 고쳐졌다면 '해결됨'으로 낮춰 표시한다.
        if level == "bad" and code == "-2147020576" and not battery_block:
            meaning, level = f"{meaning} → 설정 수정됨, 다음 실행 대기", "warn"
        out.append({
            "name": name,
            "next": grab("Next Run Time"),
            "last": grab("Last Run Time"),
            "state": grab("Scheduled Task State"),
            "result": meaning,
            "level": "bad" if txt == "" else level,
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

    return {
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
.foot{color:#5d6675;font-size:12px;margin-top:34px}
"""

STATUS_BADGE = {"done": ("발행됨", "ok"), "next": ("다음 차례", "next"), "wait": ("대기", "mut")}


def render(d: dict) -> str:
    e = html.escape
    alerts = []
    blocked = [t["name"] for t in d["tasks"] if t.get("battery_block")]
    if blocked:
        alerts.append("배터리 전원에서 실행이 차단된 작업이 있습니다 — 노트북이 충전기에 꽂혀있지 "
                      f"않으면 발행되지 않습니다: <b>{e(', '.join(blocked))}</b>")
    for t in d["tasks"]:
        if t["level"] == "bad" and not t.get("battery_block"):
            alerts.append(f"작업 <b>{e(t['name'])}</b>: {e(t['result'])}")
    if d["images"]["pool"] == 0 and d["images"]["inbox"] == 0:
        alerts.append("실물 사진 풀이 비어 있습니다 — 글마다 인포그래픽 1장만 삽입됩니다. "
                      "<code>drafts/photos/</code>에 사진을 넣으면 자동으로 섞여 들어갑니다.")
    alert_html = "".join(f'<div class="alert">{a}</div>' for a in alerts)

    cards = f"""
    <div class="cards">
      <div class="card"><div class="k">오늘 발행</div><div class="v">{d['today_ok']} <small>/ {d['max_per_day']}</small></div></div>
      <div class="card"><div class="k">발행 완료</div><div class="v">{d['done']} <small>/ {d['total']}편</small></div></div>
      <div class="card"><div class="k">남은 초안</div><div class="v">{d['total'] - d['done']} <small>편</small></div></div>
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

    lrows = "".join(
        f"<tr><td>{e(str(x.get('date','-')))} {e(str(x.get('time','')))}</td>"
        f"<td>{e(str(x.get('draft','-')))}</td>"
        f"<td>{'dry-run' if x.get('dry') else '실제'}</td><td>{x.get('images',0)}장</td>"
        f"<td><span class='badge {'ok' if x.get('ok') else 'mut'}'>{'성공' if x.get('ok') else '미발행'}</span></td></tr>"
        for x in d["log"]) or "<tr><td colspan=5>기록 없음</td></tr>"

    shots = "".join(
        f'<figure><img src="/shot/{e(s)}" alt="{e(s)}"><figcaption>{e(s)}</figcaption></figure>'
        for s in d["shots"]) or "<p style='color:#8b93a1'>스크린샷 없음</p>"

    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<title>SNS Agent 대시보드</title><meta http-equiv="refresh" content="30">
<style>{CSS}</style></head><body><div class="wrap">
<h1>SNS Agent 대시보드</h1>
<div class="sub">30초마다 자동 새로고침 · 읽기 전용</div>
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
    a = ap.parse_args()
    url = f"http://127.0.0.1:{a.port}"
    print(f"대시보드: {url}  (Ctrl+C 종료)")
    if not a.no_open:
        webbrowser.open(url)
    HTTPServer(("127.0.0.1", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
