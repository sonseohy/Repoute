# 14. Phase 4 베이스라인 — 정답 데이터셋 대비 현재 수치 (2026-10-21)

Phase 4 의 첫 작업은 "지금 어디에 있는가" 를 정답 기준으로 재는 것이다. 모든 수치는 LLM 추가 호출 없이 계산했다(이미 있는 raw 와 정적 분석만).

## 데이터셋 (evals/datasets/, README 에 스키마·한계)
- **reading_guide_gold.json**: 레포 5개(requests, httpx, click, rich, fastapi). 진입점 2~4개(총 19), 핵심 모듈 5개(총 25). 소스를 직접 읽고 라벨(Opus 에이전트), 모든 심볼·모듈이 인덱스에 존재하는지 확인. **사용자 검수 전.**
- **walkthrough_questions.json**: 질문 20개에 `expect_chain`(핵심 심볼 순서 3~6개, 총 89개 심볼, 근거 파일:줄 81개 전부 존재 확인) 추가.
- 평가자 4종(`evals/evaluators/metrics.py`, 결정론적): `entrypoint_recall`, `core_module_precision_at_k`, `reached`(끝 이름 매칭), `call_chain_accuracy`(LCS/len(gold)).

## 결과 1 — 정적 휴리스틱 vs gold (n=5 레포, `static_baseline.py`)

| 레포 | entrypoint_recall | core_module_p@5 | PageRank 상위 5 |
|---|---|---|---|
| requests | 0.00 | 0.20 | compat, models, structures, `__init__`, cookies |
| httpx | 0.00 | 0.20 | _models, _types, _urls, _exceptions, _utils |
| click | 0.25 | 0.40 | _compat, core, types, utils, _winconsole |
| rich | 0.00 | 0.40 | cells, console, _unicode_data, text, _versions |
| fastapi | 0.00 | 0.00 | fastapi, exceptions, _compat.v2, responses, datastructures |
| **평균** | **0.05** | **0.24** | |

- **진입점 휴리스틱은 사실상 작동하지 않는다** (19개 중 1개: click 의 `Command.main`, 이름이 `main` 이라 우연히 맞음). troubleshooting 10 이 수치로 확정됐다. 라이브러리형 레포의 진입점은 공개 API(`api.get`, `Client`, `Console.print`, `FastAPI`)인데 휴리스틱은 `main`/`cli.py` 만 본다.
- **PageRank 는 "많이 import 되는 하부 부품"(compat, _types, cells) 을 올리고, 흐름의 시작점(api, _client)은 내린다.** gold 핵심 모듈은 PageRank 13~31위에 있다. troubleshooting 11 확정. fastapi 는 docs_src 864개 심볼 때문에 0.00.
- 즉 **읽기 지도의 "어디서부터" 는 현재 PageRank + `main` 휴리스틱으로는 맞출 수 없다.** 인용 유효성 100%(06)는 "근거가 틀리지 않음" 이지 "출발점이 맞음" 이 아니다 — 두 지표가 분리돼 있어야 하는 이유가 여기서 확정된다.

## 결과 2 — 따라가기 호출 체인 정확도 (기존 raw 재채점, `rescore_batch.py`)

| 기록 | 설정 | 도달(끝 이름 매칭) | **call_chain_accuracy 평균** |
|---|---|---|---|
| 10 | fast, 장치 없음 | 10/20 | 0.49 |
| 12 | fast + 탐색 상한 | 4/20 | **0.28** |
| 13 | quality + 탐색 상한 | 13/20 | **0.60** |

- 체인 정확도가 도달보다 정보가 많다: 12 는 도달 20% 지만 체인의 28% 는 맞게 따라갔고, 13 은 65% 도달에 60% 체인. 탐색 상한이 fast 의 체인을 0.49 → 0.28 로 깎은 것이 수치로 보인다(근거 100% 의 대가).
- 끝 이름 매칭 도달 판정은 수동 보정값과 일치(12: 4, 13: 13).

## 다음 (Phase 4 실험 후보, 우선순위 순)
1. **진입점 탐지 v2** (LLM 없음, 비용 0): `__init__.py` 가 export 하는 이름(import·`__all__`), 공개 클래스(대문자, `_` 아님)와 그 메서드 중 이름이 `request/send/print/get` 류, `help`/`debug`/`_compat`/`testing` 파일 감점. 목표: entrypoint_recall 0.05 → 0.5 이상(n=5). 그다음 PageRank 를 "진입점에서 가까운 모듈" 로 재가중(개인화 PageRank: 진입점 모듈을 시드로) → core_module_p@5.
2. **읽기 지도 LLM 출력 채점**: e4 스크립트가 guide.entrypoints·reading_order 파일을 raw 에 남기게 하고 gold 로 `entrypoint_recall`·`core_module_p@5` 계산. 정적 베이스라인(0.05/0.24) 대비 LLM 합성이 얼마나 보정하는지(E1 의 실제 측정). 레포 5개 × $0.022.
3. **docs_src 제외**: `SKIP_DIRS` 에 `docs_src`(또는 `examples` 류 패턴) → fastapi 재측정.
4. **LangSmith 데이터셋 등록 + CI 회귀**: `langsmith_datasets.py` 로 두 데이터셋 업로드(과금 없음), GitHub Actions 에서 PR 마다 읽기 지도 5개(약 $0.11)만 돌려 `citation_validity`·`entrypoint_recall` 이 기준 아래면 실패. 따라가기 20개($0.4~1.7)는 수동 트리거.
5. **LLM-as-judge `explanation_faithfulness`**: 설명이 인용된 코드와 일치하는지. 13 의 답 20개로 첫 측정.

## 비용
- 이 기록: $0 (정적 분석 + 기존 raw).
