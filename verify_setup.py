"""환경 점검: 파이썬 버전, 의존성, .env 키 존재를 확인한다 (API 호출 없음 = 무료)."""
from __future__ import annotations

import sys

# 한국어 Windows 콘솔(cp949)에서 한글/유니코드 출력이 깨지거나 죽지 않도록 UTF-8로 강제.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass


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
        # 실제 키만 통과 (빈 값/플레이스홀더 거르기: 플레이스홀더는 한글 포함 -> not ascii).
        if key.startswith("sk-ant-") and key.isascii() and len(key) > 30:
            print(f"  [OK] ANTHROPIC_API_KEY 설정됨 (...{key[-4:]})")
        else:
            print("  [MISSING] ANTHROPIC_API_KEY  ->  .env에 실제 키 입력 (.env.example 참고)")
            ok = False
    except Exception as e:  # noqa: BLE001
        print(f"  [ERROR] config 로드 실패: {e}")
        ok = False

    print("\n검증 " + ("성공" if ok else "실패 - 위 항목 확인 필요"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
