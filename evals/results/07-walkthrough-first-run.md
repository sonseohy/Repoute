# 07. 따라가기 첫 실행 (Phase 3, 2026-10-21)

`repoute ask` 를 실제 LLM 으로 처음 돌린 기록. 이후 개선(도구·프롬프트 v2)의 비교 기준.

- **n = 1** (psf/requests@611c616). 질문: "requests.get()을 호출하면 내부에서 무슨 일이 일어나?"
- **측정 방법**: 결과는 체크포인터(`.repoute_cache/threads.sqlite`, thread `psf/requests@611c616:default`)의 최종 상태에서, 토큰·비용·지연은 LangSmith 트레이스(`repoute-dev`, 00:09:41 UTC)에서 읽음. 유효성·구체성은 `validate` 노드 결과(사후 재채점 아님).
- **모델**: agent·finalize 모두 gpt-4o, temperature 0, 프롬프트 walkthrough_v1. 상한: 도구 15회, 재작성 2회.

## 결과

| 항목 | 값 |
|---|---|
| 도구 호출 | **15회 (상한 도달)**, agent 턴 16회 |
| 도구 순서 | find_definition(requests.get) 실패 → search_code(def get) → read_code(api.py 74~94) → find_definition(requests.request) 실패 → search_code → read_code ×3 (api.py 24~84, 20줄씩) → find_definition(Session.request) → read_code ×5 (sessions.py 557~657, 20줄씩) → find_callees(Session.request) → find_definition(Session.prepare_request) [상한] |
| 읽은 범위 | 9개 창, 총 180줄 (api.py 24~94, sessions.py 557~657) |
| 답 | 4단계: get → request → Session 생성 → Session.request. 요약 1문장 |
| 인용 | 4개, 유효 100%, 구체성 100% (전부 함수 안 줄), 읽은 범위 안 100% |
| 재작성 | 0회 |
| 토큰 | gpt-4o 17회 호출, 입력 46,558 / 출력 713 |
| 비용 | **$0.080** (LangSmith) — 예상 $0.08~0.15 의 하단 |
| 시간 | 24.1초 |

## 관찰
1. **답의 근거는 전부 맞다. 그러나 흐름이 중간에 끊겼다.** 상한 때문에 `Session.request` 이후(`prepare_request` → `send` → `HTTPAdapter.send`)를 못 읽었고, 답도 거기서 끝난다. 사용자 질문의 절반만 답한 셈이다. "인용이 맞다" 와 "질문에 다 답했다" 는 다른 지표다(읽기 지도의 유효성 vs 순서 품질과 같은 구분). 완결성은 Phase 4 의 `walkthrough-qa` 정답(호출 체인)으로만 잴 수 있다.
2. **도구 예산 15회 중 약 10회가 낭비됐다.** (a) `find_definition("requests.get")` 처럼 **모듈 경로가 붙은 이름**은 심볼 DB 에 없어 실패하고 search_code 로 우회(2회 ×2). (b) 모델이 **20줄씩** 읽어 함수 하나에 4~5회를 썼다. `read_code` 는 200줄까지 허용하고 `find_definition` 결과에 `line_start-line_end` 가 있는데도 범위를 한 번에 읽지 않았다. 둘 다 도구 설명·프롬프트가 "어떻게 읽어야 효율적인지" 말하지 않은 탓이다.
3. **비용 구조**: 입력 46.5k 토큰은 ReAct 가 턴마다 대화 전체를 다시 보내기 때문. 턴을 줄이는 것(= 낭비 제거)이 곧 비용 절감이다. fast 모델로 바꾸는 실험(E5)보다 먼저 턴 수를 줄이는 게 순서.
4. 다음(실험 08): `find_definition` 이 점 붙은 이름의 마지막 조각으로 재시도, 도구 설명에 "정의를 찾으면 그 범위를 read_code 로 한 번에(최대 200줄) 읽어라" 명시, 프롬프트 v2. 같은 질문으로 재측정해 도구 호출 수·읽은 창 수·완결성(단계 수, send 까지 도달 여부)·비용을 비교.

## 비용 누적
- 이 실행 $0.080. 프로젝트 전체 약 $0.50.
