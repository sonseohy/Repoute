"""Issue Matcher 매칭 단계 평가 스크립트(issue_matcher_eval.py) 테스트.

가짜 임베더·tmp 레포만 쓴다 (실제 임베딩 API·네트워크 호출 없음).
실행: uv run pytest -q tests/test_issue_matcher_eval.py
"""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import networkx as nx
import pytest

from repoute.github.issues import Issue
from repoute.graphs.issue_matcher import QUERY_BODY, IssueDeps, match_symbols
from repoute.indexing.embeddings import SymbolEmbeddingStore
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


ev = _load("evals/experiments/issue_matcher_eval.py", "issue_matcher_eval")


def _raw(number: int, title: str, body: str = "", pr: bool = False) -> dict:
    """GitHub API 모양 이슈 dict 하나 (pr=True 면 pull_request 키가 붙은 PR)."""
    item = {"number": number, "title": title, "body": body, "labels": [], "comments": 0}
    if pr:
        item["pull_request"] = {"url": "x"}
    return item


def _item(**kw) -> dict:
    """gold 항목 하나 (필요한 필드만 기본값으로)."""
    base = {
        "id": "im-x-01",
        "repo": "o/r",
        "number": 1,
        "title": "t",
        "gold_modules": ["app.retry"],
        "gold_symbols": [{"name": "retry_request", "file": "app/retry.py", "line": 1}],
        "difficulty": "easy",
    }
    return {**base, **kw}


# ---- raw 에서 이슈 찾기 ----


@pytest.fixture
def raw_dir(tmp_path):
    """o/r 레포 raw 2쪽 + 이름이 접두어로 겹치는 o/rich 레포 raw 1쪽."""
    d = tmp_path / "raw"
    d.mkdir()
    (d / "issues_o__r.json").write_text(json.dumps([_raw(1, "first"), _raw(5, "a PR", pr=True)]))
    (d / "issues_o__r_page2.json").write_text(json.dumps([_raw(7, "second page", "b" * 3000)]))
    (d / "issues_o__rich.json").write_text(json.dumps([_raw(9, "other repo")]))
    return d


def test_find_issue_across_pages(raw_dir):
    """번호로 이슈를 찾는다. page2 파일까지 보고, 본문은 parse_issues 처럼 2000자로 잘린다."""
    assert ev.find_issue(raw_dir, "o/r", 1).title == "first"
    second = ev.find_issue(raw_dir, "o/r", 7)
    assert second.title == "second page"
    assert len(second.body) == 2000


def test_find_issue_skips_pr_and_other_repo(raw_dir):
    """PR 번호, 이름이 접두어로 겹치는 다른 레포(o/rich)의 번호, 없는 번호는 None."""
    assert ev.find_issue(raw_dir, "o/r", 5) is None
    assert ev.find_issue(raw_dir, "o/r", 9) is None
    assert ev.find_issue(raw_dir, "o/r", 999) is None
    assert ev.find_issue(raw_dir, "o/rich", 9).title == "other repo"


def test_raw_files_has_all_gold_issues():
    """실제 gold 20개가 모두 raw 파일에서 찾아진다 (dry-run 이 '없음' 을 내지 않는다)."""
    items = json.loads((ROOT / ev.DATASET).read_text(encoding="utf-8"))
    missing = [
        i["id"] for i in items if ev.find_issue(ROOT / ev.RAW_DIR, i["repo"], i["number"]) is None
    ]
    assert missing == []


def test_query_text_same_as_match_symbols():
    """쿼리 텍스트는 제목 + 줄바꿈 + 본문 앞 QUERY_BODY 자 (match_symbols 와 같은 구성)."""
    issue = Issue(number=1, title="Title", body="x" * (QUERY_BODY + 100))
    text = ev.query_text(issue)
    assert text == "Title\n" + "x" * QUERY_BODY
    assert ev.estimate_tokens(text) == len(text) // 4


def test_embed_cost_known_and_unknown_model():
    """제공자 접두어가 붙어도 단가를 찾고, 모르는 모델이면 None."""
    assert ev.embed_cost(1_000_000, "openai:text-embedding-3-small") == pytest.approx(0.02)
    assert ev.embed_cost(100, "fake") is None


