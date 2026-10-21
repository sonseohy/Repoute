# 개발 규칙

Git 운영과 측정 기록 방법을 정리한다.

커밋·PR·push는 사용자가 직접 한다. AI는 이 규칙에 맞는 커밋 메시지와 PR 본문을 복사 가능한 텍스트로 제공한다.

## 브랜치

- `main`: 보호 브랜치. 항상 실행 가능한 상태를 유지한다. 직접 push 금지, PR + squash merge만 허용.
- 작업 브랜치 이름:

| 패턴 | 용도 | 예시 |
|---|---|---|
| `feat/<scope>-<short>` | 기능 추가 | `feat/indexing-ast-parser` |
| `fix/<scope>-<short>` | 버그 수정 | `fix/walkthrough-citation-offset` |
| `eval/<experiment-id>` | 평가 실험 | `eval/e2-chunking` |
| `docs/<topic>` | 문서 | `docs/plan` |
| `chore/<topic>` | 설정·잡무 | `chore/ci-python-version` |

- 개인 프로젝트이므로 `develop` 브랜치는 두지 않는다.

## 커밋

**한국어 제목 한 줄만.** 본문은 쓰지 않는다. type은 영어 소문자, 괄호 scope는 쓰지 않는다. 커밋은 기능 단위로 묶는다(너무 잘게 쪼개지 않는다).

```
<type>: <한국어 설명>
```

- **type**: `feat` `fix` `refactor` `test` `docs` `chore` `eval` `perf` `ci`
- 커밋 하나에 변경 하나. 제목은 72자 이내.
- `eval` 타입은 제목에 핵심 지표의 전→후와 n을 짧게 넣고, 상세(평가 방법, LangSmith 실험 이름)는 `evals/results/<NN>-<experiment-id>.md`에 적는다. 측정하지 않은 수치는 쓰지 않는다.

### 예시

```
feat: ast로 함수·클래스 정의 추출
```

```
eval: 인용 검증 루프 추가, citation_validity 0.71→0.93 (n=20)
```

(수치는 실제 측정값. 측정 전에는 이 커밋을 만들지 않는다.)

```
docs: evals/results에 실험 기록 표 추가
```

## PR

- 템플릿(`.github/PULL_REQUEST_TEMPLATE.md`): 목적 / 변경 내용 / 테스트 방법 / 지표 변화(해당 시, n과 방법 포함) / LangSmith 링크
- CI(ruff, pytest) 통과 필수.
- `eval/*` PR은 `evals/results/`에 결과 파일을 함께 올린다.

## 태그

마일스톤 단위로 붙인다.

| 태그 | 마일스톤 |
|---|---|
| `v0.1.0` | 인덱싱 |
| `v0.2.0` | 읽기 지도 |
| `v0.3.0` | 따라가기 |
| `v0.4.0` | API + UI, MVP 공개 |
| `v0.5.0` | Issue Matcher |
| `v0.6.0` | PR Coach |
| `v1.0.0` | 마무리 |

## 측정 원칙 운영

원칙:

- 모든 LLM 호출은 LangSmith 트레이싱에 연결하고 비용·지연 시간을 기록한다.
- 성능 개선 변경 전에는 베이스라인을 먼저 측정한다.
- 수치에는 n과 평가 방법을 함께 적는다.
- 측정하지 않은 수치는 README·문서에 쓰지 않는다. 기획서 목표치는 가설로만 다룬다.
- 실험 결과는 `evals/results/`에 요약본으로 남긴다.

기록 방법:

- 누적 표: `evals/results/README.md` (날짜 · 실험 ID · 변경 요약 · 지표(전 → 후) · n · 평가 방법 · LangSmith 링크 · 커밋)
- 실험별 상세: `evals/results/<NN>-<experiment-id>.md`
- LangSmith 실험 이름: `<module>-<dataset>-<yyyymmdd>-<variant>` (예: `reading-guide-gold-20261101-baseline`)
- `eval` 커밋 제목에 핵심 지표 전→후와 n (상세는 evals/results)
- 프롬프트는 `src/repoute/prompts/<name>_v<N>.py`로 버전별 파일을 남긴다.

## CI 회귀 평가

