"""API 테스트 (실제 LLM·네트워크 없음). 실행: uv run pytest tests/test_api.py

가짜 LLM 은 test_supervisor 의 FakeLLM 과 같은 것을 쓰고, 레포는 tmp_path 의 로컬 git 레포다.
"""

import json
import subprocess

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from repoute.api.app import create_app
from repoute.graphs.reading_guide import ModuleSummary, ReadingGuide, ReadingStep
from repoute.graphs.supervisor import Intent, SupervisorDeps
from repoute.graphs.walkthrough import CHECKPOINT_TYPES, Step, WalkthroughAnswer
from repoute.validation.citations import CodeRef

FILES = {
    "app/__init__.py": "",
    "app/main.py": "from app.util import helper\n\n\ndef run():\n    return helper()\n",
    "app/util.py": "def helper():\n    return 1\n",
}
RUN = CodeRef(file="app/main.py", line=4, symbol="run")
HELPER = CodeRef(file="app/util.py", line=1, symbol="helper")


class FakeLLM:
    """test_supervisor 의 FakeLLM 복사본. 구조화 출력은 스키마별 큐, 도구 대화는 chat 큐."""

    class _Structured:
        """스키마 하나의 큐."""

        def __init__(self, queue: list):
            self.queue = queue

        def invoke(self, prompt):
            return self.queue.pop(0)

    def __init__(self, structured: dict | None = None, chat: list | None = None):
        self.structured = {k: list(v) for k, v in (structured or {}).items()}
        self.chat = list(chat or [])

    def with_structured_output(self, schema):
        return self._Structured(self.structured[schema])

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        return self.chat.pop(0)


class FakeFeedbackClient:
    """langsmith.Client 대역. create_feedback 호출 인자만 기록한다."""

    def __init__(self):
        self.calls = []

    def create_feedback(self, run_id, **kwargs):
        self.calls.append((run_id, kwargs))


@pytest.fixture
def repo_url(tmp_path):
    """tmp_path/owner/name 에 FILES 를 커밋한 로컬 git 레포의 file:// URL."""
    origin = tmp_path / "owner" / "name"
    for rel, text in FILES.items():
        (origin / rel).parent.mkdir(parents=True, exist_ok=True)
        (origin / rel).write_text(text)
    git = ["git", "-C", str(origin)]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    commit = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
    subprocess.run([*git, *commit, "commit", "-q", "-m", "init"], check=True)
    return f"file://{origin}"


def _client(tmp_path, intents=(), quality=None, feedback=None) -> TestClient:
    """가짜 LLM·메모리 체크포인터를 꽂은 앱의 TestClient."""
    summaries = [ModuleSummary(module="m", file="f.py", summary="요약", role="core")] * 5
    fast = FakeLLM({Intent: list(intents), ModuleSummary: summaries})
    saver = InMemorySaver(serde=JsonPlusSerializer(allowed_msgpack_modules=CHECKPOINT_TYPES))
    deps = SupervisorDeps(fast, quality or FakeLLM(), tmp_path / "cache", saver)
    return TestClient(create_app(deps, feedback_client=feedback))


def _sse(text: str) -> list[tuple[str, dict]]:
    """SSE 본문을 (event, data) 목록으로 나눈다."""
    out = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        out.append((lines["event"], json.loads(lines["data"])))
    return out


def _update_types(events) -> list[str]:
    """update 이벤트들의 events[].type 을 순서대로."""
    return [e["type"] for name, data in events if name == "update" for e in data["events"]]


def test_health(tmp_path):
    """/health 는 LLM 없이 ok 를 돌려준다."""
    with _client(tmp_path) as client:
        assert client.get("/health").json() == {"status": "ok"}


def test_index_repo(tmp_path, repo_url):
    """POST /repos 는 로컬 레포의 규모와 핵심 모듈(최대 5개)을 돌려주고, 잘못된 URL 은 400."""
    with _client(tmp_path) as client:
        body = client.post("/repos", json={"url": repo_url}).json()
        assert body["repo_id"].startswith("owner/name@")
        assert body["files"] == 3 and body["cached"] is False
        assert 0 < len(body["core_modules"]) <= 5
        bad = client.post("/repos", json={"url": "https://example.com/x"})
    assert bad.status_code == 400
    assert "알아볼 수 없음" in bad.json()["detail"]


