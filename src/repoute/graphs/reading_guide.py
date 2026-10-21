"""읽기 지도 그래프: 대상 선택 → Send 병렬 모듈 요약(fast) → 읽기 순서 합성(quality) → 인용 검증."""

import operator
from functools import partial
from pathlib import PurePosixPath
from types import ModuleType
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from pydantic import BaseModel

from repoute.graphs.deps import Deps
from repoute.indexing.entrypoints import find_entrypoints
from repoute.indexing.imports import modules_of, rank_modules_v2
from repoute.indexing.parser import Symbol
from repoute.prompts import reading_guide_v1
from repoute.validation.citations import CodeRef, validate_citations

MAX_TARGETS = 12  # 랭킹 v2 상위 몇 개 모듈을 요약할지
HEAD_LINES = 60  # 요약 프롬프트에 넣을 파일 앞부분 줄 수
MAX_EDGES = 30  # 합성 프롬프트에 넣을 import 관계 수
MAX_RETRIES = 2  # 인용 검증 실패 시 재생성 횟수 상한 (무한 루프 방지)
MAX_HINT_LINES = 15  # 힌트에 넣을 인용 가능한 심볼 줄 수
MAX_HINT_PATHS = 3  # 힌트에 넣을 비슷한 경로 수


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


def _citable(deps: Deps, file: str, limit: int | None = None) -> str:
    """인용 가능한 줄 목록. 심볼이 없는 모듈만 줄 1(모듈 시작)을 준다.

    심볼이 있는 파일에도 줄 1 을 주면 모델이 안전한 줄 1 로 쏠린다 (E6, troubleshooting 14).
    """
    symbols = _symbols(deps, file)[:limit]
    return ", ".join(f"{n}:{line}" for n, _, line in symbols) or "1(모듈 시작)"


def _ranked(deps: Deps) -> tuple[list[Symbol], list[tuple[str, float]]]:
    """(진입점, 랭킹 v2 점수 목록). 진입점이 있는 모듈을 시드로 쓴다 (기록 17)."""
    index = deps.index
    entry = find_entrypoints(index.db, index.info.path)
    seeds = modules_of(index.graph, [s.file for s in entry])
    return entry, rank_modules_v2(index.graph, seeds)


def select_targets(state: GuideState, deps: Deps) -> dict:
    """랭킹 v2 상위 모듈 + 진입점이 있는 모듈 (중복 제거, 순서 유지)."""
    entry, ranked = _ranked(deps)
    top = [module for module, _ in ranked[:MAX_TARGETS]]
    entry_modules = modules_of(deps.index.graph, [s.file for s in entry])
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


def summarize_module(state: dict, deps: Deps, prompts: ModuleType = reading_guide_v1) -> dict:
    """모듈 하나를 fast 모델로 요약한다. 입력은 Send 로 받은 {"module", "symbols"}."""
    module = state["module"]
    file = deps.index.graph.nodes[module]["file"]
    symbols = "\n".join(f"- {n} ({k}) {line}줄" for n, k, line in state["symbols"]) or "없음"
    source = (deps.index.info.path / file).read_text(encoding="utf-8", errors="replace")
    head = source.splitlines()[:HEAD_LINES]
    code = "\n".join(f"{i:>4}| {text}" for i, text in enumerate(head, start=1))
    prompt = prompts.SUMMARIZE_MODULE.format(module=module, file=file, symbols=symbols, code=code)
    result = deps.fast_llm.with_structured_output(ModuleSummary).invoke(prompt)
    return {"summaries": [result]}


