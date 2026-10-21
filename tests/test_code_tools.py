"""repoute.tools.code_tools(탐색 도구) 테스트. 실행: uv run pytest -v tests/test_code_tools.py"""

from repoute.indexing.parser import parse_repo
from repoute.indexing.symbols import SymbolDB
from repoute.tools.code_tools import _name_candidates, make_code_tools

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


def build_tools(tmp_path) -> dict:
    """작은 레포를 인덱싱하고 도구를 이름으로 꺼낼 수 있게 dict로 만든다."""
    root = tmp_path / "repo"
    src = root / FILE
    src.parent.mkdir(parents=True)
    src.write_text(APP_SOURCE, encoding="utf-8")
    db = SymbolDB()
    db.build(parse_repo(root))
    return {t.name: t for t in make_code_tools(db, root)}


def test_find_definition(tmp_path):
    """find_definition 은 "파일:줄범위"·종류·def 줄 인용 힌트를 주고, 없는 이름은 "찾지 못함"."""
    tools = build_tools(tmp_path)
    out = tools["find_definition"].invoke({"name": "create_app"})
    assert "src/app/main.py:4-5" in out and "function" in out
    assert "인용: src/app/main.py:4" in out  # def 줄(line_start) 인용 힌트 (기록 08 관찰 4)
    assert "찾지 못함" in tools["find_definition"].invoke({"name": "missing"})


def test_name_candidates():
    """점 붙은 이름은 Class.method → 끝 이름 순으로 후보를 만들고, 점이 없으면 후보가 없다."""
    assert _name_candidates("a.b.C.m") == ["C.m", "m"]
    assert _name_candidates("Session.request") == ["request"]
    assert _name_candidates("get") == []


def test_find_definition_retries_dotted_name(tmp_path):
    """모듈 경로가 붙은 이름도 끝 이름으로 다시 찾고, 안내 줄과 read_code 호출 힌트를 준다."""
    tools = build_tools(tmp_path)
    out = tools["find_definition"].invoke({"name": "app.main.create_app"})
    assert out.splitlines()[0] == "('app.main.create_app' 은 없어 'create_app' 으로 찾음)"
    assert "src/app/main.py:4-5" in out
    assert '→ read_code("src/app/main.py", 4, 5)' in out  # 범위를 그대로 복사해 쓰게


def test_find_definition_dotted_missing(tmp_path):
    """후보로 다시 찾아도 없는 이름은 여전히 "찾지 못함"이고 안내 줄은 붙지 않는다."""
    tools = build_tools(tmp_path)
    out = tools["find_definition"].invoke({"name": "pkg.mod.Nope.missing"})
    assert out == "'pkg.mod.Nope.missing' 정의를 찾지 못함"


def test_callers_and_callees(tmp_path):
    """find_callers 는 "caller -> callee"와 줄 번호를, find_callees 는 부르는 대상을 보여 준다."""
    tools = build_tools(tmp_path)
    out = tools["find_callers"].invoke({"name": "bar"})
    assert "App.run -> bar" in out and ":10" in out
    assert "bar" in tools["find_callees"].invoke({"symbol_name": "App.run"})


def test_read_code(tmp_path):
    """read_code 는 줄 번호를 붙여 범위만 읽고, 레포 밖·없는 파일은 거부한다 (경로 탈출 방지)."""
    tools = build_tools(tmp_path)
    out = tools["read_code"].invoke({"file": FILE, "start": 8, "end": 10})
    assert "    8| class App:" in out
    assert len(out.splitlines()) == 3
    escaped = tools["read_code"].invoke({"file": "../../etc/passwd", "start": 1, "end": 2})
    assert "레포 밖" in escaped
    assert "없음" in tools["read_code"].invoke({"file": "nope.py", "start": 1, "end": 2})


def test_search_code(tmp_path):
    """search_code 는 문자열이 나오는 위치를 "파일:줄"로 알려 준다."""
    tools = build_tools(tmp_path)
    assert "src/app/main.py:10" in tools["search_code"].invoke({"query": "self.bar"})


def test_tools_are_langchain_tools(tmp_path):
    """5개 모두 LLM에게 보여줄 이름과 설명을 가진다 (도구 선택의 근거)."""
    tools = build_tools(tmp_path)
    assert len(tools) == 5
    for t in tools.values():
        assert t.name and t.description
