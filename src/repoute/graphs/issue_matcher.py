"""Issue Matcher 그래프: MCP 이슈 가져오기 → 심볼 임베딩 매칭(RAG 2단계) → fast LLM 순위.

인용(refs)·모듈은 LLM 이 쓰지 않는다. 임베딩 검색 결과(정적)를 코드가 그대로 붙인다.
허용 범위는 지시가 아니라 입력/도구 층으로 강제해야 지켜진다는 교훈을 따른 구조다.
"""

import logging
from dataclasses import dataclass
from functools import partial
from types import ModuleType
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel

from repoute.github.issues import Issue, good_first_filter, parse_issues
from repoute.github.mcp import call_tool_sync
from repoute.indexing.index import RepoIndex
from repoute.prompts import issue_matcher_v1
from repoute.validation.citations import CodeRef

logger = logging.getLogger(__name__)

PER_PAGE = 30  # list_issues 한 번에 받을 이슈 수
ISSUE_STATE = "OPEN"  # github-mcp-server list_issues 의 state 값 (GraphQL 열거형은 대문자)
MAX_CANDIDATES = 10  # 임베딩 매칭·LLM 에 넘길 후보 상한 (임베딩 쿼리도 이 수만큼)
SEARCH_K = 5  # 이슈마다 찾을 심볼 수
MAX_MODULES = 3  # 이슈마다 붙일 모듈 수
MAX_REFS = 3  # 이슈마다 붙일 인용 수
QUERY_BODY = 500  # 임베딩 쿼리에 넣을 본문 글자 수
PROMPT_BODY = 300  # LLM 입력에 넣을 본문 글자 수


@dataclass
class IssueDeps:
    """Issue Matcher 노드가 쓰는 의존성. 테스트에서는 가짜 LLM·임베더·도구를 꽂는다."""

    fast_llm: Any
    index: RepoIndex
    embedder: Any  # embed_query 를 가진 임베딩 모델
    embedding_store: Any  # SymbolEmbeddingStore (search(vec, k))
    issue_tools: dict[str, Any]  # {"list_issues": tool, ...} — MCP 도구(또는 테스트용 가짜)


class Profile(BaseModel):
    """기여자 프로필."""

    interests: str = ""
    level: Literal["beginner", "intermediate", "advanced"] = "beginner"


class IssueMatch(BaseModel):
    """추천 이슈 하나. why·difficulty 는 LLM, modules·refs 는 임베딩 검색 결과."""

    number: int
    title: str
    url: str
    labels: list[str]
    why: str
    modules: list[str]
    refs: list[CodeRef]
    difficulty: Literal["easy", "medium", "hard"]


class IssueMatches(BaseModel):
    """그래프 최종 결과. considered = 입문용 필터 뒤 후보 수."""

    repo: str
    profile: Profile
    matches: list[IssueMatch]
    considered: int
    errors: list[str] = []  # 도구 호출 실패 등 (한 줄씩)


class RankedPick(BaseModel):
    """LLM 이 고른 이슈 하나 (구조화 출력). 인용 칸은 일부러 없다."""

    number: int
    why: str
    difficulty: Literal["easy", "medium", "hard"]


class Ranking(BaseModel):
    """fast LLM 구조화 출력: 고른 이슈를 잘 맞는 순서대로."""

    picks: list[RankedPick]


class IssueState(TypedDict, total=False):
    repo: str  # "owner/name"
    profile: Profile
    k: int
    candidates: list[Issue]  # 입문용 필터를 통과한 이슈
    matched: dict[int, dict]  # 이슈 번호 → {"modules": [...], "refs": [CodeRef, ...]}
    matches: list[IssueMatch]
    errors: list[str]


def _one_line(error: Exception) -> str:
    """예외를 한 줄로 ("종류: 첫 줄")."""
    text = str(error).strip()
    return f"{type(error).__name__}: {text.splitlines()[0] if text else ''}"


def fetch_issues(state: IssueState, deps: IssueDeps) -> dict:
    """MCP list_issues 로 열린 이슈를 받아 PR 을 빼고 입문용 후보만 남긴다."""
    owner, _, name = state["repo"].partition("/")
    tool = deps.issue_tools.get("list_issues")
    if tool is None:
        return {"candidates": [], "errors": ["list_issues 도구가 없음"]}
    args = {"owner": owner, "repo": name, "state": ISSUE_STATE, "perPage": PER_PAGE}
    try:
        issues = parse_issues(call_tool_sync(tool, args), repo=state["repo"])
    except Exception as e:  # 네트워크·인증·결과 형식 오류는 한 줄로 남기고 빈 후보로 끝낸다
        return {"candidates": [], "errors": [f"list_issues 실패 — {_one_line(e)}"]}
    return {"candidates": good_first_filter(issues)}


def _module_names(deps: IssueDeps, files: list[str]) -> list[str]:
    """파일 경로 → 모듈 이름 (import 그래프 노드 역조회, 없으면 경로를 점 표기로). 중복 제거."""
    by_file = {data["file"]: module for module, data in deps.index.graph.nodes(data=True)}
    names = [by_file.get(f) or f.removesuffix(".py").replace("/", ".") for f in files]
    return list(dict.fromkeys(names))


