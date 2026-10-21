"""Supervisor 그래프: 인덱스 확인 → 의도 분류(+코드 게이트) → 지도 | 따라가기 | 다음 | 안내 → 기억.

wave05: 모든 쿼리가 무거운 파이프라인을 탈 필요는 없다 (chat·next 는 LLM 없이 처리).
wave06: 게이트는 프롬프트가 아니라 코드로 (심볼 없는 ask 는 map, 지도 없는 next 는 map).
wave12: 체크포인터를 주면 guide·cursor·history 가 thread 에 남아 "다음 파일" 이 지도를 다시
만들지 않는다. Phase 5 API 의 단일 진입점. 진행 상황은 events 로 쌓여
stream_mode="updates" 로 보인다.
Phase 6: issues 의도는 기여할 이슈를 추천한다. 프로필(관심·수준)이 thread 에 없으면
interrupt() 로 멈춰 사용자에게 묻고, 같은 thread 의 다음 메시지를 Command(resume=...) 로 받아
이어 간다 (pending_interrupt·turn_input 참고).
"""

import asyncio
import operator
import re
import threading
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel

from repoute.config import DEFAULT_EMBEDDING_MODEL
from repoute.graphs.deps import Deps
from repoute.graphs.reading_guide import ReadingGuide, run_reading_guide
from repoute.graphs.walkthrough import WalkthroughAnswer, ask, build_walkthrough_graph
from repoute.indexing.embeddings import ensure_embeddings
from repoute.indexing.index import RepoIndex, build_index
from repoute.prompts import reading_guide_v3, supervisor_v2, walkthrough_v3

# 심볼 후보: 영문 식별자(점·괄호 포함 가능). 한국어 메시지에서 코드 이름을 찾는 느슨한 기준이다.
# 느슨하게 둔 이유: 이 게이트는 ask 를 map 으로 내리기만 한다. 이름이 있는 질문을 잘못 내리면
# 사용자가 원한 따라가기를 잃으므로, "이름이 정말 하나도 없을 때" 만 발동하게 한다.
SYMBOL_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*(?:\(\))?")
# "다음" 류로 시작하는 메시지. 지도가 있으면 LLM 판단과 무관하게 next 로 고정한다.
# 정규식이라 비용 0 이고, 이 말로 시작하면서 다른 기능을 원하는 경우가 드물어 오탐이 거의 없다.
# 반대 방향(지도 없는 next → map)은 아래 classify 가 따로 막으므로 잘못 고정될 일도 없다.
# "8번 다음", "3단계까지 봤어", "5번 읽었어" — 사용자가 이미 본 단계 번호. next_step 이 cursor 를
# 그 번호로 옮기므로 지도 카드에서 8단계를 다 읽고 "8번 다음" 이라 해도 1단계부터 다시 시작하지
# 않는다.
# "3번 보여줘" 처럼 그 단계 자체를 묻는 말은 뒤에 다음/이후/봤/읽었 이 없어 걸리지 않는다.
SEEN_STEP_PATTERN = re.compile(
    r"(\d+)\s*(?:번|단계)(?:\s*까지)?\s*(?:은|는|을|를|이|가)?\s*(?:다음|이후|뒤|끝|다\s*봤|봤|읽었)"
)
NEXT_PATTERN = re.compile(
    r"^\s*(?:다음|그다음|이어서|next\b)|^\s*" + SEEN_STEP_PATTERN.pattern, re.IGNORECASE
)
# 이슈 추천을 원하는 말. LLM 이 map/chat 이라 해도 이 단어가 있으면 issues 로 고친다 (비용 0).
# 오탐 위험: "이 issue 는 버그야?" 처럼 이슈를 언급만 하는 구조·흐름 질문도 issues 로 간다.
# 그래도 issues 는 프로필 질문(또는 토큰 안내)으로 시작해 되돌리기 쉽고, ask 는 건드리지 않아
# 함수 이름이 있는 질문의 따라가기는 잃지 않는다.
ISSUE_PATTERN = re.compile(r"이슈|issue|기여|contribute|good\s*first", re.IGNORECASE)
# 프로필 수준 단어 → Profile.level. 영어 셋에 한국어 별칭을 더했다
LEVEL_WORDS = {
    "beginner": "beginner",
    "초보": "beginner",
    "초급": "beginner",
    "입문": "beginner",
    "intermediate": "intermediate",
    "중급": "intermediate",
    "advanced": "advanced",
    "고급": "advanced",
}
HISTORY_IN_PROMPT = 4  # 분류 프롬프트에 넣는 최근 대화 수
HISTORY_TEXT_LIMIT = 200  # 기억 한 줄의 최대 글자 수 (프롬프트가 커지지 않게)


