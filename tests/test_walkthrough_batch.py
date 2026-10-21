"""따라가기 배치 스크립트·데이터셋 테스트 (실제 LLM·네트워크 없음).

실행: uv run pytest tests/test_walkthrough_batch.py
"""

import importlib.util
import json
from pathlib import Path

from langchain_core.messages import AIMessage, ToolMessage

from repoute.graphs.walkthrough import Step, WalkthroughAnswer
from repoute.validation.citations import CodeRef

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "evals" / "experiments" / "walkthrough_batch.py"
DATASET = ROOT / "evals" / "datasets" / "walkthrough_questions.json"


def _load_script():
    """evals/ 는 패키지로 설치되지 않아 import 가 안 되므로 파일 경로로 직접 읽는다."""
    spec = importlib.util.spec_from_file_location("walkthrough_batch", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


batch = _load_script()


def _row(qid: str, repo: str, reached: bool, validity: float, reads: int = 2) -> dict:
    """run_one 이 돌려주는 모양의 가짜 결과 한 줄. reads=0 이면 엄격 통과 실패로 둔다."""
    return {
        "id": qid,
        "repo": repo,
        "question": "requests.get()을 호출하면 내부에서 무슨 일이 일어나?",
        "expect_reach": "Session.send",
        "agent_model": "fast",
        "prompt": "v3",
        "tool_calls": 10,
        "retries": 0,
        "blocked": 1,
        "read_code_calls": reads,
        "nudges": 0 if reads else 1,
        "explore_blocked": 0 if reads else 3,
        "final_errors": 0 if reads else 4,
        "strict_pass": bool(reads),
        "steps": 4,
        "citations": 4,
        "validity": validity,
        "specificity": validity,
        "at_def": 0.5,
        "reached": reached,
        "refs": ["a.py:1"],
        "tokens": {"gpt-4o-mini": {"input_tokens": 1000, "output_tokens": 100}},
        "cost": 0.02,
        "elapsed": 10.0,
    }


Q = "requests.get()을 호출하면 내부에서 무슨 일이 일어나?"  # _row 의 질문 (표 행 기대값에 쓴다)

ROWS = [
    _row("x-01", "a/x", True, 1.0),
    _row("x-02", "a/x", False, 0.5),
    _row("y-01", "b/y", True, 1.0),
    _row("y-02", "b/y", True, 1.0, reads=0),
]


def _answer(title="", explanation="", symbol=None) -> WalkthroughAnswer:
    """단계 하나짜리 가짜 답."""
    ref = CodeRef(file="a.py", line=1, symbol=symbol)
    step = Step(order=1, title=title, explanation=explanation, refs=[ref])
    return WalkthroughAnswer(question="q", steps=[step], summary="s")


def test_dataset_shape():
    """질문 20개, id 유일, 필드 4개가 모두 있고 레포는 4개다."""
    items = json.loads(DATASET.read_text(encoding="utf-8"))
    assert len(items) == 20
    assert len({it["id"] for it in items}) == 20
    for it in items:
        assert {"id", "repo", "question", "expect_reach"} <= it.keys()
        assert all(it[k] for k in ("id", "repo", "question", "expect_reach"))
    assert len({it["repo"] for it in items}) == 4


def test_render_markdown_has_sections():
    """제목·질문별 표 헤더·레포별 평균·전체 요약(n=4, 도달 3/4)이 들어간다.

    표 헤더에 "읽기"·"엄격"(기록 10 후속), "탐색차단"(기록 11 후속) 열이 추가돼 기대값을 바꿨다.
    """
    text = batch.render_markdown(ROWS, "10. 테스트 배치")
    assert text.startswith("# 10. 테스트 배치 (Phase 3, ")
    assert (
        "| id | 질문 | 도구 | 읽기 | 차단 | 탐색차단 | 단계 | 인용 | 엄격 | 유효 | 구체 | def줄 "
        "| 도달 | 비용 | 시간 |" in text
    )
    assert "| a/x | n=2 | 75% | 75% | 50% | 1/2 (50%) |" in text
    assert "## 전체 요약" in text
    assert "| n=4 | 88% | 88% | 50% | 3/4 (75%) | 10.0 | $0.0200 | 10.0s | $0.0800 |" in text
    assert "자동 근사치" in text and "wt-batch" in text
    assert "## 관찰" in text


def test_render_markdown_strict_and_unread():
    """질문별 표에 읽기 수·엄격 O/X 가, 전체 요약에 엄격 통과 비율과 read_code 0회 건수가 찍힌다.

    "탐색차단" 열이 "차단" 뒤에 들어가 행 기대값에 그 칸(0)을 넣었다 (기록 11 후속).
    """
    text = batch.render_markdown(ROWS)
    assert f"| y-01 | {Q} | 10 | 2 | 1 | 0 | 4 | 4 | O |" in text
    assert f"| y-02 | {Q} | 10 | 0 | 1 | 3 | 4 | 4 | X |" in text
    assert "- **엄격 통과 비율**: 3/4 (75%)" in text
    assert "- **read_code 0회 건수**: 1/4" in text
    assert "엄격 통과를 주 지표로 본다" in text


def test_render_markdown_explore_blocked():
    """질문별 표의 "탐색차단" 칸에 거부 수가, 요약에 탐색 차단 발동 건수(1/4)가 찍힌다."""
    text = batch.render_markdown(ROWS)
    assert "| 차단 | 탐색차단 |" in text
    assert f"| y-02 | {Q} | 10 | 0 | 1 | 3 |" in text
    assert "- **탐색 차단 발동 건수**: 1/4" in text
    assert "**탐색차단**" in text  # 방법 절에 정의가 있다


def test_read_code_calls_skips_blocked_and_unrun():
    """실행된 read_code 만 센다: 차단("이미 읽은 범위")·상한 미실행·다른 도구 응답은 뺀다."""
    calls = [
        {"name": "read_code", "args": {}, "id": "r1"},
        {"name": "read_code", "args": {}, "id": "r2"},
        {"name": "read_code", "args": {}, "id": "r3"},
        {"name": "find_definition", "args": {}, "id": "f1"},
    ]
    messages = [
        AIMessage(content="", tool_calls=calls),
        ToolMessage("1 def run():", tool_call_id="r1"),
        ToolMessage("이미 읽은 범위입니다 (a.py 1~5줄).", tool_call_id="r2"),
        ToolMessage("실행 안 함: 상한 초과·없는 도구", tool_call_id="r3"),
        ToolMessage("a.py:1", tool_call_id="f1"),
    ]
    assert batch.read_code_calls(messages) == 1


def test_render_markdown_lists_failures():
    """실패한 질문은 표 대신 '실패' 절에 남고 n 에서 빠진다."""
    failed = {"id": "z-01", "repo": "c/z", "prompt": "v3", "error": "RuntimeError: clone 실패"}
    text = batch.render_markdown([*ROWS, failed])
    assert "- z-01 (c/z): RuntimeError: clone 실패" in text
    assert "| n=4 |" in text


def test_reached_title():
    """기대 심볼이 단계 제목에 있으면 도달."""
    assert batch.reached(_answer(title="Session.send() 가 어댑터로 보낸다"), "Session.send")


def test_reached_explanation():
    """기대 심볼이 설명에만 있어도 도달."""
    assert batch.reached(_answer(explanation="마지막에 Session.send 를 부른다"), "Session.send")


def test_reached_symbol():
    """기대 심볼이 인용 심볼에 있어도 도달."""
    assert batch.reached(_answer(title="전송", symbol="Session.send"), "Session.send")


def test_reached_missing_is_false():
    """어디에도 없으면 False (짧은 심볼 "send" 는 기대 이름을 포함하지 않으므로 미도달)."""
    answer = _answer(title="request 가 준비한다", explanation="그 뒤 전송", symbol="send")
    assert not batch.reached(answer, "Session.send")


def _messages_with_blocks() -> list:
    """find 2회 → find(탐색 차단) + read 실행 → 같은 범위 read(차단) 순서의 가짜 대화."""
    first = [
        {"name": "find_definition", "args": {}, "id": "f1"},
        {"name": "find_references", "args": {}, "id": "f2"},
    ]
    second = [
        {"name": "find_definition", "args": {}, "id": "f3"},
        {"name": "read_code", "args": {}, "id": "r1"},
    ]
    third = [{"name": "read_code", "args": {}, "id": "r2"}]
    return [
        AIMessage(content="", tool_calls=first),
        ToolMessage("a.py:1", tool_call_id="f1"),
        ToolMessage("a.py:3", tool_call_id="f2"),
        AIMessage(content="", tool_calls=second),
        ToolMessage("읽기 없이 탐색만 6회 했습니다. 이제 read_code 로 ...", tool_call_id="f3"),
        ToolMessage("1 def run():", tool_call_id="r1"),
        AIMessage(content="", tool_calls=third),
        ToolMessage("이미 읽은 범위입니다 (a.py 1~5줄).", tool_call_id="r2"),
    ]


def test_tool_sequence_marks_blocked_calls():
    """도구 이름이 호출 순서대로 나오고, 실행 안 된 호출(탐색 차단·이미 읽은 범위)은 !blocked."""
    assert batch.tool_sequence(_messages_with_blocks()) == [
        "find_definition",
        "find_references",
        "find_definition!blocked",
        "read_code",
        "read_code!blocked",
    ]


def test_tool_sequence_marks_unanswered_and_error():
    """답 ToolMessage 가 없는 호출은 !blocked, "도구 오류" 응답은 !error 로 표시한다."""
    calls = [
        {"name": "read_code", "args": {}, "id": "r1"},
        {"name": "read_code", "args": {}, "id": "r2"},
    ]
    messages = [
        AIMessage(content="", tool_calls=calls),
        ToolMessage("도구 오류: x", tool_call_id="r1"),
    ]
    assert batch.tool_sequence(messages) == ["read_code!error", "read_code!blocked"]


def test_calls_before_first_read_counts_blocked_attempts():
    """첫 실행된 read_code 앞의 호출 수: 차단된 find 도 agent 의 시도로 센다 (3)."""
    sequence = batch.tool_sequence(_messages_with_blocks())
    assert batch.calls_before_first_read(sequence) == 3


def test_calls_before_first_read_none_without_read():
    """실행된 read_code 가 없으면(차단·오류 read 만 있어도) None."""
    assert batch.calls_before_first_read(["find_definition", "find_references"]) is None
    assert batch.calls_before_first_read(["find_definition", "read_code!error"]) is None
    assert batch.calls_before_first_read([]) is None


def test_chain_accuracy_none_without_expect_chain():
    """item 에 expect_chain 이 없거나 비면 None (0.0 이 아니라 '모름')."""
    refs = [("a.py", "Session.request"), ("a.py", "Session.send")]
    assert batch.chain_accuracy(refs, {"expect_reach": "Session.send"}) is None
    assert batch.chain_accuracy(refs, {"expect_chain": []}) is None


def test_chain_accuracy_uses_metrics():
    """expect_chain 이 있으면 인용 순서 체인과의 LCS 비율 (끝 이름 정규화): 2/3."""
    refs = [("a.py", "request"), ("a.py", "request"), ("a.py", "Session.send"), ("b.py", None)]
    item = {"expect_chain": ["Session.request", "Session.prepare_request", "Session.send"]}
    assert batch.chain_accuracy(refs, item) == 2 / 3


def test_reached_tail_accepts_short_name():
    """끝 이름 매칭: 인용 심볼 "prepare_body" 로 "PreparedRequest.prepare_body" 도달 인정.

    기존 reached(문자열 포함)는 같은 답을 미도달로 본다 — 두 값이 다른 경우.
    """
    answer = _answer(title="본문 준비", symbol="prepare_body")
    item = {"expect_reach": "PreparedRequest.prepare_body"}
    assert batch.reached_tail(answer, item)
    assert not batch.reached(answer, item["expect_reach"])


NEW = {"tool_sequence": ["find_definition", "read_code"], "reached_tail": True}


def test_render_markdown_first_read_and_chain():
    """새 지표가 있는 raw 면 요약 표 끝에 첫읽기까지(읽기 없음 건수)·체인 평균 칸이 붙는다."""
    rows = [
        {**ROWS[0], **NEW, "calls_before_first_read": 1, "chain_accuracy": 0.5},
        {**ROWS[1], **NEW, "calls_before_first_read": 3, "chain_accuracy": 1.0},
        {**ROWS[2], **NEW, "calls_before_first_read": None, "chain_accuracy": None},
    ]
    text = batch.render_markdown(rows, dataset="d.json", tag="wt-test")
    assert "| 비용 합 | 첫읽기까지 | 체인 |" in text
    assert "| a/x | n=2 | 75% | 75% | 50% | 1/2 (50%) |" in text  # 기존 칸 순서 유지
    assert "| 2.0 (읽기 없음 0건) | 0.75 |" in text  # a/x 레포 평균
    assert "| 2.0 (읽기 없음 1건) | 0.75 (n=2) |" in text  # 전체
    assert "- x-01: find_definition → read_code" in text
    assert "`d.json`" in text and "`wt-test`" in text
    assert "- **끝 이름 도달**: 3/3 (100%) (기존 도달과 다른 질문: x-02)" in text


def test_render_markdown_old_rows_show_dash():
    """새 지표가 없던 raw(기록 10~13)도 그대로 렌더되고 새 칸은 "—" 다."""
    text = batch.render_markdown(ROWS)
    assert "| — | — |" in text
    assert "## 도구 호출 순서" not in text
