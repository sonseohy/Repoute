# evals/datasets — 평가 정답 데이터셋

Phase 4 평가(PLAN 6절 Phase 4) 에 쓰는 정답 데이터. 네 파일 모두 JSON 배열이라 파일 안에 메타를 넣을 수 없어서, 스키마·라벨링 방법·한계를 여기에 적는다.

| 파일 | LangSmith 데이터셋 | 항목 수 | 사용하는 평가자 |
|---|---|---|---|
| `reading_guide_gold.json` | `reading-guide-gold` | 레포 5개 | `entrypoint_recall`, `core_module_precision@5` |
| `walkthrough_questions.json` | `walkthrough-qa` | 질문 20개 (레포 4개 × 5) | 도달(`expect_reach`), `call_chain_accuracy`(`expect_chain`) |
| `supervisor_intents.json` | (로컬 전용) | 메시지 30개 | 의도 분류 정확도 (`evals/experiments/supervisor_intents.py`) |
| `walkthrough_questions_nosymbol.json` | (로컬 전용, 미등록) | 질문 10개 (레포 4개 × 2~3) | 도달(`expect_reach`), `call_chain_accuracy`(`expect_chain`) |
| `issue_matcher_gold.json` | (로컬 전용, 미등록) | 이슈 20개 (레포 4개 × 5) | `issue_module_precision_at_k`(k=3), `issue_symbol_hit` |
| `pr_coach_gold.json` | (로컬 전용, 미등록) | 머지된 PR 8개 (레포 4개 × 1~3) | `impact_recall`, `test_file_hit` |

대상 레포와 커밋 (인덱스 `.repoute_cache/index/<owner>__<name>@<commit>.sqlite` 의 커밋과 같다):
psf/requests@611c616, encode/httpx@b5addb6, pallets/click@2247b35, Textualize/rich@9d8f9a3, fastapi/fastapi@6f28852.

## 1. `reading_guide_gold.json` — 읽기 지도 정답

| 필드 | 뜻 |
|---|---|
| `repo`, `commit` | 레포와 커밋 7자리 |
| `entrypoints` | `{file, symbol}` 2~5개. "처음 온 기여자가 읽기 시작해야 하는 함수·클래스". 라이브러리면 공개 API, CLI 라이브러리면 사용자가 쓰는 데코레이터 + 실행 시작점, 웹 프레임워크면 앱 클래스 |
| `core_modules` | 모듈 이름 5개 (인덱스 graph 노드 이름 형식, 예 `src.requests.sessions`). "이 레포를 이해하려면 꼭 읽어야 하는 모듈". 순서는 의미 없음(집합) |
| `reading_order_hint` | 사람이 읽을 순서 제안. 현재 평가자는 쓰지 않음(참고용) |
| `pagerank_top5` | 라벨링 시점 `rank_modules(graph)` 상위 5개. 정답이 아니라 **비교 기준** — gold 와의 차이가 Phase 4 실험(troubleshooting 10·11)의 핵심 |
| `rationale` | 왜 그렇게 정했는지, 확인한 `파일:줄` |
| `labeled_by` | 누가 언제 라벨했는지, 검수 상태 |
| `notes` | PageRank·06 기록(읽기 지도 v3 실제 출력)과의 차이, 후보였지만 뺀 모듈 |

## 2. `walkthrough_questions.json` — 따라가기 질문

| 필드 | 뜻 |
|---|---|
| `id`, `repo`, `question` | 질문 (기존, 변경 없음) |
| `expect_reach` | 답이 도달해야 하는 마지막 심볼 (기존, 변경 없음) |
| `expect_chain` | 질문에 끝까지 답할 때 거쳐야 하는 **핵심 심볼의 순서** 3~6개. 마지막 원소 = `expect_reach`. 이름은 인덱스 symbols 테이블 `name` 형식(`Class.method` 또는 함수명) |
| `chain_evidence` | 체인의 각 호출을 확인한 `파일:줄 호출자 → 피호출자`. 체인에서 생략한 앞부분·체인 밖 보충 근거(예외가 raise 되는 줄)도 여기 적음 |

