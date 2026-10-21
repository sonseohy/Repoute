"""Phase 4 평가자·재채점·데이터셋 등록 스크립트 테스트 (실제 LLM·네트워크 없음).

실행: uv run pytest tests/test_evaluators.py
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parents[1]
DATASETS = ROOT / "evals" / "datasets"


def _load(relpath: str, name: str):
    """evals/ 는 패키지로 설치되지 않아 import 가 안 되므로 파일 경로로 직접 읽는다."""
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


m = _load("evals/evaluators/metrics.py", "metrics")
rescore = _load("evals/experiments/rescore_batch.py", "rescore_batch")
ls_datasets = _load("evals/experiments/langsmith_datasets.py", "langsmith_datasets")


def test_entrypoint_recall_matches_tail_name_in_same_file():
    """정답 "Session.send" 는 같은 파일의 "send" 예측으로도 찾은 것으로 친다."""
    gold = [{"file": "a.py", "symbol": "Session.send"}, {"file": "b.py", "symbol": "main"}]
    assert m.entrypoint_recall([{"file": "a.py", "symbol": "send"}], gold) == 0.5


def test_entrypoint_recall_requires_same_file():
    """끝 이름이 같아도 파일이 다르면 놓친 것이다. gold 가 비면 0.0."""
    gold = [{"file": "a.py", "symbol": "main"}]
    assert m.entrypoint_recall([{"file": "b.py", "symbol": "main"}], gold) == 0.0
    assert m.entrypoint_recall([{"file": "a.py", "symbol": "main"}], []) == 0.0


def test_precision_at_5_absorbs_prefix():
    """모듈 "src.requests.sessions" 와 "requests.sessions" 는 같은 모듈. 분모는 k."""
    gold = ["requests.sessions", "requests.models"]
    predicted = ["src.requests.sessions", "src.requests.models", "src.requests.utils"]
    assert m.core_module_precision_at_k(predicted, gold) == 2 / 5


def test_precision_at_k_only_counts_top_k_once():
    """상위 k 바깥과 중복 예측은 세지 않는다."""
    gold = ["pkg.a", "pkg.b"]
    assert m.core_module_precision_at_k(["pkg.a", "pkg.a", "pkg.c", "pkg.b"], gold, k=2) == 0.5


def test_issue_module_precision_at_3():
    """이슈 모듈 precision@3: 접두어 차이는 흡수하고 분모는 k=3."""
    gold = ["requests.sessions", "requests.adapters"]
    predicted = ["src.requests.sessions", "src.requests.utils", "requests.adapters", "x.y"]
    assert m.issue_module_precision_at_k(predicted, gold) == 1.0  # 정답 2개 모두 상위 3개 안
    assert m.issue_module_precision_at_k([], gold) == 0.0
    # 정답이 1개면 상위 3개 안에 있을 때 1.0 (분모 = min(k, 정답 수))
    assert (
        m.issue_module_precision_at_k(["a.b", "requests.utils", "c.d"], ["requests.utils"]) == 1.0
    )
    assert m.issue_module_precision_at_k(["a.b", "c.d", "e.f"], ["requests.utils"]) == 0.0
    assert m.issue_module_hit_at_k(["a.b", "requests.utils"], ["requests.utils"]) is True


def test_issue_symbol_hit_by_tail_name():
    """정답 "Session.request" 는 "request" 예측으로도 맞힌 것이다."""
    gold = ["Session.request", "HTTPAdapter.send"]
    assert m.issue_symbol_hit(["foo", "request"], gold)
    assert m.issue_symbol_hit(["HTTPAdapter.send"], gold)


def test_issue_symbol_hit_miss_and_empty():
    """끝 이름이 하나도 겹치지 않거나 예측이 비면 False."""
    assert not m.issue_symbol_hit(["Session.prepare_request"], ["Session.request"])
    assert not m.issue_symbol_hit([], ["Session.request"])


def test_reached_by_tail_name():
    """기록 13 관찰 2: 'prepare_body' 인용은 'PreparedRequest.prepare_body' 도달로 인정."""
    refs = [("src/requests/models.py", "prepare_body")]
    assert m.reached(refs, "", "PreparedRequest.prepare_body")
    assert not m.reached([("x.py", "prepare")], "", "PreparedRequest.prepare_body")


def test_reached_common_name_needs_file_or_qualifier():
    """흔한 이름 'send' 는 끝 이름만으로는 부족: 같은 파일이거나 한정자까지 같아야 한다."""
    expect = "HTTPAdapter.send"
    assert not m.reached([("adapters.py", "send")], "", expect)
    assert not m.reached([("adapters.py", "BaseAdapter.send")], "", expect)
    assert m.reached([("adapters.py", "send")], "", expect, expect_file="adapters.py")
    assert m.reached([("adapters.py", "HTTPAdapter.send")], "", expect)


def test_reached_by_full_name_in_text():
    """인용이 없어도 본문에 기대 심볼 전체가 있으면 도달."""
    assert m.reached([], "마지막에 Session.send 가 어댑터를 부른다", "Session.send")


def test_call_chain_accuracy_lcs():
    """끝 이름 정규화 후 LCS: [get, request, send] vs [get, request, request, send] → 3/4."""
    expect = ["get", "request", "Session.request", "Session.send"]
    assert m.call_chain_accuracy(["get", "request", "send"], expect) == 0.75


def test_call_chain_accuracy_order_and_empty():
    """순서가 뒤집히면 LCS 가 줄고, 예측이 비면 0.0."""
    assert m.call_chain_accuracy(["send", "get"], ["get", "send"]) == 0.5
    assert m.call_chain_accuracy([], ["get"]) == 0.0


def test_predicted_chain_from_refs_dedups_consecutive():
    """연속 중복과 symbol 없는 인용은 빼고, 떨어진 중복은 남긴다(정규화 없음)."""
    refs = [("a", "get"), ("a", "get"), ("b", None), ("c", "Session.send"), ("a", "get")]
    assert m.predicted_chain_from_refs(refs) == ["get", "Session.send", "get"]


def test_rescore_parses_ref_strings():
    """배치 raw 의 "file:line [symbol]" 문자열을 (file, symbol) 로 읽는다."""
    assert rescore.parse_ref("src/a.py:12 [Session.send]") == ("src/a.py", "Session.send")
    assert rescore.parse_ref("src/a.py:12") == ("src/a.py", None)


def test_rescore_row_keeps_old_hit_and_adds_tail_hit():
    """기존 도달은 유지(OR), 끝 이름으로 새로 도달, expect_chain 없으면 chain acc 는 None."""
    row = {"id": "q", "expect_reach": "A.prepare_body", "reached": False}
    row["refs"] = ["m.py:1 [prepare_body]"]
    scored = rescore.rescore_row(row, {})
    assert scored["new"] and not scored["old"] and scored["chain_acc"] is None


def test_walkthrough_chain_ends_at_expect_reach():
    """expect_chain 이 있으면 20개 모두 있고, 마지막 심볼 끝 이름이 expect_reach 와 같다."""
    items = json.loads((DATASETS / "walkthrough_questions.json").read_text(encoding="utf-8"))
    with_chain = [it for it in items if it.get("expect_chain")]
    if not with_chain:
        pytest.skip("expect_chain 아직 없음")
    assert len(with_chain) == len(items) == 20
    for it in with_chain:
        assert m._tail(it["expect_chain"][-1]) == m._tail(it["expect_reach"]), it["id"]


def test_reading_guide_gold_shape():
    """정답 지도: 레포 5개, 각각 진입점 1개 이상·핵심 모듈 5개."""
    path = DATASETS / "reading_guide_gold.json"
    if not path.is_file():
        pytest.skip("reading_guide_gold.json 아직 없음")
    items = json.loads(path.read_text(encoding="utf-8"))
    assert len({it["repo"] for it in items}) == len(items) == 5
    for it in items:
        assert it["commit"] and len(it["entrypoints"]) >= 1, it["repo"]
        assert len(it["core_modules"]) == 5, it["repo"]


class FakeClient:
    """LangSmith Client 대역: 메모리에 데이터셋·예제를 둔다 (네트워크 없음)."""

    def __init__(self):
        self.examples: list[dict] = []
        self.created = 0

    def has_dataset(self, *, dataset_name):
        return self.created > 0

    def read_dataset(self, *, dataset_name):
        return SimpleNamespace(id="d1")

    def create_dataset(self, name, *, description=None):
        self.created += 1
        return SimpleNamespace(id="d1")

    def list_examples(self, dataset_id):
        return [SimpleNamespace(metadata=e["metadata"], inputs=e["inputs"]) for e in self.examples]

    def create_examples(self, *, dataset_id, examples):
        self.examples += examples


def test_upload_is_idempotent():
    """두 번 올려도 데이터셋 1개·예제는 새 id 만 추가된다."""
    client = FakeClient()
    items = [{"id": "q1", "repo": "a/b", "question": "?", "expect_reach": "f"}]
    examples = ls_datasets.qa_examples(items)
    assert ls_datasets.upload(client, "walkthrough-qa", "", examples) == 1
    assert ls_datasets.upload(client, "walkthrough-qa", "", examples) == 0
    assert client.created == 1 and len(client.examples) == 1


def test_langsmith_dry_run_prints_counts_without_network():
    """--dry-run 은 Client 를 만들지 않고(네트워크·키 불필요) 데이터셋별 개수만 찍는다."""
    script = ROOT / "evals" / "experiments" / "langsmith_datasets.py"
    out = subprocess.run(
        [sys.executable, str(script), "--dry-run"], capture_output=True, text=True, check=True
    ).stdout
    assert "[dry-run] walkthrough-qa: 예제 20개" in out


def test_impact_recall_matches_tail_name_over_gold_count():
    """PR Coach 영향 재현율: 끝 이름으로 맞추고 분모는 정답 수. 정답이 비면 0.0."""
    gold = ["Session.send", "HTTPAdapter.build_response", "resolve_redirects"]
    assert m.impact_recall(["send", "Session.resolve_redirects", "other"], gold) == 2 / 3
    assert m.impact_recall(["send"], []) == 0.0


def test_test_file_hit_compares_file_name_only():
    """테스트 파일 적중: 경로 끝 파일명이 같으면 디렉터리가 달라도 맞은 것으로 친다."""
    gold = ["tests/test_requests.py", "tests/test_utils.py"]
    assert m.test_file_hit(["requests/tests/test_utils.py"], gold)
    assert not m.test_file_hit(["tests/test_models.py"], gold)
    assert not m.test_file_hit([], gold)
