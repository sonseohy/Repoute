"""PR Coach 그래프: MCP 로 PR 가져오기 → 변경 심볼 → 영향 범위(호출 관계) → 테스트 힌트 → 설명.

변경 심볼·영향 범위·테스트 파일·인용은 모두 정적 분석(인덱스·파일 읽기)이다.
LLM(fast 1회)은 설명 문장만 쓰고, 체크포인트의 인용은 LLM 이 고른 심볼 이름으로 코드가 붙인다.
Issue Matcher 와 같은 원칙: 허용 범위는 지시가 아니라 입력/도구 층으로 강제한다.
"""

import re
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from types import ModuleType
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel

from repoute.github.mcp import call_tool_sync
from repoute.github.prs import (
    ChangedFile,
    Hunk,
    PullRequest,
    context_symbol_name,
    parse_pull_request,
)
from repoute.indexing.explain import enclosing_symbol
from repoute.indexing.index import RepoIndex
from repoute.indexing.parser import SKIP_DIRS
from repoute.indexing.symbols import SymbolDB
from repoute.prompts import pr_coach_v1
from repoute.validation.citations import CodeRef

MAX_CHANGED = 20  # 변경 심볼 상한
MAX_CALLERS = 5  # 변경 심볼마다 호출자 수
# 피호출자는 기본으로 넣지 않는다: gold 영향 심볼은 직접 호출자 중심이고(기록 26),
# 피호출자는 코드 뷰어에서 변경 심볼을 열면 보인다. find_impact(callees_per_symbol=n) 로 켠다.
MAX_CALLEES = 0  # 변경 심볼마다 피호출자 수 (기본)
DIFF_CONTEXT = 3  # unified diff 가 hunk 앞뒤에 붙이는 문맥 줄 수
MAX_IMPACT = 30  # 영향 심볼 전체 상한
MAX_TEST_FILES = 5  # 테스트 힌트 파일 수
MAX_SCAN_FILES = 500  # 테스트 파일을 훑을 상한 (큰 레포에서 시간 제한)
MAX_CHECK_POINTS = 5
PROMPT_BODY = 500  # LLM 입력에 넣을 PR 본문 글자 수
TEST_DIRS = {"tests", "test"}
NO_SYMBOL_SUMMARY = (
    "인덱스된 Python 심볼 변경이 없습니다 (문서·설정·테스트만 바뀌었거나, "
    "바뀐 파일이 인덱스 커밋에 없습니다)."
)


@dataclass
class PRDeps:
    """PR Coach 노드가 쓰는 의존성. 테스트에서는 가짜 LLM·도구를 꽂는다."""

    fast_llm: Any
    index: RepoIndex  # db·graph·info.path 만 쓴다
    pr_tools: dict[str, Any]  # {"pull_request_read": tool} — MCP 도구(또는 테스트용 가짜)


class SymbolRef(BaseModel):
    """심볼 하나의 위치. line 은 정의 시작 줄(def/class 줄)."""

    name: str
    file: str
    line: int


class ImpactRef(SymbolRef):
    """변경 심볼 때문에 영향을 받는 심볼. via = 어느 변경 심볼 때문인지."""

    relation: Literal["caller", "callee"]
    via: str


class CheckPoint(BaseModel):
    """리뷰어가 확인할 것 하나. text 는 LLM, refs 는 코드가 붙인 인용."""

    text: str
    refs: list[CodeRef]


class PRReview(BaseModel):
    """그래프 최종 결과."""

    repo: str
    number: int
    title: str
    url: str
    summary: str
    changed_symbols: list[SymbolRef]
    impact: list[ImpactRef]
    test_files: list[str]
    check_points: list[CheckPoint]
    errors: list[str] = []  # 도구 호출 실패 등 (한 줄씩)
    notes: list[str] = []  # 오류는 아니지만 알릴 것 (인덱스 커밋에 없는 파일 등)


class CheckText(BaseModel):
    """LLM 이 쓰는 체크포인트 하나 (구조화 출력). 인용 칸은 일부러 없다."""

    text: str
    symbol: str


