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
    parse_pr_ref,
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


# --- Phase 7: pr 의도 (PR Coach 는 가짜로 대체, GitHub·LLM 호출 없음) ---


def _fake_coach(pr_mod, calls: list):
    """_run_pr_coach 대역. (repo_url, number) 를 calls 에 남기고 변경 2개·영향 1개 리뷰를 준다."""

    def fake(deps, repo_url, number):
        calls.append((repo_url, number))
        index = deps.indexes[repo_url]  # run_pr 가 연 인덱스가 있어야 한다
        return pr_mod.PRReview(
            repo=f"{index.info.owner}/{index.info.name}",
            number=number,
            title="helper 반환값 수정",
            url=f"https://github.com/{index.info.owner}/{index.info.name}/pull/{number}",
            summary="helper 가 2 를 돌려주게 바꿨다",
            changed_symbols=[
                pr_mod.SymbolRef(name="helper", file="app/util.py", line=1),
                pr_mod.SymbolRef(name="run", file="app/main.py", line=4),
            ],
            impact=[pr_mod.ImpactRef(name="run", file="app/main.py", line=4)],
            test_files=[],
            check_points=[pr_mod.CheckPoint(text="run 의 반환값이 바뀌는지 확인")],
        )

    return fake


@pytest.fixture
def coach_calls(monkeypatch, pr_mod) -> list:
    """PR 도구 로드·PR Coach 를 가짜로 바꾸고, 리뷰에 넘어간 (repo_url, number) 목록을 돌려준다."""
    calls = []
    monkeypatch.setattr(supervisor, "_run_pr_coach", _fake_coach(pr_mod, calls))
    monkeypatch.setattr(supervisor, "_load_pr_tools", lambda deps: {})
    return calls


def test_gate_sends_pr_url_to_pr(tmp_path, repo_url):
    """PR URL 이 있으면 LLM 이 ask 라 해도 코드 게이트가 pr 로 고치고, parse_pr_ref 가 (url, 12).

    URL 의 레포가 thread 레포(file://.../owner/name)와 같은 owner/name 이면 thread URL 을
    그대로 돌려줘 인덱스를 다시 열지 않는다. 다른 레포면 github.com URL 을 준다.
    """
    message = "https://github.com/owner/name/pull/12 리뷰해줘"
    assert _gate("ask", message, False)[0] == "pr"
    assert _gate("map", message, True)[0] == "pr"
    assert parse_pr_ref(message, "https://github.com/x/y") == ("https://github.com/owner/name", 12)
    assert parse_pr_ref(message, repo_url) == (repo_url, 12)
    assert parse_pr_ref("github.com/Owner/Name/pull/3", "https://github.com/owner/name") == (
        "https://github.com/owner/name",
        3,
    )
    deps = _deps(tmp_path, [Intent(kind="ask", reason="흐름 질문")])
    state = run_supervisor(deps, repo_url, message, "t1")  # 토큰 없음 → 안내로 끝난다
    deps.close()
    assert state["intent"].kind == "pr"
    assert _types(state) == ["IndexReady", "IntentDecided", "GateApplied", "Replied"]
    assert state["events"][2]["from"] == "ask" and state["events"][2]["to"] == "pr"


def test_pr_number_uses_current_repo():
    """ "PR #7 뭐 바꿨어" 는 현재 thread 레포의 7번.

    번호 없는 PR 말("PR 이 뭐야?")·pr 이 섞인 일반 단어("sprint 3")는 게이트에 걸리지 않는다.
    """
    url = "https://github.com/owner/name"
    assert parse_pr_ref("PR #7 뭐 바꿨어", url) == (url, 7)
    assert parse_pr_ref("pull 42 영향 알려줘", url) == (url, 42)
    assert parse_pr_ref("풀리퀘스트 #5 리뷰", url) == (url, 5)
    assert parse_pr_ref("#9 리뷰해줘", url) == (url, 9)  # 의도가 pr 일 때만 쓰는 느슨한 번호
    assert parse_pr_ref("pr 리뷰 해줘", url) is None
    assert _gate("chat", "PR #7 뭐 바꿨어", False)[0] == "pr"
    assert _gate("ask", "PR#7 에서 run() 바뀌었어?", False)[0] == "pr"
    assert _gate("map", "PR 이 뭐야?", False) is None
    assert _gate("map", "sprint 3 계획", False) is None


def test_pr_without_token_replies(tmp_path, repo_url, coach_calls):
    """토큰이 없으면 PR 도구·Coach 를 부르지 않고 토큰 안내(PR_NEED_TOKEN)만 준다."""
    deps = _deps(tmp_path, [Intent(kind="pr", reason="PR 리뷰")])
    state = run_supervisor(deps, repo_url, "PR #7 뭐 바꿨어", "t1")
    deps.close()
    assert state["reply"] == supervisor_v2.PR_NEED_TOKEN
    assert "GITHUB_PERSONAL_ACCESS_TOKEN" in state["reply"]
    assert state["pr_review"] is None and coach_calls == []
    assert _types(state) == ["IndexReady", "IntentDecided", "Replied"]


