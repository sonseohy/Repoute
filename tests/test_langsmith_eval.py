"""LangSmith evaluate 스크립트 테스트 (실제 LLM·LangSmith 없음).

실행: uv run pytest tests/test_langsmith_eval.py
"""

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

SCRIPT = Path(__file__).parents[1] / "evals" / "experiments" / "langsmith_eval.py"


def _load_script():
    """evals/ 는 패키지로 설치되지 않아 import 가 안 되므로 파일 경로로 직접 읽는다."""
    spec = importlib.util.spec_from_file_location("langsmith_eval", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ls_eval = _load_script()


def _pair(outputs: dict | None, gold: dict):
    """평가자가 읽는 .outputs 만 가진 가짜 run·example."""
    return SimpleNamespace(outputs=outputs), SimpleNamespace(outputs=gold)


def test_reading_guide_evaluators_score_against_gold():
    """진입점 2개 중 1개 일치 → recall 0.5, 상위 5 모듈 중 2개 정답 → p@5 0.4, 오류 있으면 0.0."""
    out = {
        "entrypoints": [{"file": "api.py", "symbol": "get"}],
        "reading_modules": ["src.pkg.api", "src.pkg.sessions", "src.pkg.compat"],
        "validation_errors": ["api.py:999 — 줄 범위 밖"],
    }
    gold = {
        "entrypoints": [{"file": "api.py", "symbol": "get"}, {"file": "api.py", "symbol": "post"}],
        "core_modules": ["pkg.api", "pkg.sessions"],
    }
    run, example = _pair(out, gold)
    assert ls_eval.entrypoint_recall(run, example) == {"key": "entrypoint_recall", "score": 0.5}
    assert ls_eval.core_module_precision_at_5(run, example)["score"] == 0.4
    assert ls_eval.citation_validity(run, example)["score"] == 0.0


def test_walkthrough_evaluators_use_refs_order_and_strict_flag():
    """[file, symbol] 인용 순서로 체인을 만들고, 기대 심볼 끝 이름이 인용에 있으면 도달."""
    out = {
        "refs": [["api.py", "get"], ["api.py", "request"], ["sessions.py", "Session.send"]],
        "answer_text": "get 이 request 를 부른다",
        "strict_pass": True,
    }
    gold = {"expect_reach": "Session.send", "expect_chain": ["get", "Session.request", "send"]}
    run, example = _pair(out, gold)
    assert ls_eval.reached(run, example)["score"] == 1.0
    assert ls_eval.call_chain_accuracy(run, example)["score"] == 1.0  # 끝 이름 LCS 3/3
    assert ls_eval.strict_pass(run, example)["score"] == 1.0


def test_failed_run_and_missing_chain_score_none():
    """타깃이 실패해 outputs 가 없거나 기대 체인이 비면 0 이 아니라 None (측정 못 함)."""
    gold = {"expect_reach": "Session.send", "expect_chain": []}
    failed, example = _pair(None, gold)
    assert ls_eval.reached(failed, example)["score"] is None
    assert ls_eval.strict_pass(failed, example)["score"] is None
    run, _ = _pair({"refs": [["a.py", "send"]], "answer_text": "", "strict_pass": False}, gold)
    assert ls_eval.call_chain_accuracy(run, example)["score"] is None


def test_dry_run_runs_without_network_for_both_datasets():
    """--dry-run 은 LangSmith·LLM 없이 두 데이터셋 모두 평가자 요약까지 출력한다."""
    for dataset in ("reading-guide-gold", "walkthrough-qa"):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--dataset", dataset, "--dry-run"],
            capture_output=True,
            text=True,
            timeout=60,
            env={"PATH": "", "LANGSMITH_TRACING": "false", "LANGSMITH_API_KEY": ""},
        )
        assert result.returncode == 0, result.stderr
        assert f"[dry-run] {dataset}: n=1" in result.stdout
