"""Supervisor 그래프 테스트 (실제 LLM 없음). 실행: uv run pytest tests/test_supervisor.py"""

import subprocess

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from repoute.graphs.reading_guide import ModuleSummary, ReadingGuide, ReadingStep
from repoute.graphs.supervisor import Intent, SupervisorDeps, run_supervisor
from repoute.graphs.walkthrough import CHECKPOINT_TYPES, Step, WalkthroughAnswer
from repoute.prompts import supervisor_v1
from repoute.validation.citations import CodeRef

# 원본 레포 파일: main 의 run() 이 util 의 helper() 를 부른다 (test_walkthrough 와 같은 구성)
FILES = {
    "app/__init__.py": "",
    "app/main.py": "from app.util import helper\n\n\ndef run():\n    return helper()\n",
    "app/util.py": "def helper():\n    return 1\n",
}


def _make_origin(tmp_path):
    """tmp_path/owner/name 에 FILES 를 커밋한 로컬 git 레포 (test_walkthrough 방식)."""
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


class FakeLLM:
    """FakeStructuredLLM + FakeToolLLM 합본. 구조화 출력은 스키마별 큐에서 순서대로 꺼낸다.

    fast 자리(Intent·ModuleSummary)와 quality 자리(ReadingGuide·WalkthroughAnswer·도구 대화)를
    한 클래스로 흉내 낸다. bind_tools(...).invoke 는 chat 큐에서 꺼낸다.
    """

    class _Structured:
        """스키마 하나의 큐."""

        def __init__(self, queue: list):
            self.queue = queue

        def invoke(self, prompt):
            return self.queue.pop(0)

    def __init__(self, structured: dict | None = None, chat: list | None = None):
        self.structured = {k: list(v) for k, v in (structured or {}).items()}
        self.chat = list(chat or [])
        self.prompts = []  # with_structured_output 으로 요청한 스키마 순서
        self.chat_inputs = []  # 도구 대화(agent 노드)가 보낸 messages 목록

    def with_structured_output(self, schema):
        self.prompts.append(schema)
        return self._Structured(self.structured[schema])

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        self.chat_inputs.append(messages)
        return self.chat.pop(0)


def _summaries() -> list[ModuleSummary]:
    """대상 모듈 수(2~3)보다 넉넉한 요약. 남는 것은 쓰이지 않는다."""
    return [ModuleSummary(module="m", file="f.py", summary="요약", role="core")] * 5


def _guide() -> ReadingGuide:
    """인용이 모두 올바른 읽기 지도."""
    run = CodeRef(file="app/main.py", line=4, symbol="run")
    helper = CodeRef(file="app/util.py", line=1, symbol="helper")
    steps = [
        ReadingStep(order=1, file="app/util.py", reason="의존성 없음", ref=helper),
        ReadingStep(order=2, file="app/main.py", reason="진입점", ref=run),
    ]
    return ReadingGuide(overview="작은 앱", entrypoints=[run], reading_order=steps)


def _call(name: str, args: dict, n: int) -> AIMessage:
    """도구 호출 하나를 담은 AIMessage."""
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"c{n}"}])


@pytest.fixture
def repo_url(tmp_path):
    """로컬 git 레포 URL."""
    return f"file://{_make_origin(tmp_path)}"


def _deps(tmp_path, intents: list, quality: FakeLLM | None = None) -> SupervisorDeps:
    """fast 자리에 Intent·요약 큐를, quality 자리에 주어진 가짜를 꽂은 Deps."""
    fast = FakeLLM({Intent: intents, ModuleSummary: _summaries()})
    saver = InMemorySaver(serde=JsonPlusSerializer(allowed_msgpack_modules=CHECKPOINT_TYPES))
    return SupervisorDeps(fast, quality or FakeLLM(), tmp_path / "cache", saver)


def _types(state) -> list[str]:
    """events 의 type 만 순서대로."""
    return [e["type"] for e in state["events"]]


def test_map_intent(tmp_path, repo_url):
    """구조 질문이 map 으로 분류되면 읽기 지도가 채워지고, 이벤트는 인덱스 → 분류 → 지도 순서다."""
    quality = FakeLLM({ReadingGuide: [_guide()]})
    deps = _deps(tmp_path, [Intent(kind="map", reason="구조 질문")], quality)
    state = run_supervisor(deps, repo_url, "이 레포는 어디서부터 읽어야 해?")
    deps.close()
    assert state["guide"].reading_order
    assert state["validation_errors"] == []
    assert _types(state) == ["IndexReady", "IntentDecided", "GuideReady"]
    assert state["events"][0]["repo_id"].startswith("owner/name@")
    assert state["events"][0]["files"] == 3


