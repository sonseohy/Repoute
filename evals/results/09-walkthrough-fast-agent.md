# 09. 실험 E5 — agent 턴을 fast 모델로 + def 줄 인용 힌트 + 재읽기 차단 (Phase 3, 2026-10-21)

08 의 비용 문제(턴마다 재전송되는 컨텍스트 × gpt-4o 단가)를 겨냥. 세 변경을 동시에 넣었으므로 통제군(quality agent)을 함께 돌려 fast 모델 효과를 분리했다.

- **n = 1** 질문(07·08 과 동일, psf/requests@611c616), 실행 2회(새 thread `run09-quality`, `run09-fast`).
- **변경**: (1) `--agent-model fast` = agent 턴 gpt-4o-mini, finalize 는 gpt-4o 유지(wave10a). (2) `find_definition` 결과에 `인용: file:line_start` 힌트 + 프롬프트 v3 FINALIZE "정의 인용은 def/class 줄". (3) 이미 읽은 범위 안 `read_code` 는 실행하지 않고 "이미 읽음" 반환(예산 미차감). A·B 모두 (2)(3) 적용, A 는 quality agent, B 는 fast agent.
- **측정**: 체크포인트 최종 상태를 `citation_validity`·`citation_specificity`·`citation_at_def` 로 채점, 토큰·비용은 LangSmith(00:23:59 / 00:24:12 UTC).

## 결과

| | 08 (v2, quality) | **A** 09 quality + v3 | **B** 09 fast + v3 |
|---|---|---|---|
| 도구 호출 | 13 | 6 | 11 |
| "이미 읽음" 차단 | — | 0 | 0 |
| 도달 지점 | Session.send (5단계) | **Session.request (3단계) — 후퇴** | **Session.send (5단계)** |
| 인용 수 | 5 | 3 | 5 |
| 유효 / 구체성 | 100% / 100% | 100% / 100% | 100% / 100% |
| **def 줄 비율** | 0% (전부 line_end) | **100%** | **100%** |
| 토큰 (agent / finalize) | gpt-4o 49.9k | gpt-4o 17.7k (합산) | mini 35.8k / gpt-4o 6.8k |
| 비용 | $0.099 | $0.043 (−56%) | **$0.025 (−75%)** |
| 시간 | 24.9s | 10.1s | 17.3s |

B 도구 순서: find_definition(get) → read_code(api 74~87) → find_callees(get) → find_definition(request) → read_code(api 24~71) → find_callees(request) → find_definition(Session) → read_code(sessions 395~905 → 200줄로 잘려 395~594) → find_callees(Session.request) → find_definition(send) → read_code(sessions 752~829). 인용: api.py:74(get), :24(request), sessions.py:395(class Session), :557(request), :752(send).

## 관찰
1. **fast agent 가 끝까지 따라가면서 비용을 75% 줄였다.** agent 턴 12회가 $0.0036. 이제 비용의 85%($0.0217)가 finalize 1회(gpt-4o, 입력 6.8k)다. 다음 비용 지렛대는 finalize 다(입력을 대화 전체 대신 읽은 코드 + 도구 결과 요약으로 줄이거나, finalize 도 fast 로 바꾸고 품질을 비교).
2. **def 줄 힌트는 입력으로 넣자 바로 100%.** 08 의 0/5 → 3/3, 5/5. 06(줄 1 제한), 08(read_code 힌트)와 같은 패턴: 원하는 값을 데이터로 보여 주면 모델이 따른다.
3. **통제군 A 는 비용이 절반으로 줄었지만 3단계에서 멈췄다.** 같은 quality 모델·거의 같은 프롬프트(v3 는 def 줄 규칙 한 줄 추가)인데 08 과 다르게 행동했다. 도구 결과 문자열이 바뀌어(힌트 추가) 모델의 다음 선택이 달라진 것일 수도, 단순 실행 편차일 수도 있다. **n=1 로는 가릴 수 없다.** ReAct 는 temperature 0 이어도 도구 출력이 조금 바뀌면 경로가 달라지는 비결정 시스템이다. 완결성은 Phase 4 의 `walkthrough-qa` 정답(기대 호출 체인)과 여러 질문으로만 판정할 수 있다.
4. **재읽기 차단은 발동하지 않았다**(두 실행 모두 0회). 08 의 한 줄 읽기 패턴이 이번엔 안 나왔다. 장치는 남겨 두고 배치 측정에서 발동 빈도를 본다.
5. fast agent 는 `read_code(sessions.py, 395, 905)` 처럼 510줄을 한 번에 요청했다(200줄로 잘림). 그래도 `class Session` 정의 줄(395)을 인용에 썼다. 큰 범위 요청이 잘리는 것을 모델이 인지하는지는 미확인.

## 다음
- Phase 3 완료 기준 "질문 20개 n=20": fast agent 기준 20 × ~$0.025 ≈ $0.5 로 가능해졌다. 레포 4개 × 질문 5개 세트와 배치 스크립트를 만들어 유효성·구체성·def 줄·도구 호출·비용·도달 여부(수동)를 기록(기록 10).

## 비용 누적
- 이 실험 $0.069. 프로젝트 전체 약 $0.67.
