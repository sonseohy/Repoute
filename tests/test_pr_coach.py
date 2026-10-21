"""PR Coach 그래프 테스트. 가짜 LLM·가짜 MCP 도구·tmp git 레포만 쓴다 (네트워크·API 호출 없음).

실행: uv run pytest -v tests/test_pr_coach.py
"""

import asyncio
import json
import subprocess

import pytest
from langchain_core.tools import BaseTool

from repoute.github.prs import parse_changed_files
from repoute.graphs.pr_coach import (
    NO_SYMBOL_SUMMARY,
    CheckText,
    Explained,
    PRDeps,
    PRReview,
    SymbolRef,
    find_changed_symbols,
    find_impact,
    run_pr_coach,
)
from repoute.indexing.index import build_index
from repoute.validation.citations import validate_citations

FILES = {
    "app/__init__.py": "",
    "app/util.py": (
        "def _clamp(x):\n"  # 1
        "    return max(x, 0)\n"
        "\n"
        "\n"
        "def helper(x):\n"  # 5
        "    return _clamp(x) + 1\n"
    ),
    "app/main.py": (
        "from app.util import helper\n"
        "\n"
        "\n"
        "def run(x):\n"  # 4
        "    return helper(x)\n"
        "\n"
        "\n"
        "class Service:\n"  # 8
        "    def start(self):\n"  # 9
        "        return helper(1)\n"
    ),
    # PR 커밋과 인덱스 커밋의 줄 어긋남 재현용: PR 쪽에서는 target 이 3줄 근처였지만
    # 인덱스 소스에서는 before 가 길어져 15줄로 밀렸다
    "app/drift.py": (
        "def before(x):\n"  # 1
        + "    x = x + 1\n" * 10  # 2~11
        + "    return x\n"  # 12
        "\n"
        "\n"
        "def target(x):\n"  # 15
        "    return x * 2\n"
        "\n"
        "\n"
        "class A:\n"  # 19
        "    def run(self):\n"  # 20
        "        return 1\n"
        "\n"
        "\n"
        "class B:\n"  # 24
        "    def run(self):\n"  # 25
        "        return 2\n"
        "\n"
        "\n"
        "def both(x):\n"  # 29
        "    return before(x) + target(x)\n"
    ),
    # tests/ 는 인덱스(SKIP_DIRS)에서 빠지지만 테스트 힌트는 파일 시스템으로 찾는다
    "tests/test_x.py": (
        "from app.util import helper\n\n\ndef test_helper():\n    assert helper(1) == 2\n"
    ),
    "tests/test_other.py": "def test_nothing():\n    assert True\n",
}

PR_GET = {
    "number": 7,
    "title": "helper 가 음수를 0 으로 자르게",
    "body": "helper 반환값 수정 " * 100,
    "html_url": "https://github.com/owner/name/pull/7",
    "user": {"login": "alice"},
    "merged_at": None,
}
PY_FILE = {
    "filename": "app/util.py",
    "status": "modified",
    "additions": 1,
    "deletions": 1,
    "patch": "@@ -5,2 +5,2 @@\n def helper(x):\n-    return x + 1\n+    return _clamp(x) + 1\n",
}
DOC_FILE = {"filename": "README.md", "status": "modified", "patch": "@@ -1 +1 @@\n-a\n+b\n"}
NEW_FILE = {
    "filename": "app/new.py",
    "status": "added",
    "patch": "@@ -0,0 +1,2 @@\n+x = 1\n+y = 2\n",
}


def _blocks(data) -> list[dict]:
    """github-mcp-server 처럼 결과를 MCP 콘텐츠 블록 [{"type": "text", "text": JSON}] 으로."""
    return [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]


class FakePullRequestRead(BaseTool):
    """MCP pull_request_read 를 흉내 내는 비동기 전용 도구. method 별로 정한 결과를 돌려준다."""

    name: str = "pull_request_read"
    description: str = "Read a pull request"
    payloads: dict = {}  # method → 돌려줄 값
    error: str = ""
    calls: list = []

    def _run(self, **kwargs):
        raise NotImplementedError("MCP 도구는 동기 실행을 지원하지 않는다")

    async def _arun(self, **kwargs):
        await asyncio.sleep(0)
        self.calls.append(kwargs)
        if self.error:
            raise RuntimeError(self.error)
        return self.payloads[kwargs["method"]]


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
    """tmp_path/owner/name 에 FILES 를 커밋한 로컬 git 레포."""
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
    """util·main 두 모듈 + tests/ 두 파일짜리 레포의 인덱스."""
    repo = build_index(f"file://{_make_origin(tmp_path)}", cache_dir=tmp_path / "cache")
    yield repo
    repo.db.close()


def _tool(files: list[dict], error: str = "") -> FakePullRequestRead:
    return FakePullRequestRead(
        payloads={"get": _blocks(PR_GET), "get_files": _blocks(files)}, error=error, calls=[]
    )