def test_read_file(tmp_path, repo_url):
    """코드 뷰어는 줄 번호와 함께 읽고, 레포 밖 경로는 400, 없는 파일은 404."""
    with _client(tmp_path) as client:
        client.post("/repos", json={"url": repo_url})
        body = client.get("/repos/owner/name/files?path=app/main.py&start=1&end=5").json()
        escape = client.get("/repos/owner/name/files?path=../x")
        missing = client.get("/repos/owner/name/files?path=app/none.py")
    assert [line["n"] for line in body["lines"]] == [1, 2, 3, 4, 5]
    assert body["lines"][3]["text"] == "def run():"
    assert escape.status_code == 400
    assert missing.status_code == 404


def test_run_map_streams(tmp_path, repo_url):
    """map 의도 실행은 update(IndexReady·IntentDecided·GuideReady) → result(guide) → done 순서다."""
    steps = [
        ReadingStep(order=1, file="app/util.py", reason="의존성 없음", ref=HELPER),
        ReadingStep(order=2, file="app/main.py", reason="진입점", ref=RUN),
    ]
    guide = ReadingGuide(overview="작은 앱", entrypoints=[RUN], reading_order=steps)
    quality = FakeLLM({ReadingGuide: [guide]})
    with _client(tmp_path, [Intent(kind="map", reason="구조")], quality) as client:
        res = client.post("/threads/t1/runs", json={"repo_url": repo_url, "message": "어디부터?"})
    assert res.headers["content-type"].startswith("text/event-stream")
    events = _sse(res.text)
    assert _update_types(events) == ["IndexReady", "IntentDecided", "GuideReady"]
    result = dict(events)["result"]
    assert result["guide"]["reading_order"][0]["file"] == "app/util.py"
    assert result["intent"]["kind"] == "map" and result["answer"] is None
    assert result["thread_id"] == "t1" and result["run_id"] is None  # 테스트는 트레이싱 꺼짐
    assert events[-1][0] == "done"


def test_run_ask_streams(tmp_path, repo_url):
    """ask 의도 실행은 result 에 따라가기 답(answer)이 담긴다."""
    read = {"name": "read_code", "args": {"file": "app/main.py", "start": 1, "end": 5}, "id": "c0"}
    chat = [AIMessage(content="", tool_calls=[read]), AIMessage(content="답")]
    step = Step(order=1, title="run() 이 helper() 를 부른다", explanation="설명", refs=[RUN])
    answer = WalkthroughAnswer(question="run 은?", steps=[step], summary="요약")
    quality = FakeLLM({WalkthroughAnswer: [answer]}, chat)
    with _client(tmp_path, [Intent(kind="ask", reason="흐름")], quality) as client:
        res = client.post("/threads/t1/runs", json={"repo_url": repo_url, "message": "run() 은?"})
    events = _sse(res.text)
    assert "AnswerReady" in _update_types(events)
    assert dict(events)["result"]["answer"]["steps"][0]["refs"][0]["line"] == 4
    assert events[-1][0] == "done"


def test_run_bad_url_is_error_event(tmp_path):
    """인덱싱 실패(잘못된 URL)는 HTTP 오류가 아니라 SSE error 이벤트로 오고 done 으로 끝난다."""
    with _client(tmp_path) as client:
        res = client.post("/threads/t1/runs", json={"repo_url": "nope", "message": "안녕"})
    events = _sse(res.text)
    assert [name for name, _ in events] == ["error", "done"]
    assert "알아볼 수 없음" in events[0][1]["message"]


def test_feedback(tmp_path):
    """피드백은 주입한 client 의 create_feedback 로 가고, run_id 가 없으면 400."""
    fake = FakeFeedbackClient()
    with _client(tmp_path, feedback=fake) as client:
        body = {"run_id": "r1", "score": 0, "comment": "줄이 틀림", "kind": "citation_error"}
        ok = client.post("/feedback", json=body)
        thumbs = client.post("/feedback", json={"run_id": "r2", "score": 1})
        missing = client.post("/feedback", json={"score": 1})
    assert ok.status_code == 200 and thumbs.status_code == 200
    assert fake.calls[0] == ("r1", {"key": "citation_error", "score": 0, "comment": "줄이 틀림"})
    assert fake.calls[1] == ("r2", {"key": "user", "score": 1, "comment": None})
    assert missing.status_code == 400
