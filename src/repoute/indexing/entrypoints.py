"""휴리스틱으로 "어디서부터 읽을지" 진입점 후보를 고르고 점수로 정렬한다 (LLM 미사용).

v2: 라이브러리형 레포는 `main` 이 아니라 공개 API 가 진입점이다. 그래서 최상위 패키지
`__init__.py` 가 내보내는 이름과, 내보낸 클래스의 핵심 메서드를 후보에 더한다.
v3: export 가 많으면(라이브러리 신호) scripts 가산을 +1 로 낮추고 CLI 경로를 −3 한다.
"""

import ast
import tomllib
from collections import Counter
from pathlib import Path

from repoute.indexing.parser import SKIP_DIRS, Symbol
from repoute.indexing.symbols import SymbolDB

ENTRY_NAMES = {"main", "create_app", "run", "cli", "app", "serve", "start"}
ENTRY_FILES = {"__main__.py", "main.py", "cli.py", "app.py", "server.py", "manage.py"}
PUBLIC_METHOD_NAMES = {
    *("request", "send", "get", "post", "print", "render", "run"),
    *("invoke", "call", "__call__", "handle", "execute", "main"),
}
# 경로 조각(앞 "_" 와 ".py" 를 뗀 것)이 이 중 하나면 감점. "_compat" → "compat"
PENALTY_PARTS = {"help", "debug", "compat", "testing", "test", "internal", "utils"}
# CLI 경로 조각(".py" 만 뗀 것). 라이브러리면 `_main`·`__main__` 도 편의 CLI 로 본다
CLI_PARTS = {"cli", "_cli"}
LIBRARY_CLI_PARTS = CLI_PARTS | {"_main", "__main__"}
# 최상위 __init__ 이 이만큼 이상 export 하면 라이브러리 (gold 5개 레포는 8~70개)
LIBRARY_EXPORTS_MIN = 5
MAX_ENTRYPOINTS = 10


def scripts_from_pyproject(root: Path) -> list[tuple[str, str]]:
    """pyproject.toml 의 [project.scripts] 값 "pkg.mod:func" 들을 (모듈, 함수) 로. 없으면 []."""
    try:
        data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):  # 파일이 없거나 깨졌으면 단서 없음
        return []
    scripts = data.get("project", {}).get("scripts", {})
    pairs = []
    for value in scripts.values():
        if isinstance(value, str) and ":" in value:
            module, func = value.split(":", 1)
            pairs.append((module.strip(), func.strip()))
    return pairs


