# 실험 결과

측정한 실험만 기록한다. 수치에는 n과 평가 방법을 함께 적는다.

| 번호 | 실험 ID | 변경 요약 | 지표(전 → 후) | n | 평가 방법 | LangSmith 링크 | 커밋 |
|---|---|---|---|---|---|---|---|
| [01](01-indexing-baseline.md) | indexing-baseline | Phase 1 정적 인덱싱 첫 측정 | files 20~541, time 1.0~3.6s (LLM $0) | 3 | `repoute index` 벽시계 시간, clone 포함 | — (LLM 미사용) | (커밋 후 기재) |
| [02](02-reading-guide-first-run.md) | reading-guide-first-run | Phase 2 읽기 지도 첫 실행 (requests) | $0.018, 8.0s, 인용 8/8 유효, 재생성 0회 | 1 | CLI 벽시계 시간 + LangSmith 토큰·비용 | trace 01a15a37-bd17-74db-a84a-7976ed002608 | (커밋 후 기재) |
| [03](03-e4-validation-loop.md) | e4-validation-loop | 인용 검증 루프 유무 (naive vs validate), 레포 5개 | citation_validity naive 97% → validate 97% (효과 없음), 비용 $0.021 → $0.029 | 5 | 최종 인용 사후 검증, 토큰 콜백, LangSmith 교차 확인 | 태그 `e4` | (커밋 후 기재) |
| [04](04-e6-prompt-v2.md) | e6-prompt-v2 | 줄 1 인용 가능 입력 + 프롬프트 v2 (fastapi 재측정) | citation_validity 83% → 100%, 재생성 2→0, 비용 $0.043→$0.022. 단, 줄 1 인용 0/6 → 5/7 (구체성 하락) | 1 | 사후 검증 + LangSmith 트레이스 인용 목록 | 태그 `prompt-v1`/`prompt-v2` | (커밋 후 기재) |

## 이름 규칙

- 상세 파일: `<NN>-<experiment-id>.md` (번호 순, 이 디렉터리에 둔다. 날짜는 파일 안에 적는다)
- LangSmith 실험 이름: `<module>-<dataset>-<yyyymmdd>-<variant>` (예: `reading-guide-gold-20261101-baseline`)
