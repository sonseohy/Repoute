"""repoute.validation.citations(인용 검증기) 테스트. 실행: uv run pytest tests/test_citations.py"""

import pytest

from repoute.indexing.parser import parse_repo
from repoute.indexing.symbols import SymbolDB
from repoute.validation.citations import (
    CodeRef,
    citation_specificity,
    citation_validity,
    validate_citations,
)

APP_SOURCE = """\
def create_app():
    return App()


class App:
    def run(self):
        pass
"""

FILE = "src/app/main.py"


@pytest.fixture
def repo(tmp_path):
    """7줄짜리 파일 하나가 있는 레포와 그 심볼 DB."""
    root = tmp_path / "repo"
    (root / "src/app").mkdir(parents=True)
    (root / FILE).write_text(APP_SOURCE, encoding="utf-8")
    db = SymbolDB()
    db.build(parse_repo(root))
    return db, root


def test_all_valid(repo):
    """파일·줄·심볼이 다 맞으면 오류 없음, 유효 비율 1.0."""
    db, root = repo
    refs = [
        CodeRef(file=FILE, line=1, symbol="create_app"),
        CodeRef(file=FILE, line=6, symbol="App.run"),
        CodeRef(file=FILE, line=7, symbol="run"),  # 끝 이름만 줘도 메서드를 찾는다
        CodeRef(file=FILE, line=3),  # 심볼 없이 줄만
    ]
    assert validate_citations(refs, db, root) == []
    assert citation_validity(refs, db, root) == 1.0


def test_missing_file(repo):
    """없는 파일은 "파일이 없음" 메시지 하나."""
    db, root = repo
    assert validate_citations([CodeRef(file="src/nope.py", line=1)], db, root) == [
        "src/nope.py:1 — 파일이 없음"
    ]


def test_line_out_of_range(repo):
    """줄 번호가 0 이거나 파일 길이를 넘으면 "파일은 n줄까지"."""
    db, root = repo
    refs = [CodeRef(file=FILE, line=99), CodeRef(file=FILE, line=0)]
    assert validate_citations(refs, db, root) == [
        f"{FILE}:99 — 파일은 7줄까지",
        f"{FILE}:0 — 파일은 7줄까지",
    ]


def test_symbol_not_on_line(repo):
    """줄은 있지만 그 줄이 심볼 범위 밖이면 심볼 오류 하나 (파일·줄 오류는 없음)."""
    db, root = repo
    refs = [CodeRef(file=FILE, line=6, symbol="create_app")]
    assert validate_citations(refs, db, root) == [f"{FILE}:6 — 'create_app' 이(가) 그 줄에 없음"]


def test_outside_repo_is_missing(repo):
    """레포 밖을 가리키는 "../x.py" 는 실제로 있어도 "파일이 없음"."""
    db, root = repo
    (root.parent / "x.py").write_text("print('outside')\n", encoding="utf-8")
    assert validate_citations([CodeRef(file="../x.py", line=1)], db, root) == [
        "../x.py:1 — 파일이 없음"
    ]


def test_empty_refs_scores_zero(repo):
    """인용이 하나도 없으면 오류는 없지만 유효 비율은 0.0 (근거 없는 답은 0점)."""
    db, root = repo
    assert validate_citations([], db, root) == []
    assert citation_validity([], db, root) == 0.0


def test_partial_validity(repo):
    """4개 중 1개가 틀리면 유효 비율 0.75."""
    db, root = repo
    refs = [
        CodeRef(file=FILE, line=1),
        CodeRef(file=FILE, line=2, symbol="create_app"),
        CodeRef(file=FILE, line=5, symbol="App"),
        CodeRef(file=FILE, line=50),
    ]
    assert citation_validity(refs, db, root) == 0.75


def test_specificity_all_in_symbols(repo):
    """모든 인용이 심볼 범위(create_app 1~2, App.run 6~7) 안이면 구체성 1.0."""
    db, root = repo
    refs = [CodeRef(file=FILE, line=2), CodeRef(file=FILE, line=6, symbol="App.run")]
    assert citation_specificity(refs, db, root) == 1.0


@pytest.fixture
def pkg(tmp_path):
    """심볼 없는 pkg/__init__.py 와, 줄 1 이 심볼 밖(x = 1)이고 f() 가 4~5줄인 pkg/core.py."""
    root = tmp_path / "pkg_repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg/__init__.py").write_text("from pkg.core import x\n\n", encoding="utf-8")
    (root / "pkg/core.py").write_text("x = 1\n\n\ndef f():\n    pass\n", encoding="utf-8")
    db = SymbolDB()
    db.build(parse_repo(root))
    return db, root


def test_specificity_line1_of_symbol_file(pkg):
    """심볼(f)이 있는 파일의 줄 1 은 유효하지만 비구체적 → f 안(4줄)과 합쳐 0.5."""
    db, root = pkg
    refs = [CodeRef(file="pkg/core.py", line=1), CodeRef(file="pkg/core.py", line=4)]
    assert citation_validity(refs, db, root) == 1.0
    assert citation_specificity(refs, db, root) == 0.5


def test_specificity_symbolless_file_and_invalid(pkg):
    """심볼 없는 파일은 줄 1 만 구체적, 유효하지 않은 인용(99줄)은 비구체적, 빈 목록은 0.0."""
    db, root = pkg
    refs = [
        CodeRef(file="./pkg/__init__.py", line=1),  # "./" 표기도 DB 경로와 맞춘다
        CodeRef(file="pkg/__init__.py", line=2),  # 심볼 없는 파일이라도 줄 1 만 인정
        CodeRef(file="pkg/core.py", line=99),
        CodeRef(file="pkg/nope.py", line=1),
    ]
    assert citation_specificity(refs[:1], db, root) == 1.0
    assert citation_specificity(refs, db, root) == 0.25
    assert citation_specificity([], db, root) == 0.0
