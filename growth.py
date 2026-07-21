"""방문자 성장 최적화 엔진.

우리가 통제할 수 있는 레버는 "무엇을, 어떤 순서로 발행하는가"다. 이 엔진은
매일 쌓이는 데이터(키워드 순위·방문자 추이)로 각 미발행 초안의 '기대 성과'를
점수화해, 성과가 좋은 주제/세그먼트를 먼저 발행하도록 큐를 재정렬한다.
그리고 자신의 과거 결정이 실제로 좋은 순위를 냈는지 평가해 가중치를 스스로 보정한다.

정직한 한계: 알고리즘이 없는 방문자를 만들지 못한다. "이길 수 있는 주제에 힘을
몰아주고, 안 되는 주제는 뒤로 미루는" 최적화다. 데이터가 쌓일수록 똑똑해진다.

구성:
  - segment_scores(): 세그먼트별 관측 성과(발행글 평균 순위 기반)
  - rank_queue(): 미발행 초안 우선순위 점수 + 설명
  - next_draft(): 다음 발행 초안(동일 세그먼트 3연속 방지)
  - evaluate_and_tune(): 과거 결정 평가 → 가중치 자가 보정 + 로그
  - report(): 사람이 읽는 요약

CLI:
  python growth.py plan       # 현재 우선순위 + 이유
  python growth.py tune       # 자가 평가·가중치 보정
  python growth.py report     # 성장 리포트
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import config  # noqa: E402
from publish.draft_parser import parse_draft  # noqa: E402

DRAFTS = ROOT / "drafts"
STATE = ROOT / "data" / "publish_state.json"
METRICS = ROOT / "data" / "metrics.json"
WEIGHTS_FILE = ROOT / "data" / "growth_weights.json"
GLOG = ROOT / "data" / "growth_log.json"

DEFAULT_WEIGHTS = {"seg": 0.35, "seo": 0.30, "diversity": 0.20, "explore": 0.15}
MAX_RANK = 30  # 이 순위 밖은 최하로 취급


# ---------- 데이터 로드 ----------

def _load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save(path, obj):
    path.parent.mkdir(exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def load_weights() -> dict:
    w = _load(WEIGHTS_FILE, dict(DEFAULT_WEIGHTS))
    # 누락 키 보정 + 정규화
    for k, v in DEFAULT_WEIGHTS.items():
        w.setdefault(k, v)
    s = sum(w[k] for k in DEFAULT_WEIGHTS) or 1.0
    return {k: w[k] / s for k in DEFAULT_WEIGHTS}


def primary_keyword(path: Path) -> str:
    t = path.read_text(encoding="utf-8")
    m = re.search(r"타깃\s*검색키워드[^:]*:\s*(.+)", t)
    return re.split(r"[,/·\n]", m.group(1).strip())[0].strip() if m else ""


def _segment(name: str) -> str:
    return name[0] if name[:1] in "abc" else "a"


def _ordered_drafts() -> list[Path]:
    a = sorted(DRAFTS.glob("sample*.md")) + sorted(DRAFTS.glob("a*.md"))
    return a + sorted(DRAFTS.glob("b*.md")) + sorted(DRAFTS.glob("c*.md"))


# ---------- 관측 성과 ----------

def _latest_ranks() -> dict:
    ranks = _load(METRICS, {}).get("ranks", {})
    if not ranks:
        return {}
    return ranks[sorted(ranks)[-1]]


def segment_scores() -> dict:
    """세그먼트별 관측 성과 0~1(높을수록 잘됨). 발행글 키워드의 평균 순위 기반.
    데이터 없으면 중립 0.5."""
    state = _load(STATE, {"published": []})
    latest = _latest_ranks()
    per_seg_ranks: dict[str, list[float]] = {"a": [], "b": [], "c": []}
    for name in state.get("published", []):
        p = DRAFTS / name
        if not p.exists():
            continue
        kw = primary_keyword(p)
        r = latest.get(kw)
        seg = _segment(name)
        if isinstance(r, int):
            per_seg_ranks[seg].append(r)
        elif kw:  # 발행됐지만 순위 밖 = 성과 낮음
            per_seg_ranks[seg].append(MAX_RANK)
    out = {}
    for seg in "abc":
        rs = per_seg_ranks[seg]
        if not rs:
            out[seg] = 0.5
        else:
            avg = sum(rs) / len(rs)
            out[seg] = max(0.0, min(1.0, 1 - (avg - 1) / (MAX_RANK - 1)))
    return out


# ---------- 초안 점수화 ----------

def _seo_score(path: Path) -> float:
    try:
        import seo
        return seo.score_draft(path)["score"] / 100.0
    except Exception:
        return 0.8


def rank_queue() -> list[dict]:
    """미발행 초안 전부를 우선순위 점수와 함께 정렬해 반환(설명 포함)."""
    w = load_weights()
    state = _load(STATE, {"published": []})
    published = set(state.get("published", []))
    segs = segment_scores()

    # 세그먼트별 발행 수(explore: 적게 발행된 세그먼트 우대)
    pub_per_seg = {"a": 0, "b": 0, "c": 0}
    for n in published:
        pub_per_seg[_segment(n)] += 1
    max_pub = max(pub_per_seg.values()) or 1

    # diversity: 미발행 세그먼트별 잔량이 많을수록 약간 우대(폭 유지)
    unpub = [p for p in _ordered_drafts() if p.name not in published]
    rem_per_seg = {"a": 0, "b": 0, "c": 0}
    for p in unpub:
        rem_per_seg[_segment(p.name)] += 1
    max_rem = max(rem_per_seg.values()) or 1

    rows = []
    for p in unpub:
        seg = _segment(p.name)
        seg_s = segs[seg]
        seo_s = _seo_score(p)
        explore = 1 - pub_per_seg[seg] / max_pub      # 적게 발행된 세그먼트 ↑
        diversity = rem_per_seg[seg] / max_rem         # 잔량 많은 세그먼트 ↑
        total = (w["seg"] * seg_s + w["seo"] * seo_s
                 + w["explore"] * explore + w["diversity"] * diversity)
        rows.append({
            "name": p.name, "seg": seg, "keyword": primary_keyword(p),
            "score": round(total, 4),
            "breakdown": {"seg": round(seg_s, 2), "seo": round(seo_s, 2),
                          "explore": round(explore, 2), "diversity": round(diversity, 2)},
        })
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows


def next_draft(recent_segments: list[str] | None = None) -> str | None:
    """최고 우선순위 초안. 단, 같은 세그먼트 3연속 발행은 피한다(폭 유지)."""
    q = rank_queue()
    if not q:
        return None
    recent = recent_segments or _recent_published_segments(2)
    for r in q:
        # 직전 2개가 모두 같은 세그먼트면 그 세그먼트는 건너뛴다
        if len(recent) >= 2 and recent[-1] == recent[-2] == r["seg"]:
            continue
        return r["name"]
    return q[0]["name"]   # 전부 걸리면 그냥 1등


def _recent_published_segments(n: int) -> list[str]:
    log = _load(STATE, {"log": []}).get("log", [])
    segs = [_segment(e["draft"]) for e in log
            if e.get("ok") and not e.get("dry") and e.get("draft")]
    return segs[-n:]


# ---------- 자가 평가·튜닝 ----------

def evaluate_and_tune(apply: bool = True) -> dict:
    """과거 발행 결정을 관측 순위로 평가해 가중치를 미세 보정한다.

    아이디어: 발행글마다 '결정 당시 가장 크게 작용한 신호'와 '실제 성과(순위)'를 대응.
    좋은 성과(순위≤5)를 낸 결정에서 큰 신호였던 항목의 가중치를 소폭↑, 나쁜 성과는 소폭↓.
    데이터가 적으면 거의 움직이지 않는다(안전).
    """
    w = load_weights()
    state = _load(STATE, {"published": []})
    latest = _latest_ranks()
    segs = segment_scores()

    adjust = {k: 0.0 for k in DEFAULT_WEIGHTS}
    samples = 0
    for name in state.get("published", []):
        p = DRAFTS / name
        if not p.exists():
            continue
        kw = primary_keyword(p)
        r = latest.get(kw)
        if not isinstance(r, int):
            continue
        samples += 1
        good = r <= 5           # 성과 판정: 1페이지 상단
        seg = _segment(name)
        # 결정 당시 신호값(근사: 현재 세그먼트점수/ SEO)
        signals = {"seg": segs[seg], "seo": _seo_score(p),
                   "explore": 0.5, "diversity": 0.5}
        # 가장 큰 신호를 좋은/나쁜 성과에 따라 소폭 조정
        top = max(signals, key=signals.get)
        adjust[top] += (0.02 if good else -0.02)

    new = {k: max(0.05, w[k] + adjust[k]) for k in DEFAULT_WEIGHTS}
    s = sum(new.values())
    new = {k: round(new[k] / s, 4) for k in new}
    result = {"samples": samples, "old": w, "new": new, "adjust": adjust}
    if apply and samples > 0:
        _save(WEIGHTS_FILE, new)
        log = _load(GLOG, {"tune": []})
        log.setdefault("tune", []).append(
            {"date": str(date.today()), "samples": samples, "weights": new})
        _save(GLOG, log)
    return result


# ---------- 리포트 ----------

def report() -> str:
    segs = segment_scores()
    w = load_weights()
    q = rank_queue()
    lines = ["===== 방문자 성장 엔진 리포트 =====",
             f"세그먼트 성과(관측): a={segs['a']:.2f} b={segs['b']:.2f} c={segs['c']:.2f} (1=최고)",
             f"현재 가중치: " + " ".join(f"{k}={w[k]:.2f}" for k in DEFAULT_WEIGHTS),
             f"다음 추천 발행: {next_draft()}",
             "우선순위 상위 5:"]
    for r in q[:5]:
        b = r["breakdown"]
        lines.append(f"  {r['score']:.3f}  [{r['seg']}] {r['name']}  "
                     f"(seg{b['seg']}/seo{b['seo']}/exp{b['explore']}/div{b['diversity']})")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["plan", "tune", "report"], nargs="?", default="report")
    a = ap.parse_args()
    if a.cmd == "tune":
        r = evaluate_and_tune()
        print(f"자가 튜닝: 샘플 {r['samples']}개")
        print("  이전:", r["old"])
        print("  이후:", r["new"])
    elif a.cmd == "plan":
        for r in rank_queue()[:10]:
            print(f"{r['score']:.3f} [{r['seg']}] {r['name']}  {r['keyword']}")
    else:
        print(report())


if __name__ == "__main__":
    main()