def _explained(*picks: tuple[str, str]) -> Explained:
    return Explained(
        summary="helper 가 _clamp 를 거치게 바뀌었다. run 과 Service.start 가 이 값을 쓴다.",
        check_points=[CheckText(text=text, symbol=symbol) for text, symbol in picks],
    )


def test_static_changed_impact_and_tests(index):
    """변경 심볼·호출자·테스트 파일이 정적으로 맞게 나오고, LLM 은 1회만 불린다.

    호출자의 줄은 호출 줄이 아니라 호출자 심볼의 정의 줄이다. 그래프는 find_impact 기본값이라
    피호출자(_clamp)는 넣지 않는다. 비 .py 파일은 매핑하지 않고, 인덱스에 없는 파일은 errors 가
    아니라 notes 에 남는다. 문맥 없는 hunk 는 줄로 잡혔다는 요약도 notes 에 한 줄.
    """
    llm = FakeLLM([_explained(("helper 의 음수 처리 확인", "helper"))])
    tool = _tool([PY_FILE, DOC_FILE, NEW_FILE])
    result = run_pr_coach(PRDeps(llm, index, {"pull_request_read": tool}), "owner/name", 7)

    assert isinstance(result, PRReview)
    assert [c["method"] for c in tool.calls] == ["get", "get_files"]
    assert tool.calls[0] == {"owner": "owner", "repo": "name", "pullNumber": 7, "method": "get"}
    assert (result.title, result.url) == (PR_GET["title"], PR_GET["html_url"])
    assert [(s.name, s.file, s.line) for s in result.changed_symbols] == [
        ("helper", "app/util.py", 5)
    ]
    assert [(i.name, i.file, i.line, i.relation, i.via) for i in result.impact] == [
        ("run", "app/main.py", 4, "caller", "helper"),
        ("Service.start", "app/main.py", 9, "caller", "helper"),
    ]
    assert result.test_files == ["tests/test_x.py"]
    assert result.errors == [] and result.notes == [
        "인덱스 커밋에 없는 파일: app/new.py",
        "hunk → 심볼: 문맥 이름으로 0개, 줄로 1개",
    ]
    # LLM 입력: 영향 관계와 테스트 파일이 들어가고 본문은 500자 이하로 잘린다
    assert llm.schemas == [Explained] and len(llm.prompts) == 1
    prompt = llm.prompts[0]
    assert "Service.start (caller of helper" in prompt and "tests/test_x.py" in prompt
    assert PR_GET["body"].strip() not in prompt
    # 모든 인용(변경·영향·체크포인트)이 실제 파일·줄·심볼과 맞는다
    refs = [ref for c in result.check_points for ref in c.refs]
    assert refs and validate_citations(refs, index.db, index.info.path) == []


def test_unknown_symbol_gets_no_refs(index):
    """LLM 이 목록에 없는 심볼을 쓰면 refs 가 빈다.

    아는 이름이면(끝 이름 "start" 처럼 하나로 정해지는 경우 포함) 코드가 인용을 붙인다.
    """
    picks = [("지어낸 함수 확인", "made_up_fn"), ("start 확인", "start"), ("빈 심볼", "")]
    llm = FakeLLM([_explained(*picks)])
    result = run_pr_coach(PRDeps(llm, index, {"pull_request_read": _tool([PY_FILE])}), "o/n", 7)
    made_up, start, empty = result.check_points
    assert made_up.refs == [] and empty.refs == []
    assert [(r.file, r.line, r.symbol) for r in start.refs] == [("app/main.py", 9, "Service.start")]


def test_tool_failure_one_error_no_llm(index):
    """MCP 도구가 실패하면 errors 에 한 줄이 남고, LLM 없이 빈 결과로 끝난다."""
    llm = FakeLLM()
    tool = _tool([PY_FILE], error="401 Bad credentials\n자세한 내용")
    result = run_pr_coach(PRDeps(llm, index, {"pull_request_read": tool}), "owner/name", 7)
    assert len(result.errors) == 1 and "\n" not in result.errors[0]
    assert "pull_request_read" in result.errors[0] and "401" in result.errors[0]
    assert (result.changed_symbols, result.impact, result.test_files) == ([], [], [])
    assert result.url == "https://github.com/owner/name/pull/7"
    assert llm.prompts == []


def test_no_changed_symbols_skips_llm(index):
    """문서·인덱스에 없는 파일만 바뀌면 변경 심볼이 0개라 LLM 을 부르지 않고 고정 요약을 준다."""
    llm = FakeLLM()
    tool = _tool([DOC_FILE, NEW_FILE])
    result = run_pr_coach(PRDeps(llm, index, {"pull_request_read": tool}), "owner/name", 7)
    assert result.changed_symbols == [] and result.impact == []
    assert result.summary == NO_SYMBOL_SUMMARY and result.check_points == []
    assert result.errors == [] and len(result.notes) == 1
    assert llm.prompts == []