체인 규칙:
- 실제 호출 순서를 따른다. 6개 상한을 넘으면 질문과 가까운 쪽(뒤쪽)을 남기고 앞부분을 생략하며, 생략 사실을 `chain_evidence` 첫 줄에 적었다 (requests-04, httpx-05).
- 동적 호출(`auth_handler(self)` → `HTTPBasicAuth.__call__`, `renderable.__rich_console__` → `Table.__rich_console__`, `self.invoke` → `Group.invoke`)은 질문의 상황(인자가 tuple, 출력 대상이 Table, 명령이 Group)으로 정해지는 구체 메서드를 적었다. 이름 기준 정적 호출 그래프(troubleshooting 12)로는 안 보이는 연결이다.
- `with` 블록 끝의 `__exit__`(rich-03) 처럼 호출문이 없는 연결도 포함했다.

## 3. `supervisor_intents.json` — Supervisor 의도 분류

Supervisor `classify`(fast 모델 + 코드 게이트 `_gate`)가 메시지를 map·ask·next·chat 중 어디로 보내는지 재는 데이터. 항목은 `{id, message, has_guide, history, expected, note}` 이고, `has_guide` 는 이 대화에 읽기 지도가 이미 있는지, `history` 는 분류 프롬프트에 들어가는 최근 대화(`{"role": "user"|"agent", "text"}`, 도우미 줄은 `_agent_summary` 모양으로 씀)다. **라벨 = 사람 수작업**(규칙·LLM 자동 라벨 아님. 초안은 Opus 에이전트가 2026-10-21 에 손으로 붙였고 사용자 검수 전. 사용자가 실제로 칠 법한 한국어 메시지 27개 + 영어 3개, 각 라벨의 이유를 `note` 에 한 줄), **n = 30**, 분포 map 9 / ask 9 / next 6 / chat 6. next 는 모두 `has_guide: true` 이고, 지도 없는 "다음"(si-06)은 게이트 규칙대로 map 으로 라벨해 map 9개에 넣었다. 함수 이름이 없는 ask 2개(si-13 리디렉트, si-14 인증 헤더)는 사람 기준으로 ask 지만 현재 게이트가 map 으로 내리므로(Phase 5.5-4 완화 대상) 결과에서 따로 집계한다. 한계: n 이 작아 한 항목이 정확도를 3%p 움직이고, next 6개는 모두 `NEXT_PATTERN` 에 걸리는 표현이라 게이트 후 next 정확도는 사실상 정규식 검사다. LLM 단독 정확도와 함께 봐야 한다.

## 4. `walkthrough_questions_nosymbol.json` — 이름 없는 따라가기 질문

**목적.** 질문에 함수·클래스 이름이 하나도 없는 따라가기 질문 세트. 현재 Supervisor 게이트(`src/repoute/graphs/supervisor.py` 의 `SYMBOL_PATTERN`)는 메시지에 식별자가 없으면 ask 를 map 으로 내리므로, 이 세트의 질문은 지금은 따라가기까지 가지 못한다. 게이트를 완화하고 심볼 임베딩 검색 도구를 넣었을 때 **전/후 비교**용이다(Phase 5.5-4).

