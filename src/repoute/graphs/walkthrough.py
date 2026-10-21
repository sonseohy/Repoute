"""따라가기 그래프: ReAct(도구 5종 반복 호출) → 구조화 답 → 인용 검증(읽은 범위 안인지) 루프."""

from functools import partial
from pathlib import PurePosixPath
from types import ModuleType
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from pydantic import BaseModel

from repoute.graphs.deps import Deps
from repoute.prompts import walkthrough_v1
from repoute.tools.code_tools import MAX_READ_LINES, make_code_tools
from repoute.validation.citations import CodeRef, validate_citations

MAX_TOOL_CALLS = 15  # 도구 호출 상한 (wave07 재계획 루프 상한)
MAX_RETRIES = 2  # 인용 검증 실패 시 재작성 상한
# 슈퍼스텝 상한을 명시: agent 1 + (tools·agent) 15쌍 + (finalize·validate) 3쌍 + 여유 5.
# langgraph 1.2 기본값은 매우 크지만, 이 그래프가 최대 몇 스텝인지 코드에 적어 두는 쪽을 택했다.
RECURSION_LIMIT = 1 + 2 * MAX_TOOL_CALLS + 2 * (MAX_RETRIES + 1) + 5
# 체크포인터가 상태의 답(pydantic)을 되살릴 때 허용할 타입 (langgraph-checkpoint 4.x: 미등록은 경고)
CHECKPOINT_TYPES = [("repoute.graphs.walkthrough", "WalkthroughAnswer")]


class Step(BaseModel):
    """호출 체인의 한 단계."""

    order: int
    title: str  # 예: "create_app() 이 FastAPI 인스턴스를 만든다"
    explanation: str  # 2~4문장
    refs: list[CodeRef]  # 최소 1개 (validate 가 검사)


class WalkthroughAnswer(BaseModel):
    """따라가기 최종 답 (quality 모델 구조화 출력)."""

    question: str
    steps: list[Step]
    summary: str


class WalkState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]  # ReAct 대화 (리듀서로 이어 붙임)
    tool_calls: int  # 도구 호출 누적 수 (상한 MAX_TOOL_CALLS)
    read_ranges: list[tuple[str, int, int]]  # read_code 로 실제 읽은 (file, start, end)
    answer: WalkthroughAnswer | None
    validation_errors: list[str]
    retry_count: int


def _spans(ranges: list, file: str) -> list[tuple[int, int]]:
    """한 파일에서 읽은 줄 범위를 정렬하고, 겹치거나 맞닿은 범위는 합친다."""
    merged: list[tuple[int, int]] = []
    for start, end in sorted((s, e) for f, s, e in ranges if f == file):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _readable(ranges: list) -> str:
    """인용 가능한 범위 문자열: "app/main.py 1~5줄, app/util.py 1~2줄"."""
    files = dict.fromkeys(f for f, _, _ in ranges)  # 읽은 순서 유지, 중복 제거
    return ", ".join(f"{f} {a}~{b}줄" for f in files for a, b in _spans(ranges, f)) or "없음"


def agent(
    state: WalkState,
    deps: Deps,
    tools: list,
    prompts: ModuleType = walkthrough_v1,
    agent_model: str = "quality",
) -> dict:
    """다음에 부를 도구를 고르거나, 도구 없이 답할 준비가 됐다고 말한다 (ReAct).

    agent_model 이 "fast" 면 fast 모델, 아니면 quality 모델로 도구를 고른다 (실험 E5).
    ReAct 비용은 턴마다 다시 보내는 대화 길이가 지배하므로(troubleshooting 19) 턴 단가를 낮춘다.
    기본값 quality 는 기록 07·08 을 재현한다. finalize 는 어느 쪽이든 quality (wave10a).
    """
    model = deps.fast_llm if agent_model == "fast" else deps.quality_llm
    llm = model.bind_tools(tools)
    return {"messages": [llm.invoke([SystemMessage(prompts.SYSTEM)] + state["messages"])]}


