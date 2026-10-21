"""Supervisor 그래프 테스트 (실제 LLM 없음). 실행: uv run pytest tests/test_supervisor.py"""

import asyncio
import subprocess
import sys
import types

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from repoute.graphs import supervisor
from repoute.graphs.reading_guide import ModuleSummary, ReadingGuide, ReadingStep
from repoute.graphs.supervisor import (
    Intent,
    SupervisorDeps,
    _gate,
    build_supervisor_graph,
    parse_profile,
    pending_interrupt,
    run_supervisor,
)
from repoute.graphs.walkthrough import Step, WalkthroughAnswer
from repoute.memory.checkpointer import CHECKPOINT_TYPES
from repoute.prompts import supervisor_v2
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

        def __init__(self, queue: list, log: list):
            self.queue, self.log = queue, log

        def invoke(self, prompt):
            self.log.append(prompt)
            return self.queue.pop(0)

    def __init__(self, structured: dict | None = None, chat: list | None = None):
        self.structured = {k: list(v) for k, v in (structured or {}).items()}
        self.chat = list(chat or [])
        self.prompts = []  # with_structured_output 으로 요청한 스키마 순서
        self.structured_inputs = []  # 구조화 출력에 보낸 프롬프트 (분류 프롬프트 확인용)
        self.chat_inputs = []  # 도구 대화(agent 노드)가 보낸 messages 목록

    def with_structured_output(self, schema):
        self.prompts.append(schema)
        return self._Structured(self.structured[schema], self.structured_inputs)

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


def _deps(
    tmp_path, intents: list, quality: FakeLLM | None = None, fast: FakeLLM | None = None
) -> SupervisorDeps:
    """fast 자리에 Intent·요약 큐를, quality 자리에 주어진 가짜를 꽂은 Deps."""
    fast = fast or FakeLLM({Intent: intents, ModuleSummary: _summaries()})
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


def test_gate_relaxed_only_when_search_available():
    """식별자 없는 ask 는 기본(can_search=False)이면 map 으로, 검색 도구가 있으면 그대로 둔다."""
    message = "리디렉트 처리는 어디서 해?"
    assert _gate("ask", message, False)[0] == "map"
    assert _gate("ask", message, False, can_search=True) is None
    assert _gate("ask", "run() 은?", False) is None  # 이름이 있으면 원래도 유지


def test_ask_without_symbol_with_embedder(tmp_path, repo_url, fake_embedder):
    """embedder 가 있으면 식별자 없는 ask 도 따라가기로 가고, 임베딩 준비 이벤트가 먼저 남는다."""
    query = {"query": "helper 를 부르는 진입 함수"}
    chat = [_call("search_symbols", query, 0), _read_main(1), AIMessage(content="답")]
    quality = FakeLLM({WalkthroughAnswer: [ANSWER]}, chat)
    deps = _deps(tmp_path, [Intent(kind="ask", reason="흐름 질문")], quality)
    deps.embedder, deps.embedding_model = fake_embedder, "fake"
    state = run_supervisor(deps, repo_url, "코드 흐름 알려줘")
    deps.close()
    assert state["intent"].kind == "ask"
    assert _types(state) == ["IndexReady", "IntentDecided", "EmbeddingsReady", "AnswerReady"]
    assert state["events"][2] == {"type": "EmbeddingsReady", "count": 2, "cached": False}
    search_result = quality.chat_inputs[1][-1].content  # search_symbols 의 ToolMessage
    assert "→ read_code(" in search_result and "인용:" in search_result


def test_chat_intent(tmp_path, repo_url):
    """chat 은 서브그래프 없이 고정 안내를 돌려주고, quality 모델은 한 번도 부르지 않는다."""
    quality = FakeLLM()
    deps = _deps(tmp_path, [Intent(kind="chat", reason="인사")], quality)
    state = run_supervisor(deps, repo_url, "안녕")
    deps.close()
    assert state["reply"] == supervisor_v2.CHAT_REPLY
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