- **스키마:** 2절 `walkthrough_questions.json` 과 같다(`id`, `repo`, `question`, `expect_reach`, `expect_chain`, `chain_evidence`). `id` 는 `ns-<레포>-NN`.
- **n = 10.** requests 3 (리디렉트 한도 예외, 쿠키 병합, 환경 변수 프록시), httpx 2 (리디렉트 시 메서드 변경, 압축 해제기 선택), click 2 (환경 변수에서 옵션 값 읽기, 프롬프트 입력), rich 3 (터미널 폭, 줄바꿈 위치, 256색 다운그레이드).
- **라벨 = Opus 에이전트 초안, 사용자 검수 전** (2026-10-21, LLM 호출 없이 소스 읽기). 질문은 "이름을 몰라도 물을 법한" 동작·현상·설정 키워드로 썼다.
- **기존 20개와 겹침 없음.** 10개 모두 `expect_reach` 가 같은 레포의 기존 `walkthrough_questions.json` 정답과 다르다. 체인 앞부분(`Session.request`, `Client.send`, `Console.print` 등)은 겹친다.
- **식별자 없음 검증:** 질문은 순 한국어(한글·숫자·문장부호)로만 썼고, `tests/test_datasets_nosymbol.py` 가 10개 모두 `SYMBOL_PATTERN.search(question) is None` 인지 확인한다(영어 단어 하나만 있어도 식별자로 매치되므로).
- **근거 확인 방법:** `expect_chain` 의 각 이름을 인덱스 `symbols` 테이블에서 `sqlite3` 로 조회해 존재를 확인하고, `chain_evidence` 의 각 `path:line` 을 `.repoute_cache/<owner>/<name>/` 소스에서 직접 열어 그 줄이 해당 체인 심볼의 `line_start~line_end` 안에 있고 설명(호출문·raise 줄)과 맞는지 봤다. 괄호 안 `:NNN` 은 같은 파일의 보충 줄이다.
- **동적 호출:** click 두 질문의 `self.consume_value`·`self.value_from_envvar`·`super().resolve_envvar_value` 는 질문 상황(옵션)으로 정해지는 `Option.*` 구체 메서드를 적었다(2절 체인 규칙과 같음). ns-rich-02 의 `renderable.__rich_console__` 은 `Text.__rich_console__`.
- **한계:** n 이 작아 한 항목이 10%p 를 움직인다. ns-rich-01 의 `self.options`·`self.size` 는 property 접근이라 인덱스 `calls` 테이블에 없다(조회로 확인) — 정적 호출 그래프만으로는 `Console.print → Console.options → Console.size` 를 따라갈 수 없다. ns-click-02 는 옵션에 프롬프트가 설정된 경우를 전제한다.

## 5. `issue_matcher_gold.json` — Issue Matcher 정답

**목적.** Phase 6 Issue Matcher(열린 이슈 → "이 이슈를 하려면 읽어야 할 모듈·심볼") 평가용. GitHub 의 실제 열린 이슈마다 고치거나 읽어야 할 모듈과 심볼을 코드를 직접 읽고 정했다.

| 필드 | 뜻 |
|---|---|
| `id` | `im-<레포>-NN` |
| `repo`, `number`, `title`, `url`, `labels` | 이슈 원본 값 (raw JSON 에서 그대로 복사) |
| `gold_modules` | 1~3개, 점 표기 패키지 경로(`requests.models`, `httpx._transports.default`). `src/` 접두어는 뺐고 `__init__.py` 는 패키지 이름(`requests`). 평가자는 끝 두 조각으로 비교하므로 인덱스 graph 노드 이름(`src.requests.models`)과도 맞는다 |
| `gold_symbols` | 1~3개 `{name, file, line}`. `name` 은 인덱스 `symbols.name`, `line` 은 그 행의 `line_start`. `@overload` 로 같은 이름 행이 여러 개면 구현부(마지막 def) 행을 적었다(`Response.iter_lines`, `Response.iter_content`, `Context.invoke`) |
| `note` | 이 이슈를 고른 이유와 이 모듈·심볼인 이유 한 줄 |
| `difficulty` | `easy`(한 함수 국소 수정) / `medium`(2곳 이상 또는 경로 추적 필요) / `hard`(원인 추적·설계 판단 필요) — 라벨러 판단 |

- **n = 20** (레포 4개 × 5). 난이도 easy 13 / medium 5 / hard 2.
- **수집.** 2026-10-21 06:38 UTC, GitHub 공개 REST API(토큰 없음). 호출 URL:
  - `https://api.github.com/repos/psf/requests/issues?state=open&per_page=50`
  - `https://api.github.com/repos/encode/httpx/issues?state=open&per_page=50` (+ `&page=2`)
  - `https://api.github.com/repos/pallets/click/issues?state=open&per_page=50`
  - `https://api.github.com/repos/Textualize/rich/issues?state=open&per_page=50`

  응답은 `raw/issues_<owner>__<name>.json` 에 가공 없이 저장했다(httpx 2쪽은 `raw/issues_encode__httpx_page2.json`). 이 API 는 PR 도 이슈로 돌려주므로 50개 중 `pull_request` 키가 있는 항목을 빼면 이슈는 requests 21 / httpx 8+28 / click 41 / rich 36 개다. httpx 는 1쪽에 이슈가 8개뿐이라 후보가 모자라 2쪽을 한 번 더 받았다.
