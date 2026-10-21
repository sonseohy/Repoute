"""PR Coach 평가 정답(pr_coach_gold.json) 형식 테스트.

심볼이 인덱스에 있는지는 라벨 때 한 번 확인했고 여기서는 보지 않는다(인덱스 캐시가 없어도 돌게).
실행: uv run pytest tests/test_pr_gold.py
"""

import json
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
DATASET = ROOT / "evals" / "datasets" / "pr_coach_gold.json"
REPOS = {"psf/requests", "encode/httpx", "pallets/click", "Textualize/rich"}
ITEMS = json.loads(DATASET.read_text(encoding="utf-8"))
IDS = [item["id"] for item in ITEMS]


def test_has_eight_to_ten_items_with_unique_ids():
    """PR 은 8~10개이고 id 가 겹치지 않는다 (심볼 없는 PR 을 빼서 10개 미만일 수 있다)."""
    assert 8 <= len(ITEMS) <= 10
    assert len(set(IDS)) == len(IDS)


def test_all_four_repos_with_one_to_three_prs():
    """requests·httpx·click·rich 4개 레포가 모두 있고 레포마다 1~3개다."""
    counts = Counter(item["repo"] for item in ITEMS)
    assert set(counts) == REPOS
    assert all(1 <= n <= 3 for n in counts.values())


def test_pr_numbers_unique_per_repo():
    """같은 레포의 같은 PR 번호가 두 번 들어가지 않는다."""
    pairs = [(item["repo"], item["number"]) for item in ITEMS]
    assert len(set(pairs)) == len(pairs)


@pytest.mark.parametrize("item", ITEMS, ids=IDS)
def test_item_fields(item):
    """필드 형식: 번호 양수, 변경 심볼 1~6, 영향 심볼 2~6, 모듈 1~4, 테스트 1~3, 난이도 3값."""
    assert isinstance(item["number"], int) and item["number"] > 0
    assert item["changed_files"]
    assert 1 <= len(item["changed_symbols"]) <= 6
    assert 2 <= len(item["gold_impact_symbols"]) <= 6
    assert 1 <= len(item["gold_impact_modules"]) <= 4
    assert 1 <= len(item["gold_test_files"]) <= 3
    for symbol in item["changed_symbols"] + item["gold_impact_symbols"]:
        assert set(symbol) == {"name", "file", "line"}
        assert isinstance(symbol["line"], int) and symbol["line"] > 0
    assert item["difficulty"] in {"easy", "medium", "hard"}
    assert item["title"] and item["summary_ko"] and item["merged_at"]
    assert item["url"] == f"https://github.com/{item['repo']}/pull/{item['number']}"


@pytest.mark.parametrize("item", ITEMS, ids=IDS)
def test_impact_symbols_are_not_changed_symbols(item):
    """영향 심볼은 변경 심볼 자신이 아니라 그 호출자다 (같은 이름·파일 쌍이 겹치지 않는다)."""
    changed = {(s["name"], s["file"]) for s in item["changed_symbols"]}
    impact = {(s["name"], s["file"]) for s in item["gold_impact_symbols"]}
    assert not changed & impact
