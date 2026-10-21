"""읽기 지도 그래프 테스트 (실제 LLM 없음). 실행: uv run pytest tests/test_reading_guide.py"""

import subprocess
import sys

import pytest

from repoute.graphs.deps import Deps
from repoute.graphs.reading_guide import (
    MAX_TARGETS,
    ModuleSummary,
    ReadingGuide,
    ReadingStep,
    _citable,
    run_reading_guide,
    select_targets,
)
from repoute.indexing.entrypoints import find_entrypoints
from repoute.indexing.imports import modules_of, rank_modules_v2
from repoute.indexing.index import build_index
from repoute.prompts import reading_guide_v2, reading_guide_v3
from repoute.validation.citations import CodeRef

# 원본 레포 파일: main 의 run() 이 util 의 helper() 를 부른다 (test_index 와 같은 구성)
FILES = {
    "app/__init__.py": "",
    "app/main.py": "from app.util import helper\n\n\ndef run():\n    return helper()\n",
    "app/util.py": "def helper():\n    return 1\n",
}


def _make_origin(tmp_path):
    """tmp_path/owner/name 에 FILES 를 커밋한 로컬 git 레포 (test_index._make_origin 방식)."""
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


class FakeStructuredLLM:
    """with_structured_output(schema).invoke(prompt) 가 미리 정한 답을 순서대로 돌려준다."""

    def __init__(self, answers: list):
        self.answers = list(answers)
        self.prompts = []

    def with_structured_output(self, schema):
        return self

    def invoke(self, prompt):
        self.prompts.append(prompt)
        return self.answers.pop(0)


@pytest.fixture
def index(tmp_path):
    """app/main.py 의 run() 이 app/util.py 의 helper() 를 부르는 레포의 인덱스."""
    repo = build_index(f"file://{_make_origin(tmp_path)}", cache_dir=tmp_path / "cache")
    yield repo
    repo.db.close()


def _summaries(n: int) -> list[ModuleSummary]:
    return [ModuleSummary(module="m", file="f.py", summary="요약", role="core")] * n


def _guide(main_line: int) -> ReadingGuide:
    """main_line 이 4 면 올바른 인용, 999 면 틀린 인용."""
    run = CodeRef(file="app/main.py", line=main_line, symbol="run")
    helper = CodeRef(file="app/util.py", line=1, symbol="helper")
    steps = [
        ReadingStep(order=1, file="app/util.py", reason="의존성 없음", ref=helper),
        ReadingStep(order=2, file="app/main.py", reason="진입점", ref=run),
    ]
    return ReadingGuide(overview="작은 앱", entrypoints=[run], reading_order=steps)


def _deps(index, n_summaries: int, guides: list) -> Deps:
    return Deps(FakeStructuredLLM(_summaries(n_summaries)), FakeStructuredLLM(guides), index)


def _target_count(index) -> int:
    return len(select_targets({}, Deps(None, None, index))["targets"])


def test_select_targets(index):
    """그래프 노드 3개(app, app.main, app.util) 중 2~3개를 중복 없이 고른다."""
    targets = select_targets({}, Deps(None, None, index))["targets"]
    assert 2 <= len(targets) <= 3
    assert len(set(targets)) == len(targets)
    assert set(targets) <= set(index.graph.nodes)
    assert "app.main" in targets  # 진입점 run() 이 있는 모듈


def test_select_targets_follows_rank_v2(index):
    """targets 앞부분은 진입점 시드 rank_modules_v2 순서 그대로이고, 진입점 모듈도 들어 있다."""
    graph = index.graph
    seeds = modules_of(graph, [s.file for s in find_entrypoints(index.db, index.info.path)])
    expected = [m for m, _ in rank_modules_v2(graph, seeds)[:MAX_TARGETS]]
    targets = select_targets({}, Deps(None, None, index))["targets"]
    assert targets[: len(expected)] == expected
    assert set(seeds) <= set(targets)
    assert "app.main" in seeds


def test_happy_path(index):
    """올바른 인용이면 재시도 없이 끝나고, 요약은 대상 수만큼 병합된다."""
    n = _target_count(index)
    deps = _deps(index, n, [_guide(4)])
    state = run_reading_guide(deps)
    assert len(state["summaries"]) == n
    assert state["validation_errors"] == []
    assert state["retry_count"] == 0
    assert state["guide"].reading_order
    assert len(deps.fast_llm.prompts) == n


