"""심볼 범위·설명 테스트 (실제 LLM 없음). 실행: uv run pytest tests/test_explain.py"""

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from repoute.indexing.explain import (
    Explanation,
    SymbolSpan,
    enclosing_symbol,
    explain_symbol,
    normalize_watch,
    watch_ref_issues,
)
from repoute.indexing.parser import parse_repo
from repoute.indexing.symbols import SymbolDB
from repoute.prompts import explain_v1, explain_v2

CODE = '''def helper(x):
    """x 를 두 배로 만든다."""
    return x * 2


class Cart:
    """장바구니."""

    total = 0

    def add(self, price):
        self.total += helper(price)
        return self.total
'''


class FakeLLM:
    """구조화 출력만 흉내 낸다. 보낸 프롬프트를 기록해 호출 횟수를 센다."""

    def __init__(self, results: list):
        self.results, self.prompts = list(results), []

    def with_structured_output(self, schema):
        return self

    def invoke(self, prompt):
        self.prompts.append(prompt)
        return self.results.pop(0)


@pytest.fixture
def repo(tmp_path) -> tuple[Path, SymbolDB]:
    """tmp_path/repo/shop.py 하나를 파싱한 (루트, SymbolDB)."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "shop.py").write_text(CODE)
    db = SymbolDB()
    db.build(parse_repo(root))
    yield root, db
    db.close()


def test_enclosing_method_and_docstring(repo):
    """메서드 안 줄은 클래스가 아니라 더 안쪽인 메서드를 준다 (docstring 없으면 None)."""
    root, db = repo
    span = enclosing_symbol(db, root, "shop.py", 12)
    assert (span.kind, span.line_start, span.line_end) == ("method", 11, 13)
    assert span.name.endswith("add") and span.docstring is None


def test_enclosing_class_body(repo):
    """클래스 바디의 메서드 밖 줄은 클래스 전체 범위와 클래스 docstring 을 준다."""
    root, db = repo
    span = enclosing_symbol(db, root, "shop.py", 9)
    assert (span.name, span.kind, span.line_start, span.line_end) == ("Cart", "class", 6, 13)
    assert span.docstring == "장바구니."


def test_enclosing_function_docstring_and_outside(repo):
    """함수 docstring 을 ast 로 뽑고, 어떤 심볼에도 안 걸리는 줄은 None."""
    root, db = repo
    span = enclosing_symbol(db, root, "shop.py", 3)
    assert span.name == "helper" and span.docstring == "x 를 두 배로 만든다."
    assert enclosing_symbol(db, root, "shop.py", 5) is None


def test_explain_symbol_caches(tmp_path, repo):
    """첫 호출은 LLM 1회(줄 번호 붙은 코드 전달), 두 번째는 캐시(cached=True, 같은 docstring_ko)."""
    root, db = repo
    span = enclosing_symbol(db, root, "shop.py", 12)
    span = replace(span, docstring="Add price and return the total.")  # docstring 있는 경우로
    expl = Explanation(
        what="가격을 더한다.",
        watch=["helper 호출 (줄 12)"],
        docstring_ko="가격을 더해 합계를 돌려준다.",
    )
    fake = FakeLLM([expl])
    cache = tmp_path / "cache" / "explanations.sqlite"
    first, cached1 = explain_symbol(fake, root, span, cache, "o/n@abc1234")
    second, cached2 = explain_symbol(fake, root, span, cache, "o/n@abc1234")
    assert (cached1, cached2) == (False, True)
    assert first == second and second.watch == ["helper 호출 (줄 12)"]
    assert second.docstring_ko == "가격을 더해 합계를 돌려준다."
    assert len(fake.prompts) == 1 and "   12| " in fake.prompts[0]


def test_explain_drops_made_up_docstring_ko(tmp_path, repo):
    """docstring 없는 심볼(Cart.add)에 LLM 이 docstring_ko 를 지어내도 빈 문자열로 정리한다."""
    root, db = repo
    span = enclosing_symbol(db, root, "shop.py", 12)
    expl = Explanation(what="가격을 더한다.", watch=["대입 (줄 12)"], docstring_ko="지어낸 설명.")
    fake = FakeLLM([expl])
    cache = tmp_path / "e.sqlite"
    first, _ = explain_symbol(fake, root, span, cache, "o/n@abc1234")
    second, cached = explain_symbol(fake, root, span, cache, "o/n@abc1234")
    assert first.docstring_ko == "" and second.docstring_ko == "" and cached


def _old_cache(path: Path, with_docstring_ko: bool) -> None:
    """prompt_version 컬럼이 없는 옛 캐시 DB 에 helper(줄 1) 행 하나를 만든다."""
    doc_col = " docstring_ko TEXT," if with_docstring_ko else ""
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE explanations (repo_id TEXT, file TEXT, line_start INTEGER,"
        f" what TEXT, watch_json TEXT,{doc_col} PRIMARY KEY (repo_id, file, line_start))"
    )
    row = ("o/n@abc1234", "shop.py", 1, "옛 설명.", '["곱셈 (줄 3)"]')
    if with_docstring_ko:
        old.execute("INSERT INTO explanations VALUES (?, ?, ?, ?, ?, ?)", (*row, "옛 번역."))
    else:
        old.execute("INSERT INTO explanations VALUES (?, ?, ?, ?, ?)", row)
    old.commit()
    old.close()


def _columns_and_rows(path: Path) -> tuple[set, list]:
    """캐시 DB 의 컬럼 이름 집합과 (prompt_version, what, docstring_ko) 행 목록."""
    conn = sqlite3.connect(path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(explanations)")}
    rows = conn.execute(
        "SELECT prompt_version, what, docstring_ko FROM explanations ORDER BY prompt_version"
    ).fetchall()
    conn.close()
    return columns, rows


def test_explain_migrates_old_cache(tmp_path, repo):
    """docstring_ko 도 없는 아주 옛 DB 는 v1 행(docstring_ko NULL)으로 옮긴다.

    docstring 있는 심볼을 v1 로 다시 물으면 NULL 이라 다시 만든다 (docstring_ko NULL 처리 유지).
    """
    root, db = repo
    span = enclosing_symbol(db, root, "shop.py", 3)  # helper, docstring 있음
    cache = tmp_path / "old.sqlite"
    _old_cache(cache, with_docstring_ko=False)
    expl = Explanation(what="두 배로 만든다.", watch=["곱셈 (줄 3)"], docstring_ko="x 를 두 배로.")
    fake = FakeLLM([expl])
    result, cached = explain_symbol(fake, root, span, cache, "o/n@abc1234", prompts=explain_v1)
    assert cached is False and len(fake.prompts) == 1
    assert result.what == "두 배로 만든다." and result.docstring_ko == "x 를 두 배로."
    columns, rows = _columns_and_rows(cache)
    assert {"docstring_ko", "prompt_version"} <= columns
    assert rows == [("v1", "두 배로 만든다.", "x 를 두 배로.")]


def test_explain_migrates_to_prompt_version(tmp_path, repo):
    """prompt_version 없는 옛 DB(행 1개)는 컬럼이 생기고 옛 행은 'v1' 로 남는다.

    v2 요청은 캐시 미스(LLM 1회)이고, 같은 심볼의 v1 요청은 옮겨진 옛 행을 그대로 쓴다.
    """
    root, db = repo
    span = enclosing_symbol(db, root, "shop.py", 3)
    cache = tmp_path / "old.sqlite"
    _old_cache(cache, with_docstring_ko=True)
    expl = Explanation(what="v2 설명.", watch=["곱셈 (줄 3)"], docstring_ko="v2 번역.")
    fake = FakeLLM([expl])
    result, cached = explain_symbol(fake, root, span, cache, "o/n@abc1234")  # 기본 v2
    assert cached is False and len(fake.prompts) == 1 and result.what == "v2 설명."
    columns, rows = _columns_and_rows(cache)
    assert "prompt_version" in columns
    assert rows == [("v1", "옛 설명.", "옛 번역."), ("v2", "v2 설명.", "v2 번역.")]
    old, cached_v1 = explain_symbol(fake, root, span, cache, "o/n@abc1234", prompts=explain_v1)
    assert cached_v1 is True and old.what == "옛 설명." and len(fake.prompts) == 1


def test_explain_cache_split_by_prompt_version(tmp_path, repo):
    """같은 심볼이라도 프롬프트 v1·v2 는 캐시가 따로라 각각 LLM 1회, 다시 물으면 둘 다 캐시."""
    root, db = repo
    span = enclosing_symbol(db, root, "shop.py", 12)
    v1 = Explanation(what="v1 설명.", watch=["대입 (줄 12)", "반환 (줄 13)"])
    v2 = Explanation(what="v2 설명.", watch=["대입 (줄 12)", "반환 (줄 13)"])
    fake = FakeLLM([v1, v2])
    cache = tmp_path / "e.sqlite"
    r1, c1 = explain_symbol(fake, root, span, cache, "o/n@abc1234", prompts=explain_v1)
    r2, c2 = explain_symbol(fake, root, span, cache, "o/n@abc1234", prompts=explain_v2)
    assert (c1, c2) == (False, False) and (r1.what, r2.what) == ("v1 설명.", "v2 설명.")
    assert "다음 중에서 고른다" in fake.prompts[0] and "첫 항목은 반드시" in fake.prompts[1]
    again1, c3 = explain_symbol(fake, root, span, cache, "o/n@abc1234", prompts=explain_v1)
    again2, c4 = explain_symbol(fake, root, span, cache, "o/n@abc1234", prompts=explain_v2)
    assert (c3, c4) == (True, True) and (again1.what, again2.what) == ("v1 설명.", "v2 설명.")
    assert len(fake.prompts) == 2


def test_explain_long_symbol_is_truncated(tmp_path):
    """MAX_READ_LINES 를 넘는 심볼은 앞 200줄만 보내고 what 앞에 표시를 붙인다."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "big.py").write_text("def big():\n" + "    x = 1\n" * 300)
    span = SymbolSpan("big", "function", "big.py", 1, 301, None)
    fake = FakeLLM([Explanation(what="긴 함수.", watch=["대입 (줄 2)"])])
    result, _ = explain_symbol(fake, root, span, tmp_path / "e.sqlite", "o/n@abc1234")
    assert result.what == "(앞 200줄 기준) 긴 함수."
    assert "  200| " in fake.prompts[0] and "  201| " not in fake.prompts[0]


