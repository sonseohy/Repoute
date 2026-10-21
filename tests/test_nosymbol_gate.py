"""이름 없는 질문 게이트 측정 스크립트(nosymbol_gate.py) 테스트 (실제 LLM 없음).

실행: uv run pytest tests/test_nosymbol_gate.py
"""

import importlib.util
from pathlib import Path

from repoute.graphs.supervisor import Intent

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "evals" / "experiments" / "nosymbol_gate.py"
DATASET = ROOT / "evals" / "datasets" / "walkthrough_questions_nosymbol.json"


def _load_script():
    """evals/ 는 패키지로 설치되지 않아 import 가 안 되므로 파일 경로로 직접 읽는다."""
    spec = importlib.util.spec_from_file_location("nosymbol_gate", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = _load_script()
ITEMS = gate.load_dataset(DATASET)


class FakeLLM:
    """Intent 큐만 가진 가짜 fast 모델. 받은 프롬프트·config 를 기록한다."""

    def __init__(self, queue: list[Intent]):
        self.queue = list(queue)
        self.prompts: list[str] = []
        self.configs: list = []

    def with_structured_output(self, schema):
        assert schema is Intent
        return self

    def invoke(self, prompt, config=None):
        self.prompts.append(prompt)
        self.configs.append(config)
        return self.queue.pop(0)


def test_all_ask_are_gated_to_map():
    """LLM 이 10개 모두 ask 라고 해도 식별자가 없어 게이트가 전부 map 으로 내린다 (현재 동작)."""
    llm = FakeLLM([Intent(kind="ask", reason="흐름 질문")] * len(ITEMS))
    rows = [gate.run_one(llm, item) for item in ITEMS]
    assert len(rows) == 10
    assert all(r["llm_kind"] == "ask" and r["final_kind"] == "map" for r in rows)
    assert all("이름이 없어" in r["gate_reason"] for r in rows)
    assert all(r["symbol_match"] is None for r in rows)
    assert not llm.queue


def test_run_one_passes_tag_and_no_history():
    """LangSmith 태그(기본 wt-nosymbol-gate)와 id 를 넘기고, 기억 없음으로 분류한다."""
    llm = FakeLLM([Intent(kind="map", reason="구조")])
    row = gate.run_one(llm, ITEMS[0])
    assert llm.configs[0]["tags"] == ["wt-nosymbol-gate", ITEMS[0]["id"]]
    assert ITEMS[0]["question"] in llm.prompts[0]
    assert row["final_kind"] == "map" and row["gate_reason"] is None  # 게이트가 바꾸지 않음


def test_non_ask_kinds_pass_through_gate():
    """지도 없음이라 next 는 map 으로, chat 은 그대로 (게이트는 ask·next 만 건드린다)."""
    llm = FakeLLM([Intent(kind="next", reason="다음"), Intent(kind="chat", reason="잡담")])
    rows = [gate.run_one(llm, item) for item in ITEMS[:2]]
    assert [r["final_kind"] for r in rows] == ["map", "chat"]
    assert rows[0]["gate_reason"] is not None and rows[1]["gate_reason"] is None


def _row(rid: str, llm_kind: str, final_kind: str, cost: float | None = 0.0001) -> dict:
    """run_one 이 돌려주는 모양의 가짜 결과 한 줄."""
    return {
        "id": rid,
        "repo": "a/x",
        "question": "리디렉트를 너무 많이 따라가면 어디서 예외가 나?",
        "llm_kind": llm_kind,
        "final_kind": final_kind,
        "gate_reason": "게이트" if llm_kind != final_kind else None,
        "symbol_match": None,
        "tokens": {},
        "cost": cost,
        "elapsed": 1.0,
    }


def test_summarize_counts():
    """ask 도달 수·LLM 분포·게이트 발동·비용 합을 모으고 실패 행은 n 에서 뺀다."""
    rows = [
        _row("a", "ask", "map"),
        _row("b", "ask", "map"),
        _row("c", "map", "map"),
        _row("d", "ask", "ask"),
        {"id": "e", "question": "q", "error": "RuntimeError: x"},
    ]
    s = gate.summarize(rows)
    assert (s["n"], s["failed"]) == (4, 1)
    assert s["reached_ask"] == 1 and s["llm_ask"] == 3
    assert s["llm_kinds"] == {"map": 1, "ask": 3, "next": 0, "chat": 0}
    assert s["final_kinds"]["map"] == 3
    assert s["gate_applied"] == 2
    assert s["symbol_matches"] == []
    assert abs(s["total_cost"] - 0.0004) < 1e-12


def test_summarize_unknown_cost_is_none():
    """비용을 모르는 행이 하나라도 있으면 합은 None (측정 안 한 수치 금지)."""
    assert gate.summarize([_row("a", "ask", "map", cost=None)])["total_cost"] is None


def test_render_table_has_summary_line():
    """표 헤더·질문 앞 30자·이유 칸과 'ask 로 따라가기에 도달한 수 k/총' 줄이 찍힌다."""
    rows = [_row("a", "ask", "map"), _row("b", "map", "map")]
    text = gate.render_table(rows, gate.summarize(rows))
    assert "| id | 질문 | LLM | 게이트 후 | 이유 |" in text
    assert "| a | 리디렉트를 너무 많이 따라가면 어디서 예외가 나? | ask | map | 게이트 |" in text
    assert "| b | 리디렉트를 너무 많이 따라가면 어디서 예외가 나? | map | map | — |" in text
    assert "- **ask 로 따라가기에 도달한 수**: 0/2" in text
    assert "SYMBOL_PATTERN 매치: 없음 (전부 None)" in text
