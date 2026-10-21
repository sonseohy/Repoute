"""Supervisor 그래프: 인덱스 확인 → 의도 분류(+코드 게이트) → 읽기 지도 | 따라가기 | 안내.

wave05: 모든 쿼리가 무거운 파이프라인을 탈 필요는 없다 (chat 은 LLM 없이 고정 안내).
wave06: 게이트는 프롬프트가 아니라 코드로 (심볼 없는 ask 는 map 으로 내린다).
Phase 5 API 의 단일 진입점. 진행 상황은 events 리듀서로 쌓여 stream_mode="updates" 로 보인다.
"""

import operator
import re
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel

from repoute.graphs.deps import Deps
from repoute.graphs.reading_guide import ReadingGuide, run_reading_guide
from repoute.graphs.walkthrough import WalkthroughAnswer, ask, build_walkthrough_graph
from repoute.indexing.index import RepoIndex, build_index
from repoute.prompts import reading_guide_v3, supervisor_v1, walkthrough_v3

# 심볼 후보: 영문 식별자(점·괄호 포함 가능). 한국어 메시지에서 코드 이름을 찾는 느슨한 기준이다.
# 느슨하게 둔 이유: 이 게이트는 ask 를 map 으로 내리기만 한다. 이름이 있는 질문을 잘못 내리면
# 사용자가 원한 따라가기를 잃으므로, "이름이 정말 하나도 없을 때" 만 발동하게 한다.
SYMBOL_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*(?:\(\))?")


class Intent(BaseModel):
    """의도 분류 결과 (fast 모델 구조화 출력)."""

    kind: Literal["map", "ask", "chat"]  # map=구조/어디서부터, ask=특정 코드 흐름, chat=인사·기타
    reason: str


class SupervisorState(TypedDict, total=False):
    repo_url: str
    message: str
    thread_id: str
    intent: Intent | None
    guide: ReadingGuide | None  # map 결과
    answer: WalkthroughAnswer | None  # ask 결과
    validation_errors: list[str]  # 서브그래프에 남은 인용 오류 (설계 원칙 2: 숨기지 않는다)
    reply: str  # chat 또는 안내 문구
    events: Annotated[list[dict], operator.add]  # 진행 상황 (SSE 용), 노드마다 이어 붙인다


@dataclass
class SupervisorDeps:
    """Supervisor 의존성. 서브그래프용 Deps 는 노드 안에서 인덱스를 붙여 만든다.

    인덱스(SQLite 연결·그래프)는 직렬화가 안 돼 상태가 아니라 여기에 둔다 (troubleshooting 17).
    """

    fast_llm: Any  # 의도 분류·모듈 요약
    quality_llm: Any  # 읽기 순서 합성·따라가기
    cache_dir: Path | str = ".repoute_cache"
    checkpointer: Any = None  # 따라가기 대화 스레드 (없으면 매번 새 대화)
    indexes: dict[str, RepoIndex] = field(default_factory=dict)  # repo_url → 이번 실행의 인덱스

    def sub_deps(self, repo_url: str) -> Deps:
        """서브그래프(읽기 지도·따라가기)가 받을 Deps."""
        return Deps(self.fast_llm, self.quality_llm, self.indexes[repo_url])

    def close(self) -> None:
        """열어 둔 인덱스 DB 연결을 모두 닫는다."""
        for repo in self.indexes.values():
            repo.db.close()
        self.indexes.clear()


def ensure_index(state: SupervisorState, deps: SupervisorDeps) -> dict:
    """인덱스가 없으면 만들고, 있으면 캐시에서 읽는다 (게이트: 인덱싱 먼저).

    SQLite 연결은 만든 스레드에서만 쓸 수 있어 실행마다 다시 연다 (캐시 히트면 빠르다).
    """
    url = state["repo_url"]
    if url in deps.indexes:
        deps.indexes.pop(url).db.close()
    repo = deps.indexes[url] = build_index(url, deps.cache_dir)
    info = {"repo_id": repo.info.repo_id, "cached": repo.cached, "files": repo.file_count}
    return {"events": [{"type": "IndexReady", **info}]}


