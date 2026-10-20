# Repoute

**Repoute = Repo + Route.** GitHub URL을 넣으면 어디서부터 어떻게 읽을지, 코드가 어떻게 흐르는지, 무슨 이슈부터 잡을지, 오픈소스 기여를 처음 시작하는 개발자를 위한 AI Agent.

## 핵심 설계

- **정적 분석 + LLM 하이브리드**: 함수·클래스·import·호출 관계는 Python `ast`로 추출하고 SQLite·NetworkX에 저장한다. LLM은 추출된 구조를 해석하는 데만 쓴다.
- **`파일:줄` 인용 검증**: 코드 설명에는 `파일:줄` 근거를 붙인다. 인용된 위치가 실제로 존재하는지, 해당 심볼을 포함하는지 검사하고, 실패하면 피드백을 넣어 다시 생성한다. 인용 없는 설명은 사용자에게 전달하지 않는다.
- **`repo@commit` 단위 인덱스 캐시**: 같은 커밋은 다시 인덱싱하지 않는다.
- **LangSmith 평가**: 모든 LLM 호출을 트레이싱하고, 기능마다 naive → improved 비교를 데이터셋과 평가자로 기록한다.
- **사람이 코드를 쓴다**: 도구는 안내와 검토만 한다.

## 기술 스택

| 영역 | 사용 기술 |
|---|---|
| 언어·패키지 | Python 3.11, uv |
| LLM 프레임워크 | LangChain (`langchain`, `langchain-core`, `langchain-openai`, `langchain-anthropic`, 선택: `langchain-ollama`) |
| 에이전트 그래프 | LangGraph (`langgraph`, `langgraph-checkpoint-sqlite`) |
| 트레이싱·평가 | LangSmith |
| 정적 분석 | 표준 라이브러리 `ast`, `sqlite3`, `networkx` (+ `scipy`, PageRank용) |
| API | FastAPI + SSE |
| 프론트 | Vite + TypeScript |
| 품질·CI | ruff, pytest, GitHub Actions |

LLM 제공자는 `init_chat_model("provider:model")`로 설정만 바꿔 전환한다(openai 기본, anthropic, ollama).

## 실행 방법

```bash
uv sync
cp .env.example .env   # API 키 입력
uv run pytest
```