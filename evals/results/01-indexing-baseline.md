# 20261021 — 인덱싱 베이스라인 (Phase 1)

LLM 미사용 정적 분석 파이프라인(clone → ast 파싱 → SQLite → import 그래프 → PageRank)의 첫 측정.

- **n = 3** (공개 Python 레포, 기본 브랜치 HEAD, shallow clone)
- **측정 방법**: `uv run repoute index <url>` 1회 실행, `time`은 clone 포함 전체 벽시계 시간(`time.perf_counter`). 캐시 없는 상태. 로컬 머신(macOS, Python 3.11), 네트워크 포함이라 재현 시 ±수백 ms 편차 예상.
- **환경**: 커밋 해시는 아래 표 참조.

| 레포 | commit | files | symbols | calls | time(s) | PageRank 1위 (참조 수) |
|---|---|---|---|---|---|---|
| psf/requests | 611c616 | 20 | 320 | 988 | 1.00 | `requests.compat` (10) |
| encode/httpx | b5addb6 | 23 | 533 | 1558 | 1.52 | `httpx._models` (13) |
| fastapi/fastapi | 6f28852 | 541 | 1588 | 6249 | 3.57 | `fastapi` (354) |

## 관찰
- 파일 541개 레포도 4초 안에 끝난다. Phase 2에서 LLM 요약 비용이 지배적일 것이므로 정적 분석 시간은 병목이 아니다.
- fastapi의 541개 중 상당수가 `docs_src/` 예제 코드다. `SKIP_DIRS`는 `docs`만 제외해서 `docs_src`가 포함됐고, 그 결과 `fastapi` 패키지가 354개 참조로 1위가 됐다. 예제 코드는 "먼저 읽을 파일" 판단에 잡음이므로 Phase 2 전에 제외 규칙을 보강할 후보다.
- requests/httpx는 "타입·모델·예외" 모듈이 상위에 온다. 의존 관계 기준으로는 맞지만, 사람이 읽기 시작할 지점(진입점)과는 다르다. 읽기 순서는 PageRank만이 아니라 진입점 탐지와 결합해야 한다(Phase 2 설계 근거).

## 비용
- LLM 호출 0회, $0.
