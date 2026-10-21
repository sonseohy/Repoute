"""따라가기 그래프 테스트 (실제 LLM 없음). 실행: uv run pytest tests/test_walkthrough.py"""

import subprocess

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from repoute.graphs.deps import Deps
from repoute.graphs.walkthrough import (
    CHECKPOINT_TYPES,
    MAX_EXPLORE_WITHOUT_READ,
    MAX_NUDGES,
    MAX_TOOL_CALLS,
    Step,
    WalkthroughAnswer,
    ask,
    build_walkthrough_graph,
)
from repoute.indexing.index import build_index
from repoute.prompts import walkthrough_v1, walkthrough_v2, walkthrough_v3
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


def _run(index, chat, structured, checkpointer=None, prompts=walkthrough_v1):
    """가짜 LLM 을 quality 자리에 꽂은 그래프와 그 LLM. 프롬프트 기본은 그래프와 같은 v1."""
    llm = FakeToolLLM(chat, structured)
    graph = build_walkthrough_graph(Deps(None, llm, index), prompts, checkpointer)
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


def test_happy_path_prompt_v2(index):
    """v2 프롬프트로도 정상 흐름이 통과하고, agent 첫 입력의 SystemMessage 에 "읽기 전략" 이 있다.

    기록 08 의 기준 프롬프트이므로 그래프가 v2 를 실제로 받아 쓰는지 확인한다
    (cli 기본값은 v3 로 바뀌었다. v3 는 test_happy_path_prompt_v3).
    """
    chat = [_call("find_definition", {"name": "run"}), _read_main(), AIMessage(content="답 준비")]
    graph, llm = _run(index, chat, [GOOD], prompts=walkthrough_v2)
    state = ask(graph, "run() 을 부르면?")
    assert state["validation_errors"] == []
    assert state["tool_calls"] == 2
    first = llm.chat_inputs[0][0]
    assert isinstance(first, SystemMessage) and "읽기 전략" in first.content


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


def test_happy_path_prompt_v3(index):
    """v3 프롬프트 정상 흐름: finalize 지시에 def/class 줄 규칙이, 정의 결과에 인용 힌트가 있다."""
    chat = [_call("find_definition", {"name": "run"}), _read_main(), AIMessage(content="답 준비")]
    graph, llm = _run(index, chat, [GOOD], prompts=walkthrough_v3)
    state = ask(graph, "run() 을 부르면?")
    assert state["validation_errors"] == []
    assert state["tool_calls"] == 2
    assert "def/class 줄" in llm.structured_inputs[0][-1].content  # FINALIZE 에 추가한 한 줄
    tool_texts = [m.content for m in state["messages"] if isinstance(m, ToolMessage)]
    assert "인용: app/main.py:4" in tool_texts[0]  # def 줄 인용 힌트가 대화에 들어갔다


def test_agent_model_fast(index):
    """agent_model="fast" 면 agent 턴은 fast_llm 이, finalize 는 quality_llm 이 맡는다 (E5)."""
    fast = FakeToolLLM([_read_main(), AIMessage(content="답 준비")], [])
    quality = FakeToolLLM([], [GOOD])
    graph = build_walkthrough_graph(Deps(fast, quality, index), walkthrough_v3, agent_model="fast")
    state = ask(graph, "run() 을 부르면?")
    assert state["validation_errors"] == []
    assert len(fast.chat_inputs) == 2 and fast.structured_inputs == []  # agent 2턴 모두 fast
    assert quality.chat_inputs == [] and len(quality.structured_inputs) == 1  # 합성만 quality


def test_reread_inside_read_range_blocked(index):
    """이미 읽은 1~5줄 안의 read_code(4~5) 는 실행 없이 "이미 읽은 범위" 를 돌려주고 세지 않는다."""
    again = _call("read_code", {"file": "app/main.py", "start": 4, "end": 5}, 2)
    graph, _ = _run(index, [_read_main(), again, AIMessage(content="답")], [GOOD])
    state = ask(graph, "run() 을 부르면?")
    assert state["tool_calls"] == 1  # 두 번째 read_code 는 예산을 쓰지 않았다
    assert state["read_ranges"] == [("app/main.py", 1, 5)]
    last_tool = [m for m in state["messages"] if isinstance(m, ToolMessage)][-1]
    assert last_tool.content.startswith("이미 읽은 범위입니다 (app/main.py 1~5줄)")
    assert "def run():" not in last_tool.content  # 코드를 다시 보내지 않는다 (재전송 비용)


