"""Issue Matcher 그래프 테스트. 가짜 LLM·임베더·MCP 도구만 쓴다 (네트워크·API 호출 없음).

실행: uv run pytest -v tests/test_issue_matcher.py
"""

import asyncio
import json
import subprocess

import pytest
from langchain_core.tools import BaseTool

from repoute.graphs.issue_matcher import (
    IssueDeps,
    IssueMatches,
    Profile,
    RankedPick,
    Ranking,
    run_issue_matcher,
)
from repoute.indexing.embeddings import SymbolEmbeddingStore
from repoute.indexing.index import build_index
from repoute.validation.citations import validate_citations

# 두 모듈이 서로 다른 단어를 써서, 가짜 임베더로도 이슈마다 다른 심볼이 1위가 된다
FILES = {
    "app/__init__.py": "",
    "app/retry.py": (
        "def retry_request(request, timeout):\n"
        '    """Retry a failed request with backoff until timeout."""\n'
        "    return request\n"
    ),
    "app/render.py": (
        "def render_template(template, context):\n"
        '    """Render html template with escaping."""\n'
        "    return template\n"
    ),
}
LONG = "Steps to reproduce are written here in detail so a newcomer can start working. " * 2
RENDER_BODY = "render html template escaping " * 12  # 360자: 80자 이상, LLM 입력에선 300자로 잘림

ISSUES = [
    {
        "number": 1,
        "title": "Retry backoff ignores timeout",
        "body": "retry request backoff timeout",
        "labels": [{"name": "good first issue"}],
        "html_url": "https://github.com/o/r/issues/1",
        "comments": 1,
        "created_at": "2026-10-01T00:00:00Z",
    },
    {
        "number": 2,
        "title": "Render template escaping is wrong",
        "body": RENDER_BODY,
        "labels": [],
        "html_url": "https://github.com/o/r/issues/2",
        "comments": 0,
        "created_at": "2026-10-02T00:00:00Z",
    },
    {  # PR 은 후보에서 빠진다
        "number": 3,
        "title": "Fix retry",
        "body": LONG,
        "labels": [{"name": "good first issue"}],
        "html_url": "https://github.com/o/r/pull/3",
        "comments": 0,
        "created_at": "2026-10-03T00:00:00Z",
        "pull_request": {},
    },
    {  # 입문 라벨 없음 + 본문 짧음 → 필터에서 빠진다
        "number": 4,
        "title": "Crash",
        "body": "crash",
        "labels": [],
        "html_url": "https://github.com/o/r/issues/4",
        "comments": 0,
        "created_at": "2026-10-04T00:00:00Z",
    },
]


class FakeListIssues(BaseTool):
    """MCP list_issues 를 흉내 내는 비동기 전용 도구. JSON 문자열을 돌려주거나 예외를 던진다."""

    name: str = "list_issues"
    description: str = "List issues"
    payload: str = "[]"
    error: str = ""
    calls: list = []

    def _run(self, **kwargs):
        raise NotImplementedError("MCP 도구는 동기 실행을 지원하지 않는다")

    async def _arun(self, **kwargs):
        await asyncio.sleep(0)
        self.calls.append(kwargs)
        if self.error:
            raise RuntimeError(self.error)
        return self.payload


class FakeLLM:
    """with_structured_output(schema).invoke(prompt) 가 큐의 답을 순서대로 돌려준다."""

    def __init__(self, answers: list | None = None):
        self.answers = list(answers or [])
        self.schemas, self.prompts = [], []

    def with_structured_output(self, schema):
        self.schemas.append(schema)
        return self

    def invoke(self, prompt):
        self.prompts.append(prompt)
        return self.answers.pop(0)


def _make_origin(tmp_path):
    """tmp_path/owner/name 에 FILES 를 커밋한 로컬 git 레포 (test_reading_guide 방식)."""
    origin = tmp_path / "owner" / "name"
    for rel, text in FILES.items():
        (origin / rel).parent.mkdir(parents=True, exist_ok=True)
        (origin / rel).write_text(text)
    git = ["git", "-C", str(origin)]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    commit = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
    subprocess.run([*git, *commit, "commit", "-q", "-m", "init"], check=True)
    return origin


@pytest.fixture
def index(tmp_path):
    """retry·render 두 모듈짜리 레포의 인덱스."""
    repo = build_index(f"file://{_make_origin(tmp_path)}", cache_dir=tmp_path / "cache")
    yield repo
    repo.db.close()


@pytest.fixture
def store(tmp_path, index, fake_embedder):
    """가짜 임베더로 만든 심볼 임베딩 저장소."""
    store = SymbolEmbeddingStore(tmp_path / "emb.sqlite")
    store.build(index.db, index.info.path, fake_embedder, "fake")
    return store


def _deps(index, store, embedder, llm, tool) -> IssueDeps:
    return IssueDeps(llm, index, embedder, store, {"list_issues": tool})