def match_symbols(state: IssueState, deps: IssueDeps) -> dict:
    """후보 이슈마다 제목+본문 앞부분으로 심볼을 검색해 관련 모듈·인용을 붙인다 (LLM 없음)."""
    candidates = state.get("candidates", [])[:MAX_CANDIDATES]
    if deps.embedding_store is None or deps.embedder is None:
        if candidates:
            logger.warning("embedding_store 가 없어 모듈 매칭 없이 순위만 매긴다")
        return {"matched": {i.number: {"modules": [], "refs": []} for i in candidates}}
    matched = {}
    for issue in candidates:
        vector = deps.embedder.embed_query(issue.title + "\n" + issue.body[:QUERY_BODY])
        hits = deps.embedding_store.search(vector, k=SEARCH_K)
        refs = [
            CodeRef(file=h["file"], line=h["line_start"], symbol=h["name"]) for h in hits[:MAX_REFS]
        ]
        modules = _module_names(deps, [h["file"] for h in hits])[:MAX_MODULES]
        matched[issue.number] = {"modules": modules, "refs": refs}
    return {"matched": matched}


def _candidate_text(issue: Issue, modules: list[str]) -> str:
    """LLM 에 넣는 이슈 한 개: 번호·제목·라벨·본문 300자·매칭 모듈만."""
    body = " ".join(issue.body[:PROMPT_BODY].split()) or "(본문 없음)"
    labels = ", ".join(issue.labels) or "없음"
    return (
        f"- #{issue.number} {issue.title}\n"
        f"  라벨: {labels}\n"
        f"  본문: {body}\n"
        f"  관련 모듈: {', '.join(modules) or '없음'}"
    )


def rank(state: IssueState, deps: IssueDeps, prompts: ModuleType = issue_matcher_v1) -> dict:
    """fast LLM 1회로 상위 k개를 고르고 이유·난이도를 받는다. 후보가 없으면 LLM 을 부르지 않는다.

    LLM 이 후보에 없는 번호를 내거나 같은 번호를 두 번 내면 버린다.
    modules·refs 는 match_symbols 결과를 그대로 붙인다.
    """
    matched = state.get("matched", {})
    candidates = [i for i in state.get("candidates", []) if i.number in matched]
    if not candidates:
        return {"matches": []}
    profile, k = state["profile"], state.get("k", 5)
    prompt = prompts.RANK.format(
        repo=state["repo"],
        k=k,
        interests=profile.interests or "(지정 안 함)",
        level=profile.level,
        candidates="\n".join(_candidate_text(i, matched[i.number]["modules"]) for i in candidates),
    )
    ranking = deps.fast_llm.with_structured_output(Ranking).invoke(prompt)
    by_number = {i.number: i for i in candidates}
    matches, seen = [], set()
    for pick in ranking.picks:
        issue = by_number.get(pick.number)
        if issue is None or pick.number in seen:
            continue
        seen.add(pick.number)
        found = matched[pick.number]
        matches.append(
            IssueMatch(
                number=issue.number,
                title=issue.title,
                url=issue.url,
                labels=issue.labels,
                why=pick.why,
                modules=found["modules"],
                refs=found["refs"],
                difficulty=pick.difficulty,
            )
        )
    return {"matches": matches[:k]}


def build_issue_matcher_graph(
    deps: IssueDeps, prompts: ModuleType = issue_matcher_v1
) -> CompiledStateGraph:
    """fetch_issues → match_symbols → rank → END. 한 번에 끝나므로 체크포인트를 쓰지 않는다."""
    builder = StateGraph(IssueState)
    builder.add_node("fetch_issues", partial(fetch_issues, deps=deps))
    builder.add_node("match_symbols", partial(match_symbols, deps=deps))
    builder.add_node("rank", partial(rank, deps=deps, prompts=prompts))
    builder.add_edge(START, "fetch_issues")
    builder.add_edge("fetch_issues", "match_symbols")
    builder.add_edge("match_symbols", "rank")
    builder.add_edge("rank", END)
    # reading_guide 와 같은 이유: Supervisor 노드 안에서 불려도 부모 체크포인터를 물려받지 않게
    return builder.compile(checkpointer=False)


def run_issue_matcher(
    deps: IssueDeps,
    repo_full_name: str,
    profile: Profile,
    prompts: ModuleType | None = None,
    k: int = 5,
) -> IssueMatches:
    """그래프를 한 번 실행해 IssueMatches 로 돌려준다. repo_full_name 은 "owner/name"."""
    graph = build_issue_matcher_graph(deps, prompts or issue_matcher_v1)
    state = graph.invoke({"repo": repo_full_name, "profile": profile, "k": k, "errors": []})
    return IssueMatches(
        repo=repo_full_name,
        profile=profile,
        matches=state.get("matches", []),
        considered=len(state.get("candidates", [])),
        errors=state.get("errors", []),
    )
