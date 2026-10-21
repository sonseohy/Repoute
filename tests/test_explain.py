"""심볼 범위·설명 테스트 (실제 LLM 없음). 실행: uv run pytest tests/test_explain.py"""

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from repoute.indexing.explain import Explanation, SymbolSpan, enclosing_symbol, explain_symbol
from repoute.indexing.parser import parse_repo
from repoute.indexing.symbols import SymbolDB

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


def test_explain_migrates_old_cache(tmp_path, repo):
    """옛 스키마(docstring_ko 없음) DB 는 컬럼을 추가하고 docstring 있는 심볼은 다시 만든다."""
    root, db = repo
    span = enclosing_symbol(db, root, "shop.py", 3)  # helper, docstring 있음
    cache = tmp_path / "old.sqlite"
    old = sqlite3.connect(cache)
    old.execute(
        "CREATE TABLE explanations (repo_id TEXT, file TEXT, line_start INTEGER,"
        " what TEXT, watch_json TEXT, PRIMARY KEY (repo_id, file, line_start))"
    )
    old.execute(
        "INSERT INTO explanations VALUES (?, ?, ?, ?, ?)",
        ("o/n@abc1234", "shop.py", 1, "옛 설명.", '["곱셈 (줄 3)"]'),
    )
    old.commit()
    old.close()
    expl = Explanation(what="두 배로 만든다.", watch=["곱셈 (줄 3)"], docstring_ko="x 를 두 배로.")
    fake = FakeLLM([expl])
    result, cached = explain_symbol(fake, root, span, cache, "o/n@abc1234")
    assert cached is False and len(fake.prompts) == 1
    assert result.what == "두 배로 만든다." and result.docstring_ko == "x 를 두 배로."
    conn = sqlite3.connect(cache)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(explanations)")}
    stored = conn.execute("SELECT docstring_ko FROM explanations WHERE line_start = 1").fetchone()
    conn.close()
    assert "docstring_ko" in columns and stored == ("x 를 두 배로.",)


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