class Intent(BaseModel):
    """의도 분류 결과 (fast 모델 구조화 출력)."""

    # map=구조/어디서부터, ask=특정 코드 흐름, next=읽기 순서 다음 단계, chat=인사·기타,
    # issues=기여할 이슈 추천 (Phase 6)
    kind: Literal["map", "ask", "chat", "next", "issues"]
    reason: str


def add_events(old: list[dict] | None, new: list[dict] | None) -> list[dict]:
    """events 리듀서: None 이 오면 비우고, 리스트가 오면 이어 붙인다.

    체크포인터가 있으면 상태가 thread 에 남으므로 operator.add 그대로면 events 가 실행마다
    끝없이 쌓이고, invoke 결과에 지난 실행의 이벤트가 섞인다. 반대로 리듀서를 없애면 노드마다
    덮어써 한 실행 안의 이벤트(IndexReady → IntentDecided → ...)를 잃는다.
    그래서 실행 시작 입력(turn_input)이 None 을 넣어 비우고, 노드는 지금처럼 이어 붙인다.
    """
    return [] if new is None else (old or []) + new


class SupervisorState(TypedDict, total=False):
    repo_url: str
    message: str
    thread_id: str
    intent: Intent | None
    guide: ReadingGuide | None  # map 결과. thread 에 남아 next 가 쓴다
    cursor: int  # 읽기 지도에서 사용자가 본 마지막 단계 번호 (0 = 아직 없음)
    answer: WalkthroughAnswer | None  # ask 결과
    next: dict | None  # next 결과 (next_step 참고)
    validation_errors: list[str]  # 서브그래프에 남은 인용 오류 (설계 원칙 2: 숨기지 않는다)
    reply: str | None  # chat 또는 안내 문구
    profile: dict | None  # 이슈 추천 프로필 (Profile.model_dump). thread 에 남아 다시 묻지 않는다
    issues: Any  # IssueMatches | None — issues 결과. 클래스는 다른 모듈이라 지연 import (Any)
    history: Annotated[list[dict], operator.add]  # 최근 대화 {"role": "user"|"agent", "text"}
    events: Annotated[list[dict], add_events]  # 진행 상황 (SSE 용), 이번 실행 분만


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
    # 옵트인 심볼 임베딩 검색. None 이면 따라가기 도구 5개·게이트 그대로 (기본 동작 불변)
    embedder: Any = None
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    stores: dict[str, Any] = field(default_factory=dict)  # repo_url → SymbolEmbeddingStore
    github_token: str = ""  # 이슈 추천용 GitHub 토큰. 비면 issues 는 안내만 한다
    issue_tools: dict | None = None  # 이름 → GitHub MCP 도구. None 이면 첫 이슈 요청에서 1회 로드

    def sub_deps(self, repo_url: str) -> Deps:
        """서브그래프(읽기 지도·따라가기)가 받을 Deps. 임베딩 저장소가 있으면 함께 넘긴다."""
        store = self.stores.get(repo_url)
        return Deps(self.fast_llm, self.quality_llm, self.indexes[repo_url], self.embedder, store)

    def close(self) -> None:
        """열어 둔 인덱스 DB 연결을 모두 닫는다."""
        for repo in self.indexes.values():
            repo.db.close()
        self.indexes.clear()
        self.stores.clear()  # 저장소는 연결을 들고 있지 않아 닫을 것이 없다


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


