"""Issue Matcher 평가 정답(issue_matcher_gold.json) 형식 테스트.

심볼이 인덱스에 있는지는 라벨 때 한 번 확인했고 여기서는 보지 않는다(인덱스 캐시가 없어도 돌게).
실행: uv run pytest tests/test_issue_gold.py
"""

import json
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
DATASET = ROOT / "evals" / "datasets" / "issue_matcher_gold.json"
REPOS = {"psf/requests", "encode/httpx", "pallets/click", "Textualize/rich"}
ITEMS = json.loads(DATASET.read_text(encoding="utf-8"))
IDS = [item["id"] for item in ITEMS]


def test_has_twenty_items_with_unique_ids():
    """이슈는 20개이고 id 가 겹치지 않는다."""
    assert len(ITEMS) == 20
    assert len(set(IDS)) == len(IDS)


def test_four_repos_five_issues_each():
    """requests·httpx·click·rich 4개 레포에 5개씩 들어 있다."""
    counts = Counter(item["repo"] for item in ITEMS)
    assert set(counts) == REPOS
    assert all(n == 5 for n in counts.values())


def test_issue_numbers_unique_per_repo():
    """같은 레포의 같은 이슈 번호가 두 번 들어가지 않는다."""
    pairs = [(item["repo"], item["number"]) for item in ITEMS]
    assert len(set(pairs)) == len(pairs)


@pytest.mark.parametrize("item", ITEMS, ids=IDS)
def test_item_fields(item):
    """필드 형식: 이슈 번호 양수, 모듈 1~3개, 심볼 1~3개({name, file, line}), 난이도 3값."""
    assert isinstance(item["number"], int) and item["number"] > 0
    assert 1 <= len(item["gold_modules"]) <= 3
    assert 1 <= len(item["gold_symbols"]) <= 3
    for symbol in item["gold_symbols"]:
        assert set(symbol) == {"name", "file", "line"}
        assert isinstance(symbol["line"], int) and symbol["line"] > 0
    assert item["difficulty"] in {"easy", "medium", "hard"}
    assert item["title"] and item["note"]
    assert item["url"].startswith(f"https://github.com/{item['repo']}/issues/")
