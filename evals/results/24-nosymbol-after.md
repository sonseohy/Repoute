# 24. 함수 이름 없는 질문 10개 — after (search_symbols 추가) (Phase 3, 2026-10-21)

## 방법

- **n = 10** 질문 (`evals/datasets/walkthrough_questions_nosymbol.json`, 레포 4개), 질문마다 체크포인터 없이 새 상태로 실행.
- **모델**: agent = gpt-4o (quality), finalize = gpt-4o (openai), temperature 0. 프롬프트 v3.
- **유효·구체·def줄**: 최종 답 인용을 `citation_validity`·`citation_specificity`·`citation_at_def` 로 사후 채점.
- **엄격**: 최종 `validation_errors` 가 0개이고 인용이 1개 이상(그래프 검증 = 존재 + read_code 로 읽은 범위 안). 사후 유효성은 파일:줄 존재만 보므로 엄격 통과를 주 지표로 본다.
- **읽기**: 실제 실행된 read_code 호출 수("이미 읽은 범위" 차단·상한 미실행 제외).
- **도달**: `expect_reach`(사람이 코드를 보고 정한 기대 도달 심볼)가 어떤 단계의 제목·설명 또는 인용 심볼 문자열에 들어 있는지 보는 **자동 근사치**. 사람 판정 아님.
- **차단**: "이미 읽은 범위" 로 막힌 read_code 수. **도구**: 실행된 도구 호출 수.
- **탐색차단**: 읽기 없이 탐색만 6회 한 뒤 실행하지 않은 find_* 수(예산은 소모).
- **비용**: `UsageMetadataCallbackHandler` 토큰 × 스크립트 `PRICES` 단가 추정치. LangSmith 태그 `wt-nosymbol-after` 로 교차 확인. **시간**: invoke 벽시계(인덱싱 제외).
- **첫읽기**: 첫 실행된 read_code 앞에 agent 가 낸 도구 호출 수(차단 포함). "없음" = 실행된 read_code 가 없음. 평균은 읽은 질문만.
- **체인**: `metrics.call_chain_accuracy(predicted_chain_from_refs(인용), expect_chain)` (인용 순서 심볼 vs 기대 호출 체인, 끝 이름 LCS / 기대 길이).
- **끝도달**: `metrics.reached`(인용 심볼 끝 이름 매칭, 흔한 이름은 파일·한정자 확인). **도달** 은 기존 문자열 포함 판정 그대로.
- **도구 호출 순서**: AIMessage.tool_calls 순서. "!blocked" = 도구 층에서 실행 안 됨, "!error" = 도구 오류.

## 질문별 결과

| id | 질문 | 도구 | 읽기 | 차단 | 탐색차단 | 단계 | 인용 | 엄격 | 유효 | 구체 | def줄 | 도달 | 비용 | 시간 | 첫읽기 | 체인 | 끝도달 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| ns-requests-01 | 리디렉트를 너무 많이 따라가면 어디서 예외가 나? | 6 | 1 | 0 | 0 | 3 | 3 | O | 100% | 100% | 33% | O | $0.0480 | 10.0s | 5 | 0.33 | O |
| ns-requests-02 | 요청을 보내기 전에 세션에 저장된 쿠키와 요청마다 넘긴 쿠키는 어디서 합 | 2 | 1 | 0 | 0 | 4 | 4 | O | 100% | 100% | 25% | O | $0.0187 | 6.8s | 1 | 0.33 | O |
| ns-requests-03 | 환경 변수에 설정해 둔 프록시 주소는 어디서 읽어 와? | 6 | 1 | 0 | 0 | 2 | 2 | O | 100% | 100% | 50% | O | $0.0328 | 9.9s | 1 | 0.33 | O |
| ns-httpx-01 | 리디렉트를 따라갈 때 다음 요청의 메서드가 바뀌는 건 어디서 정해져? | 7 | 3 | 0 | 0 | 3 | 3 | O | 100% | 100% | 100% | O | $0.0452 | 11.6s | 2 | 0.40 | O |
| ns-httpx-02 | 압축돼서 온 응답 본문을 풀 때, 압축 방식에 맞는 해제기는 어디서 골라 | 13 | 4 | 0 | 0 | 5 | 5 | O | 100% | 80% | 20% | X | $0.1144 | 16.2s | 4 | 0.00 | X |
| ns-click-01 | 옵션을 명령줄에 안 주면 환경 변수에서 값을 읽어 오던데, 그건 어디서  | 4 | 3 | 0 | 0 | 3 | 3 | O | 100% | 100% | 100% | X | $0.0410 | 8.9s | 1 | 0.17 | O |
| ns-click-02 | 값을 안 준 옵션에 대해 사용자에게 직접 입력을 물어보는 건 어디서 해? | 4 | 3 | 0 | 0 | 3 | 3 | O | 100% | 100% | 100% | O | $0.0368 | 8.7s | 1 | 0.40 | O |
| ns-rich-01 | 출력할 때 터미널 폭은 어디서 계산해? 환경 변수로 바꿀 수도 있어? | 7 | 2 | 0 | 0 | 4 | 4 | O | 100% | 100% | 25% | X | $0.0592 | 11.7s | 3 | 0.33 | O |
| ns-rich-02 | 긴 문장을 출력하면 폭에 맞춰 줄을 나누는 건 어디서 해? | 2 | 1 | 0 | 0 | 4 | 4 | O | 100% | 100% | 25% | O | $0.0217 | 7.9s | 1 | 0.20 | O |
| ns-rich-03 | 터미널이 256색까지만 지원하면 트루컬러 색은 어디서 가까운 색으로 바뀌 | 2 | 1 | 0 | 0 | 4 | 4 | O | 100% | 100% | 75% | X | $0.0319 | 11.7s | 1 | 0.00 | X |

