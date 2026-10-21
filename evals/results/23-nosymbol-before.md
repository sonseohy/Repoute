# 23. 함수 이름 없는 질문 10개 — before (기존 도구 5개) (Phase 3, 2026-10-21)

## 방법

- **n = 10** 질문 (`evals/datasets/walkthrough_questions_nosymbol.json`, 레포 4개), 질문마다 체크포인터 없이 새 상태로 실행.
- **모델**: agent = gpt-4o (quality), finalize = gpt-4o (openai), temperature 0. 프롬프트 v3.
- **유효·구체·def줄**: 최종 답 인용을 `citation_validity`·`citation_specificity`·`citation_at_def` 로 사후 채점.
- **엄격**: 최종 `validation_errors` 가 0개이고 인용이 1개 이상(그래프 검증 = 존재 + read_code 로 읽은 범위 안). 사후 유효성은 파일:줄 존재만 보므로 엄격 통과를 주 지표로 본다.
- **읽기**: 실제 실행된 read_code 호출 수("이미 읽은 범위" 차단·상한 미실행 제외).
- **도달**: `expect_reach`(사람이 코드를 보고 정한 기대 도달 심볼)가 어떤 단계의 제목·설명 또는 인용 심볼 문자열에 들어 있는지 보는 **자동 근사치**. 사람 판정 아님.
- **차단**: "이미 읽은 범위" 로 막힌 read_code 수. **도구**: 실행된 도구 호출 수.
- **탐색차단**: 읽기 없이 탐색만 6회 한 뒤 실행하지 않은 find_* 수(예산은 소모).
- **비용**: `UsageMetadataCallbackHandler` 토큰 × 스크립트 `PRICES` 단가 추정치. LangSmith 태그 `wt-nosymbol-before` 로 교차 확인. **시간**: invoke 벽시계(인덱싱 제외).
- **첫읽기**: 첫 실행된 read_code 앞에 agent 가 낸 도구 호출 수(차단 포함). "없음" = 실행된 read_code 가 없음. 평균은 읽은 질문만.
- **체인**: `metrics.call_chain_accuracy(predicted_chain_from_refs(인용), expect_chain)` (인용 순서 심볼 vs 기대 호출 체인, 끝 이름 LCS / 기대 길이).
- **끝도달**: `metrics.reached`(인용 심볼 끝 이름 매칭, 흔한 이름은 파일·한정자 확인). **도달** 은 기존 문자열 포함 판정 그대로.
- **도구 호출 순서**: AIMessage.tool_calls 순서. "!blocked" = 도구 층에서 실행 안 됨, "!error" = 도구 오류.

## 질문별 결과

| id | 질문 | 도구 | 읽기 | 차단 | 탐색차단 | 단계 | 인용 | 엄격 | 유효 | 구체 | def줄 | 도달 | 비용 | 시간 | 첫읽기 | 체인 | 끝도달 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| ns-requests-01 | 리디렉트를 너무 많이 따라가면 어디서 예외가 나? | 5 | 1 | 0 | 0 | 3 | 3 | O | 100% | 100% | 33% | O | $0.0417 | 11.9s | 4 | 0.33 | O |
| ns-requests-02 | 요청을 보내기 전에 세션에 저장된 쿠키와 요청마다 넘긴 쿠키는 어디서 합 | 4 | 2 | 0 | 0 | 3 | 3 | O | 100% | 100% | 0% | O | $0.0367 | 9.7s | 1 | 0.67 | O |
| ns-requests-03 | 환경 변수에 설정해 둔 프록시 주소는 어디서 읽어 와? | 10 | 3 | 0 | 0 | 3 | 3 | O | 100% | 100% | 33% | X | $0.0936 | 16.7s | 3 | 0.00 | X |
| ns-httpx-01 | 리디렉트를 따라갈 때 다음 요청의 메서드가 바뀌는 건 어디서 정해져? | 9 | 2 | 0 | 0 | 2 | 2 | O | 100% | 100% | 100% | O | $0.0493 | 13.3s | 5 | 0.40 | O |
| ns-httpx-02 | 압축돼서 온 응답 본문을 풀 때, 압축 방식에 맞는 해제기는 어디서 골라 | 6 | 2 | 0 | 0 | 4 | 4 | O | 100% | 75% | 0% | X | $0.0362 | 10.5s | 3 | 0.00 | X |
| ns-click-01 | 옵션을 명령줄에 안 주면 환경 변수에서 값을 읽어 오던데, 그건 어디서  | 13 | 6 | 0 | 0 | 5 | 5 | O | 100% | 100% | 100% | X | $0.1738 | 21.5s | 1 | 0.33 | X |
| ns-click-02 | 값을 안 준 옵션에 대해 사용자에게 직접 입력을 물어보는 건 어디서 해? | 7 | 1 | 0 | 0 | 3 | 3 | O | 100% | 100% | 33% | O | $0.0442 | 15.4s | 6 | 0.20 | O |
| ns-rich-01 | 출력할 때 터미널 폭은 어디서 계산해? 환경 변수로 바꿀 수도 있어? | 7 | 2 | 0 | 0 | 3 | 3 | O | 100% | 100% | 0% | X | $0.0442 | 12.0s | 4 | 0.00 | X |
| ns-rich-02 | 긴 문장을 출력하면 폭에 맞춰 줄을 나누는 건 어디서 해? | 5 | 1 | 0 | 0 | 4 | 4 | O | 100% | 100% | 25% | O | $0.0360 | 10.4s | 4 | 0.20 | O |
| ns-rich-03 | 터미널이 256색까지만 지원하면 트루컬러 색은 어디서 가까운 색으로 바뀌 | 4 | 2 | 0 | 0 | 3 | 3 | O | 100% | 67% | 67% | X | $0.0466 | 13.9s | 1 | 0.00 | X |