def _history_text(history: list[dict]) -> str:
    """최근 대화 HISTORY_IN_PROMPT 개를 프롬프트용 줄로."""
    who = {"user": "사용자", "agent": "도우미"}
    lines = [f"- {who[h['role']]}: {h['text']}" for h in history[-HISTORY_IN_PROMPT:]]
    return "\n".join(lines) or supervisor_v2.NO_HISTORY


def _gate(
    kind: str, message: str, has_guide: bool, can_search: bool = False
) -> tuple[str, str] | None:
    """코드 게이트 (wave06). 바꿀 때만 (새 kind, 이유), 아니면 None.

    can_search: 따라가기에 search_symbols(이름 없이 설명으로 찾기)가 있으면 True. 그때는
    식별자 없는 ask 도 출발점을 찾을 수 있어 map 으로 내리지 않는다 (RAG 1단계 게이트 완화).
    """
    if NEXT_PATTERN.match(message) and has_guide:
        return "next", "'다음' 으로 시작하는 말이고 이 대화에 읽기 지도가 있음"
    if kind == "next" and not has_guide:
        return "map", "이 대화에 읽기 지도가 없어 다음 단계를 고를 수 없음"
    if kind == "ask" and not can_search and not SYMBOL_PATTERN.search(message):
        return "map", "메시지에 함수·클래스 이름이 없어 따라가기 도구를 쓸 수 없음"
    if kind in ("map", "chat") and ISSUE_PATTERN.search(message):
        return "issues", "메시지에 이슈·기여 단어가 있어 이슈 추천으로 보냄"
    return None


def classify(state: SupervisorState, deps: SupervisorDeps) -> dict:
    """fast 모델로 의도를 고르고(최근 대화 포함), 코드 게이트로 바로잡는다 (wave06).

    - 심볼 없는 ask → map: ask 의 도구(find_definition 등)는 이름이 있어야 작동한다.
      단 embedder 가 있으면(search_symbols 사용 가능) 이 규칙은 쓰지 않는다.
    - 지도 없는 next → map: 이어 갈 읽기 순서가 없다.
    - 지도가 있고 "다음" 으로 시작 → next: 지도를 다시 만들면 같은 1단계를 또 보여 주고
      LLM 비용도 다시 든다 (UI 에서 발견한 문제).
    - map/chat 인데 이슈·기여 단어가 있음 → issues (ISSUE_PATTERN 참고).
    """
    history = _history_text(state.get("history") or [])
    prompt = supervisor_v2.CLASSIFY.format(history=history, message=state["message"])
    intent = deps.fast_llm.with_structured_output(Intent).invoke(prompt)
    events = [{"type": "IntentDecided", "kind": intent.kind, "reason": intent.reason}]
    has_guide, can_search = state.get("guide") is not None, deps.embedder is not None
    gate = _gate(intent.kind, state["message"], has_guide, can_search)
    if gate and gate[0] != intent.kind:
        kind, reason = gate
        events.append({"type": "GateApplied", "from": intent.kind, "to": kind, "reason": reason})
        intent = Intent(kind=kind, reason=reason)
    return {"intent": intent, "events": events}


ROUTES = {
    "map": "run_map",
    "ask": "run_ask",
    "next": "next_step",
    "chat": "chat_reply",
    "issues": "run_issues",
}


def route(state: SupervisorState) -> str:
    """의도에 맞는 노드 이름."""
    return ROUTES[state["intent"].kind]