## 레포별 평균

| 레포 | n | 유효 | 구체 | def줄 | 도달 | 도구 | 비용 | 시간 | 첫읽기까지 | 체인 |
|---|---|---|---|---|---|---|---|---|---|---|
| psf/requests | n=3 | 100% | 100% | 36% | 3/3 (100%) | 4.7 | $0.0331 | 8.9s | 2.3 (읽기 없음 0건) | 0.33 |
| encode/httpx | n=2 | 100% | 90% | 60% | 1/2 (50%) | 10.0 | $0.0798 | 13.9s | 3.0 (읽기 없음 0건) | 0.20 |
| pallets/click | n=2 | 100% | 100% | 100% | 1/2 (50%) | 4.0 | $0.0389 | 8.8s | 1.0 (읽기 없음 0건) | 0.28 |
| Textualize/rich | n=3 | 100% | 100% | 42% | 1/3 (33%) | 3.7 | $0.0376 | 10.4s | 1.7 (읽기 없음 0건) | 0.18 |

## 전체 요약

| n | 유효 | 구체 | def줄 | 도달 | 도구 | 비용 평균 | 시간 | 비용 합 | 첫읽기까지 | 체인 |
|---|---|---|---|---|---|---|---|---|---|---|
| n=10 | 100% | 98% | 55% | 6/10 (60%) | 5.3 | $0.0450 | 10.3s | $0.4497 | 2.0 (읽기 없음 0건) | 0.25 |

- **엄격 통과 비율**: 10/10 (100%)
- **read_code 0회 건수**: 0/10
- **탐색 차단 발동 건수**: 0/10
- **끝 이름 도달**: 8/10 (80%) (기존 도달과 다른 질문: ns-click-01, ns-rich-01)

## 도구 호출 순서

- ns-requests-01: search_symbols → search_code → find_definition → find_callers → find_definition → read_code
- ns-requests-02: find_definition → read_code
- ns-requests-03: search_symbols → read_code → find_definition → search_code → search_code → search_code
- ns-httpx-01: find_definition → search_symbols → read_code → find_definition → read_code → find_definition → read_code
- ns-httpx-02: find_definition → search_symbols → search_code → find_definition → read_code → read_code → find_definition → read_code → find_definition → search_symbols → find_definition → search_code → read_code
- ns-click-01: search_symbols → read_code → read_code → read_code
- ns-click-02: search_symbols → read_code → read_code → read_code
- ns-rich-01: search_symbols → search_code → search_code → read_code → search_code → search_code → read_code
- ns-rich-02: search_symbols → read_code
- ns-rich-03: search_symbols → read_code

## before(23) → after 비교 (같은 10개 질문, 같은 quality agent·프롬프트 v3, 도구만 5 → 6)