def test_normalize_merges_split_refs():
    """줄 참조 "(줄 N)" 만 있는 항목은 바로 앞 설명 항목 끝에 붙는다 (httpx get/request 결함)."""
    watch = ["return x", "(줄 195)", "y = 1", "(줄 177)"]
    assert normalize_watch(watch, 170, 200) == ["return x (줄 195)", "y = 1 (줄 177)"]


def test_normalize_drops_leading_ref_only_item():
    """앞 항목이 없는 "(줄 N)" 단독 항목은 붙일 곳이 없어 버린다."""
    assert normalize_watch(["(줄 5)", "return x (줄 6)"], 1, 10) == ["return x (줄 6)"]


def test_normalize_fixes_thousands():
    """천 자리가 잘린 줄 번호는 line_start 의 천 단위를 더해 범위 안이면 보정한다."""
    assert normalize_watch(["ctx = ... (줄 390)"], 1359, 1394) == ["ctx = ... (줄 1390)"]
    assert normalize_watch(["루프 (줄 386~393)"], 1359, 1394) == ["루프 (줄 1386~1393)"]


def test_normalize_keeps_unfixable_thousands():
    """보정값(1688)도 범위(1683~1685) 밖이면 지어내지 않고 원문 그대로 둔다."""
    watch = ["호출 (줄 1686~688)"]
    assert normalize_watch(watch, 1683, 1685) == watch
    assert watch_ref_issues(watch, 1683, 1685) == 1