- **선택 기준.** 코드 변경이 필요한 구체적 이슈만. 질문·문서·CI·배포·테스트 코드만 고치는 이슈 제외(예: requests 7599·7547 문서, rich 4222·4223 테스트, click 3081 문서). 본문(재현 코드·트레이스백·지목한 파일)으로 어느 코드인지 추론 가능한 것을 골랐다. 고친 곳이 모듈 수준 코드라 인덱스 심볼이 없는 이슈는 뺐다(rich 4201 `rich/__init__.py` 의 `_IMPORT_CWD`). 이 네 레포는 "good first issue"/"help wanted"/"bug" 라벨을 거의 쓰지 않아(click 은 주제 라벨 `parsing`·`help output` 만, 나머지는 대부분 무라벨) 라벨 우선 기준은 적용하지 못했고, 제목의 `[BUG]`·본문의 재현 코드를 대신 봤다.
- **라벨 = Opus 에이전트 초안, 사용자 검수 전** (2026-10-21, LLM 호출 없이 소스 읽기). 각 심볼은 `.repoute_cache/<owner>/<name>/` 소스에서 이슈가 말하는 동작이 일어나는 줄을 직접 열어 확인했고, `(name, line_start)` 가 인덱스 `symbols` 테이블에 있는지 `sqlite3` 로 한 번 조회했다(테스트는 형식만 본다).
- **커밋.** 인덱스·소스 커밋은 위와 같다: requests 611c616, httpx b5addb6, click 2247b35, rich 9d8f9a3.

**한계.**
- 이슈는 수집 시점(HEAD) 기준인데 인덱스·소스는 고정 커밋이라 어긋날 수 있다. 예: httpx 3400 본문 트레이스백의 `_utils.normalize_header_value` 는 인덱스 커밋에서 `_models._normalize_header_value` 로 옮겨져 있어 옮겨진 쪽을 적었다. 라벨 시점에 모든 심볼이 인덱스에 있음을 확인했다.
- 정답이 하나가 아니다. "읽어야 할 곳" 은 고칠 곳 + 호출 경로 일부라 사람마다 1~2개 다를 수 있다. 대칭 코드(httpx `AsyncClient._init_transport`, rich `save_html`·`save_svg`, click `UsageError.show`)는 3개 상한 때문에 `note` 에만 적었다.
- `issue_module_precision_at_k` 의 분모는 min(k, 정답 모듈 수) — 정답 모듈이 1개인 이슈(14개)도 상위 3개 안에 맞히면 1.0. 가장 느슨한 `issue_module_hit_at_k`(상위 k 에 정답 하나라도)도 함께 본다.
- n 이 작아 한 이슈가 5%p 를 움직인다. 이슈는 열린 상태라 나중에 닫히거나 내용이 바뀔 수 있다(raw JSON 으로 재현).

## 6. `pr_coach_gold.json` — PR Coach 정답

**목적.** Phase 7 PR Coach(PR diff → "이 변경이 영향을 주는 모듈·심볼, 돌려야 할 테스트") 평가용. 실제로 머지된 PR 마다 diff 가 건드린 심볼과 그 호출자를 코드와 인덱스로 직접 확인해 정했다.