def _read_range(args: dict) -> tuple[str, int, int]:
    """read_code 와 같은 규칙(시작은 1 이상, 최대 200줄)으로 실제 읽힐 (file, start, end)."""
    start = max(int(args["start"]), 1)
    end = min(int(args["end"]), start + MAX_READ_LINES - 1)
    return str(PurePosixPath(args["file"])), start, end


def _covering(call: dict, ranges: list) -> tuple[str, int, int] | None:
    """read_code 호출이 이미 읽은 범위 하나에 완전히 들어가면 그 (file, a, b), 아니면 None."""
    if call["name"] != "read_code":
        return None
    try:
        file, start, end = _read_range(call["args"])
    except (KeyError, TypeError, ValueError):  # 인자 오류는 도구를 실행해 그대로 알린다
        return None
    return next(((file, a, b) for a, b in _spans(ranges, file) if a <= start and end <= b), None)


def tools_node(state: WalkState, tools: list) -> dict:
    """마지막 AIMessage 의 도구 호출을 실행하고, 호출 수와 read_code 로 읽은 범위를 기록한다.

    ToolNode 는 도구를 스레드 풀에서 돌려 SQLite 스레드 오류가 난다 (troubleshooting 04).
    그래서 호출 스레드에서 하나씩 실행한다. 상한을 넘는 호출은 실행하지 않고 그렇다고 답한다.
    이미 읽은 범위 안을 다시 읽는 read_code 는 실행도, 호출 수 집계도 하지 않는다 (기록 08 관찰 3).
    """
    by_name = {t.name: t for t in tools}
    used = state.get("tool_calls", 0)
    ranges = list(state.get("read_ranges", []))
    results = []
    for call in state["messages"][-1].tool_calls:
        tool, call_id = by_name.get(call["name"]), call["id"]
        seen = _covering(call, ranges)
        if seen:  # 프롬프트 지시 대신 입력으로 막는다: 예산을 쓰지 않고 앞의 결과를 가리킨다
            file, a, b = seen
            note = f"이미 읽은 범위입니다 ({file} {a}~{b}줄). 대화의 앞부분 결과를 쓰세요."
            results.append(ToolMessage(note, tool_call_id=call_id))
            continue
        if used >= MAX_TOOL_CALLS or tool is None:
            results.append(ToolMessage("실행 안 함: 상한 초과·없는 도구", tool_call_id=call_id))
            continue
        used += 1
        try:
            results.append(tool.invoke(call))  # ToolCall 을 넣으면 ToolMessage 가 나온다
        except Exception as error:  # 인자 오류 등은 모델이 보고 고치도록 문자열로 돌려준다
            results.append(ToolMessage(f"도구 오류: {error}", tool_call_id=call_id))
            continue
        if call["name"] == "read_code":  # read_code 와 같은 규칙으로 실제 읽힌 범위를 남긴다
            ranges.append(_read_range(call["args"]))
    return {"messages": results, "tool_calls": used, "read_ranges": ranges}


def route_after_agent(state: WalkState) -> str:
    """도구 호출이 있고 상한 전이면 tools, 아니면 답 작성(finalize)."""
    last = state["messages"][-1]
    if getattr(last, "tool_calls", None) and state.get("tool_calls", 0) < MAX_TOOL_CALLS:
        return "tools"
    return "finalize"


def finalize(state: WalkState, deps: Deps, prompts: ModuleType = walkthrough_v1) -> dict:
    """대화 기록과 읽은 범위로 구조화 답을 만든다 (quality 모델)."""
    last = state["messages"][-1]
    # 상한 때문에 실행 못 한 도구 호출에도 답을 달아야 대화 형식이 맞다 (실제 API 는 거부한다)
    calls = last.tool_calls if isinstance(last, AIMessage) else []
    pending = [ToolMessage("실행 안 함: 도구 호출 상한", tool_call_id=c["id"]) for c in calls]
    prompt = prompts.FINALIZE.format(
        readable=_readable(state.get("read_ranges", [])),
        feedback="\n".join(state.get("validation_errors") or []) or "없음",
    )
    if state.get("tool_calls", 0) >= MAX_TOOL_CALLS:
        prompt = prompts.LIMIT_NOTE + prompt
    llm = deps.quality_llm.with_structured_output(WalkthroughAnswer)
    answer = llm.invoke(state["messages"] + pending + [HumanMessage(prompt)])
    # 피드백을 받고 다시 쓰는 경우에만 재생성 횟수를 올린다 (reading_guide 와 같은 의미)
    retry = state.get("retry_count", 0) + (1 if state.get("validation_errors") else 0)
    return {"messages": pending, "answer": answer, "retry_count": retry}