def test_next_walks_reading_order_without_llm(tmp_path, repo_url):
    """map 뒤 같은 thread 의 "다음" 은 지도를 다시 만들지 않고 저장된 순서를 한 단계씩 짚는다.

    quality 모델의 ReadingGuide 큐는 처음 1개에서 줄지 않고(합성 호출 0회), cursor 가
    1단계 → 2단계로 나아가며, 순서를 다 넘기면 고정 안내(reply)로 끝난다.
    LLM 이 map 이라 잘못 골라도 "다음" 으로 시작하면 코드 게이트가 next 로 고정한다.
    """
    quality = FakeLLM({ReadingGuide: [_guide()]})
    intents = [
        Intent(kind="map", reason="구조"),
        Intent(kind="next", reason="다음 단계"),
        Intent(kind="map", reason="오판"),  # 게이트가 next 로 고친다
        Intent(kind="next", reason="다음 단계"),
    ]
    deps = _deps(tmp_path, intents, quality)
    run_supervisor(deps, repo_url, "어디서부터 읽어?", "t1")
    quality.structured[ReadingGuide].append(_guide())  # 지도를 다시 만들면 이것이 꺼내진다
    first = run_supervisor(deps, repo_url, "다음 파일 알려줘", "t1")
    second = run_supervisor(deps, repo_url, "다음", "t1")
    done = run_supervisor(deps, repo_url, "그다음은?", "t1")
    deps.close()
    assert len(quality.structured[ReadingGuide]) == 1  # 합성 호출 0회
    assert first["next"]["order"] == 1 and first["next"]["file"] == "app/util.py"
    assert first["next"]["ref"] == {"file": "app/util.py", "line": 1, "symbol": "helper"}
    assert first["next"]["remaining"] == 1 and "helper 를 따라가 줘" in first["next"]["hint"]
    assert _types(first) == ["IndexReady", "IntentDecided", "NextStep"]
    assert second["next"]["order"] == 2 and second["next"]["remaining"] == 0
    assert _types(second) == ["IndexReady", "IntentDecided", "GateApplied", "NextStep"]
    assert second["events"][2]["from"] == "map" and second["events"][2]["to"] == "next"
    assert done["next"] is None and done["reply"] == supervisor_v2.NEXT_DONE
    assert done["events"][-1] == {"type": "NextStep", "order": None, "remaining": 0}
    assert done["cursor"] == 2


def test_next_after_seen_step_moves_cursor(tmp_path, repo_url):
    """ "N번 다음" 은 N단계까지 본 것으로 치고 그 다음을 준다 (지도를 다 읽은 뒤 1단계로 안 돌아감).

    지도(2단계) 뒤 "2번 다음에 뭐 봐야 해?" 는 LLM 이 map 이라 해도 게이트가 next 로 고치고,
    cursor 가 2 로 옮겨져 바로 끝 안내가 나온다. "1번까지 봤어" 는 2단계를 준다.
    """
    quality = FakeLLM({ReadingGuide: [_guide()]})
    intents = [
        Intent(kind="map", reason="구조"),
        Intent(kind="map", reason="오판"),  # 게이트가 next 로 고친다
        Intent(kind="next", reason="다음 단계"),
    ]
    deps = _deps(tmp_path, intents, quality)
    run_supervisor(deps, repo_url, "어디서부터 읽어?", "t1")
    done = run_supervisor(deps, repo_url, "2번 다음에 뭐 봐야 해?", "t1")
    back = run_supervisor(deps, repo_url, "1번까지 봤어, 다음은?", "t1")
    deps.close()
    assert done["next"] is None and done["reply"] == supervisor_v2.NEXT_DONE
    assert done["events"][2]["from"] == "map" and done["events"][2]["to"] == "next"
    assert done["cursor"] == 2
    assert back["next"]["order"] == 2 and back["next"]["remaining"] == 0 and back["cursor"] == 2


def test_next_without_guide_falls_back_to_map(tmp_path, repo_url):
    """지도가 없는 thread 에서 next 로 분류되면 코드 게이트가 map 으로 내려 지도를 만든다."""
    quality = FakeLLM({ReadingGuide: [_guide()]})
    deps = _deps(tmp_path, [Intent(kind="next", reason="다음 단계")], quality)
    state = run_supervisor(deps, repo_url, "다음 파일 알려줘", "fresh")
    deps.close()
    assert state["intent"].kind == "map"
    assert _types(state) == ["IndexReady", "IntentDecided", "GateApplied", "GuideReady"]
    assert state["events"][2]["from"] == "next" and state["events"][2]["to"] == "map"
    assert state["cursor"] == 0