def test_select_items_by_ids():
    """--ids 는 데이터셋 순서를 지키고, 모르는 id 면 ValueError."""
    items = [_item(id="a"), _item(id="b"), _item(id="c")]
    assert [i["id"] for i in ev.select_items(items, "c, a")] == ["a", "c"]
    assert ev.select_items(items, None) == items
    with pytest.raises(ValueError):
        ev.select_items(items, "a,zzz")


# ---- 예측 모듈 변환 ----


class FixedStore:
    """search 가 늘 같은 심볼 목록을 앞에서 k 개 돌려주는 가짜 저장소."""

    def __init__(self, hits: list[dict]):
        self.hits = hits
        self.ks: list[int] = []

    def search(self, vector, k: int = 5) -> list[dict]:
        self.ks.append(k)
        return self.hits[:k]


def _hit(name: str, file: str, line: int = 1) -> dict:
    return {"name": name, "file": file, "line_start": line, "score": 0.5}


def test_predict_module_conversion(fake_embedder):
    """파일 → 모듈: 그래프에 있으면 노드 이름, 없으면 경로 점 표기. 중복은 한 번, 상위 3개만."""
    graph = nx.DiGraph()
    graph.add_node("pkg.models", file="src/pkg/models.py")
    graph.add_node("pkg.utils", file="src/pkg/utils.py")
    index = SimpleNamespace(graph=graph)
    store = FixedStore(
        [
            _hit("A.a", "src/pkg/models.py"),
            _hit("b", "src/pkg/models.py"),
            _hit("c", "tools/run.py"),
            _hit("d", "src/pkg/utils.py"),
            _hit("e", "src/pkg/extra.py"),
            _hit("f", "src/pkg/more.py"),
        ]
    )
    pred = ev.predict(index, store, fake_embedder, Issue(number=1, title="t"), k_symbols=6)
    assert store.ks == [6]
    assert pred["modules"] == ["pkg.models", "tools.run", "pkg.utils"]
    assert pred["symbols"] == ["A.a", "b", "c", "d", "e"]  # 심볼 이름은 상위 5개
    assert len(pred["hits"]) == 6


# ---- 지표·집계 ----


def test_score_metrics():
    """p@3 는 분모 min(3, 정답 수), 1위 적중은 predicted[0] 만, 심볼은 끝 이름 비교."""
    item = _item(gold_modules=["pkg.models"], gold_symbols=[{"name": "Response.iter_lines"}])
    hit = ev.score(["pkg.utils", "pkg.models"], ["iter_lines"], item)
    assert hit == {"precision_at_3": 1.0, "hit_at_3": True, "symbol_hit": True, "top1_hit": False}
    miss = ev.score(["pkg.models"], ["other"], item)
    assert miss["top1_hit"] is True and miss["symbol_hit"] is False
    assert ev.score([], [], item)["hit_at_3"] is False


def _row(repo: str, difficulty: str, p: float, hit: bool, sym: bool, top1: bool) -> dict:
    """집계 입력용 이슈별 행 하나."""
    return {
        "id": f"{repo}-{difficulty}",
        "repo": repo,
        "number": 1,
        "difficulty": difficulty,
        "gold_modules": ["m"],
        "query_tokens_est": 10,
        "metrics": {"precision_at_3": p, "hit_at_3": hit, "symbol_hit": sym, "top1_hit": top1},
    }


def test_aggregate_by_repo_difficulty_overall():
    """이슈별 행 → 레포별·난이도별·전체. 오류 행은 n 에서 빼고 failed 로 센다, 빈 난이도는 없다."""
    rows = [
        _row("a/x", "easy", 1.0, True, True, True),
        _row("a/x", "hard", 0.0, False, False, False),
        _row("b/y", "easy", 0.5, True, False, False),
        {"id": "e", "repo": "b/y", "number": 2, "difficulty": "easy", "error": "raw 에 없음"},
    ]
    s = ev.aggregate(rows)
    assert list(s["by_repo"]) == ["a/x", "b/y"]
    assert s["by_repo"]["a/x"]["precision_at_3"] == pytest.approx(0.5)
    assert s["by_repo"]["b/y"]["n"] == 1 and s["by_repo"]["b/y"]["failed"] == 1
    assert list(s["by_difficulty"]) == ["easy", "hard"]  # medium 은 행이 없어 빠진다
    assert s["by_difficulty"]["easy"]["hit_at_3"] == 1.0
    overall = s["overall"]
    assert (overall["n"], overall["failed"], overall["query_tokens_est"]) == (3, 1, 30)
    assert overall["top1_hit"] == pytest.approx(1 / 3)
    assert overall["symbol_hit"] == pytest.approx(1 / 3)


