# 20. 읽기 지도 + 진입점 v3 — LangSmith Experiment 비교 (Phase 4, 2026-10-21)

기록 19 의 진입점 v3 를 그래프에 반영한 뒤 reading-guide-gold 를 다시 `evaluate()`. LangSmith 에 Experiment 2개가 나란히 있어 Compare 뷰로 레포별 변화를 볼 수 있다.

- **n = 5**, 프롬프트 v3, 랭킹 v2, 진입점 v3. Experiment `reading-guide-gold-v3-d42ebe4b` (비교 대상 18: `…-13bfef5b`). 비용 $0.103.

## 결과

| 레포 | entrypoint_recall 18 → **20** | core p@5 18 → **20** | LLM 이 고른 진입점 (20) |
|---|---|---|---|
| requests | 0.33 → **0.67** | 0.80 → 0.80 | request, get |
| httpx | 0.00 → **0.25** | 0.60 → **0.80** | request |
| click | 0.25 → **0.50** | 1.00 → 1.00 | Context, Command, Group, Command.main |
| fastapi | 0.00 → **0.25** | 0.80 → 0.80 | FastAPI |
| rich | 1.00 → 1.00 | 0.20 → 0.20 | get_console, reconfigure, print, … 6개 |
| **평균** | **0.32 → 0.53** | **0.68 → 0.72** | 인용 유효성 1.0 유지 |

## 관찰
1. **네 번째 확인: 후보를 고치면 LLM 이 따라온다.** 진입점 v3 는 정적 recall 평균을 바꾸지 않았지만(19) 1위 후보를 바꿨고, LLM 출력 recall 이 0.32 → 0.53 으로 올랐다. CLI `main` 은 어떤 레포의 LLM 출력에도 없다(18 에서는 httpx·fastapi 둘 다 `main`).
2. 핵심 모듈 p@5 도 0.68 → 0.72. httpx 가 0.60 → 0.80: 시드에서 `_main` 이 빠져 `_auth` 가 상위 5 에 들어왔다. 진입점 → 시드 → 랭킹 연쇄가 이번엔 좋은 방향으로 작동.
3. **남은 것은 둘.** (a) httpx·fastapi 진입점 0.25 — LLM 이 후보 중 1개만 고른다(`request`, `FastAPI`). gold 는 4개(`get`, `request`, `Client`, `Client.send`). 후보 상위에 있는데 안 고르는 것이라 프롬프트("진입점 2~5개")나 후보 제시 수를 볼 차례 — 처음으로 "입력이 아니라 출력 단계" 문제. (b) rich 핵심 모듈 0.20 은 19 관찰 2 의 랭킹 방향 반례. gold 레포를 늘린 뒤.
4. Phase 4 시작(14) 대비: 진입점 **0.05 → 0.53**(LLM 출력 기준 0.27 → 0.53), 핵심 모듈 **0.24 → 0.72**, 인용 유효성 100% 유지, 레포당 비용 $0.02 유지. 전부 프롬프트가 아닌 정적 입력 변경으로.

## 비용
- $0.10. 프로젝트 누적 약 $4.0.