def test_normalize_keeps_out_of_range_ref():
    """범위 밖이면서 보정도 안 되는 참조는 지우지 않고 남기며, watch_ref_issues 로 센다."""
    watch = ["대입 (줄 50)", "반환 (줄 12)", "참조 없음"]
    assert normalize_watch(watch, 10, 20) == watch
    assert watch_ref_issues(watch, 10, 20) == 2  # 범위 밖 1 + 참조 없음 1


def test_normalize_caps_at_four():
    """5개 이상이면 앞 4개만 남긴다 (첫 항목이 핵심 로직이라 앞쪽 유지)."""
    watch = [f"항목{i} (줄 {i})" for i in range(1, 7)]
    assert normalize_watch(watch, 1, 10) == watch[:4]


def test_explain_symbol_normalizes_watch(tmp_path, repo):
    """LLM 이 쪼개진 watch 를 돌려줘도 정규화된 watch 가 반환되고 캐시에도 저장된다."""
    root, db = repo
    span = enclosing_symbol(db, root, "shop.py", 12)  # Cart.add, 11~13
    raw = ["self.total += helper(price)", "(줄 12)", "return self.total", "(줄 13)"]
    fake = FakeLLM([Explanation(what="가격을 더한다.", watch=raw)])
    cache = tmp_path / "e.sqlite"
    first, _ = explain_symbol(fake, root, span, cache, "o/n@abc1234")
    second, cached = explain_symbol(fake, root, span, cache, "o/n@abc1234")
    expected = ["self.total += helper(price) (줄 12)", "return self.total (줄 13)"]
    assert first.watch == expected and second.watch == expected and cached
    assert len(fake.prompts) == 1