def test_history_goes_into_classify_prompt(tmp_path, repo_url):
    """앞 메시지와 도우미 요약이 history 로 남아 다음 분류 프롬프트에 들어간다."""
    fast = FakeLLM({Intent: [Intent(kind="chat", reason="인사")] * 2})
    deps = _deps(tmp_path, [], fast=fast)
    first = run_supervisor(deps, repo_url, "안녕", "t1")
    second = run_supervisor(deps, repo_url, "뭘 할 수 있어?", "t1")
    deps.close()
    assert supervisor_v2.NO_HISTORY in fast.structured_inputs[0]
    assert "- 사용자: 안녕" in fast.structured_inputs[1]
    assert "- 도우미: " + supervisor_v2.CHAT_REPLY[:20] in fast.structured_inputs[1]
    assert len(first["history"]) == 2 and len(second["history"]) == 4
    assert _types(second) == ["IndexReady", "IntentDecided", "Replied"]  # 이번 실행 분만


# --- Phase 6: issues 의도 (Issue Matcher 는 가짜로 대체, GitHub·LLM 호출 없음) ---


def _fake_matcher(issue_mod, calls: list):
    """_run_issue_matcher 대역. 받은 Profile 을 calls 에 남기고 이슈 1개짜리 결과를 준다."""

    def fake(deps, repo_url, profile):
        calls.append(profile)
        match = issue_mod.IssueMatch(
            number=12,
            title="helper 에 docstring 추가",
            url="https://github.com/owner/name/issues/12",
            labels=["good first issue"],
            why="관심 영역과 맞는 작은 수정",
            modules=["app.util"],
            refs=[CodeRef(file="app/util.py", line=1, symbol="helper")],
            difficulty="easy",
        )
        return issue_mod.IssueMatches(
            repo="owner/name", profile=profile, matches=[match], considered=3
        )

    return fake


@pytest.fixture
def matcher_calls(monkeypatch, issue_mod) -> list:
    """GitHub 도구 로드·이슈 매칭을 가짜로 바꾸고, 매칭에 넘어간 Profile 목록을 돌려준다."""
    calls = []
    monkeypatch.setattr(supervisor, "_run_issue_matcher", _fake_matcher(issue_mod, calls))
    monkeypatch.setattr(supervisor, "_load_issue_tools", lambda deps: {})
    return calls


def test_gate_sends_issue_words_to_issues(tmp_path, repo_url):
    """이슈·기여 단어가 있으면 LLM 이 map/chat 이라 해도 코드 게이트가 issues 로 고친다.

    ask 는 건드리지 않아 함수 이름이 있는 질문의 따라가기는 그대로다. 실행에서는 지도를 만들지
    않으므로 quality 모델은 한 번도 불리지 않는다 (토큰이 없어 안내로 끝난다).
    """
    assert _gate("map", "good first issue 추천해줘", False)[0] == "issues"
    assert _gate("chat", "이 레포에 기여하고 싶어", False)[0] == "issues"
    assert _gate("ask", "issue 에 나온 run() 은 어떻게 돌아?", False) is None
    quality = FakeLLM()
    deps = _deps(tmp_path, [Intent(kind="map", reason="구조")], quality)
    state = run_supervisor(deps, repo_url, "good first issue 추천해줘", "t1")
    deps.close()
    assert state["intent"].kind == "issues"
    assert _types(state) == ["IndexReady", "IntentDecided", "GateApplied", "Replied"]
    assert state["events"][2]["from"] == "map" and state["events"][2]["to"] == "issues"
    assert quality.prompts == []


def test_issues_without_token_replies(tmp_path, repo_url, matcher_calls):
    """토큰이 없으면 프로필을 묻지 않고(interrupt 없음) 토큰 안내만 하고, 매칭도 부르지 않는다.

    멈춘 질문이 없으므로 같은 thread 의 다음 메시지는 재개가 아니라 새 턴으로 처리된다.
    """
    intents = [Intent(kind="issues", reason="이슈 추천"), Intent(kind="chat", reason="인사")]
    deps = _deps(tmp_path, intents)
    state = run_supervisor(deps, repo_url, "어떤 이슈부터 할까?", "t1")
    graph = build_supervisor_graph(deps, deps.checkpointer)
    waiting = pending_interrupt(graph, {"configurable": {"thread_id": "t1"}})
    after = run_supervisor(deps, repo_url, "안녕", "t1")
    deps.close()
    assert state["reply"] == supervisor_v2.ISSUES_NEED_TOKEN
    assert "GITHUB_PERSONAL_ACCESS_TOKEN" in state["reply"]
    assert "ask_user" not in state and "__interrupt__" not in state
    assert waiting is None and matcher_calls == []
    assert _types(after) == ["IndexReady", "IntentDecided", "Replied"]