def test_no_candidates_skips_llm(index, store, fake_embedder):
    """필터 뒤 후보가 0개면 LLM 을 부르지 않고 빈 matches, considered=0 이다."""
    llm = FakeLLM()
    tool = FakeListIssues(payload=json.dumps(ISSUES[2:]), calls=[])  # PR + 짧은 이슈뿐
    result = run_issue_matcher(_deps(index, store, fake_embedder, llm, tool), "o/r", Profile())
    assert isinstance(result, IssueMatches)
    assert (result.matches, result.considered, result.errors) == ([], 0, [])
    assert llm.prompts == []
    assert tool.calls == [{"owner": "o", "repo": "r", "state": "OPEN", "perPage": 30}]


def test_two_candidates_one_llm_call_refs_from_embeddings(index, store, fake_embedder):
    """후보 2개 → LLM 1회. refs·modules 는 임베딩 검색 결과(실제 파일·줄)이고 LLM 이 쓴 것은 버린다.

    LLM 답에는 후보에 없는 번호(99), 중복 번호, 지어낸 refs·modules 를 섞어 둔다.
    """
    invented = {"refs": [{"file": "nope.py", "line": 999}], "modules": ["made.up"]}
    picks = [
        RankedPick.model_validate({"number": 2, "why": "템플릿 관심사", "difficulty": "easy"}),
        RankedPick(number=99, why="없는 이슈", difficulty="hard"),
        RankedPick.model_validate(
            {"number": 1, "why": "재시도 로직", "difficulty": "medium", **invented}
        ),
        RankedPick(number=2, why="중복", difficulty="hard"),
    ]
    llm = FakeLLM([Ranking(picks=picks)])
    tool = FakeListIssues(payload=json.dumps(ISSUES), calls=[])
    profile = Profile(interests="templates", level="beginner")
    result = run_issue_matcher(_deps(index, store, fake_embedder, llm, tool), "o/r", profile, k=5)

    assert llm.schemas == [Ranking] and len(llm.prompts) == 1
    assert result.considered == 2 and result.errors == []
    assert [m.number for m in result.matches] == [2, 1]  # 99·중복 제거, LLM 순서 유지
    render, retry = result.matches
    assert (render.why, render.difficulty, render.url) == (
        "템플릿 관심사",
        "easy",
        "https://github.com/o/r/issues/2",
    )
    # 임베딩 1위 심볼이 이슈 내용과 맞고, 모듈은 import 그래프 이름이다
    assert (render.refs[0].file, render.refs[0].symbol) == ("app/render.py", "render_template")
    assert (retry.refs[0].file, retry.refs[0].symbol) == ("app/retry.py", "retry_request")
    assert retry.modules[0] == "app.retry" and "made.up" not in retry.modules
    assert all(ref.file != "nope.py" for ref in retry.refs)
    # 모든 인용이 실제 파일·줄·심볼과 맞는다
    refs = [ref for m in result.matches for ref in m.refs]
    assert refs and validate_citations(refs, index.db, index.info.path) == []
    # LLM 입력에는 PR·필터 탈락 이슈가 없고, 본문은 300자 이하로 잘려 들어간다
    prompt = llm.prompts[0]
    assert "#1 " in prompt and "#2 " in prompt and "#3 " not in prompt and "#4 " not in prompt
    assert "app.render" in prompt and RENDER_BODY.strip() not in prompt


def test_k_limits_matches(index, store, fake_embedder):
    """LLM 이 k 보다 많이 골라도 결과는 상위 k 개다."""
    picks = [
        RankedPick(number=1, why="a", difficulty="easy"),
        RankedPick(number=2, why="b", difficulty="easy"),
    ]
    llm = FakeLLM([Ranking(picks=picks)])
    tool = FakeListIssues(payload=json.dumps(ISSUES), calls=[])
    result = run_issue_matcher(_deps(index, store, fake_embedder, llm, tool), "o/r", Profile(), k=1)
    assert [m.number for m in result.matches] == [1]


def test_tool_failure_records_one_error(index, store, fake_embedder):
    """MCP 도구가 실패하면 errors 에 한 줄이 남고, LLM 없이 빈 matches 로 끝난다."""
    llm = FakeLLM()
    tool = FakeListIssues(error="401 Bad credentials\n자세한 내용", calls=[])
    result = run_issue_matcher(_deps(index, store, fake_embedder, llm, tool), "o/r", Profile())
    assert result.matches == [] and result.considered == 0
    assert len(result.errors) == 1 and "\n" not in result.errors[0]
    assert "list_issues" in result.errors[0] and "401" in result.errors[0]
    assert llm.prompts == []


def test_without_embedding_store_ranks_without_refs(index, caplog):
    """embedding_store 가 None 이면 경고를 남기고, modules·refs 없이 순위만 매긴다."""
    llm = FakeLLM([Ranking(picks=[RankedPick(number=1, why="a", difficulty="easy")])])
    tool = FakeListIssues(payload=json.dumps(ISSUES), calls=[])
    deps = IssueDeps(llm, index, None, None, {"list_issues": tool})
    with caplog.at_level("WARNING"):
        result = run_issue_matcher(deps, "o/r", Profile())
    assert [(m.number, m.modules, m.refs) for m in result.matches] == [(1, [], [])]
    assert "embedding_store" in caplog.text
