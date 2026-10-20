"""repoute.indexing.entrypoints 테스트. 실행: uv run pytest tests/test_entrypoints.py"""

from repoute.indexing.entrypoints import find_entrypoints, scripts_from_pyproject
from repoute.indexing.parser import parse_repo
from repoute.indexing.symbols import SymbolDB

PYPROJECT = """\
[project]
name = "demo"

[project.scripts]
app = "pkg.cli:main"
"""

FILES = {
    "pkg/__init__.py": "",
    "pkg/cli.py": "def main():\n    pass\n",
    "main.py": "def run():\n    pass\n",
    "pkg/factory.py": "def create_app():\n    pass\n\n\nclass App:\n    pass\n",
    "pkg/util.py": "def helper():\n    pass\n",
}


def make_repo(tmp_path, pyproject: str | None = PYPROJECT):
    """FILES 로 작은 레포를 만들고 (db, root) 를 돌려준다."""
    root = tmp_path / "repo"
    for rel, source in FILES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(source, encoding="utf-8")
    if pyproject is not None:
        (root / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    db = SymbolDB()
    db.build(parse_repo(root))
    return db, root


def names(symbols):
    return [(s.file, s.name) for s in symbols]


def test_scripts_from_pyproject(tmp_path):
    """[project.scripts] 값을 (모듈, 함수) 로 나누고, 없거나 깨진 파일이면 []."""
    _, root = make_repo(tmp_path)
    assert scripts_from_pyproject(root) == [("pkg.cli", "main")]
    (root / "pyproject.toml").write_text("[project\nbroken", encoding="utf-8")
    assert scripts_from_pyproject(root) == []
    (root / "pyproject.toml").unlink()
    assert scripts_from_pyproject(root) == []


def test_priority_order(tmp_path):
    """scripts 의 cli.py:main → main.py 의 run → 아무 파일의 create_app 순. 클래스 등은 제외."""
    db, root = make_repo(tmp_path)
    assert names(find_entrypoints(db, root)) == [
        ("pkg/cli.py", "main"),
        ("main.py", "run"),
        ("pkg/factory.py", "create_app"),
    ]


def test_without_pyproject(tmp_path):
    """pyproject 가 없으면 (2) 진입점 파일 → (3) 이름 순으로만. cli.py 도 ENTRY_FILES 라 (2)."""
    db, root = make_repo(tmp_path, pyproject=None)
    assert names(find_entrypoints(db, root)) == [
        ("main.py", "run"),
        ("pkg/cli.py", "main"),
        ("pkg/factory.py", "create_app"),
    ]


def test_no_duplicates(tmp_path):
    """여러 단계에 동시에 걸리는 심볼(pkg/cli.py:main)도 한 번만 나온다."""
    db, root = make_repo(tmp_path)
    found = find_entrypoints(db, root)
    assert len(found) == len(set(found))
    assert names(found).count(("pkg/cli.py", "main")) == 1
