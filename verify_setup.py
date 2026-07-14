"""환경 점검: 파이썬 버전, 의존성, .env 키 존재를 확인한다 (API 호출 없음 = 무료)."""
from __future__ import annotations

import sys


def main() -> int:
    ok = True
    print(f"Python: {sys.version.split()[0]}")

    for mod in ("anthropic", "playwright", "dotenv"):
        try:
            __import__(mod)
            print(f"  [OK] {mod}")
        except ImportError:
            print(f"  [MISSING] {mod}  ->  pip install -r requirements.txt")
            ok = False

    try:
        import config

        key = config.ANTHROPIC_API_KEY
        if key and key.startswith("sk-"):
            print(f"  [OK] ANTHROPIC_API_KEY 설정됨 (...{key[-4:]})")
        else:
            print("  [MISSING] ANTHROPIC_API_KEY  ->  .env에 키 입력 (.env.example 참고)")
            ok = False
    except Exception as e:  # noqa: BLE001
        print(f"  [ERROR] config 로드 실패: {e}")
        ok = False

    print("\n검증 " + ("성공" if ok else "실패 — 위 항목 확인 필요"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