def _exports(init_path: Path) -> dict[str, str | None]:
    """공개 이름 → 가져온 모듈("termui", "_api.sub"). __all__ 에만 있으면 None (출처 모름).

    함수 안·TYPE_CHECKING 안의 import 도 센다 (rich 의 Console 처럼 공개 함수가 쓰는 것).
    """
    try:
        tree = ast.parse(init_path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return {}
    package = init_path.parent.name
    found: dict[str, str | None] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level == 1 or module.split(".")[0] == package:  # 외부 패키지 이름은 정의가 없다
                source = module if node.level == 1 else module.removeprefix(package + ".")
                found.update({a.name: source for a in node.names if a.name != "*"})
        elif isinstance(node, (ast.Assign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
                if isinstance(node.value, (ast.List, ast.Tuple)):
                    for e in node.value.elts:
                        if isinstance(e, ast.Constant) and isinstance(e.value, str):
                            found.setdefault(e.value, None)  # import 로 출처를 알면 그것을 둔다
    return found


def exported_names(init_path: Path) -> list[str]:
    """__init__.py 가 자기 패키지에서 import 하는 이름 + __all__ 의 이름 (중복 제거). 실패면 []."""
    return list(_exports(init_path))


def _export_defs(db: SymbolDB, prefix: str, name: str, source: str | None) -> list[Symbol]:
    """공개 이름의 최상위 정의. 출처 모듈을 알면 그 파일, 모르면 __init__.py → 패키지 전체 순."""
    defs = [s for s in db.find_definition(name) if s.name == name and s.file.startswith(prefix)]
    if source is not None:
        base = prefix + source.replace(".", "/")
        return [s for s in defs if s.file in (base + ".py", base + "/__init__.py")]
    in_init = [s for s in defs if s.file == prefix + "__init__.py"]
    return in_init or defs


def top_packages(root: Path) -> list[Path]:
    """루트 또는 src/ 바로 아래에서 __init__.py 가 있는 디렉터리 (tests 등 SKIP_DIRS 제외)."""
    dirs = [d for base in (root, root / "src") if base.is_dir() for d in base.iterdir()]
    return sorted(d for d in dirs if d.name not in SKIP_DIRS and (d / "__init__.py").is_file())


def _short(symbol: Symbol) -> str:
    """메서드 "App.run" 은 끝 이름 "run" 으로 비교한다."""
    return symbol.name.rsplit(".", 1)[-1]


def _penalty(symbol: Symbol, library: bool) -> int:
    """디버그·호환·도우미 경로 −2, CLI 경로 −2(라이브러리면 −3), `_` 비공개 이름 −2.

    라이브러리의 CLI(httpx/_main.py, fastapi/cli.py)는 편의 기능이라 더 깎는다 (기록 18 관찰 3).
    `__call__` 같은 dunder 는 비공개로 보지 않는다.
    """
    stems = [p.removesuffix(".py") for p in symbol.file.split("/")]
    score = -2 if any(p.lstrip("_") in PENALTY_PARTS for p in stems) else 0
    if any(p in (LIBRARY_CLI_PARTS if library else CLI_PARTS) for p in stems):
        score -= 3 if library else 2
    name = _short(symbol)
    if name.startswith("_") and not (name.startswith("__") and name.endswith("__")):
        score -= 2
    return score


def _methods(db: SymbolDB, cls: Symbol) -> list[Symbol]:
    """클래스의 메서드 중 이름이 PUBLIC_METHOD_NAMES 인 것 (같은 파일의 "Cls.name")."""
    return [
        m
        for name in PUBLIC_METHOD_NAMES
        for m in db.find_definition(name)
        if m.name == f"{cls.name}.{name}" and m.file == cls.file
    ]


def find_entrypoints_scored(db: SymbolDB, root: Path) -> list[tuple[Symbol, int]]:
    """(심볼, 점수) 를 점수 내림차순 → 파일 → 줄 순으로. 점수 0 이하는 빼고 최대 10개.

    같은 (file, name) 은 규칙 사이에서 점수를 합산하고, 한 규칙 안에서는 한 번만 센다
    (@overload 로 같은 메서드가 여러 번 정의돼도 부풀지 않게). 대표 심볼은 첫 줄의 것.
    """
    scores: Counter[tuple[str, str]] = Counter()
    first: dict[tuple[str, str], Symbol] = {}

    def add(symbols: list[Symbol], points: int) -> None:
        for s in symbols:  # find_definition 이 줄 순이라 처음 본 것이 첫 줄
            first.setdefault((s.file, s.name), s)
        scores.update({(s.file, s.name): points for s in symbols})  # dict 라 한 규칙에 한 번

    # (1) 패키지 공개 API +3, 그중 클래스의 핵심 메서드 +2
    export_count = 0  # 최상위 패키지 중 가장 많이 export 하는 것의 이름 수 = 라이브러리 신호
    for pkg in top_packages(root):
        prefix = pkg.relative_to(root).as_posix() + "/"
        exports = _exports(pkg / "__init__.py")
        export_count = max(export_count, len(exports))
        for name, source in exports.items():
            defs = _export_defs(db, prefix, name, source)
            add(defs, 3)
            add([m for c in defs if c.kind == "class" for m in _methods(db, c)], 2)
    # (2) pyproject scripts +3 (라이브러리면 +1): 모듈 경로("pkg/mod.py")로 끝나는 파일의 그 함수
    library = export_count >= LIBRARY_EXPORTS_MIN
    for module, func in scripts_from_pyproject(root):
        suffix = module.replace(".", "/") + ".py"
        hits = db.find_definition(func)  # "xpkg/mod.py" 는 제외
        hits = [s for s in hits if s.file == suffix or s.file.endswith("/" + suffix)]
        add(hits, 1 if library else 3)  # 라이브러리의 scripts 는 편의 CLI (httpx, fastapi)
    # (3) 이름이 ENTRY_NAMES 인 함수·메서드 +1, 진입점다운 파일 안이면 +2 더 (클래스 제외)
    for name in ENTRY_NAMES:
        named = [s for s in db.find_definition(name) if s.kind != "class" and _short(s) == name]
        add(named, 1)
        add([s for s in named if Path(s.file).name in ENTRY_FILES], 2)
    ranked = [(first[k], n + _penalty(first[k], library)) for k, n in scores.items()]
    ranked = [(s, n) for s, n in ranked if n > 0 and _short(s) != "__init__"]
    ranked.sort(key=lambda p: (-p[1], p[0].file, p[0].line_start))
    return ranked[:MAX_ENTRYPOINTS]


def find_entrypoints(db: SymbolDB, root: Path) -> list[Symbol]:
    """진입점 후보를 점수 순으로 (중복 없음, 최대 10개). 호출 측 호환용으로 Symbol 만."""
    return [s for s, _ in find_entrypoints_scored(db, root)]