class Explained(BaseModel):
    """fast LLM 구조화 출력: 요약과 체크포인트."""

    summary: str
    check_points: list[CheckText]


class PRState(TypedDict, total=False):
    repo: str  # "owner/name"
    number: int
    pr: PullRequest | None
    changed: list[SymbolRef]
    impact: list[ImpactRef]
    test_files: list[str]
    summary: str
    check_points: list[CheckPoint]
    errors: list[str]
    notes: list[str]


def _one_line(error: Exception) -> str:
    """예외를 한 줄로 ("종류: 첫 줄")."""
    text = str(error).strip()
    return f"{type(error).__name__}: {text.splitlines()[0] if text else ''}"


def _tail(name: str) -> str:
    """끝 이름. "Session.send" → "send" (calls 테이블의 callee 는 끝 이름이다)."""
    return name.rsplit(".", 1)[-1]


# ---- 정적 단계 (LLM 없음). 평가 스크립트도 이 함수들을 그대로 쓴다 ----


def _first_changed_line(hunk: Hunk) -> int:
    """hunk 의 첫 변경 줄 추정. 앞 문맥 DIFF_CONTEXT 줄을 건너뛴 줄이 범위 안이면 그 줄.

    아니면(짧은 hunk) 시작 줄. hunk 시작 3줄은 바뀌지 않은 문맥 줄이라, 함수 첫머리를 바꾼
    hunk 의 시작 줄은 앞 심볼에 있다.
    """
    line = hunk.new_start + DIFF_CONTEXT
    return line if line <= hunk.new_end else hunk.new_start


def _context_symbol(db: SymbolDB, file: str, name: str, near: int) -> SymbolRef | None:
    """file 안에서 이름이 name 이거나 ".name" 으로 끝나는(메서드) 심볼. 없으면 None.

    여러 개면(같은 이름 메서드가 여러 클래스에) 정의 줄이 near(hunk 새 시작 줄)에 가장 가까운 것.
    """
    rows = db.conn.execute(
        "SELECT name, file, line_start FROM symbols WHERE file = ? AND (name = ? OR name LIKE ?)",
        (file, name, "%." + name),
    ).fetchall()
    rows = [r for r in rows if _tail(r["name"]) == name]  # LIKE 의 _ 는 한 글자 와일드카드
    if not rows:
        return None
    best = min(rows, key=lambda r: (abs(r["line_start"] - near), r["line_start"]))
    return SymbolRef(name=best["name"], file=best["file"], line=best["line_start"])


def find_changed_symbols(index: Any, files: list[ChangedFile]) -> tuple[list[SymbolRef], list[str]]:
    """.py 변경 파일의 hunk 마다 바뀐 심볼을 찾는다. 반환 (심볼, notes).

    (a) hunk 헤더 문맥(예 "def get_netrc_auth(...):")에 이름이 있으면 그 파일에서 그 이름의
        심볼을 찾는다. PR 커밋과 인덱스 커밋이 달라 줄이 어긋나도 이름은 맞기 때문이다.
    (b) 문맥 이름이 없거나 인덱스에서 못 찾으면, hunk 의 첫 변경 줄을 감싸는 심볼(enclosing_symbol).
    줄 번호는 PR 의 새 파일 기준, 심볼은 인덱스 커밋 기준이다. 인덱스에 없는 파일은 건너뛰고
    notes 에 남긴다. 어느 경로로 몇 hunk 를 잡았는지도 notes 에 한 줄. (name, file) 로 중복을
    빼고 MAX_CHANGED 개까지.
    """
    indexed = {data["file"] for _, data in index.graph.nodes(data=True)}
    root = Path(index.info.path)
    symbols: dict[tuple[str, str], SymbolRef] = {}
    notes = []
    by_context = by_line = 0
    for changed in files:
        if not changed.is_python:
            continue
        if changed.filename not in indexed:
            notes.append(f"인덱스 커밋에 없는 파일: {changed.filename}")
            continue
        for hunk in changed.hunks:
            name = context_symbol_name(hunk.context)
            ref = None
            if name is not None:
                ref = _context_symbol(index.db, changed.filename, name, hunk.new_start)
            if ref is not None:
                by_context += 1
            else:
                line = _first_changed_line(hunk)
                span = enclosing_symbol(index.db, root, changed.filename, line)
                if span is None:
                    continue
                ref = SymbolRef(name=span.name, file=span.file, line=span.line_start)
                by_line += 1
            symbols.setdefault((ref.name, ref.file), ref)
    if by_context or by_line:
        notes.append(f"hunk → 심볼: 문맥 이름으로 {by_context}개, 줄로 {by_line}개")
    return list(symbols.values())[:MAX_CHANGED], notes