def test_retry_with_feedback(index):
    """첫 답의 틀린 인용(999줄)은 피드백으로 전달되고, 둘째 답에서 통과한다."""
    deps = _deps(index, _target_count(index), [_guide(999), _guide(4)])
    state = run_reading_guide(deps)
    assert state["retry_count"] == 1
    assert state["validation_errors"] == []
    assert "app/main.py:999" in deps.quality_llm.prompts[1]
    assert "999" not in deps.quality_llm.prompts[0]  # 처음엔 피드백 "없음"


def test_retry_limit(index):
    """계속 틀리면 재생성 2회(합성 총 3회) 후 멈추고 오류가 남는다 (무한 루프 방지)."""
    deps = _deps(index, _target_count(index), [_guide(999)] * 5)
    state = run_reading_guide(deps)
    assert state["retry_count"] == 2
    assert state["validation_errors"]
    assert len(deps.quality_llm.prompts) == 3


def test_naive_skips_validation(index):
    """validate=False 면 틀린 인용이어도 검증 오류가 없고 재시도도 없다 (E4 베이스라인)."""
    deps = _deps(index, _target_count(index), [_guide(999)])
    state = run_reading_guide(deps, validate=False)
    assert not state.get("validation_errors")
    assert state["retry_count"] == 0
    assert len(deps.quality_llm.prompts) == 1


def test_v2_prompt_happy_path(index):
    """prompts=v2 로도 정상 흐름이 되고, 합성 프롬프트에 v2 규칙과 줄 1(모듈 시작)이 들어간다."""
    deps = _deps(index, _target_count(index), [_guide(4)])
    state = run_reading_guide(deps, prompts=reading_guide_v2)
    assert state["validation_errors"] == []
    assert state["retry_count"] == 0
    prompt = deps.quality_llm.prompts[0]
    assert "인용 가능한 줄: 1(모듈 시작)" in prompt  # 심볼 없는 모듈도 인용할 줄이 생긴다
    assert "1 이상의 정수" in prompt  # v2 에만 있는 규칙


def test_citable_line1_only_without_symbols(index):
    """심볼 있는 app/main.py 엔 줄 1 을 넣지 않고, 심볼 없는 app/__init__.py 엔 줄 1 만 준다."""
    deps = Deps(None, None, index)
    assert _citable(deps, "app/main.py") == "run:4"
    assert _citable(deps, "app/__init__.py") == "1(모듈 시작)"


def test_v3_prompt_happy_path(index):
    """prompts=v3 로도 정상 흐름이 되고, 합성 프롬프트에 v3 규칙이 들어가고 v2 문구는 빠진다."""
    deps = _deps(index, _target_count(index), [_guide(4)])
    state = run_reading_guide(deps, prompts=reading_guide_v3)
    assert state["validation_errors"] == []
    assert state["retry_count"] == 0
    prompt = deps.quality_llm.prompts[0]
    assert "목록에 줄 1 이 없는 모듈에 줄 1 을 쓰지 말 것" in prompt  # v3 에만 있는 규칙
    assert "심볼이 없는 모듈(import 만 있는" not in prompt  # v2 문구는 빠졌다


def test_feedback_hint_lists_citable_lines(index):
    """줄 범위 밖 인용(999)의 피드백에 "대신 쓸 줄"(run:4) 힌트가 붙는다 (줄 1 은 없음)."""
    deps = _deps(index, _target_count(index), [_guide(999), _guide(4)])
    run_reading_guide(deps)
    hint = "app/main.py:999 — 파일은 5줄까지 → 이 파일에서 인용 가능한 줄: run:4"
    assert hint in deps.quality_llm.prompts[1]
    assert "run:4" not in deps.quality_llm.prompts[0]  # 요약 목록이 아니라 힌트에서 온 값


def test_feedback_hint_similar_path(index):
    """없는 파일(main.py) 인용이면 같은 파일명의 인덱스 경로(app/main.py)를 힌트로 준다."""
    wrong = _guide(4)
    wrong.entrypoints = [CodeRef(file="main.py", line=4, symbol="run")]
    deps = _deps(index, _target_count(index), [wrong, _guide(4)])
    run_reading_guide(deps)
    hint = "main.py:4 — 파일이 없음 → 인덱스에 있는 비슷한 경로: app/main.py"
    assert hint in deps.quality_llm.prompts[1]


def test_cli_help_shows_map():
    """--help 에 map 서브커맨드가 보인다 (LLM 호출 없음)."""
    command = [sys.executable, "-m", "repoute.cli", "--help"]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0
    assert "map" in result.stdout