def test_summary_of_empty_rows():
    """행이 없으면 지표는 None (0 이 아니라 '잴 것 없음')."""
    s = ev.summarize([])
    assert s["n"] == 0 and s["precision_at_3"] is None


def test_render_tables():
    """이슈별 표는 행마다 9칸(오류 행 포함), 요약 표는 토큰을 '추정' 으로 표시한다."""
    rows = [
        {
            **_row("a/x", "easy", 1.0, True, True, True),
            "title": "a | b " + "z" * 40,
            "modules": ["m", "n"],
        },
        {
            "id": "e",
            "repo": "a/x",
            "number": 2,
            "title": "missing",
            "difficulty": "easy",
            "gold_modules": ["m"],
            "error": "raw 에 없음",
        },
    ]
    table = ev.render_issue_table(rows).splitlines()
    assert all(line.replace("\\|", "").count("|") == 10 for line in table)
    summary = ev.render_summary_table(
        "전체", {"전체": ev.summarize(rows)}, "text-embedding-3-small"
    )
    assert "추정" in summary and "n" in summary and "(실패 1)" in summary


# ---- end-to-end (tmp 레포 + 가짜 임베더) ----

FILES = {
    "app/__init__.py": "",
    "app/retry.py": (
        "def retry_request(request, timeout):\n"
        '    """Retry a failed request with backoff until timeout."""\n'
        "    return request\n"
    ),
    "app/render.py": (
        "def render_template(template, context):\n"
        '    """Render html template with escaping."""\n'
        "    return template\n"
    ),
}


@pytest.fixture
def tmp_repo(tmp_path, fake_embedder):
    """retry·render 두 모듈짜리 tmp 레포: SymbolDB·import 그래프·가짜 임베딩 저장소."""
    root = tmp_path / "repo"
    for rel, text in FILES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    parsed = parse_repo(root)
    db = SymbolDB()
    db.build(parsed)
    store = SymbolEmbeddingStore(tmp_path / "emb.sqlite")
    store.build(db, root, fake_embedder, "fake")
    index = SimpleNamespace(
        db=db, graph=build_import_graph(parsed), info=SimpleNamespace(path=root)
    )
    yield index, store
    db.close()


def test_evaluate_item_end_to_end(tmp_repo, fake_embedder):
    """이슈 하나: 임베딩 검색 → 예측 모듈·심볼 → 지표 dict. 단어가 겹치는 retry 가 1위다."""
    index, store = tmp_repo
    issue = Issue(number=1, title="Retry backoff ignores timeout", body="retry request backoff")
    row = ev.evaluate_item(index, store, fake_embedder, _item(), issue, k_symbols=5)
    assert row["modules"][0] == "app.retry"
    assert row["symbols"][0] == "retry_request"
    assert row["metrics"] == {
        "precision_at_3": 1.0,
        "hit_at_3": True,
        "symbol_hit": True,
        "top1_hit": True,
    }
    assert row["query_tokens_est"] == len(ev.query_text(issue)) // 4
    assert fake_embedder.calls == [2]  # 저장소 build 때 한 번뿐, 쿼리는 embed_query


def test_predict_matches_issue_matcher_node(tmp_repo, fake_embedder):
    """같은 이슈에 대해 스크립트의 예측 모듈이 그래프 노드 match_symbols 결과와 같다."""
    index, store = tmp_repo
    issue = Issue(number=2, title="Render template escaping", body="render html " * 80)
    deps = IssueDeps(None, index, fake_embedder, store, {})
    node = match_symbols({"candidates": [issue]}, deps)["matched"][2]
    pred = ev.predict(index, store, fake_embedder, issue)
    assert pred["modules"] == node["modules"]
    assert [r.symbol for r in node["refs"]] == pred["symbols"][: len(node["refs"])]
