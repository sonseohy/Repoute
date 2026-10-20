"""repoute.indexing.imports(import 그래프) 테스트. 실행: uv run pytest -v tests/test_imports.py"""

from repoute.indexing.imports import build_import_graph, rank_modules, resolve_module
from repoute.indexing.parser import ParsedFile

KNOWN = {"src.repoute.config", "src.repoute.cli", "pkg.a"}


def parsed(module: str, imports: list[str]) -> ParsedFile:
    """심볼·호출은 비워 두고 모듈·import 만 채운 ParsedFile 을 만든다."""
    return ParsedFile(module.replace(".", "/") + ".py", module, [], imports, [])


def test_resolve_module():
    """import 이름을 레포 안 모듈로 맞춘다: 정확 일치·접두어 차이·심볼은 찾고 외부는 None."""
    assert resolve_module("pkg.a", KNOWN) == "pkg.a"  # 정확 일치
    assert resolve_module("repoute.config", KNOWN) == "src.repoute.config"  # 접두어 차이
    assert resolve_module("src.repoute.config.get_llm", KNOWN) == "src.repoute.config"  # 심볼
    assert resolve_module("os", KNOWN) is None  # 외부 패키지


def three_modules():
    """a 는 b·c 를, b 는 c 를, c 는 외부 os 만 import, d 는 고립된 예제 모듈들."""
    return [
        parsed("app.a", ["app.b", "app.c"]),
        parsed("app.b", ["app.c"]),
        parsed("app.c", ["os"]),
        parsed("app.d", []),
    ]


def test_build_import_graph():
    """레포 안 import 만 엣지가 되고 외부 패키지는 빠지며, 고립 모듈도 노드(file 포함)가 된다."""
    graph = build_import_graph(three_modules())
    assert set(graph.edges) == {("app.a", "app.b"), ("app.a", "app.c"), ("app.b", "app.c")}
    assert "os" not in graph
    assert "app.d" in graph  # 고립 모듈도 노드
    assert graph.nodes["app.a"]["file"] == "app/a.py"


def test_rank_modules():
    """가장 많이 import 되는 모듈이 1등이고, 빈 그래프는 빈 순위를 돌려준다."""
    ranking = rank_modules(build_import_graph(three_modules()))
    assert ranking[0][0] == "app.c"  # 가장 많이 import 되는 모듈이 1등
    assert rank_modules(build_import_graph([])) == []
