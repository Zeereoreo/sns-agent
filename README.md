# SNS Agent — 네이버 블로그 자동 콘텐츠 에이전트 (MVP)

사업 아이템을 주제로 네이버 블로그에 **콘텐츠를 자동 생성·게시**하고, 성과를 측정해
개선 루프를 도는 에이전트. 최종 목표는 월 방문자 1000명(≈ 하루 30~35명).

> ⚠️ **먼저 읽어주세요.** 네이버는 공식 게시 API가 없고 자동 게시를 강하게 탐지합니다.
> 계정 정지/저품질 위험과 현실적 기대치는 계획서를 참고하세요:
> `~/.claude/plans/calm-painting-platypus.md`

## 요구사항
- Windows + PowerShell
- Python 3.11+ (현재 3.14 확인됨)
- Claude(Anthropic) API 키 — https://console.anthropic.com/

## 빠른 시작
```powershell
# 1) 개발환경 자동 셋업 (venv + 의존성 + Playwright + .env 생성 + 검증)
.\scripts\setup.ps1

# 2) .env 파일을 열어 ANTHROPIC_API_KEY 를 입력

# 3) 설정 검증 (API 호출 없음 = 무료)
.\.venv\Scripts\python.exe verify_setup.py
```

## API 키 관리 방식
- 실제 키는 **`.env` 파일에만** 저장하며 git에 올라가지 않습니다(`.gitignore`).
- `.env.example` 은 템플릿으로만 공유합니다. (`Copy-Item .env.example .env`)
- 시스템 환경변수 `ANTHROPIC_API_KEY` 가 있으면 그것도 자동 사용됩니다.

## GitHub에 올리기
```powershell
.\scripts\push.ps1 "커밋 메시지"
```
원격: https://github.com/Zeereoreo/sns-agent

## 프로젝트 구조
```
config.py            # .env 로드 + 전역 설정
verify_setup.py      # 환경 점검(무료)
content/             # 키워드 선정 -> 초안 생성 -> 품질 게이트
publish/naver.py     # Playwright 네이버 게시 (로그인 세션 재사용)
analytics/           # 성과 수집 / 피드백 / 저품질 서킷브레이커
scheduler.py         # 하루 1회 파이프라인 실행
scripts/             # setup.ps1(셋업), push.ps1(업로드)
```

## 진행 단계
- **Phase 0** 사업 아이템 예시 확보 → 콘텐츠 페르소나/키워드 세팅 *(사용자 입력 대기)*
- **Phase 1** 콘텐츠 생성 엔진 (게시 없이 파일로 초안 출력)
- **Phase 2** 네이버 게시 자동화 (수동 트리거로 1건 검증)
- **Phase 3** 완전 자동 루프 + 측정 + 서킷브레이커
- **Phase 4** 유튜브 → 인스타 확장
