"""휴리스틱으로 "어디서부터 읽을지" 진입점 후보를 고른다 (LLM 미사용)."""

import tomllib
from pathlib import Path

from repoute.indexing.parser import Symbol
from repoute.indexing.symbols import SymbolDB

ENTRY_NAMES = {"main", "create_app", "run", "cli", "app", "serve", "start"}
ENTRY_FILES = {"__main__.py", "main.py", "cli.py", "app.py", "server.py", "manage.py"}
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


def _short(symbol: Symbol) -> str:
    """메서드 "App.run" 은 끝 이름 "run" 으로 비교한다."""
    return symbol.name.rsplit(".", 1)[-1]


def _sorted(symbols: list[Symbol]) -> list[Symbol]:
    return sorted(symbols, key=lambda s: (s.file, s.line_start))


def find_entrypoints(db: SymbolDB, root: Path) -> list[Symbol]:
    """진입점 후보를 우선순위 순으로 (중복 제거, 최대 10개)."""
    # (1) pyproject scripts: 모듈 경로("pkg/mod.py")로 끝나는 파일의 그 함수
    scripted = []
    for module, func in scripts_from_pyproject(root):
        suffix = module.replace(".", "/") + ".py"
        scripted += [
            s
            for s in db.find_definition(func)
            if s.file == suffix or s.file.endswith("/" + suffix)  # "xpkg/mod.py" 는 제외
        ]
    # 이름이 ENTRY_NAMES 인 함수·메서드 전부 (클래스 제외)
    named = [
        s
        for name in ENTRY_NAMES
        for s in db.find_definition(name)
        if s.kind != "class" and _short(s) == name
    ]
    # (2) 진입점다운 파일 안의 것 → (3) 아무 파일
    in_entry_files = [s for s in named if Path(s.file).name in ENTRY_FILES]

    ordered = _sorted(scripted) + _sorted(in_entry_files) + _sorted(named)
    unique = list(dict.fromkeys(ordered))  # Symbol 은 frozen 이라 dict 키로 중복 제거 (순서 유지)
    return unique[:MAX_ENTRYPOINTS]