def classify(state: SupervisorState, deps: SupervisorDeps) -> dict:
    """fast 모델로 의도를 고르고, 심볼 없는 ask 는 코드로 map 으로 내린다 (wave06).

    ask 의 도구(find_definition 등)는 이름이 있어야 작동하므로, 이름 없는 흐름 질문은
    레포 전체 읽기 지도가 더 쓸모 있다.
    """
    prompt = supervisor_v1.CLASSIFY.format(message=state["message"])
    intent = deps.fast_llm.with_structured_output(Intent).invoke(prompt)
    events = [{"type": "IntentDecided", "kind": intent.kind, "reason": intent.reason}]
    if intent.kind == "ask" and not SYMBOL_PATTERN.search(state["message"]):
        reason = "메시지에 함수·클래스 이름이 없어 따라가기 도구를 쓸 수 없음"
        events.append({"type": "GateApplied", "from": "ask", "to": "map", "reason": reason})
        intent = Intent(kind="map", reason=reason)
    return {"intent": intent, "events": events}


def route(state: SupervisorState) -> str:
    """의도에 맞는 노드 이름."""
    return {"map": "run_map", "ask": "run_ask", "chat": "chat_reply"}[state["intent"].kind]


def run_map(state: SupervisorState, deps: SupervisorDeps) -> dict:
    """읽기 지도 서브그래프 (검증 루프 켬, 프롬프트 v3)."""
    sub = deps.sub_deps(state["repo_url"])
    result = run_reading_guide(sub, validate=True, prompts=reading_guide_v3)
    guide, errors = result["guide"], result.get("validation_errors") or []
    event = {"type": "GuideReady", "steps": len(guide.reading_order), "errors": len(errors)}
    return {"guide": guide, "validation_errors": errors, "events": [event]}


def run_ask(state: SupervisorState, deps: SupervisorDeps) -> dict:
    """따라가기 서브그래프. thread_id 가 없으면 레포별 기본 스레드를 쓴다 (cli 와 같은 규칙)."""
    sub = deps.sub_deps(state["repo_url"])
    thread_id = state.get("thread_id") or f"{sub.index.info.repo_id}:default"
    graph = build_walkthrough_graph(
        sub, prompts=walkthrough_v3, checkpointer=deps.checkpointer, agent_model="quality"
    )
    result = ask(graph, state["message"], thread_id)
    errors = result.get("validation_errors") or []
    event = {"type": "AnswerReady", "steps": len(result["answer"].steps), "errors": len(errors)}
    return {"answer": result["answer"], "validation_errors": errors, "events": [event]}


def chat_reply(state: SupervisorState) -> dict:
    """LLM 없이 고정 안내를 돌려준다."""
    return {"reply": supervisor_v1.CHAT_REPLY, "events": [{"type": "Replied"}]}


def build_supervisor_graph(deps: SupervisorDeps):
    """START → ensure_index → classify → (run_map | run_ask | chat_reply) → END."""
    builder = StateGraph(SupervisorState)
    for node in (ensure_index, classify, run_map, run_ask):
        builder.add_node(node.__name__, partial(node, deps=deps))
    builder.add_node("chat_reply", chat_reply)
    builder.add_edge(START, "ensure_index")
    builder.add_edge("ensure_index", "classify")
    builder.add_conditional_edges("classify", route, ["run_map", "run_ask", "chat_reply"])
    for node in ("run_map", "run_ask", "chat_reply"):
        builder.add_edge(node, END)
    return builder.compile()


def run_supervisor(
    deps: SupervisorDeps, repo_url: str, message: str, thread_id: str = ""
) -> SupervisorState:
    """메시지 하나를 처리하고 최종 상태를 돌려준다."""
    graph = build_supervisor_graph(deps)
    start = {"repo_url": repo_url, "message": message, "thread_id": thread_id, "events": []}
    return graph.invoke(start)
