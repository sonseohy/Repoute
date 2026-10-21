"""SQLite 체크포인터 테스트 (실제 LLM 없음). 실행: uv run pytest tests/test_checkpointer.py"""

import subprocess

import pytest
from langchain_core.messages import AIMessage

from repoute.graphs.deps import Deps
from repoute.graphs.walkthrough import Step, WalkthroughAnswer, ask, build_walkthrough_graph
from repoute.indexing.index import build_index
from repoute.memory.checkpointer import open_checkpointer
from repoute.validation.citations import CodeRef

# tests 는 패키지가 아니므로 test_walkthrough 에서 import 하지 않고 필요한 최소만 옮겼다
FILES = {"app/__init__.py": "", "app/main.py": "def run():\n    return 1\n"}
REF = CodeRef(file="app/main.py", line=1, symbol="run")
GOOD = WalkthroughAnswer(
    question="run 은?",
    steps=[Step(order=1, title="run() 이 1 을 돌려준다", explanation="설명", refs=[REF])],
    summary="요약",
)


class FakeToolLLM:
    """agent 쪽 invoke 는 scripted AIMessage 를 순서대로, 구조화 출력은 늘 GOOD 을 돌려준다."""

    class _Structured:
        """finalize 가 쓰는 구조화 출력 자리."""

        def invoke(self, messages):
            return GOOD

    def __init__(self, chat_answers: list):
        self.chat_answers = list(chat_answers)

    def bind_tools(self, tools):
        return self

    def with_structured_output(self, schema):
        return self._Structured()

    def invoke(self, messages):
        return self.chat_answers.pop(0)


@pytest.fixture
def index(tmp_path):
    """tmp_path/owner/name 에 FILES 를 커밋한 로컬 git 레포의 인덱스."""
    origin = tmp_path / "owner" / "name"
    for rel, text in FILES.items():
        (origin / rel).parent.mkdir(parents=True, exist_ok=True)
        (origin / rel).write_text(text)
    git = ["git", "-C", str(origin)]
    commit = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, *commit, "commit", "-q", "-m", "init"], check=True)
    repo = build_index(f"file://{origin}", cache_dir=tmp_path / "cache")
    yield repo
    repo.db.close()


def _read_main() -> AIMessage:
    """app/main.py 1~2줄 읽기. add_messages 가 id 를 붙이므로 매번 새로 만든다."""
    call = {"name": "read_code", "args": {"file": "app/main.py", "start": 1, "end": 2}, "id": "c1"}
    return AIMessage(content="", tool_calls=[call])


def _graph(index, saver, chat: list):
    """가짜 LLM 을 quality 자리에 꽂고 saver 를 체크포인터로 쓰는 따라가기 그래프."""
    return build_walkthrough_graph(Deps(None, FakeToolLLM(chat), index), checkpointer=saver)


def test_thread_survives_reopen(index, tmp_path):
    """같은 thread 로 두 번 물은 뒤 연결을 닫고 파일에서 다시 열어도 두 질문이 다 남아 있다.

    프로세스 재시작 시뮬레이션 (wave12). 답(WalkthroughAnswer)도 pydantic 객체로 되살아난다.
    """
    path = tmp_path / "threads" / "threads.sqlite"  # 없는 디렉터리도 만들어야 한다
    saver = open_checkpointer(path)
    chat = [_read_main(), AIMessage(content="답1"), _read_main(), AIMessage(content="답2")]
    graph = _graph(index, saver, chat)
    ask(graph, "첫 질문", thread_id="t1")
    ask(graph, "둘째 질문", thread_id="t1")
    saver.conn.close()

    reopened = open_checkpointer(path)
    state = _graph(index, reopened, []).get_state({"configurable": {"thread_id": "t1"}})
    humans = [m.content for m in state.values["messages"] if m.type == "human"]
    assert "첫 질문" in humans and "둘째 질문" in humans
    assert state.values["answer"] == GOOD
    # 같은 thread 에서 누적된다. 직렬화를 거치면 tuple 이 list 로 돌아오므로 tuple 로 맞춰 비교
    assert [tuple(r) for r in state.values["read_ranges"]] == [("app/main.py", 1, 2)] * 2
    reopened.conn.close()


def test_other_thread_is_empty(index):
    """다른 thread_id 는 앞 대화와 섞이지 않고 비어 있다 (":memory:" 도 받는다)."""
    saver = open_checkpointer(":memory:")
    graph = _graph(index, saver, [_read_main(), AIMessage(content="답")])
    ask(graph, "첫 질문", thread_id="t1")
    other = graph.get_state({"configurable": {"thread_id": "t2"}})
    assert other.values == {}
    saver.conn.close()
