# 16. 읽기 지도 LLM 출력 vs gold (E1 실측) (Phase 2, 2026-10-21)

## 방법

- **n = 5** 레포, 모드 2개(naive=`validate=False`, validate=검증 루프, 재생성 상한 2회)
- **모델**: fast = gpt-4o-mini, quality = gpt-4o (openai), temperature 0. 프롬프트 v3.
- **합성 입력**: 모듈마다 `인용 가능한 줄: 이름:줄, ...` 목록(심볼 없는 모듈만 `1(모듈 시작)`)과 검증 오류별 힌트(`→ 인용 가능한 줄`·`비슷한 경로`)를 넣는다. 03·04 기록 이후 모든 프롬프트 공통으로 바뀐 입력이라, 그 결과와는 프롬프트 외에 이 점도 다르다.
- **인용 유효성**: 두 모드 모두 최종 guide 의 인용(진입점 + 각 단계 ref)을 `citation_validity`·`validate_citations` 로 사후 검증 (naive 는 루프 없이 결과만 채점).
- **인용 구체성**: `citation_specificity` = 유효하면서 그 파일의 심볼 범위 안을 가리키는 인용 비율 (심볼 없는 파일은 줄 1 도 인정).
- **토큰**: `UsageMetadataCallbackHandler` 로 그래프 안 LLM 호출을 모델별 합산.
- **비용**: 스크립트의 `PRICES` 단가로 계산한 추정치. LangSmith(태그 `e4`)의 비용과 교차 확인 가능.
- **시간**: 그래프 `invoke` 의 벽시계 시간(perf_counter). 인덱싱·clone 제외.
- **gold 채점**: 진입점 recall = gold 진입점 중 guide.entrypoints 에 (같은 file, 심볼 끝 이름) 으로 있는 비율, 핵심 p@5 = reading_order 파일의 모듈 상위 5개 중 gold 핵심 모듈 비율(`evals/evaluators/metrics.py`). gold 에 없는 레포는 '—'. 정적 베이스라인(기록 14): recall 0.05, p@5 0.24.

## 레포별 결과

| 레포 | 모드 | files | targets | 인용 수 | 유효 비율 | 구체성 | 오류 수 | 재생성 | in/out 토큰 | 비용 | 시간 | 진입점 recall | 핵심 p@5 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| psf/requests@611c616 | validate | 20 | 13 | 6 | 100% | 100% | 0 | 0 | 19,573 / 1,426 | $0.0175 | 6.0s | 0.33 | 0.40 |
| encode/httpx@b5addb6 | validate | 23 | 14 | 9 | 100% | 100% | 0 | 0 | 23,017 / 1,703 | $0.0215 | 7.4s | 0.00 | 0.20 |
| fastapi/fastapi@6f28852 | validate | 76 | 16 | 11 | 100% | 100% | 0 | 0 | 18,552 / 1,943 | $0.0193 | 8.0s | 0.25 | 0.00 |
| pallets/click@2247b35 | validate | 17 | 12 | 12 | 100% | 100% | 0 | 0 | 24,582 / 1,605 | $0.0249 | 6.3s | 0.25 | 0.40 |
| Textualize/rich@9d8f9a3 | validate | 109 | 14 | 9 | 100% | 100% | 0 | 0 | 21,497 / 1,550 | $0.0195 | 6.7s | 0.50 | 0.60 |

## 모드별 평균

| 모드 | n | 유효 비율 평균 | 구체성 평균 | 오류 합 | 비용 평균 | 시간 평균 | 진입점 recall 평균 | 핵심 p@5 평균 |
|---|---|---|---|---|---|---|---|---|
| validate | n=5 | 100% | 100% | 0 | $0.0205 | 6.9s | 0.27 | 0.32 |

## 관찰 (LangSmith 교차 확인: 5건 $0.103)

**E1 실측: LLM 합성은 정적 입력을 거의 그대로 따라 쓴다.** 정적 베이스라인(14·15) 대비 LLM 출력의 개선 폭은 작다.

| 지표 | 정적 (PageRank / 진입점 v2 후보) | **LLM 출력 (16)** |
|---|---|---|
| entrypoint_recall | 후보 상위 10: 0.58, 상위 5: 0.43 | **0.27** (LLM 이 후보 중 1~4개를 고름) |
| core_module_precision@5 | PageRank 0.24 | **0.32** |
| citation_validity / specificity | — | 100% / 100% (06 과 같음) |
| 비용 / 시간 | $0 | $0.021 / 6.9s |

1. **진입점**: LLM 은 후보 목록에서 고르는데, httpx 는 `_main.py:main`, fastapi 는 `cli.py:main` 을 1순위로 골랐다. 15 에서 지적한 "CLI main 이 라이브러리에서 1위" 가 그대로 LLM 출력으로 전달된 것. requests 는 `api.request` 1개만(gold 3개 중 1), click 은 클래스 4개 중 `Command`·`Group` 만 맞음(gold 는 `command`·`option` 데코레이터 포함). rich 가 가장 좋음(0.50: `print`, `Console.print`).
2. **읽기 순서 = PageRank 순서.** requests: compat → models → structures → `__init__` → api (PageRank 1~4위 그대로). httpx: _models → _types → _urls → _exceptions. fastapi: exceptions → types → _compat. 프롬프트가 "의존성 없는 것부터" 라고 지시하고 PageRank 상위를 "핵심 모듈" 로 제시하니 모델이 그 순서를 쓴다. **LLM 은 입력의 순서를 재배열하지 않는다.** 06·08·09·12 와 같은 교훈의 또 한 번: 원하는 결과는 입력에 있어야 한다.
3. **따라서 개선 대상은 프롬프트가 아니라 정적 입력이다**: (a) 핵심 모듈 랭킹을 "많이 import 되는" (PageRank) 에서 "진입점에서 가까운" (진입점 모듈을 시드로 한 개인화 PageRank, 또는 진입점에서의 import 거리) 으로, (b) compat·utils·types·exceptions 같은 하부 부품을 랭킹에서 감점(이름 패턴, 또는 "import 는 많이 되지만 import 는 거의 안 하는" 리프 특성), (c) 진입점 후보에서 scripts 없는 레포의 `main` 제거. 전부 LLM 없이 `static_baseline.py` 로 먼저 재고(측정 원칙 2), 좋아지면 LLM 1회($0.1)로 확인.
4. **인용 유효성·구체성은 100% 유지.** 진입점 v2·docs_src 제외가 그래프에 들어갔지만 근거 품질은 흔들리지 않았다. fastapi 76 파일 기준 비용 $0.019 (541 파일 때 $0.022).
5. 이 기록으로 기획서 실험 E1("LLM 단독 vs 정적+LLM")의 절반이 나왔다. "LLM 단독"(파일을 직접 넣는 방식)은 측정하지 않았고, 비용·맥락 길이 때문에 레포 1개로만 비교할 가치가 있다(후순위).

## 비용
- 이 실행 $0.10. 프로젝트 누적 약 $3.75.