| 필드 | 뜻 |
|---|---|
| `id` | `pr-<레포>-NN` |
| `repo`, `number`, `title`, `url`, `merged_at` | PR 원본 값 (`raw/prs_<owner>__<name>.json` 에서 number 로 찾아 그대로 복사, url 은 `html_url`) |
| `changed_files` | files API 가 돌려준 파일 경로 전부(문서·테스트 포함) |
| `changed_symbols` | 1~6개 `{name, file, line}`. diff hunk(files API `patch`)가 건드린 줄을 감싸는 함수·클래스 중 인덱스 커밋에 있는 것. 6개를 넘으면 동작이 바뀐 것을 남기고 나머지는 `note` 에 |
| `gold_impact_symbols` | 2~6개 `{name, file, line}`. 변경 심볼의 **직접 호출자**(인덱스 `calls` 에서 callee = 변경 심볼 끝 이름 → caller) 중 "이 PR 때문에 다시 봐야 할" 것. 변경 심볼 자신은 넣지 않는다. 직접 호출자가 1개뿐이면 그 위 호출자(2단계)를 더하고 `note` 에 적었다 |
| `gold_impact_modules` | 1~4개, 점 표기 패키지 경로(`src/` 접두어 없음). 영향 심볼의 모듈을 먼저, 자리가 남으면 변경 심볼의 모듈 |
| `gold_test_files` | 1~3개. PR 이 직접 고친 테스트 파일을 먼저, 그다음 레포 `tests/` 에서 변경 심볼 이름·오류 문구를 grep 해 찾은 파일. 모두 인덱스 커밋 소스에 있음을 확인 |
| `summary_ko` | PR 제목 번역 + 핵심 변경 한 줄 |
| `note` | 예외 처리·제외한 심볼·2단계 호출자를 넣은 이유 |
| `difficulty` | `easy`(심볼 1~2개 국소 변경) / `medium`(여러 심볼, 2단계 호출자 필요) / `hard`(넓은 변경이거나 property 처럼 정적 호출 그래프로 안 보이는 영향) — 라벨러 판단 |

- **n = 8** (requests 2, httpx 1, click 3, rich 2). 난이도 easy 3 / medium 3 / hard 2.
- **수집.** 2026-10-21, GitHub REST API. 레포마다 `pulls?state=closed&per_page=30&sort=updated&direction=desc` 1회(`raw/prs_<owner>__<name>.json`), 고른 PR 마다 `pulls/<n>/files` 1회(`raw/pr_files_<owner>__<name>_<n>.json`, 10개). 둘 다 가공 없이 저장.
- **선택 기준.** `merged_at` 이 있고 소스 `.py` 를 고친 PR(문서·CI·의존성 bump·테스트 전용 제외). 최근 30개 중 click·rich 는 `[rejected AI]`·spam 처럼 머지되지 않은 PR 이 많고, requests·httpx 는 머지된 PR 대부분이 bump·문서라 후보가 적었다. 그래서 2023 년(rich 3180)·2025 년 PR 도 들어 있다.
- **파일 목록까지 받았다가 뺀 PR (10 → 8):**
  - httpx #1 "Add connections" (2019): 변경 파일이 모두 `httpcore/*` 라 현재 인덱스에 없다.
  - requests #6966 "Bump version…v2.32.4": 소스는 `src/requests/__version__.py` 의 상수만 바뀌어 감싸는 함수·클래스가 없다.
