"""따라가기 그래프 테스트 (실제 LLM 없음). 실행: uv run pytest tests/test_walkthrough.py"""

import subprocess

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from repoute.graphs.deps import Deps
from repoute.graphs.walkthrough import (
    CHECKPOINT_TYPES,
    MAX_TOOL_CALLS,
    Step,
    WalkthroughAnswer,
    ask,
    build_walkthrough_graph,
)
from repoute.indexing.index import build_index
from repoute.validation.citations import CodeRef

# 원본 레포 파일: main 의 run() 이 util 의 helper() 를 부른다 (test_reading_guide 와 같은 구성)
FILES = {
    "app/__init__.py": "",
    "app/main.py": "from app.util import helper\n\n\ndef run():\n    return helper()\n",
    "app/util.py": "def helper():\n    return 1\n",
}


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


class FakeToolLLM:
    """bind_tools(...).invoke(messages) 는 scripted AIMessage 를 순서대로,
    with_structured_output(S).invoke 는 scripted 답을 순서대로 돌려준다. 받은 입력은 기록한다."""

    class _Structured:
        """구조화 출력 쪽 큐. 대화 큐와 섞이지 않게 따로 둔다."""

        def __init__(self, parent):
            self.parent = parent

        def invoke(self, messages):
            self.parent.structured_inputs.append(messages)
            return self.parent.structured_answers.pop(0)

    def __init__(self, chat_answers: list, structured_answers: list):
        self.chat_answers = list(chat_answers)
        self.structured_answers = list(structured_answers)
        self.chat_inputs = []  # agent 가 보낸 messages 목록
        self.structured_inputs = []  # finalize 가 보낸 messages 목록

    def bind_tools(self, tools):
        return self

    def with_structured_output(self, schema):
        return self._Structured(self)

    def invoke(self, messages):
        self.chat_inputs.append(messages)
        return self.chat_answers.pop(0)


@pytest.fixture
def index(tmp_path):
    """app/main.py 의 run() 이 app/util.py 의 helper() 를 부르는 레포의 인덱스."""
    repo = build_index(f"file://{_make_origin(tmp_path)}", cache_dir=tmp_path / "cache")
    yield repo
    repo.db.close()


def _call(name: str, args: dict, n: int = 0) -> AIMessage:
    """도구 호출 하나를 담은 AIMessage."""
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"c{n}"}])


def _read_main() -> AIMessage:
    """app/main.py 1~5줄 읽기. add_messages 가 메시지에 id 를 붙이므로 매번 새로 만든다."""
    return _call("read_code", {"file": "app/main.py", "start": 1, "end": 5}, 1)


def _answer(*refs: CodeRef) -> WalkthroughAnswer:
    """refs 를 가진 단계 하나짜리 답 (refs 가 없으면 인용 없는 단계)."""
    step = Step(order=1, title="run() 이 helper() 를 부른다", explanation="설명", refs=list(refs))
    return WalkthroughAnswer(question="run 은?", steps=[step], summary="요약")


GOOD = _answer(CodeRef(file="app/main.py", line=4, symbol="run"))


def _run(index, chat, structured, checkpointer=None):
    """가짜 LLM 을 quality 자리에 꽂은 그래프와 그 LLM."""
    llm = FakeToolLLM(chat, structured)
    graph = build_walkthrough_graph(Deps(None, llm, index), checkpointer=checkpointer)
    return graph, llm


def test_happy_path(index):
    """정의 찾기 → 코드 읽기 → 답. 도구가 실제로 실행되고 읽은 범위 안 인용은 통과한다."""
    chat = [_call("find_definition", {"name": "run"}), _read_main(), AIMessage(content="답 준비")]
    graph, _ = _run(index, chat, [GOOD])
    state = ask(graph, "run() 을 부르면?")
    assert state["answer"].steps
    assert state["validation_errors"] == []
    assert state["tool_calls"] == 2
    assert state["read_ranges"] == [("app/main.py", 1, 5)]
    tool_texts = [m.content for m in state["messages"] if isinstance(m, ToolMessage)]
    assert "app/main.py:4-5" in tool_texts[0]  # find_definition 이 실제 인덱스를 조회했다
    assert "def run():" in tool_texts[1]  # read_code 가 실제 파일을 읽었다


def test_unread_code_rejected(index):
    """존재하지만 읽지 않은 app/util.py:1 인용은 거부되고, 힌트를 받은 두 번째 답이 통과한다."""
    unread = _answer(CodeRef(file="app/util.py", line=1, symbol="helper"))
    graph, llm = _run(index, [_read_main(), AIMessage(content="답")], [unread, GOOD])
    state = ask(graph, "run() 을 부르면?")
    assert state["retry_count"] == 1
    assert state["validation_errors"] == []
    second_prompt = llm.structured_inputs[1][-1]
    assert isinstance(second_prompt, HumanMessage)
    assert "app/util.py:1 — 읽지 않은 코드" in second_prompt.content
    assert "인용 가능한 범위: app/main.py 1~5줄" in second_prompt.content  # 대신 쓸 범위 힌트
    assert "읽지 않은 코드" not in llm.structured_inputs[0][-1].content  # 처음엔 피드백 "없음"


def test_tool_call_limit(index):
    """매번 도구를 부르는 모델이어도 MAX_TOOL_CALLS 에서 멈추고 finalize 로 간다."""
    chat = [_call("find_definition", {"name": "run"}, n) for n in range(2, 22)]
    graph, llm = _run(index, [_read_main()] + chat, [GOOD])
    state = ask(graph, "run() 을 부르면?")
    assert state["tool_calls"] == MAX_TOOL_CALLS == 15
    assert len(llm.chat_inputs) == MAX_TOOL_CALLS + 1  # 마지막 agent 호출의 도구는 실행 안 함
    assert "상한" in llm.structured_inputs[0][-1].content  # finalize 가 상한 도달을 알린다
    # 실행 못 한 도구 호출에도 ToolMessage 가 달려 대화 형식이 맞다
    assert isinstance(llm.structured_inputs[0][-2], ToolMessage)


def test_step_without_refs(index):
    """인용 없는 단계는 "인용 없음" 오류이고, 계속 그러면 재작성 2회 후 멈춘다."""
    graph, llm = _run(index, [_read_main(), AIMessage(content="답")], [_answer()] * 3)
    state = ask(graph, "run() 을 부르면?")
    assert any("step 1 — 인용 없음" in e for e in state["validation_errors"])
    assert state["retry_count"] == 2
    assert len(llm.structured_inputs) == 3


def test_checkpointer_keeps_history(index):
    """같은 thread_id 로 두 번 물으면 두 번째 agent 입력에 첫 질문과 그 대화가 들어 있다."""
    chat = [_read_main(), AIMessage(content="답1"), _read_main(), AIMessage(content="답2")]
    saver = InMemorySaver(serde=JsonPlusSerializer(allowed_msgpack_modules=CHECKPOINT_TYPES))
    graph, llm = _run(index, chat, [GOOD, GOOD], checkpointer=saver)
    ask(graph, "첫 질문", thread_id="t1")
    state = ask(graph, "둘째 질문", thread_id="t1")
    second = llm.chat_inputs[2]  # 두 번째 질문의 첫 agent 입력
    # System + 첫 질문 + AI(read_code) + ToolMessage + AI(답1) + 둘째 질문
    assert len(second) == 6
    assert second[1].content == "첫 질문"
    assert second[-1].content == "둘째 질문"
    assert state["validation_errors"] == []