def synthesize(state: GuideState, deps: Deps, prompts: ModuleType = reading_guide_v1) -> dict:
    """요약·import 관계·진입점·랭킹 v2 로 읽기 순서를 합성한다 (quality 모델).

    프롬프트의 "PageRank 상위" 라벨은 그대로 두고, 넘기는 목록만 랭킹 v2 순서로 바꿨다.
    """
    index = deps.index
    summaries = "\n".join(
        f"- {s.module} ({s.file}, {s.role}): {s.summary}\n"
        f"  인용 가능한 줄: {_citable(deps, s.file)}"
        for s in state.get("summaries", [])
    )
    entry, ranked_list = _ranked(deps)
    ranked_scores = dict(ranked_list)
    # 중요한 모듈(랭킹 v2 점수 높은 쪽)로 들어가는 import 관계부터 상위 MAX_EDGES 개
    top_edges = sorted(index.graph.edges, key=lambda e: -ranked_scores.get(e[1], 0))[:MAX_EDGES]
    edges = "\n".join(f"- {a} → {b}" for a, b in top_edges) or "없음"
    entrypoints = "\n".join(f"- {s.file}:{s.line_start} {s.name}" for s in entry) or "없음"
    ranked = "\n".join(f"- {m} ({score:.3f})" for m, score in ranked_list[:10])
    prompt = prompts.SYNTHESIZE_GUIDE.format(
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


def _hint(error: str, ref: CodeRef, deps: Deps) -> str:
    """검증 오류 하나에 "대신 무엇을 쓸지" 알려 주는 힌트 (E4 에서 피드백만으로는 못 고쳤다)."""
    file = str(PurePosixPath(ref.file))  # "./a.py" 같은 표기를 DB 경로("a.py")와 맞춘다
    if error.endswith("파일이 없음"):
        name = PurePosixPath(file).name
        files = {data["file"] for _, data in deps.index.graph.nodes(data=True)}
        similar = sorted(f for f in files if PurePosixPath(f).name == name)[:MAX_HINT_PATHS]
        return f"인덱스에 있는 비슷한 경로: {', '.join(similar) or '없음'}"
    return f"이 파일에서 인용 가능한 줄: {_citable(deps, file, MAX_HINT_LINES)}"


def validate(state: GuideState, deps: Deps) -> dict:
    """진입점·각 단계의 인용을 실제 코드와 대조하고, 오류마다 힌트를 붙인다 (wave08 Validator)."""
    guide = state["guide"]
    refs = guide.entrypoints + [step.ref for step in guide.reading_order]
    errors = []
    for ref in refs:  # 오류와 인용을 짝지으려고 하나씩 검사한다
        for error in validate_citations([ref], deps.index.db, deps.index.info.path):
            errors.append(f"{error} → {_hint(error, ref, deps)}")
    return {"validation_errors": errors}


_validate = validate  # 빌더의 매개변수 validate 가 이 함수 이름을 가리므로 별칭을 둔다


def after_validate(state: GuideState) -> str:
    """오류가 있고 상한 전이면 피드백을 넣어 다시 합성, 아니면 끝."""
    if state.get("validation_errors") and state.get("retry_count", 0) < MAX_RETRIES:
        return "synthesize"
    return END


def build_reading_guide_graph(
    deps: Deps, validate: bool = True, prompts: ModuleType = reading_guide_v1
):
    """validate=False 면 검증 루프 없는 naive 버전 (실험 E4 베이스라인용).

    prompts 는 SUMMARIZE_MODULE·SYNTHESIZE_GUIDE 를 가진 프롬프트 모듈 (기본 v1: E4 재현용).
    """
    builder = StateGraph(GuideState)
    builder.add_node("select_targets", partial(select_targets, deps=deps))
    builder.add_node("summarize_module", partial(summarize_module, deps=deps, prompts=prompts))
    builder.add_node("synthesize", partial(synthesize, deps=deps, prompts=prompts))
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
    # 한 번에 끝나는 파이프라인이라 체크포인트가 필요 없다. False 로 두지 않으면 체크포인터를 단
    # Supervisor 노드 안에서 부를 때 부모 것을 물려받아 실행마다 "run_map:<task id>" 칸에
    # 요약(ModuleSummary)까지 쌓는다 (직접 확인).
    return builder.compile(checkpointer=False)


def run_reading_guide(
    deps: Deps, validate: bool = True, prompts: ModuleType = reading_guide_v1
) -> GuideState:
    """그래프를 만들어 한 번 실행하고 최종 상태를 돌려준다."""
    graph = build_reading_guide_graph(deps, validate=validate, prompts=prompts)
    return graph.invoke({"retry_count": 0, "validation_errors": []})
