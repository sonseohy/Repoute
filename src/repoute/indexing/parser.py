"""파이썬 파일을 ast로 읽어 심볼·import·호출을 뽑는다 (LLM 미사용)."""

import ast
from dataclasses import dataclass
from pathlib import Path

# 경로에 이 이름의 디렉터리가 있으면 인덱싱하지 않는다 (가상환경·빌드 산출물·테스트·문서)
SKIP_DIRS = {
    ".git",
    ".repoute_cache",  # 이 도구의 clone 캐시 (레포 자신을 인덱싱할 때 섞이지 않게)
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "build",
    "dist",
    "site-packages",
    "tests",
    "test",
    "docs",
    "examples",
}


@dataclass(frozen=True)
class Symbol:
    """함수·클래스·메서드 하나."""

    name: str  # "create_app", 메서드는 "App.run"
    kind: str  # function | class | method
    file: str  # 레포 루트 기준 상대 경로 (posix)
    line_start: int
    line_end: int


@dataclass(frozen=True)
class Call:
    """이름 기준 호출 하나: caller 안에서 callee()를 부른다."""

    caller: str  # 호출이 들어 있는 심볼 이름, 모듈 최상위면 "<module>"
    callee: str  # foo() → "foo", a.b.c() → "c"
    file: str
    line: int


@dataclass(frozen=True)
class ParsedFile:
    """파일 하나의 파싱 결과."""

    file: str
    module: str  # "src.app.main"
    symbols: list[Symbol]
    imports: list[str]  # 절대 모듈 이름들
    calls: list[Call]


def module_name(rel: str) -> str:
    """상대 경로 → 모듈 이름. "src/app/main.py" → "src.app.main", __init__.py는 패키지 이름."""
    parts = rel.removesuffix(".py").split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def resolve_from(module: str, is_package: bool, level: int, name: str | None) -> str:
    """from-import의 모듈을 절대 이름으로. level 0은 절대, 1은 현재 패키지, 2는 그 위."""
    if level == 0:
        return name or ""
    parts = module.split(".")
    if not is_package:
        parts = parts[:-1]  # 일반 파일은 마지막 조각을 떼야 자기 패키지가 된다
    base = parts[: max(len(parts) - (level - 1), 0)]  # 점 하나 늘 때마다 한 단계 위로
    return ".".join(base + ([name] if name else []))


class _Visitor(ast.NodeVisitor):
    """트리를 한 번 돌며 심볼·import·호출을 모은다."""

    def __init__(self, file: str, module: str, is_package: bool):
        self.file = file
        self.module = module
        self.is_package = is_package
        self.stack: list[Symbol] = []  # 지금 들어와 있는 심볼들 (바깥 → 안쪽)
        self.symbols: list[Symbol] = []
        self.imports: list[str] = []
        self.calls: list[Call] = []

    def _enter(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef, kind: str):
        parent = self.stack[-1] if self.stack else None
        name = f"{parent.name}.{node.name}" if parent else node.name
        if kind == "function" and parent and parent.kind == "class":
            kind = "method"  # 클래스 바로 안의 함수는 메서드
        symbol = Symbol(name, kind, self.file, node.lineno, node.end_lineno or node.lineno)
        self.symbols.append(symbol)
        self.stack.append(symbol)
        self.generic_visit(node)  # 몸통 안의 호출·중첩 정의를 이 심볼 안에서 방문
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef):
        self._enter(node, "function")

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        self._enter(node, "function")

    def visit_ClassDef(self, node: ast.ClassDef):
        self._enter(node, "class")

    def visit_Import(self, node: ast.Import):
        self.imports.extend(alias.name for alias in node.names)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        self.imports.append(resolve_from(self.module, self.is_package, node.level, node.module))

    def visit_Call(self, node: ast.Call):
        func = node.func
        if isinstance(func, ast.Name):
            callee = func.id  # foo()
        elif isinstance(func, ast.Attribute):
            callee = func.attr  # obj.bar() → "bar"
        else:
            callee = None  # f()() 처럼 이름이 없는 호출은 건너뜀
        if callee:
            caller = self.stack[-1].name if self.stack else "<module>"
            self.calls.append(Call(caller, callee, self.file, node.lineno))
        self.generic_visit(node)  # 인자 안의 호출도 센다


def parse_file(path: Path, root: Path) -> ParsedFile | None:
    """파일 하나를 파싱한다. 읽거나 파싱할 수 없으면 None."""
    rel = path.relative_to(root).as_posix()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
    except (SyntaxError, UnicodeDecodeError, ValueError):  # ValueError: 널 바이트 (3.11)
        return None
    visitor = _Visitor(rel, module_name(rel), path.name == "__init__.py")
    visitor.visit(tree)
    imports = list(dict.fromkeys(visitor.imports))  # 순서는 지키고 중복만 제거
    return ParsedFile(rel, visitor.module, visitor.symbols, imports, visitor.calls)


def parse_repo(root: Path) -> list[ParsedFile]:
    """레포의 .py 파일을 경로 순으로 파싱한다. SKIP_DIRS 아래와 파싱 실패 파일은 뺀다."""
    results = []
    for path in sorted(root.rglob("*.py")):
        if SKIP_DIRS.intersection(path.relative_to(root).parts[:-1]):
            continue
        parsed = parse_file(path, root)
        if parsed is not None:
            results.append(parsed)
    return results