def _hint(file: str, ranges: list) -> str:
    """오류 하나에 "대신 어디를 인용할지" 알려 주는 힌트 (troubleshooting 09)."""
    spans = _spans(ranges, file)
    if spans:
        return "이 파일에서 읽은 범위: " + ", ".join(f"{a}~{b}줄" for a, b in spans)
    return f"이 파일은 읽지 않음. 인용 가능한 범위: {_readable(ranges)}"


def validate(state: WalkState, deps: Deps) -> dict:
    """인용이 (a) 실제로 존재하고 (b) read_code 로 읽은 범위 안인지 검사한다.

    (b) 는 허용 범위를 지시가 아니라 입력으로 좁히는 장치다 (troubleshooting 14).
    """
    ranges = state.get("read_ranges", [])
    errors = []
    for step in state["answer"].steps:
        if not step.refs:
            errors.append(f"step {step.order} — 인용 없음 → 인용 가능한 범위: {_readable(ranges)}")
        for ref in step.refs:
            file = str(PurePosixPath(ref.file))  # "./a.py" 같은 표기를 read_ranges 와 맞춘다
            found = validate_citations([ref], deps.index.db, deps.index.info.path)
            if not found and not any(a <= ref.line <= b for a, b in _spans(ranges, file)):
                found = [f"{ref.file}:{ref.line} — 읽지 않은 코드"]
            errors += [f"{error} → {_hint(file, ranges)}" for error in found]
    return {"validation_errors": errors}


def after_validate(state: WalkState) -> str:
    """오류가 있고 상한 전이면 피드백을 넣어 다시 작성, 아니면 끝."""
    if state.get("validation_errors") and state.get("retry_count", 0) < MAX_RETRIES:
        return "finalize"
    return END


def build_walkthrough_graph(
    deps: Deps,
    prompts: ModuleType = walkthrough_v1,
    checkpointer=None,
    agent_model: str = "quality",
):
    """START → agent ⇄ tools → finalize → validate → (finalize | END).

    checkpointer 를 주면 같은 thread_id 로 대화를 이어 갈 수 있다 (wave12).
    agent_model("fast" | "quality") 은 agent 노드만 바꾼다. finalize 는 항상 quality.
    """
    tools = make_code_tools(deps.index.db, deps.index.info.path)
    builder = StateGraph(WalkState)
    agent_node = partial(agent, deps=deps, tools=tools, prompts=prompts, agent_model=agent_model)
    builder.add_node("agent", agent_node)
    builder.add_node("tools", partial(tools_node, tools=tools))
    builder.add_node("finalize", partial(finalize, deps=deps, prompts=prompts))
    builder.add_node("validate", partial(validate, deps=deps))
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", route_after_agent, ["tools", "finalize"])
    builder.add_edge("tools", "agent")
    builder.add_edge("finalize", "validate")
    builder.add_conditional_edges("validate", after_validate, ["finalize", END])
    return builder.compile(checkpointer=checkpointer)


def ask(graph, question: str, thread_id: str = "default") -> WalkState:
    """질문 하나를 실행한다. 호출 수는 질문마다 새로 세고, 읽은 범위는 같은 thread 에서 누적된다.

    read_ranges 를 여기서 초기화하지 않아야 후속 질문이 앞에서 읽은 코드를 인용할 수 있다
    (체크포인터가 있을 때). 체크포인터가 없으면 빈 상태에서 시작한다.
    """
    start = {
        "messages": [HumanMessage(question)],
        "tool_calls": 0,
        "retry_count": 0,
        "validation_errors": [],
    }
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": RECURSION_LIMIT}
    return graph.invoke(start, config=config)
