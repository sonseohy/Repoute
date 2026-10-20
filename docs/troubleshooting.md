# 트러블슈팅 기록

개발 중 실제로 겪은 문제와 해결을 번호 순으로 쌓는다. 포트폴리오의 "실패 사례와 한계" 절의 재료이고, 같은 문제를 두 번 겪지 않기 위한 기록이다.
형식: 증상 → 원인 → 해결 → 교훈. 해결 커밋이 있으면 적는다. 아직 안 풀린 것은 "미해결"로 표시.

---

## 01. ruff가 마크다운 안의 Python 코드 블록까지 포맷 검사 (Phase 1)
- **증상**: `uv run ruff format --check .` 가 `docs/code-reading/*.md` 9개를 "would be reformatted"로 실패시킴. 소스는 전부 통과.
- **원인**: ruff 0.17부터 마크다운의 ```python 블록도 포맷 대상. 설명용 코드 조각은 일부러 줄여 쓰기 때문에 정식 포맷과 다르다.
- **해결**: `pyproject.toml` `[tool.ruff] extend-exclude = ["*.md"]`.
- **교훈**: 린터 버전을 올리면 검사 범위가 늘 수 있다. CI가 문서 때문에 깨지면 안 된다.

## 02. `nx.pagerank` 가 scipy 없이는 동작하지 않음 (Phase 1)
- **증상**: `ModuleNotFoundError: No module named 'numpy'` (networkx 3.6).
- **원인**: 최신 networkx의 pagerank는 내부에서 scipy 희소행렬 경로만 쓴다. 순수 파이썬 구현은 비공개 API.
- **해결**: 처음엔 15줄짜리 거듭제곱 반복을 직접 짰으나, 포트폴리오에서는 표준 한 줄(`nx.pagerank`)이 읽기 쉬워 `uv add scipy` 후 되돌림.
- **교훈**: "의존성 하나 vs 직접 구현 15줄"은 설치 크기보다 읽는 사람 기준으로 정한다.

## 03. 레포 자신을 인덱싱하면 clone 캐시까지 섞임 (Phase 1)
- **증상**: `repoute index .` 결과에 `.repoute_cache/` 아래 다른 레포의 모듈이 포함.
- **원인**: `SKIP_DIRS` 에 이 도구의 캐시 폴더가 없었다.
- **해결**: `parser.SKIP_DIRS` 에 `.repoute_cache` 추가.
- **교훈**: 도구가 만드는 산출물 폴더는 입력에서 제외해야 한다. 비슷한 미해결: fastapi 의 `docs_src/` 예제 541개 중 다수가 포함되어 PageRank 1위가 `fastapi` 패키지(354 참조)로 왜곡됨. `docs` 만 제외하고 `docs_src` 는 안 걸러진다. **미해결**, Phase 4 정답셋 만들 때 제외 규칙 보강 예정.

## 04. Send 병렬 노드에서 SQLite 스레드 오류 (Phase 2)
- **증상**: `sqlite3.ProgrammingError: SQLite objects created in a thread can only be used in that same thread`.
- **원인**: LangGraph 는 노드가 하나인 단계는 호출 스레드에서, `Send` 로 퍼진 병렬 노드는 스레드 풀에서 돌린다. SQLite 연결은 만든 스레드에서만 쓸 수 있다.
- **해결**: `fan_out` 이 호출 스레드에서 심볼 목록을 미리 읽어 `Send(..., {"module": m, "symbols": [...]})` 로 넘긴다. 병렬 노드는 DB 를 만지지 않는다.
- **교훈**: 병렬 노드에는 "데이터"를 넘기고 "연결"은 넘기지 않는다. `check_same_thread=False` 로 우회할 수도 있지만 동시 쓰기 위험이 있어 쓰지 않았다.

## 05. pytest 가 LangSmith 프로젝트를 오염 (Phase 2)
- **증상**: `repoute-dev` 에 LangGraph 트레이스 16개가 쌓였는데 전부 토큰 0. 실제 실행 1건을 찾기 어려움.
- **원인**: `.env` 의 `LANGSMITH_TRACING=true` 를 `config.py` 의 `load_dotenv()` 가 import 시점에 올리고, 가짜 LLM 으로 도는 테스트 그래프도 그대로 기록됐다.
- **해결**: `tests/conftest.py` 에서 `os.environ["LANGSMITH_TRACING"] = "false"` 를 import 전에 설정. `load_dotenv` 는 이미 있는 환경변수를 덮어쓰지 않는다. 테스트 후 트레이스 수가 그대로인 것을 확인.
- **교훈**: 측정 원칙 1(모든 호출 기록)의 반대면, "기록하면 안 되는 호출"도 정해 둬야 측정이 깨끗하다.

## 06. LangSmith 조회 스크립트가 `Invalid token` (Phase 2)
- **증상**: `Client().list_runs(...)` 가 401. 같은 키로 `requests.get` 은 200.
- **원인**: 임시 스크립트에서 `.env` 를 로드하지 않아 키가 비어 있었다. 추가로 stdin 으로 넘긴 스크립트에서는 `load_dotenv()` 가 `find_dotenv` 의 프레임 탐색에서 `AssertionError` 를 낸다.
- **해결**: `load_dotenv(".env")` 처럼 경로를 명시.
- **교훈**: "키가 잘못됐다"는 메시지의 절반은 키가 전달되지 않은 것이다. 먼저 어떤 값이 실제로 보이는지(길이·접두어) 찍어 본다.

## 07. 자동 모드 권한 분류기가 과금 API 실행을 차단 (Phase 2)
- **증상**: Claude 가 `uv run repoute map ...` 을 실행하려 하자 "Real-World Transactions" 사유로 거부. `repoute check`(핑 1회)는 통과.
- **원인**: 실제 비용이 드는 명령은 자동 승인 대상이 아니다.
- **해결**: 우회하지 않고, 과금 실행은 항상 사용자가 터미널에서 직접 실행하는 규칙으로 정함(`docs/PLAN.md` 협업 방식). 결과는 Claude 가 터미널 출력·LangSmith API 로 읽는다.
- **교훈**: 비용 통제 측면에서 오히려 바람직한 구조. 예상 비용 → 승인 → 사용자 실행 → 실측 기록.

## 08. 재시도 횟수의 의미가 애매함 (Phase 2)
- **증상**: `retry_count` 가 "검증 실패 횟수"였다. 상한 2에 닿으면 실제 재생성은 1번뿐인데 CLI 는 "재시도 2회"로 표시.
- **원인**: 카운트를 validate 노드에서 올렸다.
- **해결**: synthesize 가 피드백을 받고 다시 합성할 때만 올리도록 변경. 이제 상한 2 = 재생성 2회(합성 총 3회). 테스트 기대값 수정.
- **교훈**: 상한 값이 사용자에게 보이는 숫자라면, 카운트가 무엇을 세는지 먼저 정의한다.

## 09. 인용 검증 루프가 실패를 고치지 못함 — `fastapi/__init__.py:0` (Phase 2, 실험 E4)
- **증상**: fastapi 만 유효 비율 83%. 재생성 2회에도 같은 인용 `:0` 반복. 비용만 2.6배($0.022 → $0.057).
- **원인**: (a) `__init__.py` 는 import 만 있어 프롬프트에 "인용 가능한 심볼 줄"이 하나도 없었고 모델이 0번 줄을 지어냄. (b) 피드백 "파일은 25줄까지"는 무엇이 틀렸는지만 말하고 대신 무엇을 쓸지 말하지 않음. LangSmith 트레이스의 validate 노드 출력으로 확인.
- **해결**: 합성 입력의 "인용 가능한 줄" 목록에 `1(모듈 시작)` 을 넣고, 검증 피드백에 "이 파일에서 인용 가능한 줄: …" 힌트를 붙임. 실험 04: 유효 83% → 100%, 재생성 2 → 0, 비용 절반. 프롬프트 v2 문구 자체는 통제군과 차이 없음(입력 변경이 효과의 전부). 그러나 14번 문제를 낳았다.
- **교훈**: 검증 루프는 "틀렸다"가 아니라 "이렇게 고쳐라"를 돌려줘야 작동한다(wave08 Validator 의 피드백 조건과 같다). 그리고 E4 결과 자체: 프롬프트가 이미 강하게 제한하면 루프의 정확도 효과는 작다. 루프의 가치는 안전장치다.

## 10. 진입점 휴리스틱이 디버그 함수를 고름 — `requests/help.py:main` (Phase 2)
- **증상**: requests 의 진입점으로 `help.py:126 main` 이 뽑힘. 실제 진입은 `api.py` 의 `get/post` 또는 `__init__.py` 의 공개 API.
- **원인**: 이름이 `main` 이면 무조건 후보. 라이브러리형 레포(CLI 가 아닌)는 `main` 이 진입점이 아니다.
- **해결**: **미해결**. 후보: `__init__.py` 의 `__all__`/import 된 이름을 후보에 추가, `help`·`debug`·`_internal` 파일명 감점, 레포 유형(라이브러리/앱/CLI) 판별. Phase 4 의 `entrypoint_recall` 로 측정 후 손댄다(측정 원칙 2).

## 11. 읽기 순서가 PageRank 순위를 그대로 따라감 (Phase 2)
- **증상**: requests 읽기 순서 1~3위가 PageRank 1~3위(compat, models, structures)와 같음. 사람은 `api.py → sessions.py → adapters.py` 흐름을 먼저 읽는다.
- **원인**: 프롬프트가 "PageRank 상위"를 핵심 모듈로 제시하고, 모델이 그 순서를 거의 그대로 쓴다. 의존 관계 순서와 학습 순서는 다르다.
- **해결**: **미해결**. 측정 수단(`reading-guide-gold` 정답셋, `core_module_precision@5`)이 먼저 필요. 인용이 100% 유효해도 순서가 좋다는 뜻은 아니라는 것이 이 프로젝트의 중요한 구분.

## 12. 파서의 알려진 한계 (Phase 1, 설계상 수용)
- 호출은 이름 기준이라 `a.save()` 와 `b.save()` 를 구분 못 함. 내장 함수도 섞임.
- `from . import m` 은 패키지 이름으로 잡혀 하위 모듈 간선이 빠짐(imports.py 의 `resolve_module` 이 일부 보완).
- 동적 import, `if TYPE_CHECKING:` 미구분.
- `find_definition` 의 `LIKE '%.name'` 에서 `_` 가 와일드카드라 드물게 다른 이름이 걸림(진입점 탐지는 끝 이름을 다시 거름).
- 대응: Walkthrough(Phase 3)에서 LLM 이 `read_code` 로 실제 코드를 읽어 보완하되, 인용은 실제 `파일:줄` 만 허용.

## 13. `evals/` 가 테스트에서 import 되지 않음 (Phase 2)
- **증상**: `from evals.experiments...` 가 `ModuleNotFoundError`. `evals/__init__.py` 를 둬도 같음.
- **원인**: pytest 는 `tests/` 를 sys.path 에 넣고 레포 루트는 넣지 않는다. `evals/` 는 패키지로 설치되지 않는다.
- **해결**: 테스트에서 `importlib.util.spec_from_file_location` 으로 스크립트 파일을 직접 로드.
- **교훈**: 실험 스크립트는 패키지 밖에 두는 게 맞지만, 테스트할 함수는 스크립트 안에 분리해 둔다(`estimate_cost`, `render_markdown`).

## 14. 유효성 지표를 고치자 인용이 전부 "줄 1"로 쏠림 (Phase 2, 실험 E6) — Goodhart
- **증상**: 04 에서 fastapi 인용 7개 중 5개가 `파일:1`. 03 에서는 0/6 이었고 `routing.py:129` 처럼 실제 함수 줄을 가리켰다. `citation_validity` 는 100% 로 완벽.
- **원인**: "줄 1(모듈 시작)" 을 모든 모듈의 인용 가능 목록에 넣자, 모델이 가장 안전한 선택으로 줄 1 을 골랐다. 프롬프트 v2 의 "심볼 없는 모듈만 줄 1" 지시는 지켜지지 않았다. 측정하는 지표(유효성)만 최적화되고 측정하지 않는 품질(구체성)이 무너진 것.
- **해결**: (1) `citation_specificity`(심볼 범위 안을 가리키는 인용 비율, 심볼 없는 파일의 줄 1 은 인정) 지표 추가 → 03 83%, 04 43% 로 수치 확인. (2) 입력 쪽에서 강제 — 심볼이 있는 파일의 "인용 가능한 줄" 목록에서 줄 1 제거(`_citable`), 프롬프트 v3. (3) 실험 05: 유효 100% 유지, 구체성 100%, 재생성 0, $0.022.
- **교훈**: 지표 하나로 루프를 돌리면 모델은 그 지표만 맞춘다. "유효한가" 와 "유용한가" 는 따로 재야 하고, 허용 범위는 프롬프트 지시보다 입력 데이터로 좁히는 편이 확실하다.