def run_map(state: SupervisorState, deps: SupervisorDeps) -> dict:
    """읽기 지도 서브그래프 (검증 루프 켬, 프롬프트 v3)."""
    sub = deps.sub_deps(state["repo_url"])
    result = run_reading_guide(sub, validate=True, prompts=reading_guide_v3)
    guide, errors = result["guide"], result.get("validation_errors") or []
    event = {"type": "GuideReady", "steps": len(guide.reading_order), "errors": len(errors)}
    # 새 지도는 처음부터: 카드가 전체를 보여 주고, 첫 "다음" 이 1단계를 짚는다
    return {"guide": guide, "cursor": 0, "validation_errors": errors, "events": [event]}


def run_ask(state: SupervisorState, deps: SupervisorDeps) -> dict:
    """따라가기 서브그래프. thread_id 가 없으면 레포별 기본 스레드를 쓴다 (cli 와 같은 규칙).

    노드 안에서 따로 invoke 한 따라가기 그래프는 부모의 하위 그래프가 아니라 독립 루트로
    체크포인트된다(네임스페이스 ""). Supervisor 도 같은 체크포인터·thread_id 의 "" 칸을 쓰므로
    그대로 두면 두 상태가 한 칸에서 섞여 따라가기 대화가 사라진다(직접 확인). 그래서
    따라가기 thread 이름에 ":walkthrough" 를 붙여 같은 SqliteSaver 안에서 칸을 나눈다.

    embedder 가 있으면 먼저 심볼 임베딩을 준비한다. 첫 호출은 임베딩 API 비용이 들어
    EmbeddingsReady 이벤트(건수·캐시 여부)로 드러낸다. 같은 커밋이면 파일 캐시라 비용이 없다.
    """
    events = []
    if deps.embedder is not None:
        url = state["repo_url"]
        store = ensure_embeddings(
            deps.indexes[url], deps.embedder, deps.embedding_model, deps.cache_dir
        )
        deps.stores[url] = store  # 인덱스처럼 deps 에 두고 sub_deps 가 넘긴다
        events.append({"type": "EmbeddingsReady", "count": store.count(), "cached": store.cached})
    sub = deps.sub_deps(state["repo_url"])
    base = state.get("thread_id")
    thread_id = f"{base}:walkthrough" if base else f"{sub.index.info.repo_id}:default"
    graph = build_walkthrough_graph(
        sub, prompts=walkthrough_v3, checkpointer=deps.checkpointer, agent_model="quality"
    )
    result = ask(graph, state["message"], thread_id)
    errors = result.get("validation_errors") or []
    event = {"type": "AnswerReady", "steps": len(result["answer"].steps), "errors": len(errors)}
    return {"answer": result["answer"], "validation_errors": errors, "events": events + [event]}


def next_step(state: SupervisorState) -> dict:
    """읽기 지도의 다음 단계 하나를 짚어 준다 (LLM 없음).

    "다음" 의 정의: 지도 카드는 전체 순서를 한 번에 보여 주므로, "다음" 은 그 단계를
    하나씩 짚어 주는 기능이다. run_map 이 cursor=0 으로 두므로 첫 "다음" 은 1단계를
    이유·인용·따라가기 힌트와 함께 보여 주고, 그 뒤로 한 단계씩 나아간다.
    메시지에 "N번 다음" 처럼 본 단계 번호가 있으면 cursor 를 N 으로 옮긴 뒤 다음을 고른다.
    마지막 단계를 넘기면 고정 안내(reply)를 준다.
    """
    order = state["guide"].reading_order
    cursor = state.get("cursor", 0)
    if m := SEEN_STEP_PATTERN.search(state["message"]):  # "8번 다음" → 8단계까지는 본 것으로
        cursor = min(int(m.group(1)), len(order))
    if cursor >= len(order):
        event = {"type": "NextStep", "order": None, "remaining": 0}
        return {"reply": supervisor_v2.NEXT_DONE, "cursor": cursor, "events": [event]}
    step = order[cursor]
    target = step.ref.symbol or step.file
    nxt = {
        "order": step.order,
        "file": step.file,
        "reason": step.reason,
        "ref": step.ref.model_dump(),
        "remaining": len(order) - cursor - 1,
        "hint": f'"{target} 를 따라가 줘" 라고 물으면 이 파일의 흐름을 설명해 드립니다',
    }
    event = {"type": "NextStep", "order": step.order, "remaining": nxt["remaining"]}
    return {"next": nxt, "cursor": cursor + 1, "events": [event]}


