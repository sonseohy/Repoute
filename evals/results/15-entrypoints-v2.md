# 15. 진입점 탐지 v2 — 공개 API 기반 후보·점수 (Phase 4, 2026-10-21)

기록 14 의 정적 베이스라인(entrypoint_recall 0.05)을 겨냥한 첫 개선. LLM 없음, 비용 $0.

- **n = 5** 레포(reading_guide_gold.json, 진입점 19개). `evals/experiments/static_baseline.py` 로 전/후 측정.
- **변경**(`indexing/entrypoints.py`, 일반 규칙만, gold 를 보고 레포별 하드코딩 없음): (1) 최상위 패키지 `__init__.py` 가 import/`__all__` 로 export 하는 이름을 **출처 모듈에서** 찾아 +3, (2) export 된 클래스의 핵심 메서드(request/send/get/print/render/…) +2, (3) 기존 규칙(pyproject scripts +3, main 류 이름 +1, 진입점 파일 +2) 유지, (4) 감점: help/debug/compat/testing/utils/cli 경로 −2, `_` 비공개 −2, (5) 점수 합산 → 상위 10. 함께 들어간 변경: `SKIP_DIRS` 에 `docs_src`(fastapi 541 → 76 파일).

## 결과

| 레포 | 전 recall@10 | **후 recall@10** | 후 recall@5 | 놓친 것(순위) |
|---|---|---|---|---|
| requests | 0.00 | 0.67 | 0.67 | `Session.request` 25위 — +3 동점 24개(api 함수 8, 예외 11) 아래 |
| httpx | 0.00 | 0.50 | 0.50 | `Client` 16위, `Client.send` 68위 — +3 동점 65개, 1위는 CLI 의 `_main.py:main`(4점) |
| click | 0.25 | 0.50 | 0.50 | `command` 13위, `option` 16위 — core.py 클래스 6개가 파일순으로 앞 |
| rich | 0.00 | 1.00 | 0.25 | 상위 5 에는 `print` 만(Console 8위, render 9위, print 10위) |
| fastapi | 0.00 | 0.25 | 0.25 | `FastAPI.get` 18위; `APIRouter.add_api_route`·`get_request_handler` 는 후보 자체가 안 됨(규칙 상한 0.5) |
| **평균** | **0.05** | **0.58** | **0.43** | |

단계별 기여(recall@10): 규칙 그대로 0.53 → export 를 출처 모듈에서만 찾기 0.58(동명 함수 `_termui_impl.getchar`, `jupyter.print` 제거) → @overload 중복 합산 금지 0.58/@5 0.43(`Command.main` 3개가 상위 3자리 차지하던 것 제거).

## 관찰
1. **목표(0.5)는 넘었지만 수치의 상당 부분이 운이다.** httpx·click 은 +3 동점이 60개를 넘고 파일 이름순으로 가른다. `api.py` 가 알파벳 앞이라 requests 가 0.67 인 것. 동점을 가르는 **일반 규칙**이 없으면 다른 레포에서는 재현되지 않는다. 다음 후보: (a) 다른 공개 함수에서 호출되는 export 가산(호출 그래프 활용 — 이미 있는 calls 테이블), (b) 핵심 메서드를 가진 클래스에 가산, (c) 예외 클래스 감점(실험했으나 이 5개에서는 변화 없어 미채택 — n 이 커지면 다시).
2. **CLI `main` 이 라이브러리에서 1위가 되는 문제**(httpx `_main.py`, fastapi `cli.py`)는 troubleshooting 10 그대로 남아 있다. "레포 유형" 판별 없이 이름 규칙만으로는 못 가른다. 후보: `[project.scripts]` 가 없는 레포에서 `main` 가산 제거.
3. 이 변경은 읽기 지도 그래프에도 들어간다(`select_targets` 의 진입점 파일, `synthesize` 의 진입점 후보). 따라서 다음 LLM 측정(기록 16)은 "프롬프트 v3 + 진입점 v2 + docs_src 제외" 의 합산 효과다. 분리하려면 진입점만 v1 로 되돌린 통제군이 필요하지만, 지금은 합산으로 베이스라인(14: 0.05/0.24)과 비교한다.
4. 상위 5 재현율 0.43 이 사용자에게 보이는 값에 가깝다(읽기 지도는 진입점을 몇 개만 보여 준다). 목표를 "recall@5 ≥ 0.6" 으로 다시 둔다.

## 비용
- $0.