- **규칙 예외 1개.** pr-httpx-01(#3699)은 모듈 수준 `__all__` 줄만 바꿔 감싸는 심볼이 없다. 빼면 httpx 가 0개가 되므로 공개 범위가 바뀐 클래스 `FunctionAuth` 를 변경 심볼로 적었다(`note` 참고).
- **라벨 = Opus 에이전트 초안, 사용자 검수 전** (2026-10-21, LLM 호출 없이 소스·인덱스 읽기). 방법:
  1. 각 hunk 의 바뀐 줄 내용을 인덱스 커밋 소스에서 찾아(예상 줄에 가장 가까운 일치) 감싸는 가장 작은 심볼에 대응시켰다. 인덱스 커밋 전에 머지된 PR 은 `+` 줄로, 뒤에 머지된 PR(click #3884)은 `-` 줄로 찾았다. 그다음 hunk 를 직접 읽어 `return`·`else:` 같은 흔한 줄의 우연 일치를 걸렀다.
  2. 호출자는 `calls` 테이블을 callee 끝 이름으로 조회하고 `tests/` 호출은 뺐다. property 접근(`self.spec`)은 `calls` 에 없어 `grep` 으로 확인했다.
  3. `(name, file)` 이 인덱스 `symbols` 에 있는지 조회했고, `line` 은 그 행의 `line_start` 다(같은 이름 행이 여럿이면 마지막 행).
- **커밋.** 인덱스·소스 커밋은 위와 같다: requests 611c616, httpx b5addb6, click 2247b35, rich 9d8f9a3. click #3877 은 인덱스 커밋 2247b35 의 바로 그 PR 이다.

**한계.**
- PR 은 수집 시점(HEAD) 기준인데 인덱스·소스는 고정 커밋이다. 인덱스보다 오래된 PR(rich #3180 은 2023 년)은 그 뒤 코드가 더 바뀌었고, 뒤에 머지된 PR(click #3884)은 아직 인덱스에 반영되지 않았다. diff 의 줄 번호는 인덱스 줄과 어긋나므로 **심볼 이름 기준으로 라벨**했고 `line` 은 인덱스 커밋 기준이다.
- "직접 호출자" 는 이름 기준 정적 호출 그래프다. 동적 디스패치·property·`super()` 경유 호출은 빠지거나 끝 이름이 같은 다른 메서드와 섞일 수 있어, 사람이 보고 골랐다(2~6개). 정답이 하나가 아니다.
- 6개 상한 때문에 넓은 PR(click #3877, rich #4070)은 변경·영향 심볼 일부를 `note` 에만 적었다. `impact_recall` 의 분모는 gold 수라, 상한 밖 심볼을 맞혀도 점수에 들어가지 않는다.
- n = 8 이 작아 한 PR 이 12.5%p 를 움직이고, httpx 는 1개뿐이며 그것도 규칙 예외다.

## 라벨링 방법

1. 캐시된 소스(`.repoute_cache/<owner>/<name>/`)를 `grep`·파일 읽기로 직접 확인. 호출 관계는 호출문이 있는 줄을 찾아 `chain_evidence` 에 남김.
2. 진입점·체인의 심볼 이름과 파일 경로는 인덱스 symbols 테이블에 실제로 있는 값만 사용 (`sqlite3` 조회로 확인).
3. 핵심 모듈은 PageRank 를 베끼지 않고 소스 구조(공개 API 가 어디서 정의되고, 요청/실행 한 번이 어떤 모듈을 지나는지)로 판단. PageRank 상위 5 는 비교를 위해 별도 필드에 기록.
4. fastapi 는 `docs_src/`(인덱스 심볼의 절반 이상)를 제외하고 `fastapi/` 패키지 기준.
5. LLM 호출 없음. 라벨러는 Opus 에이전트(소스 읽기), 2026-10-21.

## 한계 (측정 원칙: 방법과 한계 명시)

- **에이전트 라벨, 사용자 검수 전.** "사람이 정한 정답" 이 아니다. 특히 `core_modules` 5번째 자리(requests 의 auth vs utils, httpx 의 _auth vs _content, fastapi 의 openapi.utils vs encoders)는 판단이 갈릴 수 있고 `notes` 에 후보를 남겼다.
- **n 이 작다.** reading-guide-gold 5개(PLAN 목표 10개), walkthrough-qa 20개(목표 50개). 레포 하나·질문 하나가 평균을 20%·5% 움직인다. 비교 결과에는 반드시 n 을 함께 적는다.
- **fastapi 질문 없음.** walkthrough 질문은 requests·httpx·click·rich 4개 레포뿐.
- **레포 유형 편중.** HTTP 클라이언트 2개, CLI 1개, 터미널 출력 1개, 웹 프레임워크 1개. 앱형(Django 프로젝트 등) 레포가 없다.
- **진입점 정답이 하나가 아니다.** "읽기 시작할 곳" 은 사람마다 다를 수 있어 2~5개 집합으로 두었다. `entrypoint_recall` 은 이 집합 중 몇 개를 찾았는지만 본다.
- **체인은 대표 경로 하나.** 같은 질문을 다른 경로(예: async 클라이언트)로 답해도 틀린 답은 아니지만 `call_chain_accuracy` 는 낮게 나온다.
- 줄 번호는 위 커밋 기준. 레포를 다시 clone 해 커밋이 바뀌면 근거 줄이 어긋난다.
