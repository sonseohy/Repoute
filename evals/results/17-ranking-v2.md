# 17. 핵심 모듈 랭킹 v2 — 역방향 개인화 PageRank + `__init__` 감쇠 (Phase 4, 2026-10-21)

기록 14·16 의 결론("PageRank 는 하부 부품을 올리고, LLM 은 그 순서를 그대로 쓴다")에 대한 정적 입력 개선. LLM 없음, $0.

- **n = 5** 레포, gold core_modules 25개. `static_baseline.py` 로 측정. 변형 비교는 일회성 스크립트(레포 밖)로 22가지.
- **채택**: `rank_modules_v2(graph, seeds)` = `graph.reverse()` 에서 진입점 모듈 시드로 개인화 PageRank → `__init__.py` 모듈 점수 ×0.2. 시드가 없으면 기존 `rank_modules`.

## 결과

| 레포 | 기존 PageRank p@5 | **v2 p@5** | v2 상위 5 | gold |
|---|---|---|---|---|
| requests | 0.20 | **0.80** | sessions, api, models, exceptions, adapters | api, sessions, models, adapters, auth |
| httpx | 0.20 | **0.60** | _transports.default, _client, _api, _main, `httpx` | _api, _client, _models, _transports.default, _auth |
| click | 0.40 | **0.60** | core, decorators, testing, termui, types | core, decorators, parser, types, exceptions |
| rich | 0.40 | **0.20** (후퇴) | console, _extension, _inspect, `rich`, progress | console, segment, text, style, markup |
| fastapi | 0.00 | **0.80** | applications, utils, routing, dependencies.utils, openapi.utils | applications, routing, dependencies.utils, params, openapi.utils |
| **평균** | **0.24** | **0.60** | | |

변형별 평균(발췌): 정방향 개인화 0.32 / **역방향 0.52** / 역방향+리프 감점 0.52 / 역방향+이름 힌트(compat·utils…) 0.48 (fastapi 의 gold `dependencies.utils` 까지 깎음) / 역방향+시드 가산 0.36 / **역방향+`__init__`×0.2 = 0.60** / 역방향(시드 없음)+`__init__`×0.2 = **0.64**.

## 관찰
1. **효과의 출처는 "방향" 이다.** 엣지 A→B = "A 가 B 를 import" 에서 PageRank 는 많이 import 되는 B(부품)로 질량이 흐른다. 역방향에서는 많이 import 하는 A(조립하는 흐름 모듈: sessions, _client, routing, applications)로 흐른다. "먼저 읽을 모듈" 은 조립하는 쪽이라는 직관이 수치로 맞았다(0.24 → 0.52).
2. **`__init__.py` 허브 감쇠**가 두 번째 기여(0.52 → 0.60). 패키지 `__init__` 은 하위 모듈을 재수출하느라 모두가 거치는 허브라 역방향에서 5개 레포 전부 상위 5 에 들지만 gold 에는 없다. Python 패키지 구조에 근거한 규칙이라 레포 특수적이지 않다. ×0.5 는 효과 없음, ×0.2 에서 개선 — 이 계수는 n=5 에서 정했다.
3. **진입점 시드의 효과는 확인되지 않았다.** 시드 없는 역방향 + 감쇠가 0.64 로 오히려 1개 더 맞는다. 차이는 rich 하나: 약한 시드(`_extension`, `_inspect`)가 질량을 끌어가 0.40 → 0.20. 즉 **진입점 v2 의 약점(15: 동점 운 의존)이 랭킹으로 전파된다.** 시드를 남긴 이유는 진입점이 좋아지면 랭킹도 좋아지는 경로를 두기 위해서다. 진입점이 더 좋아지지 않으면 시드를 빼는 것이 맞다.
4. **과적합 경고.** 22개 변형 중 최고를 n=5(핵심 모듈 25개)에서 골랐다. 평균 0.04 = 25개 중 1개. 새 레포 2~3개를 gold 에 추가해 재측정하기 전에는 "0.60" 을 확정 수치로 쓰지 않는다(측정 원칙 4).
5. 남은 오답: click 의 testing·termui(많이 조립하지만 핵심 흐름 아님), fastapi 의 `utils`, rich 후퇴.

## 다음
- 읽기 지도 그래프(`select_targets`, `synthesize`)와 `repoute index` 출력에 v2 연결 → LLM 1회(레포 5개, $0.1)로 16 의 p@5 0.32 가 얼마나 오르는지(기록 18).
- gold 에 레포 2~3개 추가(다른 유형: 앱/CLI) 후 재측정.

## 비용
- $0.
