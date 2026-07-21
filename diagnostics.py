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
    sys.exit(1 if n_bad else 0)


if __name__ == "__main__":
    main()
