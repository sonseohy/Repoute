"""repoute.indexing.symbols(심볼 DB) 테스트. 실행: uv run pytest -v tests/test_symbols.py"""

from repoute.indexing.parser import Call, Symbol, parse_repo
from repoute.indexing.symbols import SymbolDB

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

FILE = "src/app/main.py"


def build_db(tmp_path, path=":memory:") -> SymbolDB:
    """작은 레포를 만들어 파싱한 뒤 DB에 넣는다."""
    src = tmp_path / "repo" / FILE
    src.parent.mkdir(parents=True)
    src.write_text(APP_SOURCE, encoding="utf-8")
    db = SymbolDB(path)
    db.build(parse_repo(tmp_path / "repo"))
    return db


def test_find_definition(tmp_path):
    """정확한 이름도, 메서드 끝 이름("run" → "App.run")도 찾고 없으면 빈 리스트."""
    db = build_db(tmp_path)
    assert db.find_definition("create_app") == [Symbol("create_app", "function", FILE, 4, 5)]
    assert [s.name for s in db.find_definition("run")] == ["App.run"]
    assert db.find_definition("missing") == []


def test_callers_and_callees(tmp_path):
    """bar를 부르는 곳은 App.run, create_app은 App을 부른다 (따라가기의 기반)."""
    db = build_db(tmp_path)
    assert db.find_callers("bar") == [Call("App.run", "bar", FILE, 10)]
    assert "App" in [c.callee for c in db.find_callees("create_app")]


def test_count_and_rebuild(tmp_path):
    """심볼 4개, 호출 3개(App(), self.bar(), foo()). 다시 build해도 중복되지 않는다."""
    db = build_db(tmp_path)
    assert db.count() == (4, 3)
    db.build(parse_repo(tmp_path / "repo"))
    assert db.count() == (4, 3)


def test_file_persists(tmp_path):
    """파일 DB는 닫았다가 다시 열어도 데이터가 남아 있다 (인덱스 캐시 재사용의 전제)."""
    path = tmp_path / "idx.sqlite"
    build_db(tmp_path, path).close()
    db = SymbolDB(path)
    assert db.count() == (4, 3)
    db.close()
