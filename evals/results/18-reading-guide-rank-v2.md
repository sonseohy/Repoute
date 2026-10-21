# 18. 읽기 지도 + 랭킹 v2 — LLM 출력 vs gold, 첫 LangSmith Experiment (Phase 4, 2026-10-21)

기록 17 의 랭킹 v2 를 그래프에 연결한 뒤 레포 5개를 다시 생성. 이번부터 측정은 `langsmith_eval.py` → LangSmith `evaluate()` 로 돌려 **Experiment** 로도 남는다.

- **n = 5** 레포(reading-guide-gold 데이터셋), 프롬프트 v3, validate 모드, 진입점 v2 + 랭킹 v2 + docs_src 제외.
- **LangSmith Experiment**: `reading-guide-gold-v3-13bfef5b` (Datasets & Experiments → reading-guide-gold). 평가자 3개가 각 예제의 feedback 으로 붙음. 비용 $0.102 (트레이스 합).

## 결과

| 레포 | entrypoint_recall | **core_module_p@5** | citation_validity | LLM 이 고른 진입점 | 읽기 순서 상위 |
|---|---|---|---|---|---|
| requests | 0.33 | **0.80** (16: 0.40) | 1.0 | request | models, exceptions, sessions, api, adapters |
| httpx | 0.00 | **0.60** (0.20) | 1.0 | `main` ✗ | _transports.default, _client, _api, _main |
| click | 0.25 | **1.00** (0.40) | 1.0 | Context, Command, Group | core, decorators, types, parser, exceptions |
| fastapi | 0.00 | **0.80** (0.00) | 1.0 | `main` ✗ | applications, routing, utils, utils |
| rich | 1.00 | 0.20 (0.60) | 1.0 | get_console, print, Console, Console.print … 10개 | console, rich, _extension, _inspect |
| **평균** | **0.32** (16: 0.27) | **0.68** (16: 0.32) | **1.0** | | |

정적 베이스라인(14) → 정적 v2(17) → LLM 출력(18): p@5 **0.24 → 0.60 → 0.68**.

## 관찰
1. **입력을 고치면 출력이 따라온다 — 세 번째 확인.** 16 에서 LLM 출력 p@5 가 0.32 였던 것은 프롬프트 탓이 아니라 PageRank 입력 탓이었다. 랭킹 v2 를 넣자 0.68. 프롬프트는 한 글자도 바꾸지 않았다. LLM 출력(0.68)이 정적 v2(0.60)보다 높은 것은 모델이 요약 내용을 보고 testing·termui 같은 비핵심을 뒤로 밀었기 때문(click 1.00).
2. **rich 후퇴(0.60 → 0.20)는 17 의 시드 문제가 그대로 전파된 것.** 약한 진입점(`_extension`, `_inspect`, `reconfigure`, `load_ipython_extension`)이 시드가 되어 그 모듈을 올렸고, LLM 은 진입점을 10개나 나열했다. 진입점 품질 → 랭킹 → 읽기 순서로 오류가 연쇄된다.
3. **진입점 0.32 의 원인은 `[project.scripts]` 가산이다.** httpx(`httpx = "httpx:main"`)·fastapi(`fastapi = "fastapi.cli:main"`)는 라이브러리지만 편의 CLI 를 scripts 에 등록했고, 휴리스틱이 +3 을 준다. 독자의 진입점은 `httpx.get`·`FastAPI` 다. 다음 규칙 후보(일반적): 패키지가 `__init__` 에서 많은 이름을 export 하면(라이브러리 신호) scripts 가산을 +1 로 낮추고, `cli`/`_main`/`__main__` 경로를 −3. 정적으로 먼저 측정(진입점 recall@5 0.43 → ?).
4. **LangSmith Experiment 의 쓸모**: 이 실행은 데이터셋 예제별 점수와 트레이스가 묶여 저장됐다. 다음 변경 뒤 같은 명령을 돌리면 Compare 뷰에서 레포별로 어디가 좋아지고 나빠졌는지 나란히 보인다(rich 후퇴 같은 것). 로컬 md 기록은 계속 주 기록이고, Experiment 는 비교·캡처용.
5. 비용·시간은 변화 없음(레포당 $0.02, 6~8s).

## 다음
- 진입점 v3(scripts 가산 조정, CLI 경로 감점) — 정적 측정 → 좋아지면 Experiment 재실행($0.1)으로 rich 회복 여부 확인.
- gold 레포 추가(앱/CLI 유형 2~3개)로 과적합 점검.
- walkthrough-qa 도 Experiment 로 1회(quality, 5개, $0.4).

## 비용
- $0.10. 프로젝트 누적 약 $3.85.
