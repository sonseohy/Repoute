"""읽기 지도 그래프: 대상 선택 → Send 병렬 모듈 요약(fast) → 읽기 순서 합성(quality) → 인용 검증."""

import operator
from functools import partial
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from pydantic import BaseModel

from repoute.graphs.deps import Deps
from repoute.indexing.entrypoints import find_entrypoints
from repoute.indexing.imports import rank_modules
from repoute.prompts.reading_guide_v1 import SUMMARIZE_MODULE, SYNTHESIZE_GUIDE
from repoute.validation.citations import CodeRef, validate_citations

MAX_TARGETS = 12  # PageRank 상위 몇 개 모듈을 요약할지
HEAD_LINES = 60  # 요약 프롬프트에 넣을 파일 앞부분 줄 수
MAX_EDGES = 30  # 합성 프롬프트에 넣을 import 관계 수
MAX_RETRIES = 2  # 인용 검증 실패 시 재생성 횟수 상한 (무한 루프 방지)


class ModuleSummary(BaseModel):
    """모듈 하나의 요약 (fast 모델 구조화 출력)."""

    module: str
    file: str
    summary: str
    role: str  # entry | core | util | config | other


class ReadingStep(BaseModel):
    """읽기 순서의 한 단계."""

    order: int
    file: str
    reason: str
    ref: CodeRef


class ReadingGuide(BaseModel):
    """최종 읽기 지도 (quality 모델 구조화 출력)."""

    overview: str  # 프로젝트 한 줄 개요
    entrypoints: list[CodeRef]
    reading_order: list[ReadingStep]


class GuideState(TypedDict, total=False):
    targets: list[str]  # 요약할 모듈 이름
    summaries: Annotated[list[ModuleSummary], operator.add]  # Send 병렬 결과 병합(리듀서)
    guide: ReadingGuide | None
    validation_errors: list[str]
    retry_count: int


def _symbols(deps: Deps, file: str) -> list[tuple[str, str, int]]:
    """파일 하나의 (이름, 종류, 시작 줄) 목록."""
    rows = deps.index.db.conn.execute(
        "SELECT name, kind, line_start FROM symbols WHERE file = ? ORDER BY line_start", (file,)
    )
    return [tuple(row) for row in rows]


def select_targets(state: GuideState, deps: Deps) -> dict:
    """PageRank 상위 모듈 + 진입점이 있는 모듈 (중복 제거, 순서 유지)."""
    graph = deps.index.graph
    top = [module for module, _ in rank_modules(graph)[:MAX_TARGETS]]
    by_file = {data["file"]: module for module, data in graph.nodes(data=True)}  # 파일 → 모듈
    entry = find_entrypoints(deps.index.db, deps.index.info.path)
    entry_modules = [by_file[s.file] for s in entry if s.file in by_file]
    return {"targets": list(dict.fromkeys(top + entry_modules))}


def fan_out(state: GuideState, deps: Deps) -> list[Send]:
    """모듈마다 summarize_module 을 하나씩 병렬 실행 (wave10b).

    SQLite 연결은 만든 스레드에서만 쓸 수 있어, 심볼은 여기(호출 스레드)서 미리 읽어 넘긴다.
    """
    graph = deps.index.graph
    return [
        Send("summarize_module", {"module": m, "symbols": _symbols(deps, graph.nodes[m]["file"])})
        for m in state["targets"]
    ]


def summarize_module(state: dict, deps: Deps) -> dict:
    """모듈 하나를 fast 모델로 요약한다. 입력은 Send 로 받은 {"module", "symbols"}."""
    module = state["module"]
    file = deps.index.graph.nodes[module]["file"]
    symbols = "\n".join(f"- {n} ({k}) {line}줄" for n, k, line in state["symbols"]) or "없음"
    source = (deps.index.info.path / file).read_text(encoding="utf-8", errors="replace")
    head = source.splitlines()[:HEAD_LINES]
    code = "\n".join(f"{i:>4}| {text}" for i, text in enumerate(head, start=1))
    prompt = SUMMARIZE_MODULE.format(module=module, file=file, symbols=symbols, code=code)
    result = deps.fast_llm.with_structured_output(ModuleSummary).invoke(prompt)
    return {"summaries": [result]}