def _profile_class():
    """Profile 클래스 (issue_matcher 를 쓸 때만 import 한다)."""
    from repoute.graphs.issue_matcher import Profile

    return Profile


def parse_profile(text: str):
    """사용자 답 한 줄 → Profile. 쉼표로 나눈 조각에서 수준 단어를 찾고 나머지는 관심 영역.

    "인증, beginner" → (인증, beginner)
    "HTTP 인증, CLI 파싱 고급" → (HTTP 인증, CLI 파싱, advanced)
    수준 단어가 없으면 beginner (기여가 처음인 사용자가 가장 흔하다).
    """
    level, parts = "beginner", []
    for chunk in text.split(","):
        words = []
        for word in chunk.split():
            found = LEVEL_WORDS.get(word.strip(".!?()").lower())
            if found:
                level = found
            else:
                words.append(word)
        if words:
            parts.append(" ".join(words))
    return _profile_class()(interests=", ".join(parts), level=level)


def _run_async(coro):
    """코루틴을 끝까지 돌린다. 실행 중인 루프가 있으면 새 스레드의 새 루프에서 돌린다.

    API 는 그래프를 별도 스레드에서 돌려 보통 루프가 없지만, 노트북·async 코드에서 부르면
    asyncio.run 이 "이미 루프가 돈다" 로 실패하므로 그 경우만 스레드를 하나 띄운다.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    box = {}

    def target():
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as e:  # 스레드 밖으로 그대로 다시 던진다
            box["error"] = e

    worker = threading.Thread(target=target)
    worker.start()
    worker.join()
    if "error" in box:
        raise box["error"]
    return box["value"]


def _load_issue_tools(deps: SupervisorDeps) -> dict:
    """GitHub MCP 도구를 처음 한 번만 불러 deps.issue_tools 에 둔다 (테스트는 대체한다)."""
    if deps.issue_tools is None:
        from repoute.github.mcp import load_github_tools

        tools = _run_async(load_github_tools(deps.github_token))
        deps.issue_tools = {tool.name: tool for tool in tools}
    return deps.issue_tools


def _run_issue_matcher(deps: SupervisorDeps, repo_url: str, profile):
    """Issue Matcher 서브그래프를 부르는 얇은 래퍼 (지연 import, 테스트는 대체한다).

    embedder 가 있으면 따라가기처럼 심볼 임베딩을 준비해 넘긴다 (같은 커밋이면 파일 캐시).
    """
    from repoute.graphs.issue_matcher import IssueDeps, run_issue_matcher

    index, store = deps.indexes[repo_url], None
    if deps.embedder is not None:
        store = ensure_embeddings(index, deps.embedder, deps.embedding_model, deps.cache_dir)
        deps.stores[repo_url] = store
    sub = IssueDeps(
        fast_llm=deps.fast_llm,
        index=index,
        embedder=deps.embedder,
        embedding_store=store,
        issue_tools=deps.issue_tools or {},
    )
    return run_issue_matcher(sub, f"{index.info.owner}/{index.info.name}", profile)


def run_issues(state: SupervisorState, deps: SupervisorDeps) -> dict:
    """기여할 이슈를 추천한다. 프로필이 없으면 interrupt() 로 멈춰 사용자에게 묻는다.

    interrupt 는 노드를 예외로 멈추고, Command(resume=답) 으로 재개하면 이 노드를 처음부터
    다시 실행하며 그때 interrupt() 가 답을 돌려준다. 그래서 interrupt 앞에는 다시 해도 되는
    일(토큰 확인)만 두고, 비용이 드는 일(도구 로드·매칭)은 뒤에 둔다.
    재개 실행은 ensure_index 를 거치지 않으므로 인덱스가 없으면 여기서 연다 (캐시 히트).
    토큰이 없거나 thread 가 없으면(재개할 곳이 없음) 멈추지 않고 안내(reply)만 준다.
    """
    if not deps.github_token:
        return {"reply": supervisor_v2.ISSUES_NEED_TOKEN, "events": [{"type": "Replied"}]}
    if not state.get("thread_id"):
        return {"reply": supervisor_v2.ISSUES_NEED_THREAD, "events": [{"type": "Replied"}]}
    update = {}
    if state.get("profile") is None:
        answer = interrupt({"kind": "profile", "question": supervisor_v2.PROFILE_QUESTION})
        profile = parse_profile(str(answer))
        update["profile"] = profile.model_dump()
    else:
        profile = _profile_class()(**state["profile"])
    url = state["repo_url"]
    if url not in deps.indexes:
        deps.indexes[url] = build_index(url, deps.cache_dir)
    _load_issue_tools(deps)
    issues = _run_issue_matcher(deps, url, profile)
    event = {"type": "IssuesReady", "count": len(issues.matches), "considered": issues.considered}
    return {**update, "issues": issues, "events": [event]}


def chat_reply(state: SupervisorState) -> dict:
    """LLM 없이 고정 안내를 돌려준다."""
    return {"reply": supervisor_v2.CHAT_REPLY, "events": [{"type": "Replied"}]}


def _one_line(text: str) -> str:
    """첫 줄만, HISTORY_TEXT_LIMIT 글자까지."""
    return (text.strip().splitlines() or [""])[0][:HISTORY_TEXT_LIMIT]


def _agent_summary(state: SupervisorState) -> str:
    """이번 실행에서 도우미가 한 일을 한 줄로 (다음 분류의 문맥)."""
    kind = state["intent"].kind
    if kind == "map":
        order = state["guide"].reading_order
        first = f" (1단계: {order[0].file})" if order else ""
        return f"읽기 지도 {len(order)}단계를 보여 줌{first}"
    if kind == "ask":
        return f"따라가기 답: {state['answer'].summary}"
    if kind == "next" and state.get("next"):
        nxt = state["next"]
        return f"읽기 순서 {nxt['order']}단계 {nxt['file']} 를 짚음 (남은 {nxt['remaining']}개)"
    if kind == "issues" and state.get("issues") is not None:
        matches = state["issues"].matches
        numbers = ", ".join(f"#{m.number}" for m in matches)
        return f"이슈 추천 {len(matches)}개: {numbers}" if matches else "추천할 이슈를 찾지 못함"
    return state.get("reply") or ""


def remember(state: SupervisorState) -> dict:
    """사용자 메시지와 도우미 한 줄 요약을 history 에 붙인다. 모든 경로가 END 전에 거친다."""
    user = {"role": "user", "text": _one_line(state["message"])}
    agent = {"role": "agent", "text": _one_line(_agent_summary(state))}
    return {"history": [user, agent]}


def build_supervisor_graph(deps: SupervisorDeps, checkpointer=None):
    """START → ensure_index → classify → (run_map | run_ask | next_step | chat_reply | run_issues)
    → remember → END.

    checkpointer 를 주면 같은 thread_id 로 guide·cursor·history 를 이어 간다 (wave12).
    """
    builder = StateGraph(SupervisorState)
    for node in (ensure_index, classify, run_map, run_ask, run_issues):
        builder.add_node(node.__name__, partial(node, deps=deps))
    for node in (next_step, chat_reply, remember):
        builder.add_node(node.__name__, node)
    builder.add_edge(START, "ensure_index")
    builder.add_edge("ensure_index", "classify")
    builder.add_conditional_edges("classify", route, list(ROUTES.values()))
    for node in ROUTES.values():
        builder.add_edge(node, "remember")
    builder.add_edge("remember", END)
    return builder.compile(checkpointer=checkpointer)


def pending_interrupt(graph, config: dict) -> dict | None:
    """이 thread 에서 답을 기다리는 interrupt 의 값({"kind", "question"}), 없으면 None.

    state.next 만 보지 않는 이유: 노드가 예외로 끝난 실행도 next 가 남는다. 그 thread 에
    새 메시지를 resume 으로 넘기면 실패한 노드를 그 메시지로 다시 돌리게 되므로, interrupt 가
    실제로 걸린 경우만 재개하고 나머지는 새 턴으로 처음부터 실행한다.
    """
    if graph.checkpointer is None or not config["configurable"].get("thread_id"):
        return None
    for task in graph.get_state(config).tasks:
        for item in task.interrupts:
            return item.value
    return None


def _turn_reset() -> dict:
    """이번 실행의 결과 칸을 비우는 값. events 는 None 으로 비운다 (add_events 참고)."""
    return {
        "answer": None,
        "next": None,
        "reply": None,
        "issues": None,
        "validation_errors": [],
        "events": None,
    }


def turn_input(repo_url: str, message: str, thread_id: str, graph=None) -> dict | Command:
    """메시지 하나의 시작 입력. thread 에 남길 것(guide·cursor·history·profile)은 건드리지 않는다.

    이번 실행의 결과 칸(answer·next·reply·issues·validation_errors)은 비워 지난 메시지의 결과가
    섞이지 않게 한다. graph 를 주고 그 thread 에 답을 기다리는 interrupt 가 있으면
    (pending_interrupt) 새 메시지를 그 답으로 보는 Command(resume=message) 를 돌려준다.
    이때 update 로 결과 칸을 비워야 멈춘 실행의 events 가 재개 결과에 섞이지 않는다.
    원래 message·intent 는 그대로 둔다 (history 에는 이슈를 요청한 원래 말이 남는다).
    한계: 질문을 받은 뒤 전혀 다른 말을 해도 그 말을 답으로 쓴다 (프로필로 파싱된다).
    """
    if graph is not None:
        config = {"configurable": {"thread_id": thread_id}}
        if pending_interrupt(graph, config) is not None:
            return Command(resume=message, update=_turn_reset())
    return {"repo_url": repo_url, "message": message, "thread_id": thread_id, **_turn_reset()}


# interrupt kind → 진행 이벤트 이름
INTERRUPT_EVENTS = {"profile": "ProfileRequested"}


def ask_user_update(interrupts) -> dict:
    """그래프 출력의 __interrupt__ 를 결과 칸으로 바꾼다: reply=질문, ask_user=True, 이벤트.

    기존 프론트는 reply 를 그대로 보여 주므로 질문을 reply 에 넣는 것이 최소 변경이다.
    """
    value = interrupts[0].value
    question = value.get("question", "")
    kind = INTERRUPT_EVENTS.get(value.get("kind"), "InputRequested")
    event = {"type": kind, "question": question}
    return {"reply": question, "ask_user": True, "events": [event]}


def run_supervisor(
    deps: SupervisorDeps, repo_url: str, message: str, thread_id: str = ""
) -> SupervisorState:
    """메시지 하나를 처리하고 최종 상태를 돌려준다.

    thread_id 가 있으면 deps.checkpointer 로 상태를 thread 에 남긴다. 없으면 기억 없이 한 번.
    그 thread 가 질문(interrupt)에서 멈춰 있으면 이 메시지로 재개한다. 이번 실행이 질문에서
    멈추면 상태에 ask_user=True 와 reply=질문, ProfileRequested 이벤트를 더해 돌려준다.
    """
    saver = deps.checkpointer if thread_id else None
    graph = build_supervisor_graph(deps, saver)
    config = {"configurable": {"thread_id": thread_id}}
    state = graph.invoke(turn_input(repo_url, message, thread_id, graph), config=config)
    if interrupts := state.get("__interrupt__"):
        asked = ask_user_update(interrupts)
        state = {**state, **asked, "events": (state.get("events") or []) + asked["events"]}
    return state
