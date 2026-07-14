"""매일 1회 파이프라인 실행 (오전 8~10시 랜덤 지터).

흐름(Phase 3에서 구현):
  서킷브레이커 확인 -> 키워드 선정 -> 초안 생성 -> 품질 게이트
  -> 이미지 준비 -> 네이버 게시 -> 성과 수집/피드백

Windows 작업 스케줄러가 이 파일을 하루 1회 호출하고,
내부에서 발행 시각을 무작위로 지연시켜 고정 패턴을 피한다.
"""
from __future__ import annotations


def run_once() -> None:
    """하루치 파이프라인을 1회 실행한다."""
    raise NotImplementedError("Phase 3에서 구현 예정")


if __name__ == "__main__":
    run_once()
