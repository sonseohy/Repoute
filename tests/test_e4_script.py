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
    """제목·레포별 표 헤더·모드별 평균 행(n=2)이 모두 들어간다."""
    text = render_markdown(ROWS)
    assert text.startswith("# 03. 실험 E4 — 인용 검증 루프 유무 (Phase 2, ")
    assert "| 레포 | 모드 | files | targets | 인용 수 | 유효 비율 | 오류 수 | 재생성 " in text
    assert "| naive | n=2 | 62% | 6 | $0.0100 | 8.0s |" in text
    assert "| validate | n=2 | 100% | 0 |" in text
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