def test_nudge_before_answer_then_read(index):
    """read_code 없이 바로 답하려 하면 넛지가 들어가고, 그 뒤 읽고 답하면 통과한다 (기록 10)."""
    chat = [AIMessage(content="바로 답"), _read_main(), AIMessage(content="답 준비")]
    graph, llm = _run(index, chat, [GOOD])
    state = ask(graph, "run() 을 부르면?")
    assert state["nudges"] == 1
    assert state["validation_errors"] == []
    assert state["read_ranges"] == [("app/main.py", 1, 5)]
    nudges = [m for m in state["messages"] if isinstance(m, HumanMessage)]
    assert nudges[-1].content.startswith("아직 read_code")
    assert llm.chat_inputs[1][-1].content.startswith("아직 read_code")  # 다음 agent 턴이 받았다


def test_nudge_only_once(index):
    """넛지 뒤에도 안 읽고 답하면 재넛지 없이 finalize 로 가며 읽지 않은 코드 오류가 남는다."""
    chat = [AIMessage(content="바로 답"), AIMessage(content="또 바로 답")]
    graph, llm = _run(index, chat, [GOOD] * 3)
    state = ask(graph, "run() 을 부르면?")
    assert state["nudges"] == MAX_NUDGES == 1
    assert len(llm.chat_inputs) == 2  # agent 는 두 번만 불렸다
    assert any("읽지 않은 코드" in e for e in state["validation_errors"])
    assert state["retry_count"] == 2  # 재작성해도 읽은 범위가 없으니 그대로 실패로 끝난다


def test_explore_without_read_blocked_then_read(index):
    """읽기 없이 find_definition 만 8번이면 7·8번째는 실행 안 하고 "읽으라" 를 돌려준다 (기록 11).

    거부도 호출 수를 소모하고, 그 뒤 read_code 는 실행되며 이후 find_* 도 다시 실행된다.
    """
    finds = [_call("find_definition", {"name": "run"}, n) for n in range(10, 18)]
    after = _call("find_definition", {"name": "run"}, 20)
    chat = [*finds, _read_main(), after, AIMessage(content="답 준비")]
    graph, _ = _run(index, chat, [GOOD])
    state = ask(graph, "run() 을 부르면?")
    tool_texts = [m.content for m in state["messages"] if isinstance(m, ToolMessage)]
    assert MAX_EXPLORE_WITHOUT_READ == 6
    assert all("app/main.py:4-5" in t for t in tool_texts[:6])  # 6번째까지는 실제 실행
    assert tool_texts[6].startswith("읽기 없이 탐색만 6회 했습니다")
    assert tool_texts[7].startswith("읽기 없이 탐색만 7회 했습니다")
    assert state["explore_blocked"] == 2
    assert state["tool_calls"] == 10  # 8 + read_code 1 + find 1: 거부도 예산을 썼다
    assert "def run():" in tool_texts[8]  # read_code 는 차단되지 않고 실행됐다
    assert "app/main.py:4-5" in tool_texts[9]  # 읽은 뒤의 find_definition 은 다시 실행된다
    assert state["validation_errors"] == []


def test_explore_not_blocked_after_read(index):
    """이미 읽은 범위가 있으면 find_* 를 몇 번 불러도 탐색 차단은 발동하지 않는다."""
    finds = [_call("find_definition", {"name": "run"}, n) for n in range(10, 20)]
    graph, _ = _run(index, [_read_main(), *finds, AIMessage(content="답")], [GOOD])
    state = ask(graph, "run() 을 부르면?")
    tool_texts = [m.content for m in state["messages"] if isinstance(m, ToolMessage)]
    assert state["explore_blocked"] == 0
    assert state["tool_calls"] == 11
    assert all("app/main.py:4-5" in t for t in tool_texts[1:])
