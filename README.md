# SNS Agent — 네이버 블로그 무인 발행 시스템

메이드어스(made-us) 콘텐츠를 **자동 생성·이미지 삽입·태그·발행**하는 에이전트.
현재 **하루 3회 무인 발행**까지 동작합니다. (테스트 블로그: made-us2)

> ⚠️ 현실: 네이버는 자동 게시를 탐지합니다. 안전 페이싱(하루 3회·시간 분산)을 지키고,
> 검색의도형·정보성 글로 갑니다. 상세 리스크·전략은 `~/.claude/plans/calm-painting-platypus.md`.

## 지금 상태 (무엇이 되나)
- ✅ 로그인 세션 재사용(사람이 1회 로그인) · 제목·본문·**이미지(클립보드)**·태그·발행 전 과정 자동
- ✅ 초안 30편(세그먼트 A 방송피켓 / B 클럽버킷 / C 간판) + 인포그래픽 6종
- ✅ 스케줄러(큐·하루상한·주제분산) + Windows 작업 3회/일 등록됨
- ✅ 실제 발행 1건 검증 완료(a02)

## 폴더 구조
```
publish/naver.py     네이버 게시 자동화(login / publish / --review)
publish/images.py    이미지 선택(인박스>인포그래픽>사진풀 순환)
publish/draft_parser.py  초안 md → 제목/태그/블록
scheduler.py         무인 발행(run / status)
dashboard.py         운영 대시보드 웹 UI(읽기 전용) · dashboard.cmd 로 실행
drafts/*.md          초안 30편 (gitignore)
drafts/images/       인포그래픽 PNG (내가 생성)
drafts/photos/       실물 사진 풀 (← 여기에 사진 넣으면 자동 재활용)
drafts/photos/inbox/ 새 사진 (여기 넣으면 다음 글에 우선 사용)
data/publish_state.json  발행 이력  |  data/scheduler.log  실행 로그
run_scheduler.cmd    작업 스케줄러가 호출하는 실행 래퍼
```

## 연락처 규칙
글 하단 CTA 는 **전화 `010-8846-8320` 한 줄만** 쓴다.
네이버 톡톡 / 스마트스토어 / 인스타그램 계정은 노출하지 않는다 (2026-07-20 결정).
초안 30편 전부 `👉 문의: 010-8846-8320` 으로 통일되어 있으니 새 글도 같은 형식으로.

## 사진 넣는 법 (당신 몫)
- **평소**: `drafts/photos/` 에 실물 제품 사진을 넣어두면 글마다 자동 재활용.
- **새 사진**: `drafts/photos/inbox/` 에 넣으면 다음 발행 글에 우선 삽입(후 used로 이동).
- 사진이 없어도 주제 매칭 인포그래픽으로 게시됩니다.

## 대시보드 (권장 — 눈으로 확인)
**바탕화면의 `SNS Agent 대시보드` 아이콘 더블클릭.**
검은 콘솔 창 없이 뜨고(pythonw), 브라우저가 자동으로 열립니다.
- **끄기**: 웹페이지 오른쪽 위 **■ 종료** 버튼 (콘솔이 없어 Ctrl+C 를 못 쓰므로)
- 아이콘을 두 번 눌러도 서버가 중복 실행되지 않고 기존 탭만 열립니다
- 서버 없이 파일로만 보고 싶으면: `dashboard.py --snapshot` → `data/dashboard.html`

> 아이콘은 `.lnk` 바로가기(대상 `.venv\Scripts\pythonw.exe dashboard.py`)입니다.
> exe 로 굳이 빌드하지 않은 이유: 이 앱은 어차피 프로젝트 폴더의 초안·상태 파일을
> 읽어야 해서 폴더를 벗어나면 동작하지 않고, exe 로 묶으면 코드 고칠 때마다
> 재빌드해야 합니다. 바로가기는 항상 최신 코드를 실행합니다.
발행 현황·다음 대상·**스케줄 작업의 마지막 실행 결과**·큐 30편·최근 이력·
마지막 실행 스크린샷을 한 화면에서 봅니다. 30초마다 자동 새로고침, 읽기 전용.
문제가 있으면 상단에 빨간 배너로 뜹니다(배터리 차단, 사진 풀 없음 등).

## 운영 명령
```powershell
# 진행 현황
.\.venv\Scripts\python.exe scheduler.py status

# 수동 1회 발행(테스트: --dry-run, 실제: 빼기)
.\.venv\Scripts\python.exe scheduler.py run --dry-run

# 반자동(에디터에 다 채우고 멈춤 → 직접 이미지·발행)
.\.venv\Scripts\python.exe -m publish.naver publish --draft drafts/<파일>.md --review

# 로그인 다시(세션 만료 시)
.\.venv\Scripts\python.exe -m publish.naver login
```

## 무인 스케줄 (등록됨)
매일 **09:07 / 13:23 / 18:41** 에 자동 발행(작업명 `SNS-Agent-1/2/3`).

> 2026-07-20 수정: 등록 당시 기본값이던 `DisallowStartIfOnBatteries`(배터리면 실행 거부) 때문에
> 7/17~7/20 4일간 한 건도 발행되지 않았음(Last Result `0x800710E0`). 노트북이라 해당됨.
> 배터리 조건 해제 + `StartWhenAvailable`(놓친 작업 복구) + `WakeToRun`(절전 깨우기)로 변경.
> 작업 설정을 다시 만들 일이 있으면 이 3가지를 반드시 같이 켤 것.
```powershell
# 잠시 멈추기 / 다시 켜기
schtasks /change /tn "SNS-Agent-1-Morning" /disable
schtasks /change /tn "SNS-Agent-1-Morning" /enable
# (2-Noon, 3-Evening 도 동일)
```

## 실서비스 전환 (made-us 클라이언트 블로그로)
1. `.env` 의 `NAVER_BLOG_ID` 를 실제 블로그 아이디로 변경
2. `python -m publish.naver login` 으로 그 계정 로그인
3. `data/publish_state.json` 초기화(발행 이력 리셋)

## 실패는 조용히 넘어가지 않는다 (2026-07-20 개편)
예전엔 `naver.publish()` 가 모든 실패를 print 만 하고 None 을 반환해서,
스케줄러의 `ok = not dry_run` 이 **항상 True** 였다. 세션이 만료돼 아무것도
안 올라가도 초안이 "발행됨"으로 큐에서 영구 제거됐다.

지금은:
- `publish()` 가 `{ok, reason, images_inserted, url, title}` 를 반환한다
- `ok=True` 의 뜻은 **블로그 목록에서 그 제목을 실제로 확인했다** (버튼 클릭 성공이 아님)
- 실패하면 초안을 큐에 **남겨두고** 다음 실행에서 재시도한다
- 제목/본문 입력이 실패하면 반쪽짜리 글을 올리지 않고 중단한다
- 실패 사유(`session_expired`, `not_found_after_publish` 등)가 상태 파일에 남고
  대시보드 상단에 빨간 배너로 뜬다

## 한계 / 다음 개선
- 이미지 자동삽입은 **클립보드 붙여넣기**로 해결(파일선택창은 hang). 원본 사진은 사용자가 폴더에 공급.
- 완전 무인으로 "글 생성"까지 하려면 Anthropic API 키 필요(현재는 초안을 미리 써둔 큐 방식).
- B세그먼트 글은 이미지 슬롯 4개인데 현재 인포그래픽 1장만 → 사진 풀 채우면 자동 보강.