def _read_main(n: int) -> AIMessage:
    """app/main.py 1~5줄 읽기."""
    return _call("read_code", {"file": "app/main.py", "start": 1, "end": 5}, n)


REF = CodeRef(file="app/main.py", line=4, symbol="run")
STEP = Step(order=1, title="run() 이 helper() 를 부른다", explanation="설명", refs=[REF])
ANSWER = WalkthroughAnswer(question="run 은?", steps=[STEP], summary="요약")


def test_ask_intent(tmp_path, repo_url):
    """함수 이름이 있는 흐름 질문이 ask 로 가면 따라가기 답이 채워진다 (도구는 실제 인덱스 조회)."""
    chat = [_call("find_definition", {"name": "run"}, 0), _read_main(1), AIMessage(content="답")]
    quality = FakeLLM({WalkthroughAnswer: [ANSWER]}, chat)
    deps = _deps(tmp_path, [Intent(kind="ask", reason="함수 흐름")], quality)
    state = run_supervisor(deps, repo_url, "run() 을 호출하면 무슨 일이 일어나?", "t1")
    deps.close()
    assert state["answer"].steps[0].refs == [REF]
    assert state["validation_errors"] == []
    assert _types(state) == ["IndexReady", "IntentDecided", "AnswerReady"]
    assert "guide" not in state


def test_gate_demotes_ask_without_symbol(tmp_path, repo_url):
    """LLM 이 ask 라 해도 메시지에 식별자가 없으면 코드 게이트가 map 으로 내린다 (wave06)."""
    quality = FakeLLM({ReadingGuide: [_guide()]})
    deps = _deps(tmp_path, [Intent(kind="ask", reason="흐름 질문")], quality)
    state = run_supervisor(deps, repo_url, "코드 흐름 알려줘")
    deps.close()
    assert state["intent"].kind == "map"
    assert state["guide"] is not None
    assert _types(state) == ["IndexReady", "IntentDecided", "GateApplied", "GuideReady"]
    assert state["events"][2]["from"] == "ask" and state["events"][2]["to"] == "map"


def test_chat_intent(tmp_path, repo_url):
    """chat 은 서브그래프 없이 고정 안내를 돌려주고, quality 모델은 한 번도 부르지 않는다."""
    quality = FakeLLM()
    deps = _deps(tmp_path, [Intent(kind="chat", reason="인사")], quality)
    state = run_supervisor(deps, repo_url, "안녕")
    deps.close()
    assert state["reply"] == supervisor_v1.CHAT_REPLY
    assert _types(state) == ["IndexReady", "IntentDecided", "Replied"]
    assert quality.prompts == []


def test_second_run_uses_cache(tmp_path, repo_url):
    """같은 레포를 두 번 처리하면 첫 IndexReady 는 cached=False, 두 번째는 cached=True."""
    intents = [Intent(kind="chat", reason="인사"), Intent(kind="chat", reason="인사")]
    deps = _deps(tmp_path, intents)
    first = run_supervisor(deps, repo_url, "안녕")
    second = run_supervisor(deps, repo_url, "또 안녕")
    deps.close()
    assert first["events"][0]["cached"] is False
    assert second["events"][0]["cached"] is True


def test_ask_thread_keeps_history(tmp_path, repo_url):
    """Supervisor 노드 안에서 부른 따라가기도 같은 thread_id 면 앞 대화를 이어 받는다.

    서브그래프를 노드 안에서 invoke 해도 체크포인트가 thread 기준으로 남는지 확인한다.
    """
    chat = [_read_main(1), AIMessage(content="답1"), _read_main(2), AIMessage(content="답2")]
    quality = FakeLLM({WalkthroughAnswer: [ANSWER, ANSWER]}, chat)
    deps = _deps(tmp_path, [Intent(kind="ask", reason="흐름")] * 2, quality)
    run_supervisor(deps, repo_url, "run() 첫 질문", "t1")
    run_supervisor(deps, repo_url, "run() 둘째 질문", "t1")
    deps.close()
    second = quality.chat_inputs[2]  # 두 번째 질문의 첫 agent 입력
    assert second[1].content == "run() 첫 질문"
    assert second[-1].content == "run() 둘째 질문"
