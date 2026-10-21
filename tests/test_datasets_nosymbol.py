"""이름 없는 따라가기 질문 데이터셋(walkthrough_questions_nosymbol.json) 형식 테스트.

실행: uv run pytest tests/test_datasets_nosymbol.py
"""

import json
from pathlib import Path

import pytest

from repoute.graphs.supervisor import SYMBOL_PATTERN

ROOT = Path(__file__).parents[1]
DATASET = ROOT / "evals" / "datasets" / "walkthrough_questions_nosymbol.json"
REPOS = {"psf/requests", "encode/httpx", "pallets/click", "Textualize/rich"}
ITEMS = json.loads(DATASET.read_text(encoding="utf-8"))


def test_has_ten_items_with_unique_ids():
    """질문은 10개이고 id 가 겹치지 않는다."""
    ids = [item["id"] for item in ITEMS]
    assert len(ITEMS) == 10
    assert len(set(ids)) == len(ids)


def test_covers_all_four_repos():
    """requests·httpx·click·rich 4개 레포가 모두 들어 있고 다른 레포는 없다."""
    assert {item["repo"] for item in ITEMS} == REPOS


def test_same_schema_as_walkthrough_questions():
    """기존 walkthrough_questions.json 과 같은 필드만 쓴다."""
    keys = {"id", "repo", "question", "expect_reach", "expect_chain", "chain_evidence"}
    for item in ITEMS:
        assert set(item) == keys, item["id"]


@pytest.mark.parametrize("item", ITEMS, ids=[item["id"] for item in ITEMS])
def test_question_has_no_identifier(item):
    """질문에 Supervisor 게이트가 식별자로 보는 문자열이 하나도 없다."""
    assert SYMBOL_PATTERN.search(item["question"]) is None


@pytest.mark.parametrize("item", ITEMS, ids=[item["id"] for item in ITEMS])
def test_chain_ends_at_reach(item):
    """체인은 3~6개이고 마지막 심볼이 expect_reach 다."""
    chain = item["expect_chain"]
    assert 3 <= len(chain) <= 6
    assert chain[-1] == item["expect_reach"]


@pytest.mark.parametrize("item", ITEMS, ids=[item["id"] for item in ITEMS])
def test_evidence_has_three_path_lines(item):
    """근거는 3줄 이상이고 각 줄이 '경로:줄번호 설명' 모양이다."""
    evidence = item["chain_evidence"]
    assert len(evidence) >= 3
    for line in evidence:
        location = line.split(" ", 1)[0]
        path, _, number = location.rpartition(":")
        assert path and number.isdigit(), line
