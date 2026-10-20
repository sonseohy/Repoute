"""repoute.indexing.parser(ast 파싱) 테스트. 실행: uv run pytest -v tests/test_parser.py"""

from pathlib import Path

from repoute.indexing.parser import Call, Symbol, parse_file, parse_repo

APP_SOURCE = """\
import os


def create_app():
    return App()


class App:
    def run(self):
        self.bar()

    def bar(self):
        pass


foo()
"""


def write(root: Path, rel: str, text: str) -> Path:
    """tmp_path 아래에 파일을 만들고 경로를 돌려준다."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_symbols(tmp_path):
    """함수·클래스·메서드가 이름·종류·줄 범위와 함께 나온다 (읽기 지도의 기본 단위)."""
    parsed = parse_file(write(tmp_path, "src/app/main.py", APP_SOURCE), tmp_path)
    assert parsed.file == "src/app/main.py"
    assert parsed.module == "src.app.main"
    assert parsed.symbols == [
        Symbol("create_app", "function", "src/app/main.py", 4, 5),
        Symbol("App", "class", "src/app/main.py", 8, 13),
        Symbol("App.run", "method", "src/app/main.py", 9, 10),
        Symbol("App.bar", "method", "src/app/main.py", 12, 13),
    ]


def test_imports(tmp_path):
    """절대 import는 그대로, 상대 import는 현재 모듈 기준 절대 이름으로 바뀐다."""
    source = "import os\nimport a.b\nfrom x.y import z\nfrom . import m\nfrom ..p import q\n"
    parsed = parse_file(write(tmp_path, "pkg/sub/mod.py", source), tmp_path)
    assert parsed.imports == ["os", "a.b", "x.y", "pkg.sub", "pkg.p"]

    # __init__.py는 자기 자신이 패키지라서 "." 이 자기 이름이 된다
    init = parse_file(write(tmp_path, "pkg/sub/__init__.py", source), tmp_path)
    assert init.module == "pkg.sub"
    assert init.imports[-2:] == ["pkg.sub", "pkg.p"]


def test_calls(tmp_path):
    """최상위 호출은 caller "<module>", 메서드 안 self.bar()는 callee "bar" 로 기록된다."""
    parsed = parse_file(write(tmp_path, "src/app/main.py", APP_SOURCE), tmp_path)
    assert Call("<module>", "foo", "src/app/main.py", 16) in parsed.calls
    assert Call("App.run", "bar", "src/app/main.py", 10) in parsed.calls
    assert Call("create_app", "App", "src/app/main.py", 5) in parsed.calls


def test_parse_repo_skips(tmp_path):
    """tests/·.venv/ 아래 파일과 문법 오류 파일은 결과에서 빠진다 (한 파일 오류로 멈추지 않음)."""
    write(tmp_path, "src/app/main.py", APP_SOURCE)
    write(tmp_path, "src/app/__init__.py", "")
    write(tmp_path, "src/app/broken.py", "def oops(:\n")
    write(tmp_path, "tests/test_main.py", "def test_x():\n    pass\n")
    write(tmp_path, ".venv/lib/thing.py", "x = 1\n")
    assert [p.file for p in parse_repo(tmp_path)] == ["src/app/__init__.py", "src/app/main.py"]