def synthesize(state: GuideState, deps: Deps) -> dict:
    """요약·import 관계·진입점·PageRank 로 읽기 순서를 합성한다 (quality 모델)."""
    index = deps.index
    summaries = "\n".join(
        f"- {s.module} ({s.file}, {s.role}): {s.summary}\n"
        f"  심볼: {', '.join(f'{n}:{line}' for n, _, line in _symbols(deps, s.file)) or '없음'}"
        for s in state.get("summaries", [])
    )
    edges = "\n".join(f"- {a} → {b}" for a, b in list(index.graph.edges)[:MAX_EDGES]) or "없음"
    entry = find_entrypoints(index.db, index.info.path)
    entrypoints = "\n".join(f"- {s.file}:{s.line_start} {s.name}" for s in entry) or "없음"
    ranked = "\n".join(f"- {m} ({score:.3f})" for m, score in rank_modules(index.graph)[:10])
    prompt = SYNTHESIZE_GUIDE.format(
        repo_id=index.info.repo_id,
        summaries=summaries or "없음",
        edges=edges,
        entrypoints=entrypoints,
        ranked=ranked or "없음",
        feedback="\n".join(state.get("validation_errors") or []) or "없음",
    )
    result = deps.quality_llm.with_structured_output(ReadingGuide).invoke(prompt)
    # 피드백을 받고 다시 합성하는 경우에만 재생성 횟수를 올린다
    retry = state.get("retry_count", 0) + (1 if state.get("validation_errors") else 0)
    return {"guide": result, "retry_count": retry}


def validate(state: GuideState, deps: Deps) -> dict:
    """진입점·각 단계의 인용을 실제 코드와 대조한다 (wave08 Validator)."""
    guide = state["guide"]
    refs = guide.entrypoints + [step.ref for step in guide.reading_order]
    errors = validate_citations(refs, deps.index.db, deps.index.info.path)
    return {"validation_errors": errors}


_validate = validate  # 빌더의 매개변수 validate 가 이 함수 이름을 가리므로 별칭을 둔다


def after_validate(state: GuideState) -> str:
    """오류가 있고 상한 전이면 피드백을 넣어 다시 합성, 아니면 끝."""
    if state.get("validation_errors") and state.get("retry_count", 0) < MAX_RETRIES:
        return "synthesize"
    return END


def build_reading_guide_graph(deps: Deps, validate: bool = True):
    """validate=False 면 검증 루프 없는 naive 버전 (실험 E4 베이스라인용)."""
    builder = StateGraph(GuideState)
    builder.add_node("select_targets", partial(select_targets, deps=deps))
    builder.add_node("summarize_module", partial(summarize_module, deps=deps))
    builder.add_node("synthesize", partial(synthesize, deps=deps))
    builder.add_edge(START, "select_targets")
    builder.add_conditional_edges(
        "select_targets", partial(fan_out, deps=deps), ["summarize_module"]
    )
    builder.add_edge("summarize_module", "synthesize")  # Send 가 모두 끝나면 한 번 실행
    if validate:
        builder.add_node("validate", partial(_validate, deps=deps))
        builder.add_edge("synthesize", "validate")
        builder.add_conditional_edges("validate", after_validate, ["synthesize", END])
    else:
        builder.add_edge("synthesize", END)
    return builder.compile()


def run_reading_guide(deps: Deps, validate: bool = True) -> GuideState:
    """그래프를 만들어 한 번 실행하고 최종 상태를 돌려준다."""
    graph = build_reading_guide_graph(deps, validate=validate)
    return graph.invoke({"retry_count": 0, "validation_errors": []})