def _definition(db: SymbolDB, name: str, file: str | None = None):
    """이름이 정확히 같은 심볼 정의. file 을 주면 그 파일에서만. 없으면 None."""
    for symbol in db.find_definition(name):
        if symbol.name == name and (file is None or symbol.file == file):
            return symbol
    return None


def _callee_definition(db: SymbolDB, callee: str, file: str):
    """피호출자 끝 이름의 정의. 같은 파일에 있으면 그것, 아니면 레포에 하나뿐일 때만.

    같은 끝 이름이 여러 파일에 있으면 어느 것인지 정할 수 없으므로 None (지어내지 않는다).
    """
    candidates = [s for s in db.find_definition(callee) if _tail(s.name) == callee]
    same_file = [s for s in candidates if s.file == file]
    if same_file:
        return same_file[0]
    return candidates[0] if len(candidates) == 1 else None


def find_impact(
    db: SymbolDB,
    changed: list[SymbolRef],
    callers_per_symbol: int = MAX_CALLERS,
    callees_per_symbol: int = MAX_CALLEES,
) -> list[ImpactRef]:
    """변경 심볼마다 호출자 callers_per_symbol 개·피호출자 callees_per_symbol 개 (기본 5·0).

    호출자: callee 끝 이름 = 변경 심볼 끝 이름인 호출의 caller. 줄은 호출 줄이 아니라
    호출자 심볼의 정의 줄(코드 뷰어가 심볼 전체를 열 수 있게). 모듈 최상위 호출은 뺀다.
    피호출자(기본 0 = 끔): 변경 심볼 안의 호출. 정의를 하나로 정할 수 있을 때만 남긴다.
    변경 심볼 자신과 이미 나온 (이름, 파일) 은 다시 넣지 않는다 — 여러 변경 심볼의 호출자면
    한 번만, via 는 처음 만난 변경 심볼. 전체 MAX_IMPACT 개.
    """
    seen = {(s.name, s.file) for s in changed}
    impact: list[ImpactRef] = []

    def add(symbol, relation: str, via: str) -> bool:
        key = (symbol.name, symbol.file)
        if key in seen:
            return False
        seen.add(key)
        ref = ImpactRef(
            name=symbol.name, file=symbol.file, line=symbol.line_start, relation=relation, via=via
        )
        impact.append(ref)
        return True

    for target in changed:
        callers = 0
        for call in db.find_callers(_tail(target.name)):
            if callers >= callers_per_symbol:
                break
            symbol = None if call.caller == "<module>" else _definition(db, call.caller, call.file)
            if symbol is not None and add(symbol, "caller", target.name):
                callers += 1
        callees = 0
        for call in db.find_callees(target.name):
            if callees >= callees_per_symbol:
                break
            if call.file != target.file:  # 같은 이름의 심볼이 다른 파일에도 있을 수 있다
                continue
            symbol = _callee_definition(db, call.callee, call.file)
            if symbol is not None and add(symbol, "callee", target.name):
                callees += 1
    return impact[:MAX_IMPACT]


def _is_test_file(rel: Path) -> bool:
    """tests/·test/ 아래이거나 test_*.py·*_test.py 인가."""
    return bool(TEST_DIRS.intersection(rel.parts[:-1])) or (
        rel.name.startswith("test_") or rel.name.endswith("_test.py")
    )


