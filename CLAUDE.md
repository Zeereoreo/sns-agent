# CLAUDE.md

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 0. 자율 성장 루프 (이 프로젝트: SNS 네이버 블로그 에이전트)

**목표 = 네이버 블로그 방문자 성장.** 사용자가 "진행해"·"자율 개발 계속"·loop 로 트리거하면,
지시를 매번 기다리지 말고 **스스로 아래 사이클을 반복**한다. 플레이북·상태는 `GROWTH_LOOP.md`.

매 주기: **① 측정**(scheduler.py status / growth.py report / demand.py audit / data/metrics.json / GROWTH_LOOP.md)
→ **② 진단**(방문자 성장을 막는 가장 큰 레버 1개) → **③ 구현**(그 1개만; 값 없으면 정직히 "조치 불필요")
→ **④ 검증**(test_smoke.py 통과, 발행 변경 시 dry-run) → **⑤ 커밋·GROWTH_LOOP.md 갱신**
→ **⑥ 자기평가**(다음 주기에 방문자·순위로 이어졌는지). 자율 진행 시 ScheduleWakeup 으로 self-pace.

**불변 규칙**: 억지 작업 금지 · 정직(가짜 데이터/귀속 금지) · 라이브 무중단(dry-run만) ·
drafts/ 는 gitignore(라이브 반영) · 새 글 seo.py A등급 · 자동생성(API키)·인스타/X 는 사용자 지시로 보류.
방문자 = 검색수요 키워드 × 순위 × 블로그지수(시간 필요). C(간판)에 수요 집중.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.