def test_pr_with_token_runs_coach(tmp_path, repo_url, coach_calls):
    """토큰이 있으면 현재 레포·번호로 PR Coach 를 부르고 pr_review·PRReviewReady·history 를 남긴다.

    체크포인터를 거쳐도 PRReview 가 되살아나야 하므로 같은 thread 의 다음 턴(chat)에서는
    결과 칸이 비고(turn_input) history 는 이어진다.
    """
    intents = [Intent(kind="pr", reason="PR 리뷰"), Intent(kind="chat", reason="인사")]
    deps = _deps(tmp_path, intents)
    deps.github_token = "test-token"
    state = run_supervisor(deps, repo_url, "PR #7 뭐 바꿨어", "t1")
    after = run_supervisor(deps, repo_url, "고마워", "t1")
    deps.close()
    assert coach_calls == [(repo_url, 7)]
    review = state["pr_review"]
    assert review.number == 7 and review.repo == "owner/name"
    assert len(review.changed_symbols) == 2 and len(review.impact) == 1
    assert _types(state) == ["IndexReady", "IntentDecided", "PRReviewReady"]
    assert state["events"][-1] == {"type": "PRReviewReady", "number": 7, "changed": 2, "impact": 1}
    assert state["history"][-2:] == [
        {"role": "user", "text": "PR #7 뭐 바꿨어"},
        {"role": "agent", "text": "PR #7 리뷰: 변경 심볼 2개, 영향 1개"},
    ]
    assert after["pr_review"] is None and len(after["history"]) == 4


def test_pr_without_number_asks_for_it(tmp_path, repo_url, coach_calls):
    """의도가 pr 인데 번호·URL 이 없으면 Coach 를 부르지 않고 PR_NEED_NUMBER 안내를 준다."""
    deps = _deps(tmp_path, [Intent(kind="pr", reason="PR 리뷰")])
    deps.github_token = "test-token"
    state = run_supervisor(deps, repo_url, "pr 리뷰 해줘", "t1")
    deps.close()
    assert state["reply"] == supervisor_v2.PR_NEED_NUMBER
    assert "#123" in state["reply"] and coach_calls == []
    assert _types(state) == ["IndexReady", "IntentDecided", "Replied"]


def test_pr_in_other_repo_opens_its_index(tmp_path, repo_url, coach_calls):
    """PR URL 의 레포가 thread 레포와 다르면 그 레포를 build_index 로 열어 Coach 에 넘긴다.

    네트워크 없이 확인하려고 다른 로컬 레포(other/repo)를 캐시 자리에 미리 clone 해 둔다
    (clone_repo 는 캐시 디렉터리가 있으면 clone 하지 않는다).
    """
    other = tmp_path / "src2" / "other" / "repo"
    other.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", repo_url, str(other)], check=True)
    cached = tmp_path / "cache" / "other" / "repo"
    cached.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", f"file://{other}", str(cached)], check=True)
    deps = _deps(tmp_path, [Intent(kind="map", reason="오판")])
    deps.github_token = "test-token"
    state = run_supervisor(deps, repo_url, "https://github.com/other/repo/pull/3 봐줘", "t1")
    deps.close()
    assert coach_calls == [("https://github.com/other/repo", 3)]
    assert state["pr_review"].repo == "other/repo" and state["intent"].kind == "pr"


def test_pr_end_to_end_with_real_coach(tmp_path, repo_url, monkeypatch):
    """실제 PR Coach 그래프까지 통째로: pr 게이트 → MCP 가짜 도구 → 정적 영향 → fast 설명.

    `_run_pr_coach` 를 가짜로 바꾸지 않는다. 도구 로드만 가짜(pull_request_read: get·get_files).
    app/util.py 의 helper 를 바꾼 PR 이면 변경 심볼은 helper, 호출자 영향은 app/main.py 의 run.
    LLM 체크포인트의 심볼 이름 run 에는 코드가 인용(app/main.py:4)을 붙인다.
    """
    import json

    from langchain_core.tools import BaseTool

    from repoute.graphs.pr_coach import CheckText, Explained

    def blocks(data):
        return [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]

    class FakePullRequestRead(BaseTool):
        name: str = "pull_request_read"
        description: str = "Read a pull request"

        def _run(self, **kwargs):
            raise NotImplementedError

        async def _arun(self, **kwargs):
            if kwargs["method"] == "get":
                return blocks(
                    {
                        "number": 12,
                        "title": "helper 반환값 수정",
                        "body": "2 를 돌려준다",
                        "html_url": "https://github.com/owner/name/pull/12",
                        "user": {"login": "a"},
                        "merged_at": None,
                    }
                )
            return blocks(
                [
                    {
                        "filename": "app/util.py",
                        "status": "modified",
                        "additions": 1,
                        "deletions": 1,
                        "patch": "@@ -1,2 +1,2 @@\n def helper():\n-    return 1\n+    return 2\n",
                    }
                ]
            )

    fast = FakeLLM(
        {
            Intent: [Intent(kind="ask", reason="오판")],  # 게이트가 pr 로 고친다
            ModuleSummary: _summaries(),
            Explained: [
                Explained(
                    summary="helper 가 2 를 돌려주게 바뀌었다.",
                    check_points=[CheckText(text="run 의 반환값도 바뀐다", symbol="run")],
                )
            ],
        }
    )
    deps = _deps(tmp_path, [], fast=fast)
    deps.github_token = "test-token"
    monkeypatch.setattr(
        supervisor,
        "_load_pr_tools",
        lambda d: d.__setattr__("pr_tools", {"pull_request_read": FakePullRequestRead()}),
    )
    state = run_supervisor(deps, repo_url, "https://github.com/owner/name/pull/12 뭐 바꿨어?", "t1")
    deps.close()
    review = state["pr_review"]
    assert review.number == 12 and review.errors == []
    assert [s.name for s in review.changed_symbols] == ["helper"]
    callers = [(i.name, i.file, i.line, i.via) for i in review.impact if i.relation == "caller"]
    assert callers == [("run", "app/main.py", 4, "helper")]
    assert review.check_points[0].refs[0].model_dump() == {
        "file": "app/main.py",
        "line": 4,
        "symbol": "run",
    }
    assert _types(state)[-1] == "PRReviewReady"
    assert state["events"][-1] == {
        "type": "PRReviewReady",
        "number": 12,
        "changed": 1,
        "impact": len(review.impact),
    }