def find_test_files(root: Path, names: list[str]) -> list[str]:
    """변경 심볼 끝 이름이 텍스트로(단어 단위) 등장하는 테스트 파일 상위 MAX_TEST_FILES 개.

    인덱스는 SKIP_DIRS 로 tests 를 빼므로 파일 시스템을 직접 읽는다 (최대 MAX_SCAN_FILES 개).
    등장 횟수가 많은 순, 같으면 경로 순. 한 번도 안 나온 파일은 뺀다.
    """
    tails = sorted({_tail(n) for n in names})
    if not tails:
        return []
    pattern = re.compile(r"\b(?:" + "|".join(re.escape(t) for t in tails) + r")\b")
    skip = SKIP_DIRS - TEST_DIRS
    counts: list[tuple[int, str]] = []
    scanned = 0
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if skip.intersection(rel.parts[:-1]) or not _is_test_file(rel):
            continue
        scanned += 1
        if scanned > MAX_SCAN_FILES:
            break
        n = len(pattern.findall(path.read_text(encoding="utf-8", errors="replace")))
        if n:
            counts.append((-n, rel.as_posix()))
    return [file for _, file in sorted(counts)[:MAX_TEST_FILES]]


def attach_refs(
    picks: list[CheckText], changed: list[SymbolRef], impact: list[ImpactRef]
) -> list[CheckPoint]:
    """LLM 이 고른 심볼 이름으로 인용을 붙인다. 목록에 없는 이름이면 refs 는 빈 목록.

    전체 이름이 같으면 그 심볼, 아니면 끝 이름이 같은 심볼이 하나뿐일 때만 그것.
    """
    known = changed + impact
    by_name = {s.name: s for s in reversed(known)}  # 같은 이름이면 앞(변경 심볼)을 남긴다
    checks = []
    for pick in picks[:MAX_CHECK_POINTS]:
        symbol = by_name.get(pick.symbol)
        if symbol is None:
            same_tail = {s.name: s for s in known if _tail(s.name) == _tail(pick.symbol)}
            symbol = next(iter(same_tail.values())) if len(same_tail) == 1 else None
        refs = (
            []
            if symbol is None
            else [CodeRef(file=symbol.file, line=symbol.line, symbol=symbol.name)]
        )
        checks.append(CheckPoint(text=pick.text, refs=refs))
    return checks


# ---- 노드 ----


def fetch_pr(state: PRState, deps: PRDeps) -> dict:
    """MCP pull_request_read 를 method="get"·"get_files" 로 두 번 불러 PullRequest 를 만든다."""
    owner, _, name = state["repo"].partition("/")
    tool = deps.pr_tools.get("pull_request_read")
    if tool is None:
        return {"pr": None, "errors": ["pull_request_read 도구가 없음"]}
    args = {"owner": owner, "repo": name, "pullNumber": state["number"]}
    try:
        raw_get = call_tool_sync(tool, {**args, "method": "get"})
        if not raw_get.lstrip().startswith(("{", "[")):
            # 서버가 JSON 대신 오류 문장을 돌려준다 (예: "failed to get pull request: … 404")
            return {"pr": None, "errors": [f"pull_request_read 실패 — {raw_get.strip()[:200]}"]}
        raw_files = call_tool_sync(tool, {**args, "method": "get_files"})
        pr = parse_pull_request(raw_get, raw_files, repo=state["repo"])
    except Exception as e:  # 네트워크·인증·결과 형식 오류는 한 줄로 남기고 빈 값으로 끝낸다
        return {"pr": None, "errors": [f"pull_request_read 실패 — {_one_line(e)}"]}
    return {"pr": pr}


def changed_symbols(state: PRState, deps: PRDeps) -> dict:
    """변경 파일의 hunk → 변경 심볼 (LLM 없음)."""
    pr = state.get("pr")
    if pr is None:
        return {"changed": [], "notes": []}
    symbols, notes = find_changed_symbols(deps.index, pr.changed_files)
    return {"changed": symbols, "notes": notes}