| 지표 | before (23) | **after (24)** |
|---|---|---|
| 도달 (expect_reach 문자열) | 5/10 | **6/10** |
| 끝 이름 도달 (metrics.reached) | 5/10 | **8/10** |
| 체인 정확도 평균 | 0.21 | **0.25** |
| 첫 read_code 까지 호출 수 | 3.2 | **2.0** |
| 도구 호출 평균 | 7.0 | **5.3** |
| `search_code`(부분 문자열) 호출 합 | 15 | **10** |
| `search_symbols` 호출 합 | — | 10 (10개 중 8개가 첫 호출로 사용) |
| 엄격 통과 / 유효 | 10/10 / 100% | 10/10 / 100% |
| 비용 합 / 질문당 | $0.60 / $0.060 | **$0.45 / $0.045 (−25%)** |
| 시간 평균 | 13.5s | **10.3s** |
| UI 경로(게이트) 기준 도달 | **0/10** (전부 읽기 지도로) | 6/10 (게이트 완화 시) |

질문별 도달 변화: ns-requests-03 X→O (`search_symbols → read_code` 로 바로 `get_environ_proxies`). 나머지 9개는 전/후 같음. 체인은 4개 상승·2개 하락(ns-requests-02 0.67→0.33, ns-click-01 0.33→0.17: 바로 찾아 짧게 답해 중간 단계가 빠짐).

## 관찰

- **가장 큰 효과는 UI 경로다.** 이름 없는 질문은 게이트 때문에 0/10 이 따라가기에 닿지 못했는데, 임베딩 도구가 있으면 게이트를 열 수 있고 그러면 6/10 (끝 이름 기준 8/10) 이 답을 받는다. 도구 자체의 효과(5→6)보다 "도구가 있어서 게이트를 열 수 있게 된 것" 이 더 크다.
- **탐색이 짧아졌다.** 첫 read_code 까지 3.2 → 2.0 회, 도구 7.0 → 5.3, 비용 −25%. 10개 중 8개가 `search_symbols` 로 시작했고, 그 중 5개는 바로 `read_code` 로 넘어갔다(`search_symbols → read_code`). before 에서 이름을 추측해 `find_definition` 으로 시작하던 경로가 검색으로 바뀌었다.
- **아직 실패하는 4건**: ns-httpx-02(`Response._get_content_decoder`, 13회·$0.11 로 가장 비쌈 — 검색 2회 뒤에도 `search_code` 로 헛돔), ns-click-01(`Parameter.resolve_envvar_value` — 검색 뒤 읽기 3회로 끝, 끝 이름 기준은 도달), ns-rich-01(`Console.size` property — 호출 그래프에 없음, 끝 이름 기준 도달), ns-rich-03(`Color.downgrade` — 검색 → 읽기 1회로 끝, 엉뚱한 곳). 이 중 2건은 "끝 이름 매칭" 으로는 도달이라 `reached` 두 지표가 갈린다 — 사람이 봐야 하는 구간.
- **`search_code` 가 사라지지 않았다(15 → 10).** 검색 결과 상위 5개에 정답이 없을 때 모델은 다시 문자열 검색으로 간다. 상위 5개에 정답이 있었는지는 이 raw 로는 모른다(도구 출력 미기록) — 다음 측정에서 `search_symbols` 의 **recall@5** 를 따로 재야 한다(LangSmith 트레이스에서 도구 출력 읽기, 또는 스크립트가 도구 출력 저장).
- **비용**: 임베딩 4레포 첫 생성은 토큰 콜백에 잡히지 않아 표의 비용에 없다. 추정 $0.003(기록 23 계산식). 질문 비용이 $0.15 줄어 임베딩 비용은 첫 질문에서 회수된다.
- **한계**: n=10, 레포 4개 모두 유명 라이브러리(before 에서 이름 추측이 통한 이유). 체인 정확도는 0.25 로 여전히 낮다 — 검색은 "어디" 를 찾아 주지만 "어떤 순서로 흘러가는지" 는 못 준다. 임베딩 모델은 text-embedding-3-small 하나만 비교.

**결정**: `search_symbols` 를 API·UI 기본으로 켠다(게이트 완화 포함). 레포당 첫 질문에서 임베딩 1회(수천 심볼 ≤ $0.005), 이후 캐시.