def test_issues_asks_profile_then_resumes(tmp_path, repo_url, matcher_calls):
    """프로필이 없으면 interrupt 로 묻고, 같은 thread 의 다음 메시지로 재개해 매칭한다.

    첫 턴: result 에 ask_user 와 질문(reply), 이벤트 ProfileRequested, 매칭 호출 0회.
    둘째 턴 "인증, beginner": Command(resume) 로 재개되어 Profile(인증, beginner) 로 매칭하고
    issues 가 채워진다. 분류는 다시 하지 않는다(Intent 큐 1개로 충분). 멈춘 턴의 이벤트는
    섞이지 않고, history 에는 원래 요청과 추천 요약이 남는다.
    """
    deps = _deps(tmp_path, [Intent(kind="issues", reason="이슈 추천")])
    deps.github_token = "test-token"
    first = run_supervisor(deps, repo_url, "good first issue 추천해줘", "t1")
    graph = build_supervisor_graph(deps, deps.checkpointer)
    waiting = pending_interrupt(graph, {"configurable": {"thread_id": "t1"}})
    second = run_supervisor(deps, repo_url, "인증, beginner", "t1")
    deps.close()
    assert first["ask_user"] is True and first["reply"] == supervisor_v2.PROFILE_QUESTION
    assert _types(first) == ["IndexReady", "IntentDecided", "ProfileRequested"]
    assert first["events"][-1]["question"] == supervisor_v2.PROFILE_QUESTION
    assert waiting == {"kind": "profile", "question": supervisor_v2.PROFILE_QUESTION}
    assert [p.model_dump() for p in matcher_calls] == [{"interests": "인증", "level": "beginner"}]
    assert second["issues"].matches[0].number == 12 and second["issues"].considered == 3
    assert second["profile"] == {"interests": "인증", "level": "beginner"}
    assert _types(second) == ["IssuesReady"]
    assert second["events"][0] == {"type": "IssuesReady", "count": 1, "considered": 3}
    assert "ask_user" not in second
    assert second["history"][-2:] == [
        {"role": "user", "text": "good first issue 추천해줘"},
        {"role": "agent", "text": "이슈 추천 1개: #12"},
    ]


def test_issues_with_saved_profile_runs_directly(tmp_path, repo_url, matcher_calls):
    """thread 에 프로필이 있으면 두 번째 이슈 요청은 묻지 않고(interrupt 없음) 바로 매칭한다."""
    deps = _deps(tmp_path, [Intent(kind="issues", reason="이슈 추천")] * 2)
    deps.github_token = "test-token"
    run_supervisor(deps, repo_url, "기여할 이슈 추천해줘", "t1")
    run_supervisor(deps, repo_url, "CLI 파싱 intermediate", "t1")
    again = run_supervisor(deps, repo_url, "이슈 하나 더 추천해줘", "t1")
    deps.close()
    assert "ask_user" not in again
    assert _types(again) == ["IndexReady", "IntentDecided", "IssuesReady"]
    assert len(matcher_calls) == 2 and matcher_calls[1] == matcher_calls[0]
    assert matcher_calls[1].interests == "CLI 파싱" and matcher_calls[1].level == "intermediate"
    assert again["issues"].matches[0].number == 12


def test_parse_profile(issue_mod):
    """수준 단어를 찾아 level 로, 나머지를 interests 로. 수준이 없으면 beginner."""
    parsed = parse_profile("CLI 파싱 intermediate")
    assert (parsed.interests, parsed.level) == ("CLI 파싱", "intermediate")
    assert parse_profile("인증, beginner").model_dump() == {
        "interests": "인증",
        "level": "beginner",
    }
    many = parse_profile("HTTP 인증, CLI 파싱, Advanced")
    assert (many.interests, many.level) == ("HTTP 인증, CLI 파싱", "advanced")
    assert parse_profile("문서화").level == "beginner"
    assert parse_profile("중급, 테스트").level == "intermediate"