def impact(state: PRState, deps: PRDeps) -> dict:
    """변경 심볼의 호출자 (LLM 없음). 피호출자는 기본값(0)이라 넣지 않는다."""
    return {"impact": find_impact(deps.index.db, state.get("changed", []))}


def suggest_tests(state: PRState, deps: PRDeps) -> dict:
    """노드 test_hints: 변경 심볼 이름이 등장하는 테스트 파일 (LLM 없음).

    함수 이름이 test_ 로 시작하면 테스트에서 import 할 때 pytest 가 테스트로 모으므로 이름을 바꿨다.
    """
    names = [s.name for s in state.get("changed", [])]
    return {"test_files": find_test_files(Path(deps.index.info.path), names)}


def _lines(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items) or "(없음)"


def explain(state: PRState, deps: PRDeps, prompts: ModuleType = pr_coach_v1) -> dict:
    """fast LLM 1회로 요약·체크포인트 문장을 받는다. 변경 심볼이 없으면 LLM 을 부르지 않는다."""
    pr, changed = state.get("pr"), state.get("changed", [])
    if pr is None:
        return {"summary": "", "check_points": []}
    if not changed:
        return {"summary": NO_SYMBOL_SUMMARY, "check_points": []}
    impacts = state.get("impact", [])
    prompt = prompts.EXPLAIN.format(
        repo=state["repo"],
        number=pr.number,
        title=pr.title,
        body=" ".join(pr.body[:PROMPT_BODY].split()) or "(본문 없음)",
        files=_lines(
            [f"{f.filename} ({f.status}, +{f.additions}/-{f.deletions})" for f in pr.changed_files]
        ),
        changed=_lines([f"{s.name} ({s.file})" for s in changed]),
        impact=_lines([f"{s.name} ({s.relation} of {s.via}, {s.file})" for s in impacts]),
        tests=_lines(state.get("test_files", [])),
    )
    result = deps.fast_llm.with_structured_output(Explained).invoke(prompt)
    return {
        "summary": result.summary,
        "check_points": attach_refs(result.check_points, changed, impacts),
    }


def build_pr_coach_graph(deps: PRDeps, prompts: ModuleType = pr_coach_v1) -> CompiledStateGraph:
    """fetch_pr → changed_symbols → impact → test_hints → explain → END."""
    builder = StateGraph(PRState)
    builder.add_node("fetch_pr", partial(fetch_pr, deps=deps))
    builder.add_node("changed_symbols", partial(changed_symbols, deps=deps))
    builder.add_node("impact", partial(impact, deps=deps))
    builder.add_node("test_hints", partial(suggest_tests, deps=deps))
    builder.add_node("explain", partial(explain, deps=deps, prompts=prompts))
    builder.add_edge(START, "fetch_pr")
    builder.add_edge("fetch_pr", "changed_symbols")
    builder.add_edge("changed_symbols", "impact")
    builder.add_edge("impact", "test_hints")
    builder.add_edge("test_hints", "explain")
    builder.add_edge("explain", END)
    # issue_matcher 와 같은 이유: 부모 그래프의 체크포인터를 물려받지 않게
    return builder.compile(checkpointer=False)


def run_pr_coach(
    deps: PRDeps, repo_full_name: str, number: int, prompts: ModuleType | None = None
) -> PRReview:
    """그래프를 한 번 실행해 PRReview 로 돌려준다. repo_full_name 은 "owner/name"."""
    graph = build_pr_coach_graph(deps, prompts or pr_coach_v1)
    state = graph.invoke({"repo": repo_full_name, "number": number, "errors": []})
    pr = state.get("pr")
    return PRReview(
        repo=repo_full_name,
        number=number,
        title=pr.title if pr else "",
        url=pr.url if pr else f"https://github.com/{repo_full_name}/pull/{number}",
        summary=state.get("summary", ""),
        changed_symbols=state.get("changed", []),
        impact=state.get("impact", []),
        test_files=state.get("test_files", []),
        check_points=state.get("check_points", []),
        errors=state.get("errors", []),
        notes=state.get("notes", []),
    )
