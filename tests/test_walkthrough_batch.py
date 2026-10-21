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

    표 헤더에 "읽기"·"엄격" 열이 추가돼 헤더 문자열 기대값을 바꿨다 (기록 10 후속).
    """
    text = batch.render_markdown(ROWS, "10. 테스트 배치")
    assert text.startswith("# 10. 테스트 배치 (Phase 3, ")
    assert (
        "| id | 질문 | 도구 | 읽기 | 차단 | 단계 | 인용 | 엄격 | 유효 | 구체 | def줄 | 도달 "
        "| 비용 | 시간 |" in text
    )
    assert "| a/x | n=2 | 75% | 75% | 50% | 1/2 (50%) |" in text
    assert "## 전체 요약" in text
    assert "| n=4 | 88% | 88% | 50% | 3/4 (75%) | 10.0 | $0.0200 | 10.0s | $0.0800 |" in text
    assert "자동 근사치" in text and "wt-batch" in text
    assert "## 관찰" in text


def test_render_markdown_strict_and_unread():
    """질문별 표에 읽기 수·엄격 O/X 가, 전체 요약에 엄격 통과 비율과 read_code 0회 건수가 찍힌다."""
    text = batch.render_markdown(ROWS)
    assert (
        "| y-01 | requests.get()을 호출하면 내부에서 무슨 일이 일어나? | 10 | 2 | 1 | 4 | 4 | O |"
        in text
    )
    assert (
        "| y-02 | requests.get()을 호출하면 내부에서 무슨 일이 일어나? | 10 | 0 | 1 | 4 | 4 | X |"
        in text
    )
    assert "- **엄격 통과 비율**: 3/4 (75%)" in text
    assert "- **read_code 0회 건수**: 1/4" in text
    assert "엄격 통과를 주 지표로 본다" in text


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
