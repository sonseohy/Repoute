"""인덱스와 레포 파일을 읽는 LangChain 도구 5종."""

from pathlib import Path

from langchain_core.tools import tool

from repoute.indexing.parser import SKIP_DIRS
from repoute.indexing.symbols import SymbolDB

MAX_RESULTS = 20  # 정의·호출 결과 상한
MAX_SEARCH = 30  # 문자열 검색 결과 상한
MAX_READ_LINES = 200  # read_code 한 번에 읽는 줄 수 상한


def _join(lines: list[str], limit: int = MAX_RESULTS) -> str:
    """상위 limit 줄만 남기고, 넘치면 "... 외 N개"를 붙인다."""
    if len(lines) > limit:
        return "\n".join(lines[:limit] + [f"... 외 {len(lines) - limit}개"])
    return "\n".join(lines)


def make_code_tools(db: SymbolDB, root: Path) -> list:
    """인덱스(db)와 레포 루트(root)를 붙여 LangChain 도구 5개를 만든다 (Phase 3 에이전트가 사용)."""
    root = root.resolve()

    # 도구는 db·root를 알아야 하지만 LLM에게는 name 같은 인자만 보여야 해서 클로저로 만든다
    @tool
    def find_definition(name: str) -> str:
        """함수·클래스·메서드가 어느 파일 몇 줄에 정의돼 있는지 찾을 때 쓴다.
        name은 심볼 이름("create_app", "App.run", 메서드 끝 이름 "run"도 가능)."""
        lines = [
            f"{s.file}:{s.line_start}-{s.line_end}  {s.kind}  {s.name}"
            for s in db.find_definition(name)
        ]
        return _join(lines) if lines else f"'{name}' 정의를 찾지 못함"

    @tool
    def find_callers(name: str) -> str:
        """어떤 함수가 어디서 호출되는지(누가 부르는지) 찾을 때 쓴다.
        name은 호출되는 함수 이름(점 없이 끝 이름, 예: "bar")."""
        lines = [f"{c.file}:{c.line}  {c.caller} -> {c.callee}" for c in db.find_callers(name)]
        return _join(lines) if lines else f"'{name}'을(를) 호출하는 곳을 찾지 못함"

    @tool
    def find_callees(symbol_name: str) -> str:
        """어떤 함수·메서드가 안에서 무엇을 호출하는지 따라갈 때 쓴다.
        symbol_name은 정의된 전체 이름(예: "create_app", "App.run")."""
        calls = db.find_callees(symbol_name)
        lines = [f"{c.file}:{c.line}  {c.caller} -> {c.callee}" for c in calls]
        return _join(lines) if lines else f"'{symbol_name}' 안의 호출을 찾지 못함"

    @tool
    def read_code(file: str, start: int, end: int) -> str:
        """파일의 실제 코드를 줄 번호와 함께 읽을 때 쓴다(인용 근거).
        file은 레포 루트 기준 경로, start·end는 1부터 세는 줄 번호(end 포함, 최대 200줄)."""
        path = (root / file).resolve()
        if not path.is_relative_to(root):
            return "레포 밖 경로라 읽을 수 없음"  # "../../etc/passwd" 같은 경로 탈출 방지
        if not path.is_file():
            return f"'{file}' 파일이 없음"
        start = max(start, 1)
        end = min(end, start + MAX_READ_LINES - 1)  # 최대 200줄
        text = path.read_text(encoding="utf-8", errors="replace").splitlines()
        picked = text[start - 1 : end]
        if not picked:
            return f"'{file}'에 {start}번째 줄이 없음 (전체 {len(text)}줄)"
        return "\n".join(f"{n:>5}| {line}" for n, line in enumerate(picked, start))

    @tool
    def search_code(query: str) -> str:
        """정의·호출 인덱스로 안 잡히는 문자열(변수명, 설정 키, 문구 등)을 찾을 때 쓴다.
        query는 찾을 부분 문자열이며 대소문자를 구분하지 않는다."""
        needle = query.lower()
        hits = []
        for path in sorted(root.rglob("*.py")):
            rel = path.relative_to(root)
            if SKIP_DIRS.intersection(rel.parts[:-1]):
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            for n, line in enumerate(lines, 1):
                if needle in line.lower():
                    hits.append(f"{rel.as_posix()}:{n}  {line.strip()[:120]}")
        return _join(hits, MAX_SEARCH) if hits else f"'{query}'을(를) 찾지 못함"

    return [find_definition, find_callers, find_callees, read_code, search_code]
