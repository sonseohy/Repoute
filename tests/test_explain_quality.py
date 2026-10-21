"""/explain 품질 측정 스크립트의 파싱·자동 지표 테스트 (실제 LLM·네트워크 없음).

실행: uv run pytest tests/test_explain_quality.py
"""

import importlib.util
import json
from pathlib import Path

from repoute.indexing.explain import Explanation, SymbolSpan

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "evals" / "experiments" / "explain_quality.py"
DATASET = ROOT / "evals" / "datasets" / "walkthrough_questions.json"


def _load_script():
    """evals/ 는 패키지로 설치되지 않아 import 가 안 되므로 파일 경로로 직접 읽는다."""
    spec = importlib.util.spec_from_file_location("explain_quality", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


eq = _load_script()

SOURCE = '''def total(items):
    """항목 가격의
    합계를 돌려준다."""
    if not items:
        return 0
    return sum(i.price for i in items)
'''
LINES = SOURCE.splitlines()
SPAN = SymbolSpan("total", "function", "shop.py", 1, 6, "항목 가격의\n합계를 돌려준다.")
NO_DOC = SymbolSpan("total", "function", "shop.py", 1, 6, None)


def test_parse_evidence():
    """앞부분 "path:line" 만 뽑고, "87·71" 처럼 여러 줄이면 첫 줄, 형식이 아니면 None."""
    assert eq.parse_evidence("src/requests/api.py:87 get → request") == (
        "src/requests/api.py",
        87,
    )
    assert eq.parse_evidence("src/requests/api.py:87·71 get → request") == (
        "src/requests/api.py",
        87,
    )
    assert eq.parse_evidence("httpx/_api.py:195·109, _client.py:825 get") == ("httpx/_api.py", 195)
    assert eq.parse_evidence("@command 가 Command 를 만든다") is None


def test_evidence_locations_dedup_and_dataset():
    """레포별로 순서를 지키며 중복 (파일, 줄)을 빼고, 실제 데이터셋은 4개 레포 모두 위치가 있다."""
    items = [
        {"repo": "a/b", "chain_evidence": ["x.py:1 f", "x.py:2 g", "설명만"]},
        {"repo": "a/b", "chain_evidence": ["x.py:1 f", "y.py:3 h"]},
        {"repo": "c/d", "chain_evidence": ["z.py:9 k"]},
    ]
    assert eq.evidence_locations(items) == {
        "a/b": [("x.py", 1), ("x.py", 2), ("y.py", 3)],
        "c/d": [("z.py", 9)],
    }
    real = eq.evidence_locations(json.loads(DATASET.read_text(encoding="utf-8")))
    assert len(real) == 4 and all(len(locs) >= eq.PER_REPO for locs in real.values())


def test_line_refs():
    """ "(줄 N)" 은 (N, N), "(줄 N~M)" 은 (N, M), 참조가 없으면 빈 목록."""
    assert eq.line_refs("합계 계산 (줄 6)") == [(6, 6)]
    assert eq.line_refs("빈 목록 분기 (줄 4~5)") == [(4, 5)]
    assert eq.line_refs("sum 호출") == []


def test_ref_validity_and_has_ref_rate():
    """범위 밖·참조 없는 항목은 무효, has_ref_rate 는 참조 있는 항목 비율. 빈 watch 는 None."""
    watch = ["합계 (줄 6)", "분기 (줄 4~5)", "범위 밖 (줄 9)", "참조 없음"]
    assert eq.ref_validity(watch, SPAN) == 0.5
    assert eq.has_ref_rate(watch) == 0.75
    assert eq.ref_validity(["걸침 (줄 5~7)"], SPAN) == 0.0  # 끝이 범위 밖
    assert eq.ref_validity([], SPAN) is None and eq.has_ref_rate([]) is None


def test_count_ok():
    """watch 는 2~4개일 때만 True."""
    assert [eq.count_ok(["a"] * n) for n in (1, 2, 4, 5)] == [False, True, True, False]


def test_docstring_ko_ok():
    """docstring 있으면 docstring_ko 가 비어 있지 않아야, 없으면 비어 있어야 True."""
    assert eq.docstring_ko_ok(SPAN, "합계를 돌려준다.") is True
    assert eq.docstring_ko_ok(SPAN, "  ") is False
    assert eq.docstring_ko_ok(NO_DOC, "") is True
    assert eq.docstring_ko_ok(NO_DOC, "지어낸 번역.") is False


def test_first_is_def_or_doc():
    """첫 항목이 def 줄·docstring 범위면 True, 로직 줄이면 False, 참조가 없으면 None."""
    assert eq.first_is_def_or_doc(["정의 (줄 1)", "합계 (줄 6)"], SPAN, LINES) is True
    assert eq.first_is_def_or_doc(["docstring (줄 2~3)"], SPAN, LINES) is True
    assert eq.first_is_def_or_doc(["합계 (줄 6)", "정의 (줄 1)"], SPAN, LINES) is False
    assert eq.first_is_def_or_doc(["분기 (줄 4)"], SPAN, LINES) is False
    assert eq.first_is_def_or_doc(["참조 없음"], SPAN, LINES) is None
    assert eq.first_is_def_or_doc([], SPAN, LINES) is None


def test_first_is_def_or_doc_class_without_docstring():
    """docstring 없는 클래스는 class 줄만 True, 다음 줄(속성)은 False."""
    lines = ["class Cart:", "    total = 0"]
    span = SymbolSpan("Cart", "class", "shop.py", 1, 2, None)
    assert eq.first_is_def_or_doc(["선언 (줄 1)"], span, lines) is True
    assert eq.first_is_def_or_doc(["속성 (줄 2)"], span, lines) is False


def test_metrics_and_render_table():
    """metrics 는 지표 5개를 모으고, 표는 프롬프트별 한 줄에 비율·비용을 채운다 (오류 행 제외)."""
    expl = Explanation(
        what="합계를 구한다.", watch=["합계 (줄 6)", "분기 (줄 4~5)"], docstring_ko="합계."
    )
    m = eq.metrics(expl, SPAN, LINES)
    assert m == {
        "ref_validity": 1.0,
        "has_ref_rate": 1.0,
        "count_ok": True,
        "docstring_ko_ok": True,
        "first_is_def_or_doc": False,
    }
    rows = [
        {"prompt": "v2", "metrics": m, "judge": {"core_logic": True, "reason": "-"}, "cost": 0.001},
        {"prompt": "v2", "metrics": m, "judge": None, "cost": 0.0},
        {"prompt": "v2", "error": "X"},
    ]
    table = eq.render_table(rows)
    assert "| v2 | 2 | 100% | 100% | 2/2 (100%) | 2/2 (100%) | 0/2 (0%) | 1/1 (100%) " in table
    assert "$0.0010 | $0.0005 |" in table
