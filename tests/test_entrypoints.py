"""repoute.indexing.entrypoints 테스트. 실행: uv run pytest tests/test_entrypoints.py"""

from repoute.indexing.entrypoints import (
    exported_names,
    find_entrypoints,
    find_entrypoints_scored,
    scripts_from_pyproject,
)
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


# 라이브러리형 가짜 레포: 공개 API(get, Client)와 디버그용 help.main
LIB_FILES = {
    "pkg/__init__.py": "from .api import get\nfrom .client import Client\n",
    "pkg/api.py": "def get():\n    pass\n",
    "pkg/client.py": "class Client:\n    def send(self):\n        pass\n",
    "pkg/help.py": "def main():\n    pass\n",
}


def make_repo(tmp_path, pyproject: str | None = PYPROJECT, files: dict[str, str] = FILES):
    """files 로 작은 레포를 만들고 (db, root) 를 돌려준다."""
    root = tmp_path / "repo"
    for rel, source in files.items():
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
    """scripts 의 cli.py:main → main.py 의 run → create_app 순. 클래스 등은 제외.

    v2 점수: main 3+1+2−2(cli 경로 감점)=4, run 1+2=3, create_app 1. 기대값은 v1 과 같다.
    """
    db, root = make_repo(tmp_path)
    assert names(find_entrypoints(db, root)) == [
        ("pkg/cli.py", "main"),
        ("main.py", "run"),
        ("pkg/factory.py", "create_app"),
    ]


def test_without_pyproject(tmp_path):
    """pyproject 가 없으면 main.py 의 run(3) 이 1위.

    cli.py:main 은 1+2−2(cli 경로 감점)=1 로 create_app(1) 과 동점이고, 동점은 파일 순
    (pkg/cli.py < pkg/factory.py)이라 기대값은 v1 과 같다.
    """
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


def test_exported_names(tmp_path):
    """from-import 의 이름과 __all__ 의 이름을 뽑는다. 외부 패키지(typing)에서 온 이름은 뺀다."""
    init = tmp_path / "pkg" / "__init__.py"
    init.parent.mkdir()
    init.write_text(
        'from typing import Any\nfrom .api import get, post\n__all__ = ["Session"]\n',
        encoding="utf-8",
    )
    assert exported_names(init) == ["get", "post", "Session"]
    assert exported_names(tmp_path / "missing.py") == []


def test_public_api_before_help(tmp_path):
    """공개 함수 get·클래스 Client(+3) → Client.send(+2) → help.main 순.

    help.main 은 scripts +3, 이름 +1 이지만 경로의 help 로 −2 라 2점, Client.send 와 같은 점수면
    파일 이름 순(pkg/client.py < pkg/help.py)으로 뒤에 온다.
    """
    script = '[project]\nname = "lib"\n\n[project.scripts]\nlib = "pkg.help:main"\n'
    db, root = make_repo(tmp_path, pyproject=script, files=LIB_FILES)
    assert [(s.name, n) for s, n in find_entrypoints_scored(db, root)] == [
        ("get", 3),
        ("Client", 3),
        ("Client.send", 2),
        ("main", 2),
    ]


def test_drops_non_positive(tmp_path):
    """scripts 가 없으면 help.main 은 이름 +1, help 경로 −2 로 −1점이라 아예 빠진다."""
    db, root = make_repo(tmp_path, pyproject=None, files=LIB_FILES)
    scored = find_entrypoints_scored(db, root)
    assert ("pkg/help.py", "main") not in names(s for s, _ in scored)
    assert all(n > 0 for _, n in scored)
