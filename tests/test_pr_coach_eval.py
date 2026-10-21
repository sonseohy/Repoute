"""PR Coach 정적 단계 평가 스크립트(pr_coach_eval.py) 테스트.

tmp 레포·가짜 gold·가짜 raw 파일만 쓴다 (LLM·MCP·네트워크 호출 없음).
실행: uv run pytest -q tests/test_pr_coach_eval.py
"""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from repoute.indexing.imports import build_import_graph
from repoute.indexing.parser import parse_repo
from repoute.indexing.symbols import SymbolDB

ROOT = Path(__file__).parents[1]


def _load(relpath: str, name: str):
    """evals/ 는 패키지로 설치되지 않아 import 가 안 되므로 파일 경로로 직접 읽는다."""
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ev = _load("evals/experiments/pr_coach_eval.py", "pr_coach_eval")

FILES = {
    "app/__init__.py": "",
    "app/util.py": "def helper(x):\n    return x + 1\n",
    "app/main.py": "from app.util import helper\n\n\ndef run(x):\n    return helper(x)\n",
    "tests/test_util.py": "from app.util import helper\n\n\ndef test_helper():\n    helper(1)\n",
}
GOLD = {
    "id": "pr-o-01",
    "repo": "o/r",
    "number": 3,
    "title": "helper 수정",
    "url": "https://github.com/o/r/pull/3",
    "changed_files": ["app/util.py"],
    "changed_symbols": [{"name": "helper", "file": "app/util.py", "line": 1}],
    "gold_impact_symbols": [
        {"name": "run", "file": "app/main.py", "line": 4},
        {"name": "Cli.main", "file": "app/cli.py", "line": 9},  # 예측에 없는 정답
    ],
    "gold_impact_modules": ["app.main"],
    "gold_test_files": ["tests/test_util.py"],
    "summary_ko": "helper 반환값 수정",
    "difficulty": "easy",
}
RAW_FILES = [
    {
        "filename": "app/util.py",
        "status": "modified",
        "additions": 1,
        "deletions": 1,
        "patch": "@@ -1,2 +1,2 @@\n def helper(x):\n-    return x\n+    return x + 1\n",
    }
]


@pytest.fixture
def tmp_index(tmp_path):
    """util·main 모듈 + tests/ 파일짜리 tmp 레포의 인덱스 대역 (db·graph·info.path)."""
    root = tmp_path / "repo"
    for rel, text in FILES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    parsed = parse_repo(root)
    db = SymbolDB()
    db.build(parsed)
    yield SimpleNamespace(db=db, graph=build_import_graph(parsed), info=SimpleNamespace(path=root))
    db.close()


@pytest.fixture
def raw_dir(tmp_path):
    """o/r PR 3 의 REST get_files 응답 하나가 든 raw 폴더."""
    d = tmp_path / "raw"
    d.mkdir()
    (d / "pr_files_o__r_3.json").write_text(json.dumps(RAW_FILES))
    return d


def test_load_files_and_missing(raw_dir):
    """raw 파일을 그래프와 같은 파서로 읽고(hunk → 줄 범위), 없는 번호면 None."""
    files = ev.load_files(raw_dir, "o/r", 3)
    assert [(f.filename, f.changed_ranges) for f in files] == [("app/util.py", [(1, 2)])]
    assert ev.load_files(raw_dir, "o/r", 99) is None


def test_impact_precision():
    """분모는 예측 수(끝 이름 중복은 한 번), 예측이 없으면 0.0."""
    assert ev.impact_precision(["run", "A.run", "other"], ["Cli.run"]) == pytest.approx(0.5)
    assert ev.impact_precision([], ["run"]) == 0.0


def test_evaluate_item_end_to_end(tmp_index, raw_dir):
    """가짜 gold 1건: raw → 변경 심볼 helper → 호출자 run → 테스트 파일, 지표 4개."""
    row = ev.evaluate_item(tmp_index, GOLD, ev.load_files(raw_dir, "o/r", 3))
    assert [s["name"] for s in row["changed"]] == ["helper"]
    assert [(s["name"], s["relation"], s["line"]) for s in row["impact"]] == [("run", "caller", 4)]
    assert row["test_files"] == ["tests/test_util.py"]
    assert row["metrics"] == {
        "changed_recall": 1.0,
        "impact_recall": 0.5,  # 정답 2개 중 run 만
        "impact_precision": 1.0,  # 예측 1개(run)가 정답
        "test_file_hit": True,
    }


def _row(repo: str, difficulty: str, recall: float, hit: bool) -> dict:
    """집계 입력용 PR 별 행 하나."""
    metrics = {
        "changed_recall": 1.0,
        "impact_recall": recall,
        "impact_precision": recall,
        "test_file_hit": hit,
    }
    return {"id": f"{repo}-{difficulty}", "repo": repo, "number": 1, "difficulty": difficulty,
            "changed": [], "impact": [], "metrics": metrics}  # fmt: skip


def test_aggregate_and_tables():
    """레포·난이도·전체 요약. 오류 행은 failed 로 세고, 행 없는 난이도는 빠지며, 표 칸 수가 맞다."""
    rows = [
        _row("a/x", "easy", 1.0, True),
        _row("a/x", "hard", 0.0, False),
        {"id": "e", "repo": "a/x", "number": 2, "difficulty": "easy", "error": "raw 없음"},
    ]
    s = ev.aggregate(rows)
    assert list(s["by_difficulty"]) == ["easy", "hard"]
    overall = s["overall"]
    assert (overall["n"], overall["failed"]) == (2, 1)
    assert overall["impact_recall"] == pytest.approx(0.5) and overall["test_file_hit"] == 0.5
    assert ev.summarize([])["impact_recall"] is None
    table = ev.render_pr_table(rows).splitlines()
    assert all(line.count("|") == 9 for line in table)
    assert "(실패 1)" in ev.render_summary_table("전체", {"전체": overall})


def test_dry_run_without_gold(tmp_path, capsys):
    """gold 파일이 없으면 dry-run 은 '파일 없음' 안내만 한다."""
    ev.dry_run(tmp_path / "nope.json", str(tmp_path))
    assert "파일 없음" in capsys.readouterr().out


def test_dry_run_lists_raw_state(tmp_path, raw_dir, capsys):
    """gold 가 있으면 PR 마다 raw 파일 유무를 보여 준다."""
    dataset = tmp_path / "gold.json"
    dataset.write_text(json.dumps([GOLD, {**GOLD, "id": "pr-o-02", "number": 4}]))
    ev.dry_run(dataset, str(raw_dir))
    out = capsys.readouterr().out
    assert "pr-o-01 o/r#3: raw 있음" in out and "pr-o-02 o/r#4: raw 파일 없음" in out
