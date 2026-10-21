# 실험 결과

측정한 실험만 기록한다. 수치에는 n과 평가 방법을 함께 적는다.

| 번호 | 실험 ID | 변경 요약 | 지표(전 → 후) | n | 평가 방법 | LangSmith 링크 | 커밋 |
|---|---|---|---|---|---|---|---|
| [01](01-indexing-baseline.md) | indexing-baseline | Phase 1 정적 인덱싱 첫 측정 | files 20~541, time 1.0~3.6s (LLM $0) | 3 | `repoute index` 벽시계 시간, clone 포함 | — (LLM 미사용) | (커밋 후 기재) |
| [02](02-reading-guide-first-run.md) | reading-guide-first-run | Phase 2 읽기 지도 첫 실행 (requests) | $0.018, 8.0s, 인용 8/8 유효, 재생성 0회 | 1 | CLI 벽시계 시간 + LangSmith 토큰·비용 | trace 01a15a37-bd17-74db-a84a-7976ed002608 | (커밋 후 기재) |
| [03](03-e4-validation-loop.md) | e4-validation-loop | 인용 검증 루프 유무 (naive vs validate), 레포 5개 | citation_validity naive 97% → validate 97% (효과 없음), 비용 $0.021 → $0.029 | 5 | 최종 인용 사후 검증, 토큰 콜백, LangSmith 교차 확인 | 태그 `e4` | (커밋 후 기재) |
| [04](04-e6-prompt-v2.md) | e6-prompt-v2 | 줄 1 인용 가능 입력 + 프롬프트 v2 (fastapi 재측정) | citation_validity 83% → 100%, 재생성 2→0, 비용 $0.043→$0.022. 단, citation_specificity 83% → 43% (구체성 하락) | 1 | 사후 검증 + LangSmith 트레이스 인용 목록 | 태그 `prompt-v1`/`prompt-v2` | (커밋 후 기재) |
| [05](05-e6-prompt-v3.md) | e6-prompt-v3 | 심볼 있는 파일엔 줄 1 비허용 + 프롬프트 v3 (fastapi 재측정) | citation_validity 100% 유지, citation_specificity 43% → 100%, 재생성 0, $0.022 | 1 | 사후 검증 citation_validity·specificity, 토큰 콜백 | 태그 `prompt-v3` | (커밋 후 기재) |
| [06](06-v3-regression.md) | v3-regression | 프롬프트 v3 기본값으로 레포 5개 회귀 측정 | citation_validity 100%, citation_specificity 100%, 재생성 0, 평균 $0.022·7.6s | 5 | 사후 검증 validity·specificity, 토큰 콜백 | 태그 `prompt-v3` | (커밋 후 기재) |
| [07](07-walkthrough-first-run.md) | walkthrough-first-run | Phase 3 따라가기 첫 실행 (requests.get 질문) | 인용 4/4 유효·구체적, 재작성 0, 도구 15회(상한), $0.080, 24.1s. Session.request 이후 미도달 | 1 | 체크포인트 최종 상태 + LangSmith 토큰·비용 | 00:09:41 UTC 트레이스 | (커밋 후 기재) |
| [08](08-walkthrough-v2.md) | walkthrough-v2 | 점 붙은 이름 재시도·read_code 힌트·읽기 전략 프롬프트 (같은 질문 재측정) | 도구 15→13, Session.send 도달(4→5단계), 인용 5/5 유효·구체적. 비용 $0.080→$0.099 (+23%, 턴당 컨텍스트 증가) | 1 | 체크포인트 + LangSmith | 00:16:07 UTC | (커밋 후 기재) |
| [09](09-walkthrough-fast-agent.md) | walkthrough-fast-agent (E5) | agent 턴 gpt-4o-mini + def 줄 힌트 + 재읽기 차단 (통제군 포함) | fast: $0.099→$0.025 (−75%), send 도달 유지, def 줄 0%→100%. quality 통제군: $0.043, 3단계에서 멈춤 | 1×2 | 체크포인트 채점 + LangSmith | 00:23:59 / 00:24:12 UTC | (커밋 후 기재) |
| [10](10-walkthrough-batch.md) | walkthrough-batch | 따라가기 질문 20개 배치 (레포 4개, fast agent, v3) | 사후 유효 90% 이나 **엄격 통과 13/20=65%** (read_code 0회 7건이 전부 실패), 도달 11/20(수동), 평균 $0.022·19.3s | 20 | 체크포인트 채점 + LangSmith validate 출력 교차 확인 | 태그 `wt-batch` | (커밋 후 기재) |
| [11](11-walkthrough-batch-nudge.md) | walkthrough-batch-nudge | 10 + "읽지 않고 답하면 넛지" | 엄격 13/20 (변화 없음). 넛지 발동 3건은 전부 성공, 실패 7건은 답하기 전 상한 도달로 넛지 미발동 | 20 | 10 과 동일 | 태그 `wt-batch` | (커밋 후 기재) |
| [12](12-walkthrough-batch-explore-limit.md) | walkthrough-batch-explore-limit | 11 + 읽기 없이 탐색 6회면 도구 층에서 거부 | **엄격 통과 13→20/20, read_code 0회 7→0, 재작성 7→0, $0.42→$0.37.** 도달 8→4/20 (얕은 답, trade-off) | 20 | 10 과 동일 | 태그 `wt-batch` | (커밋 후 기재) |
| [13](13-walkthrough-batch-quality.md) | walkthrough-batch-quality (E5) | 12 와 동일, agent 만 gpt-4o | 엄격 20/20 유지, **도달 4→13/20**, 단계 3.3→4.9, 도구 11.9→9.9, 시간 17→14s, 질문당 $0.018→$0.083 (4.5배) | 20 | 12 와 동일 | 태그 `wt-batch` | (커밋 후 기재) |
| [14](14-phase4-baselines.md) | phase4-baselines | 정답 데이터셋(레포 5, 질문 20 체인) + 평가자 4종, 현재 수치 측정 | 정적 진입점 재현율 **0.05**, PageRank p@5 **0.24**; 체인 정확도 fast 0.49 / fast+상한 0.28 / quality 0.60 | 5 레포 / 20 질문 | 결정론적 평가자, 기존 raw 재채점 | — (LLM 미사용) | (커밋 후 기재) |
| [15](15-entrypoints-v2.md) | entrypoints-v2 | 공개 API export 기반 진입점 후보·점수, docs_src 제외 | 정적 entrypoint_recall@10 **0.05 → 0.58**, @5 0.43 (동점 처리가 운에 의존, fastapi 76파일) | 5 | static_baseline.py (LLM 미사용) | — | (커밋 후 기재) |
| [16](16-reading-guide-gold.md) | reading-guide-gold (E1 실측) | 읽기 지도 LLM 출력(v3 + 진입점 v2 + docs_src 제외)을 gold 로 채점 | entrypoint_recall **0.27**, core_module_p@5 **0.32** (정적 0.24), 인용 100%. 읽기 순서가 PageRank 순서를 그대로 따름 | 5 | e4 스크립트 gold 채점, LangSmith 교차 | 태그 `prompt-v3` | (커밋 후 기재) |
| [17](17-ranking-v2.md) | ranking-v2 | 역방향 개인화 PageRank + `__init__` 감쇠 (22개 변형 비교) | 정적 core_module_p@5 **0.24 → 0.60** (시드 효과 미확인, 과적합 경고) | 5 | static_baseline.py | — | (커밋 후 기재) |
| [18](18-reading-guide-rank-v2.md) | reading-guide-rank-v2 | 랭킹 v2 연결 후 읽기 지도 vs gold, 첫 LangSmith Experiment | core_module_p@5 **0.32 → 0.68**, entrypoint_recall 0.27 → 0.32 (scripts `main` 문제), 인용 100% | 5 | langsmith_eval.py (evaluate) | Experiment `reading-guide-gold-v3-13bfef5b` | (커밋 후 기재) |

## 이름 규칙

- 상세 파일: `<NN>-<experiment-id>.md` (번호 순, 이 디렉터리에 둔다. 날짜는 파일 안에 적는다)
- LangSmith 실험 이름: `<module>-<dataset>-<yyyymmdd>-<variant>` (예: `reading-guide-gold-20261101-baseline`)
