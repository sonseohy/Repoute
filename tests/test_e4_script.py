"""실험 E4 스크립트 테스트 (실제 LLM·네트워크 없음). 실행: uv run pytest tests/test_e4_script.py"""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "evals" / "experiments" / "e4_validation_loop.py"


def _load_script():
    """evals/ 는 패키지로 설치되지 않아 import 가 안 되므로 파일 경로로 직접 읽는다."""
    spec = importlib.util.spec_from_file_location("e4_validation_loop", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


e4 = _load_script()
estimate_cost, render_markdown = e4.estimate_cost, e4.render_markdown


def _row(repo: str, mode: str, valid: float, errors: int) -> dict:
    """run_one 이 돌려주는 모양의 가짜 결과 한 줄."""
    return {
        "repo": repo,
        "mode": mode,
        "files": 20,
        "targets": 13,
        "citations": 8,
        "valid": valid,
        "specificity": valid / 2,  # 유효한 인용 중 절반만 심볼을 가리킨다고 가정
        "errors": errors,
        "retries": 0 if mode == "naive" else 1,
        "tokens": {"gpt-4o-mini": {"input_tokens": 1000, "output_tokens": 100}},
        "cost": 0.01,
        "elapsed": 8.0,
    }


ROWS = [
    _row("a/x@1234567", "naive", 0.75, 2),
    _row("a/x@1234567", "validate", 1.0, 0),
    _row("b/y@7654321", "naive", 0.5, 4),
    _row("b/y@7654321", "validate", 1.0, 0),
]


def test_render_markdown_has_sections():
    """제목·레포별 표 헤더(구체성 열 포함)·모드별 평균 행(n=2)이 모두 들어간다."""
    text = render_markdown(ROWS)
    assert text.startswith("# 03. 실험 E4 — 인용 검증 루프 유무 (Phase 2, ")
    assert "| 인용 수 | 유효 비율 | 구체성 | 오류 수 | 재생성 " in text
    assert "| a/x@1234567 | naive | 20 | 13 | 8 | 75% | 38% | 2 |" in text
    assert "| 모드 | n | 유효 비율 평균 | 구체성 평균 | 오류 합 |" in text
    assert "| naive | n=2 | 62% | 31% | 6 | $0.0100 | 8.0s |" in text
    assert "| validate | n=2 | 100% | 50% | 0 |" in text
    assert "1,000 / 100" in text
    assert "## 관찰" in text


def test_render_markdown_lists_failures():
    """실패한 레포는 표 대신 '실패' 절에 한 줄로 남고 평균에서 빠진다."""
    failed = {"repo": "c/z", "mode": "naive", "error": "RuntimeError: clone 실패"}
    text = render_markdown([*ROWS, failed])
    assert "- c/z (naive): RuntimeError: clone 실패" in text
    assert "| naive | n=2 |" in text


def test_render_markdown_records_prompt_version():
    """방법 절에 row 의 프롬프트 버전이 찍힌다. prompt 키가 없는 예전 row 는 v1 로 본다."""
    assert "프롬프트 v1." in render_markdown(ROWS)
    rows = [{**r, "prompt": "v2"} for r in ROWS]
    assert "프롬프트 v2." in render_markdown(rows)


def test_estimate_cost_uses_prices():
    """gpt-4o-mini 입력 100만 토큰 = $0.15, gpt-4o 출력 100만 토큰 = $10."""
    assert estimate_cost({"gpt-4o-mini": {"input_tokens": 1_000_000, "output_tokens": 0}}) == 0.15
    assert estimate_cost({"gpt-4o": {"input_tokens": 0, "output_tokens": 1_000_000}}) == 10.0


def test_estimate_cost_dated_model_name():
    """OpenAI 가 돌려주는 날짜 붙은 이름도 가장 긴 접두어(gpt-4o-mini) 단가로 계산한다."""
    usage = {"gpt-4o-mini-2024-07-18": {"input_tokens": 1_000_000, "output_tokens": 0}}
    assert estimate_cost(usage) == 0.15


def test_estimate_cost_unknown_model_is_none():
    """단가 모르는 모델이 섞이거나 토큰이 비면 0 이 아니라 None (측정 안 한 수치 금지)."""
    assert estimate_cost({"mystery-model": {"input_tokens": 10, "output_tokens": 10}}) is None
    assert estimate_cost({}) is None


def test_render_markdown_old_rows_without_specificity():
    """specificity 키가 없는 예전 row(03·04)는 구체성 칸이 '—' 로 찍힌다 (측정 안 한 수치 금지)."""
    rows = [{k: v for k, v in r.items() if k != "specificity"} for r in ROWS]
    text = render_markdown(rows)
    assert "| 75% | — | 2 |" in text
    assert "| naive | n=2 | 62% | — | 6 |" in text


GOLD_ITEM = {
    "entrypoints": [
        {"file": "pkg/api.py", "symbol": "get"},
        {"file": "pkg/sessions.py", "symbol": "Session.request"},
    ],
    "core_modules": ["pkg.api", "pkg.sessions", "pkg.models", "pkg.adapters", "pkg.auth"],
}


def test_score_against_gold():
    """진입점은 (file, 끝 이름)으로 1/2 일치, 읽기 모듈 상위 5개 중 gold 2개 → (0.5, 0.4)."""
    entrypoints = [
        {"file": "pkg/api.py", "symbol": "get"},
        {"file": "pkg/help.py", "symbol": "main"},
    ]
    modules = ["pkg.compat", "pkg.api", "pkg.utils", "pkg.models", "pkg.cookies", "pkg.auth"]
    assert e4.score_against_gold(entrypoints, modules, GOLD_ITEM) == (0.5, 0.4)


def test_score_against_gold_without_gold_is_none():
    """gold 에 없는 레포는 0 이 아니라 (None, None) (측정 안 한 수치 금지)."""
    assert e4.score_against_gold([{"file": "a.py", "symbol": "f"}], ["a"], None) == (None, None)


def test_files_to_modules_uses_graph_then_path():
    """그래프 노드의 file 속성으로 모듈명을 찾고, 노드에 없는 파일은 경로를 모듈명으로 바꾼다."""
    import networkx as nx

    graph = nx.DiGraph()
    graph.add_node("src.pkg", file="src/pkg/__init__.py")
    files = ["src/pkg/__init__.py", "other/mod.py"]
    assert e4.files_to_modules(files, graph) == ["src.pkg", "other.mod"]


def test_render_markdown_gold_columns():
    """레포별 표·모드별 평균에 진입점 recall·핵심 p@5 열이 붙고, gold 없는 레포는 '—'."""
    scored = {"entrypoint_recall": 0.5, "core_module_p5": 0.4}
    unscored = {"entrypoint_recall": None, "core_module_p5": None}
    rows = [
        {**ROWS[0], **scored},
        {**ROWS[1], **scored},
        {**ROWS[2], **unscored},
        {**ROWS[3], **scored},
    ]
    text = render_markdown(rows)
    assert "| 시간 | 진입점 recall | 핵심 p@5 |" in text
    assert "| 시간 평균 | 진입점 recall 평균 | 핵심 p@5 평균 |" in text
    assert "| 8.0s | 0.50 | 0.40 |" in text
    assert "| 8.0s | — | — |" in text
    assert "| validate | n=2 | 100% | 50% | 0 | $0.0100 | 8.0s | 0.50 | 0.40 |" in text
    assert "| naive | n=2 | 62% | 31% | 6 | $0.0100 | 8.0s | — | — |" in text
    assert "recall 0.05, p@5 0.24" in text
