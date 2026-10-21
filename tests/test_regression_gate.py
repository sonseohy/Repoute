"""CI 회귀 평가 스크립트 테스트 (실제 LLM·네트워크 없음).

실행: uv run pytest tests/test_regression_gate.py
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "evals" / "experiments" / "regression_gate.py"


def _load_script():
    """evals/ 는 패키지로 설치되지 않아 import 가 안 되므로 파일 경로로 직접 읽는다."""
    spec = importlib.util.spec_from_file_location("regression_gate", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = _load_script()
THRESHOLDS = {"citation_validity": 1.0, "core_module_p5": 0.5, "entrypoint_recall": 0.2}


def _row(recall: float | None, p5: float | None, valid: float | None) -> dict:
    """run() 이 모으는 모양의 가짜 레포 결과 한 줄."""
    return {
        "repo": "a/x@1234567",
        "entrypoint_recall": recall,
        "core_module_p5": p5,
        "citation_validity": valid,
    }


def test_dry_run_passes():
    """--dry-run (기록 18 수치의 가짜 row) 은 LLM 없이 PASS 로 끝나고 exit 0 이다."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--dry-run"], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "평균 (n=5)" in result.stdout
    assert "PASS" in result.stdout


def test_passes_returns_empty_when_all_meet_thresholds():
    """평균이 모든 기준치 이상(같은 값 포함)이면 실패 지표가 없다."""
    summary = gate.summarize([_row(0.2, 0.5, 1.0), _row(0.4, 0.9, 1.0)])
    assert gate.passes(summary, THRESHOLDS) == []


def test_passes_catches_metrics_below_threshold():
    """인용 하나라도 무효(평균 < 1.0)·p@5 평균 0.3 이면 두 지표를 실패로 돌려준다."""
    summary = gate.summarize([_row(0.5, 0.2, 1.0), _row(0.5, 0.4, 0.9)])
    assert gate.passes(summary, THRESHOLDS) == ["citation_validity", "core_module_p5"]


def test_failed_repo_counts_as_failure():
    """레포 하나가 실패(error, 지표 없음)하면 평균이 None 이 되어 모든 지표가 실패다."""
    summary = gate.summarize([_row(1.0, 1.0, 1.0), {"repo": "b/y", "error": "RuntimeError: x"}])
    assert all(v is None for v in summary.values())
    assert gate.passes(summary, THRESHOLDS) == list(THRESHOLDS)


def test_thresholds_match_record_18_margin():
    """스크립트의 기준치가 기록 18 기준으로 정한 값(1.0 / 0.5 / 0.2)과 같다."""
    assert gate.THRESHOLDS == THRESHOLDS
