"""Supervisor 의도 분류 데이터셋·스크립트 테스트 (실제 LLM 없음).

실행: uv run pytest tests/test_supervisor_intents.py
"""

import importlib.util
from collections import Counter
from pathlib import Path

from repoute.graphs.supervisor import Intent

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "evals" / "experiments" / "supervisor_intents.py"
DATASET = ROOT / "evals" / "datasets" / "supervisor_intents.json"
GUIDE_HISTORY = [
    {"role": "user", "text": "어디서부터 봐야 해?"},
    {"role": "agent", "text": "읽기 지도 8단계를 보여 줌 (1단계: src/requests/api.py)"},
]


def _load_script():
    """evals/ 는 패키지로 설치되지 않아 import 가 안 되므로 파일 경로로 직접 읽는다."""
    spec = importlib.util.spec_from_file_location("supervisor_intents", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


intents = _load_script()


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


def _row(rid: str, expected: str, llm_kind: str, final_kind: str, **extra) -> dict:
    """run_one 이 돌려주는 모양의 가짜 결과 한 줄. 게이트가 바꿨으면 이유를 채운다."""
    return {
        "id": rid,
        "message": "m",
        "has_guide": False,
        "expected": expected,
        "llm_kind": llm_kind,
        "final_kind": final_kind,
        "gate_reason": "게이트" if llm_kind != final_kind else None,
        "llm_correct": llm_kind == expected,
        "final_correct": final_kind == expected,
        "symbolless_ask": False,
        "tokens": {},
        "cost": 0.001,
        "elapsed": 1.0,
        **extra,
    }


def test_dataset_shape():
    """데이터셋은 30개, id 중복 없음, 필드가 다 있고 expected 는 네 종류 안이다."""
    items = intents.load_dataset(DATASET)
    assert len(items) == 30
    assert len({it["id"] for it in items}) == 30
    keys = {"id", "message", "has_guide", "history", "expected", "note"}
    for it in items:
        assert keys <= it.keys(), it["id"]
        assert it["expected"] in {"map", "ask", "chat", "next"}
        assert isinstance(it["has_guide"], bool)
        assert all(h["role"] in {"user", "agent"} and h["text"] for h in it["history"])
        assert it["note"].strip()


def test_dataset_distribution():
    """분포는 map 9 / ask 9 / next 6 / chat 6 이다."""
    counts = Counter(it["expected"] for it in intents.load_dataset(DATASET))
    assert counts == {"map": 9, "ask": 9, "next": 6, "chat": 6}


def test_dataset_next_needs_guide():
    """next 라벨은 모두 지도가 있는 대화다 (지도 없는 '다음' 은 map 으로 라벨)."""
    items = intents.load_dataset(DATASET)
    assert all(it["has_guide"] for it in items if it["expected"] == "next")
    bare_next = [it for it in items if it["message"] == "다음" and not it["has_guide"]]
    assert [it["expected"] for it in bare_next] == ["map"]


def test_dataset_symbolless_ask_two():
    """함수 이름 없는 ask 는 정확히 2개이고 note 에 Phase 5.5-4 를 적었다."""
    items = [it for it in intents.load_dataset(DATASET) if intents.is_symbolless_ask(it)]
    assert len(items) == 2
    assert all("Phase 5.5-4" in it["note"] for it in items)


def test_classify_message_gate_fixes_next():
    """지도가 있고 '다음' 으로 시작하면 LLM 이 map 이라 해도 게이트가 next 로 고친다."""
    llm = FakeLLM([Intent(kind="map", reason="순서를 묻는다")])
    config = {"tags": ["t"]}
    got = intents.classify_message(llm, "다음 파일 알려줘", GUIDE_HISTORY, True, config)
    llm_kind, final_kind, reason = got
    assert (llm_kind, final_kind) == ("map", "next")
    assert reason and "다음" in reason
    # 프롬프트에 기억과 메시지가 들어가고, config 는 그대로 넘어간다
    assert "src/requests/api.py" in llm.prompts[0] and "다음 파일 알려줘" in llm.prompts[0]
    assert llm.configs == [config]


def test_classify_message_next_without_guide_is_map():
    """지도가 없는데 LLM 이 next 라 하면 게이트가 map 으로 내린다."""
    llm = FakeLLM([Intent(kind="next", reason="다음 단계")])
    assert intents.classify_message(llm, "그다음은?", [], False)[:2] == ("next", "map")
    assert "(없음)" in llm.prompts[0]  # 빈 기억은 NO_HISTORY 로


def test_classify_message_no_change():
    """게이트가 바꾸지 않으면 두 kind 가 같고 이유는 None 이다."""
    llm = FakeLLM([Intent(kind="ask", reason="메서드 흐름")])
    got = intents.classify_message(llm, "Session.send() 가 뭐해?", [], False)
    assert got == ("ask", "ask", None)


def test_summarize_accuracy_and_confusion():
    """정확도·kind 별 맞은 수·혼동 행렬·게이트 효과를 작은 가짜 결과로 확인한다."""
    rows = [
        _row("a", "map", "map", "map"),
        _row("b", "next", "map", "next"),  # 게이트가 고침
        _row("c", "ask", "ask", "map", symbolless_ask=True),  # 게이트가 망침
        _row("d", "chat", "ask", "ask"),  # 둘 다 틀림
        {"id": "e", "message": "m", "expected": "map", "error": "Boom: x"},  # 실패는 제외
    ]
    s = intents.summarize(rows)
    assert (s["n"], s["failed"]) == (4, 1)
    assert (s["llm_correct"], s["final_correct"]) == (2, 2)
    assert s["llm_accuracy"] == s["final_accuracy"] == 0.5
    assert s["per_kind"]["next"] == {"total": 1, "llm_correct": 0, "final_correct": 1}
    assert s["per_kind"]["ask"] == {"total": 1, "llm_correct": 1, "final_correct": 0}
    assert s["confusion"]["ask"]["map"] == 1 and s["confusion"]["chat"]["ask"] == 1
    assert sum(sum(c.values()) for c in s["confusion"].values()) == 4
    assert (s["gate_applied"], s["gate_fixed"], s["gate_broke"]) == (2, 1, 1)
    assert [r["id"] for r in s["symbolless_ask"]] == ["c"]
    assert abs(s["total_cost"] - 0.004) < 1e-9 and abs(s["avg_cost"] - 0.001) < 1e-9
    assert s["avg_elapsed"] == 1.0


def test_summarize_unknown_cost_is_none():
    """비용을 모르는 행이 하나라도 있으면 합·평균은 0 이 아니라 None 이다."""
    s = intents.summarize([_row("a", "map", "map", "map", cost=None)])
    assert s["total_cost"] is None and s["avg_cost"] is None


def test_render_markdown_has_tables():
    """마크다운에 정확도 줄, 혼동 행렬, 함수 이름 없는 ask 표, 실패 목록이 들어간다."""
    rows = [
        _row("a", "map", "map", "map"),
        _row("c", "ask", "ask", "map", symbolless_ask=True),
        {"id": "e", "message": "m", "expected": "map", "error": "Boom: x"},
    ]
    md = intents.render_markdown(rows, intents.summarize(rows), "gpt-4o-mini")
    assert "LLM 단독 정확도**: 2/2 (100%)" in md
    assert "게이트 후 정확도**: 1/2 (50%)" in md
    assert "| ask | 1 | 0 | 0 | 0 |" in md  # 혼동 행렬: ask 를 map 으로 보냄
    assert "| c | m | ask | map |" in md
    assert "- e: Boom: x" in md


def test_dry_run_prints_estimate(capsys):
    """--dry-run 은 LLM 없이 항목 수·호출 수·대략 비용만 찍는다 (30 × (500×0.15+40×0.6)/1M)."""
    intents.dry_run(intents.load_dataset(DATASET))
    out = capsys.readouterr().out
    assert "항목 30개" in out and "호출 30회" in out
    assert "$0.0030" in out
