# 25. Issue Matcher 1차 — MCP 도구 토큰 · 이슈→모듈 매칭 precision (Phase 6, 2026-10-21)

## A. 이슈 → 모듈·심볼 매칭 (RAG 2단계, LLM 0)

- **n = 20** 이슈(`evals/datasets/issue_matcher_gold.json`, 레포 4개 × 5, 열린 이슈 2026-10-21 수집, 라벨 = Opus 에이전트 초안·사용자 검수 전). raw: `25-issue-matcher-gold.json`.
- **방법**: `evals/experiments/issue_matcher_eval.py` — 이슈 제목 + 본문 500자를 `text-embedding-3-small` 로 임베딩 → 심볼 임베딩(기록 24 의 `.emb.sqlite`, 레포당 캐시) 코사인 상위 5 심볼 → 파일을 모듈로 바꿔 상위 3개. **Issue Matcher 그래프의 `match_symbols` 노드와 같은 로직**(테스트로 일치 확인). MCP·LLM 없이 매칭 단계만.
- **지표**: p@3 = 상위 3 모듈 중 정답 비율, 분모 min(3, 정답 수) / hit@3 = 정답 하나라도 / 심볼 = 상위 5 심볼 중 정답 끝 이름 / 1위 = 1위 모듈이 정답. 모듈 비교는 끝 두 조각(`src.requests.models` = `requests.models`).
- **비용**: 임베딩 쿼리 20회, 추정 2.8k 토큰 ≈ **$0.00006**. 심볼 임베딩은 캐시(4레포 모두).

| | n | p@3 | hit@3 | 심볼 적중 | 1위 적중 |
|---|---|---|---|---|---|
| **전체** | 20 | **0.85** | **0.90** | 0.75 | 0.80 |
| psf/requests | 5 | 0.70 | 0.80 | 0.80 | 0.60 |
| encode/httpx | 5 | 0.90 | 1.00 | 0.40 | 1.00 |
| pallets/click | 5 | 0.80 | 0.80 | 0.80 | 0.60 |
| Textualize/rich | 5 | 1.00 | 1.00 | 1.00 | 1.00 |
| easy / medium / hard | 13 / 5 / 2 | 0.85 / 0.80 / 1.00 | 0.85 / 1.00 / 1.00 | 0.69 / 0.80 / 1.00 | 0.77 / 0.80 / 1.00 |

**관찰**
- **임베딩만으로 이슈의 "어디" 를 20개 중 18개 맞혔다(hit@3 0.90).** 이슈 본문에 트레이스백·클래스 이름·재현 코드가 있어 심볼 텍스트(이름 + 시그니처 + docstring)와 어휘가 겹친다. gold 선정 기준이 "본문이 코드를 가리키는 이슈" 라서 유리한 표본이기도 하다 — 막연한 기능 요청은 더 어려울 것.
- **실패 2건은 어휘가 안 겹치는 경우**: im-requests-04 "missing TLS material → FileNotFoundError"(정답 `HTTPAdapter.cert_verify`, 예측은 `SSLError`·`check_compatibility` — 예외 이름에 끌림), im-click-03 "write_usage breaks options at a hyphen"(정답 `formatting.HelpFormatter.write_usage`·`_textwrap.wrap_text`, 예측은 `core.Command.format_usage` — 비슷한 이름의 호출자 쪽). 둘 다 **정답은 예측 심볼의 피호출자**다 — 검색 결과에 `find_callees` 1단계를 더하면 잡힐 가능성이 있다(측정 후 결정).
- **심볼 적중이 모듈보다 낮다(0.75)**: httpx 는 모듈은 다 맞고 심볼은 2/5. 메서드가 많은 모듈에서 상위 5 심볼이 같은 클래스의 이웃 메서드(`Client.options`, `AsyncClient.options`)로 채워진다. 동기/비동기 쌍(`Client`/`AsyncClient`)이 자리를 절반 먹는다 — 같은 이름의 Async 쌍을 하나로 접으면 k 가 두 배로 쓰인다.
- 표시 문제: 예측 모듈 이름이 `src.requests.models` 처럼 `src.` 를 달고 나온다(import 그래프 노드 이름). 지표는 끝 두 조각 비교라 영향 없지만 UI 에는 `src.` 를 떼야 한다.

## B. MCP 도구 설명 토큰 (LLM 0)

`evals/experiments/mcp_tool_tokens.py` — 사용자가 실행, 결과 표 붙이기 (아래 채움).

(결과 대기)

## C. 실제 흐름 1회 (CLI `repoute issues`)

(결과 대기)

## 비용 누적
프로젝트 API 비용 약 $4.6 → 약 $4.7 (기록 23·24 포함).
