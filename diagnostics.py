"""운영 진단 CLI — 세션·초안·이미지·작업·큐를 한 번에 점검한다(브라우저 없이).

사용:  python diagnostics.py
대시보드 '진단' 탭과 동일한 점검을 터미널에 출력. 문제 있으면 종료코드 1.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import dashboard  # noqa: E402


# 진단 항목 → '지금 이걸 먼저 해야 한다'의 순서.
# 성장 루프 원칙: 한 주기에 레버 1개. 문제가 여럿이면 **가장 앞선 전제**부터 푼다.
# (색인 안 되면 순위가 없고, 세션 죽으면 발행이 없고, 발행이 없으면 나머지가 무의미)
PRIORITY = ["검색 유입", "네이버 색인", "네이버 세션", "자동 실행 작업", "발행 큐",
            "라이브 글 점검", "외부 색인", "사진 풀", "키워드 중복", "콘텐츠 전환 게이트",
            "초안 파싱", "상태 파일"]


def next_action(checks: list[dict]) -> dict | None:
    """지금 손대야 할 **한 가지**를 고른다. 없으면 None."""
    problems = [c for c in checks if c["level"] in ("bad", "warn")]
    if not problems:
        return None
    def rank(c):
        try:
            return (0 if c["level"] == "bad" else 1, PRIORITY.index(c["name"]))
        except ValueError:
            return (0 if c["level"] == "bad" else 1, len(PRIORITY))
    return sorted(problems, key=rank)[0]


def main() -> None:
    d = dashboard.collect()
    checks = dashboard.run_diagnostics(d)
    mark = {"ok": "[정상]", "warn": "[주의]", "bad": "[문제]"}
    print("===== SNS Agent 운영 진단 =====")
    for c in checks:
        line = f"{mark[c['level']]:6} {c['name']:12} {c['detail']}"
        if c["fix"]:
            line += f"   → {c['fix']}"
        print(line)
    n_bad = sum(1 for c in checks if c["level"] == "bad")
    n_warn = sum(1 for c in checks if c["level"] == "warn")
    print(f"\n문제 {n_bad} · 주의 {n_warn} · 정상 {len(checks) - n_bad - n_warn}")

    nxt = next_action(checks)
    print("\n===== 지금 할 일 1가지 =====")
    if nxt:
        print(f"  ▶ {nxt['name']}: {nxt['detail']}")
        if nxt["fix"]:
            print(f"    {nxt['fix']}")
    else:
        print("  ▶ 조치 불필요 — 발행이 쌓이는 걸 기다린다.")
    sys.exit(1 if n_bad else 0)


if __name__ == "__main__":
    main()
