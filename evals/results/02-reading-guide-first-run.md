# 02. 읽기 지도 첫 실행

읽기 지도 그래프(`repoute map`)를 실제 LLM 으로 처음 돌린 기록. 성능 "실험"이 아니라 베이스라인 전 동작 확인이며, 이후 비교의 기준점이다.

- **n = 1** (psf/requests@611c616, 파일 20개)
- **측정 방법**: `uv run repoute map https://github.com/psf/requests` 1회(인덱스는 캐시 히트). 시간은 CLI 의 벽시계 시간, 토큰·비용은 LangSmith 트레이스(`repoute-dev`, trace `01a15a37-bd17-74db-a84a-7976ed002608`)에서 읽음. 비용은 LangSmith 가 계산한 값.
- **모델**: fast = gpt-4o-mini, quality = gpt-4o, temperature 0. 프롬프트 v1.

## 결과

| 항목 | 값 |
|---|---|
| 요약 대상 모듈 | 13개 (PageRank 상위 12 + 진입점 파일 1) |
| summarize_module 호출 | 13회 (gpt-4o-mini), 입력 14,933 / 출력 1,033 토큰, $0.0029 |
| synthesize 호출 | 1회 (gpt-4o), 입력 3,974 / 출력 528 토큰, $0.0152 |
| 인용 검증 | 인용 8개(진입점 1 + 단계 7) 전부 유효, 재생성 0회 |
| 총 비용 | **$0.018** |
| 시간 | 8.0초 (그래프 7.6초) |

## 출력 (요약)

- 진입점: `src/requests/help.py:126 main`
- 읽기 순서: compat.py → models.py → structures.py → `__init__.py` → cookies.py → exceptions.py → utils.py (7단계, 모두 `파일:줄` 인용 포함)

## 관찰 (다음 개선의 근거)

1. **비용·시간은 목표 대비 여유가 크다.** 기획서 목표(5분, $1)보다 두 자릿수 아래. 비용의 84%가 synthesize 1회(gpt-4o)다. 품질을 올려야 한다면 요약 단계가 아니라 합성 단계에 투자할 여지가 있다.
2. **인용은 전부 유효했지만, 그것이 읽기 순서의 품질을 뜻하지는 않는다.** 읽기 순서가 PageRank 순위와 거의 같다(compat, models, structures 가 그대로 1~3위). "먼저 읽을 파일"은 사람이 보기에 `api.py`(get/post) → `sessions.py` → `adapters.py` → `models.py` 흐름이 더 자연스럽다. 이 차이를 재려면 Phase 4 의 `reading-guide-gold`(사람이 정한 정답)와 `core_module_precision@5` 가 필요하다.
3. **진입점 탐지가 틀렸다.** `help.py:main` 은 디버그 정보 출력용이지 라이브러리 진입점이 아니다. 휴리스틱이 이름 `main` 만 보고 고른 결과. 라이브러리형 레포는 `__init__.py` 의 공개 API 나 `api.py` 가 진입점이어야 한다. 개선 후보: `__init__.py` 의 `__all__`/import 된 이름을 진입점 후보에 추가, `help`·`debug` 같은 파일명은 감점.
4. **테스트가 LangSmith 를 오염시켰다.** pytest 의 가짜 LLM 그래프 실행이 `repoute-dev` 에 LangGraph 트레이스 16개를 남겼다. `tests/conftest.py` 에서 테스트 중 트레이싱을 끄도록 고쳤다(이 기록 이후부터 적용).

## 비용 누적
- 이 실행 $0.018 + 연결 확인(`repoute check`) $0.000002.