def test_server_error_text_is_surfaced(index, tmp_path):
    """서버가 JSON 대신 오류 문장을 돌려주면(없는 PR → "404 Not Found") 그 문장을 errors 에 남긴다.

    JSONDecodeError 가 아니라 서버 문장이 보여야 번호가 틀렸음을 알 수 있다. get_files 는 안 부른다.
    """
    text = (
        "failed to get pull request: GET https://api.github.com/repos/o/n/pulls/9: 404 Not Found []"
    )
    tool = FakePullRequestRead(payloads={"get": [{"type": "text", "text": text}]}, calls=[])
    llm = FakeLLM()
    result = run_pr_coach(PRDeps(llm, index, {"pull_request_read": tool}), "owner/name", 9)
    assert result.errors == [f"pull_request_read 실패 — {text}"]
    assert [c["method"] for c in tool.calls] == ["get"] and llm.prompts == []


def _drift_files(*hunks: str) -> list:
    """app/drift.py 를 바꾼 get_files 응답 하나 → ChangedFile 목록 (hunk 헤더만 다르게)."""
    patch = "".join(f"{header}\n ctx\n-old\n+new\n" for header in hunks)
    return parse_changed_files([{"filename": "app/drift.py", "status": "modified", "patch": patch}])


def test_context_name_beats_shifted_lines(index):
    """줄이 어긋난 패치: hunk 줄(3~6)은 인덱스에서 before 안이지만 문맥 이름으로 target 을 잡는다.

    같은 이름 메서드(A.run·B.run)가 여럿이면 hunk 새 시작 줄에 가장 가까운 것을 고른다.
    """
    files = _drift_files(
        "@@ -3,4 +3,4 @@ def target(x):",
        "@@ -24,3 +24,3 @@     def run(self):",  # B.run(25줄)이 A.run(20줄)보다 가깝다
        "@@ -18,3 +18,3 @@     def run(self):",  # A.run 이 더 가깝다
    )
    symbols, notes = find_changed_symbols(index, files)
    assert [(s.name, s.file, s.line) for s in symbols] == [
        ("target", "app/drift.py", 15),
        ("B.run", "app/drift.py", 25),
        ("A.run", "app/drift.py", 20),
    ]
    assert notes == ["hunk → 심볼: 문맥 이름으로 3개, 줄로 0개"]


def test_line_fallback_uses_first_changed_line(index):
    """문맥이 없거나(def/class 가 아니거나) 인덱스에 없는 이름이면 줄로 찾는다.

    줄은 hunk 시작이 아니라 앞 문맥 3줄을 건너뛴 첫 변경 줄(12+3=15 → target)이다.
    hunk 가 3줄보다 짧으면(12~13) 시작 줄 12 를 써서 before 가 잡힌다.
    """
    files = _drift_files(
        "@@ -12,6 +12,6 @@",  # 문맥 없음 → 15줄 → target
        "@@ -12,6 +12,6 @@ import os",  # 함수 문맥이 아님 → 줄
        "@@ -12,6 +12,6 @@ def gone(x):",  # 인덱스에 없는 이름 → 줄
        "@@ -12,2 +12,2 @@",  # 짧은 hunk → 12줄 → before
    )
    symbols, notes = find_changed_symbols(index, files)
    assert [(s.name, s.line) for s in symbols] == [("target", 15), ("before", 1)]
    assert notes == ["hunk → 심볼: 문맥 이름으로 0개, 줄로 4개"]


def test_impact_callees_off_by_default(index):
    """find_impact 는 기본으로 호출자만 넣고, callees_per_symbol 로 피호출자를 켤 수 있다.

    켜면 정의를 하나로 정할 수 있는 _clamp 만 들어가고 내장 max 는 빠진다(기존 동작 유지).
    """
    helper = [SymbolRef(name="helper", file="app/util.py", line=5)]
    default = find_impact(index.db, helper)
    assert [(i.name, i.relation) for i in default] == [
        ("run", "caller"),
        ("Service.start", "caller"),
    ]
    with_callees = find_impact(index.db, helper, callees_per_symbol=3)
    assert [(i.name, i.file, i.line, i.relation, i.via) for i in with_callees] == [
        ("run", "app/main.py", 4, "caller", "helper"),
        ("Service.start", "app/main.py", 9, "caller", "helper"),
        ("_clamp", "app/util.py", 1, "callee", "helper"),
    ]
    assert find_impact(index.db, helper, callers_per_symbol=1) == default[:1]


def test_shared_caller_listed_once(index):
    """여러 변경 심볼(before·target)의 호출자 both 는 한 번만, via 는 먼저 나온 변경 심볼."""
    changed = [
        SymbolRef(name="before", file="app/drift.py", line=1),
        SymbolRef(name="target", file="app/drift.py", line=15),
    ]
    impact = find_impact(index.db, changed)
    assert [(i.name, i.relation, i.via) for i in impact] == [("both", "caller", "before")]