def test_load_issue_tools_once_even_inside_running_loop(tmp_path, monkeypatch):
    """GitHub 도구는 처음 한 번만 불러 deps 에 캐시하고, 실행 중인 루프 안에서도 불러진다.

    repoute.github.mcp 는 가짜 모듈로 바꿔 네트워크 없이 load_github_tools 호출 수만 센다.
    """
    calls = []

    class FakeTool:
        """이름만 있는 도구."""

        name = "list_issues"

    async def load_github_tools(token, toolset="issues/readonly", names=None):
        calls.append(token)
        return [FakeTool()]

    fake = types.ModuleType("repoute.github.mcp")
    fake.load_github_tools = load_github_tools
    monkeypatch.setitem(sys.modules, "repoute.github.mcp", fake)
    deps = _deps(tmp_path, [])
    deps.github_token = "test-token"
    tools = supervisor._load_issue_tools(deps)
    assert list(tools) == ["list_issues"] and supervisor._load_issue_tools(deps) is tools

    async def inside_loop():
        fresh = _deps(tmp_path, [])
        fresh.github_token = "test-token"
        return supervisor._load_issue_tools(fresh)  # asyncio.run 대신 새 스레드에서 돈다

    assert list(asyncio.run(inside_loop())) == ["list_issues"]
    assert calls == ["test-token", "test-token"]


def test_issues_end_to_end_with_real_matcher(tmp_path, repo_url, monkeypatch, fake_embedder):
    """실제 Issue Matcher 그래프까지 통째로: 프로필 재개 → MCP 가짜 도구 → 임베딩 매칭 → fast 랭킹.

    `_run_issue_matcher` 를 가짜로 바꾸지 않는다. 도구 로드만 가짜(list_issues JSON) 로 대체하고,
    임베더는 conftest 의 결정론 FakeEmbedder. refs 는 LLM 이 아니라 임베딩 검색이 붙인 것이라
    가짜 레포의 실제 심볼(app.main.run / app.util.helper)·파일·줄을 가리켜야 한다.
    """
    import json

    from langchain_core.tools import BaseTool

    from repoute.graphs.issue_matcher import RankedPick, Ranking

    class FakeListIssues(BaseTool):
        name: str = "list_issues"
        description: str = "List issues"

        def _run(self, **kwargs):
            raise NotImplementedError

        async def _arun(self, **kwargs):
            body = "helper 가 run 에서 호출될 때 결과가 틀립니다. " * 4
            return json.dumps(
                [
                    {
                        "number": 7,
                        "title": "run helper bug",
                        "body": body,
                        "labels": [{"name": "bug"}],
                        "html_url": "https://github.com/owner/name/issues/7",
                        "comments": 1,
                        "created_at": "2026-10-21T00:00:00Z",
                    },
                    {
                        "number": 8,
                        "title": "pr",
                        "body": body,
                        "labels": [],
                        "html_url": "u",
                        "comments": 0,
                        "created_at": "2026-10-21T00:00:00Z",
                        "pull_request": {},
                    },
                ]
            )

    fast = FakeLLM(
        {
            Intent: [Intent(kind="issues", reason="이슈 추천")],
            ModuleSummary: _summaries(),
            Ranking: [
                Ranking(picks=[RankedPick(number=7, why="helper 호출 버그", difficulty="easy")])
            ],
        }
    )
    deps = _deps(tmp_path, [], fast=fast)
    deps.github_token = "test-token"
    deps.embedder = fake_embedder
    monkeypatch.setattr(
        supervisor,
        "_load_issue_tools",
        lambda d: d.__setattr__("issue_tools", {"list_issues": FakeListIssues()}),
    )
    run_supervisor(deps, repo_url, "good first issue 추천해줘", "t1")
    second = run_supervisor(deps, repo_url, "인증, beginner", "t1")
    deps.close()
    issues = second["issues"]
    assert issues.considered == 1 and issues.errors == []  # PR 은 제외됐다
    match = issues.matches[0]
    assert match.number == 7 and match.difficulty == "easy" and match.why == "helper 호출 버그"
    assert match.refs and all(r.file in ("app/main.py", "app/util.py") for r in match.refs)
    assert {r.symbol for r in match.refs} <= {"run", "helper"}
    assert match.modules and set(match.modules) <= {"app.main", "app.util"}
    assert _types(second) == ["EmbeddingsReady", "IssuesReady"] or _types(second) == ["IssuesReady"]