## 레포별 평균

| 레포 | n | 유효 | 구체 | def줄 | 도달 | 도구 | 비용 | 시간 | 첫읽기까지 | 체인 |
|---|---|---|---|---|---|---|---|---|---|---|
| psf/requests | n=3 | 100% | 100% | 22% | 2/3 (67%) | 6.3 | $0.0573 | 12.8s | 2.7 (읽기 없음 0건) | 0.33 |
| encode/httpx | n=2 | 100% | 88% | 50% | 1/2 (50%) | 7.5 | $0.0428 | 11.9s | 4.0 (읽기 없음 0건) | 0.20 |
| pallets/click | n=2 | 100% | 100% | 67% | 1/2 (50%) | 10.0 | $0.1090 | 18.5s | 3.5 (읽기 없음 0건) | 0.27 |
| Textualize/rich | n=3 | 100% | 89% | 31% | 1/3 (33%) | 5.3 | $0.0422 | 12.1s | 3.0 (읽기 없음 0건) | 0.07 |

## 전체 요약

| n | 유효 | 구체 | def줄 | 도달 | 도구 | 비용 평균 | 시간 | 비용 합 | 첫읽기까지 | 체인 |
|---|---|---|---|---|---|---|---|---|---|---|
| n=10 | 100% | 94% | 39% | 5/10 (50%) | 7.0 | $0.0602 | 13.5s | $0.6023 | 3.2 (읽기 없음 0건) | 0.21 |

- **엄격 통과 비율**: 10/10 (100%)
- **read_code 0회 건수**: 0/10
- **탐색 차단 발동 건수**: 0/10
- **끝 이름 도달**: 5/10 (50%)

## 도구 호출 순서

- ns-requests-01: find_definition → find_callers → find_definition → find_callees → read_code
- ns-requests-02: find_definition → read_code → find_definition → read_code
- ns-requests-03: find_definition → search_code → find_definition → read_code → find_callers → find_definition → read_code → find_definition → read_code → find_callers
- ns-httpx-01: find_definition → search_code → find_definition → search_code → find_definition → read_code → find_callees → find_definition → read_code
- ns-httpx-02: find_definition → search_code → search_code → read_code → search_code → read_code
- ns-click-01: find_definition → read_code → find_callees → find_definition → read_code → find_definition → read_code → find_definition → read_code → find_definition → read_code → find_definition → read_code
- ns-click-02: find_definition → search_code → search_code → search_code → search_code → find_definition → read_code
- ns-rich-01: find_definition → search_code → search_code → search_code → read_code → search_code → read_code
- ns-rich-02: find_definition → search_code → search_code → find_definition → read_code
- ns-rich-03: search_code → read_code → find_definition → read_code

## before-1: Supervisor 게이트 (같은 10개, fast 분류 10회, $0.0009, raw `23-nosymbol-before-gate.json`)

| | 수 |
|---|---|
| LLM 이 ask 로 분류 | 9/10 |
| 게이트 적용 후 ask (따라가기 도달) | **0/10** |
| 게이트가 map 으로 내림 | 9 |
| LLM 이 chat 으로 오분류 | 1 (ns-rich-03 "트루컬러 색은 어디서 가까운 색으로 바뀌어?") |

`SYMBOL_PATTERN` 매치 0건 — 10개 모두 식별자가 없어 현재 UI 에서는 **전부 읽기 지도가 다시 나온다**(사용자가 질문한 것과 무관한 응답). 이것이 Phase 5.5-4 의 before 값이다.

## 관찰 (before-2: 게이트를 우회해 기존 도구 5개로 돌렸을 때)

- **이름 없이도 절반은 도달한다 (5/10, 체인 0.21).** quality agent 는 질문의 뜻에서 식별자를 추측해 `find_definition` 으로 시작했다(10개 중 9개의 첫 호출). "리디렉트 횟수" → `resolve_redirects`, "쿠키 합치기" → `merge_cookies` 처럼 널리 알려진 라이브러리라 이름을 아는 것이지, 코드에서 찾은 것이 아니다 — 생소한 레포에서는 기대할 수 없는 경로다.
- **실패 5건은 전부 "어디" 를 못 찾은 경우다.** `search_code`(부분 문자열) 를 15회 썼고, 그 중 ns-click-02 는 4연속, ns-rich-01 은 4회. 문자열 검색은 키워드가 코드에 그대로 있어야 맞는데(예: "환경 변수" → `envvar` 를 모델이 번역해야 함) 질문의 말과 코드의 말이 달라 헛돈다. 도달한 5건의 첫 read_code 까지 호출 수 평균 3.8, 실패 5건 2.4 — 실패는 **일찍 읽었지만 엉뚱한 곳**을 읽은 것이다(ns-click-01: 읽기 6회, $0.17, 도달 X).
- **엄격 통과 10/10, 유효 100%** — 인용 장치는 이름 없는 질문에서도 그대로 작동한다. 틀린 답도 "읽은 코드" 를 인용한다는 뜻이라, 도달 여부는 인용 지표로는 알 수 없다(기록 12·13 과 같은 결론).
- 비용 질문당 $0.060, 13.5s — 기록 13(이름 있는 질문, $0.083·14.4s) 보다 싸다. 이름을 모르니 깊이 못 들어가고 일찍 끝낸 것.
- **after 에서 볼 것**: 도달 5/10 → ?, 체인 0.21 → ?, `search_code` 15회 → `search_symbols` 로 대체되는지, 첫 read_code 까지 호출 수 3.2 → ?, 임베딩 비용(레포당 1회) 포함 총비용.