실제 LLM 을 부르는 평가를 PR 에서 자동으로 돌려, 지표가 떨어진 변경이 main 에 들어가지 않게 한다. 일반 CI(`ci.yml`, ruff·pytest)와는 별도 워크플로우다.

- **언제 도는가**: `.github/workflows/eval-regression.yml`. main 대상 PR 중 `src/repoute/**` 또는 `evals/**` 가 바뀐 것만, 그리고 Actions 탭의 수동 실행(`workflow_dispatch`, 레포 일부만 고를 수 있음). 문서만 바뀐 PR 에서는 돌지 않는다(비용).
- **무엇을 재는가**: `evals/experiments/regression_gate.py` 가 gold 레포 5개를 validate × 프롬프트 v3 로 돌려 결정론적 지표 평균(citation_validity, core_module_p5, entrypoint_recall)을 낸다. LLM-judge 는 쓰지 않는다. 1회 약 $0.10.
- **기준치는 어디에**: 스크립트의 `THRESHOLDS` 상수 하나. 평균이 하나라도 기준치 아래거나, 레포가 하나라도 실패해 측정을 못 하면 exit 1 → job 실패.
- **현재 기준치**: citation_validity 1.0 / core_module_p5 0.5 / entrypoint_recall 0.2. 기록 18(1.0 / 0.68 / 0.32, n=5)에서 여유를 둔 값으로, 개선이 아니라 회귀를 잡는 용도다. n=5 라 레포 하나가 0.2 씩 움직이므로 여유를 좁게 잡지 않는다.
- **기준치를 올리는 규칙**:
  1. 측정 후에만 올린다. 바라는 값이 아니라 실제로 나온 값을 근거로 한다.
  2. 근거를 `evals/results/<NN>-*.md` 에 남긴다(n, 방법, 레포별 수치). 한 번의 좋은 실행보다 같은 설정에서 반복해도 유지되는 값을 쓴다.
  3. `THRESHOLDS` 변경은 그 기록을 링크한 PR 로만 한다. 기준치를 내리는 것도 같은 절차를 밟고, 왜 내리는지(gold 변경, 지표 정의 변경 등)를 적는다.
- **secrets**: 저장소 Settings → Secrets and variables → Actions 에 `OPENAI_API_KEY`, `LANGSMITH_API_KEY`. 키가 없으면(fork PR 등) "secrets 미설정, 평가 건너뜀" 을 출력하고 성공으로 끝난다. 키 값은 어떤 파일에도 쓰지 않는다.
- **로컬에서 먼저**: `uv run python evals/experiments/regression_gate.py --dry-run`(LLM 없이 판정 경로만), 실제 실행은 `--repos psf/requests` 처럼 일부만 돌려 비용을 줄인다.
- 트레이스는 LangSmith 프로젝트 `repoute-ci` 에 남는다.

## 테스트

- `tests/`는 `src/repoute/` 구조를 그대로 따른다. 예: `src/repoute/indexing/parser.py` → `tests/indexing/test_parser.py`.
- 우리 코드의 동작만 테스트한다. 라이브러리 자체 동작(LangChain이 응답을 돌려주는지 등)은 테스트하지 않는다.
- LLM이 필요한 노드는 `FakeListChatModel`을 꽂아 API 키 없이 돈다. 실제 모델 호출은 `evals/`에서만 한다.

## 코드 읽기 문서와 테스트

- 구현이 끝난 파일은 `docs/code-reading/NN-<파일>.md`에 읽는 순서대로 번호를 매겨 설명을 남긴다(로컬 학습용, git에 올리지 않음). 파일 상단에 함수 순서 목차, 본문은 코드 한 줄 + 설명. 대화창에는 요약과 검증 방법만.
- 테스트 함수마다 무엇을 검증하는지 docstring을 쓴다.
- 개발 중 겪은 문제는 `docs/troubleshooting.md`에 번호 순으로 "증상 → 원인 → 해결 → 교훈" 형식으로 남긴다. 미해결도 적는다.
- 단계가 끝날 때마다 직접 돌려볼 명령(`uv run pytest -v`, CLI, REPL 스니펫)과 LangSmith에서 확인할 항목을 문서에 적는다.
