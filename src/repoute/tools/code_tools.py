"""인덱스와 레포 파일을 읽는 LangChain 도구 5종 (+ 옵트인 search_symbols)."""

from pathlib import Path

from langchain_core.tools import tool

from repoute.indexing.parser import SKIP_DIRS
from repoute.indexing.symbols import SymbolDB

MAX_RESULTS = 20  # 정의·호출 결과 상한
MAX_SEARCH = 30  # 문자열 검색 결과 상한
MAX_READ_LINES = 200  # read_code 한 번에 읽는 줄 수 상한
MAX_SYMBOL_HITS = 5  # search_symbols 결과 수 (유사도 상위)


def _join(lines: list[str], limit: int = MAX_RESULTS) -> str:
    """상위 limit 줄만 남기고, 넘치면 "... 외 N개"를 붙인다."""
    if len(lines) > limit:
        return "\n".join(lines[:limit] + [f"... 외 {len(lines) - limit}개"])
    return "\n".join(lines)


def _name_candidates(name: str) -> list[str]:
    """점 붙은 이름이 실패했을 때 다시 물어볼 이름들 (troubleshooting 18).

    "a.b.C.m" → ["C.m", "m"] (Class.method 다음 끝 이름). 점이 없으면 빈 목록.
    """
    parts = name.split(".")
    if len(parts) < 2:
        return []
    candidates = [".".join(parts[-2:])] if len(parts) > 2 else []
    return candidates + [parts[-1]]


def make_code_tools(db: SymbolDB, root: Path, store=None, embedder=None) -> list:
    """인덱스(db)와 레포 루트(root)를 붙여 LangChain 도구 5개를 만든다 (Phase 3 에이전트가 사용).

    store(SymbolEmbeddingStore)와 embedder 를 둘 다 주면 6번째 도구 search_symbols 를 붙인다.
    하나라도 없으면 도구 목록은 이전과 똑같다 (before 측정과 같은 조건을 지키는 옵트인).
    """
    root = root.resolve()

    # 도구는 db·root를 알아야 하지만 LLM에게는 name 같은 인자만 보여야 해서 클로저로 만든다
    @tool
    def find_definition(name: str) -> str:
        """함수·클래스·메서드가 어느 파일 몇 줄에 정의돼 있는지 찾을 때 쓴다.
        name은 모듈 경로 없이 심볼 이름만 쓴다(예: "get", "Session.request", 끝 이름 "run").
        결과 줄 끝의 read_code(...) 호출을 그대로 쓰면 정의 전체를 한 번에 읽는다."""
        note = ""
        symbols = db.find_definition(name)
        for candidate in [] if symbols else _name_candidates(name):
            symbols = db.find_definition(candidate)
            if symbols:  # 모듈 경로가 붙어 실패한 이름을 뒷부분으로 다시 찾았다
                note = f"('{name}' 은 없어 '{candidate}' 으로 찾음)"
                break
        lines = [
            f"{s.file}:{s.line_start}-{s.line_end}  {s.kind}  {s.name}"
            f'  → read_code("{s.file}", {s.line_start}, {s.line_end})'
            f"  인용: {s.file}:{s.line_start}"  # 정의는 def/class 줄로 인용하라는 데이터 힌트
            for s in symbols
        ]
        if not lines:
            return f"'{name}' 정의를 찾지 못함"
        return "\n".join(([note] if note else []) + [_join(lines)])

    @tool
    def find_callers(name: str) -> str:
        """어떤 함수가 어디서 호출되는지(누가 부르는지) 찾을 때 쓴다.
        name은 호출되는 함수 이름(점 없이 끝 이름, 예: "bar")."""
        lines = [f"{c.file}:{c.line}  {c.caller} -> {c.callee}" for c in db.find_callers(name)]
        return _join(lines) if lines else f"'{name}'을(를) 호출하는 곳을 찾지 못함"

    @tool
    def find_callees(symbol_name: str) -> str:
        """어떤 함수·메서드가 안에서 무엇을 호출하는지 볼 때 쓴다.
        다음으로 따라갈 함수를 고르는 용도.
        symbol_name은 정의된 전체 이름(예: "create_app", "App.run")."""
        calls = db.find_callees(symbol_name)
        lines = [f"{c.file}:{c.line}  {c.caller} -> {c.callee}" for c in calls]
        return _join(lines) if lines else f"'{symbol_name}' 안의 호출을 찾지 못함"

    @tool
    def read_code(file: str, start: int, end: int) -> str:
        """파일의 실제 코드를 줄 번호와 함께 읽을 때 쓴다(인용 근거).
        file은 레포 루트 기준 경로, start·end는 1부터 세는 줄 번호(end 포함, 최대 200줄).
        정의를 찾았으면 그 line_start~line_end 범위를 한 번에 읽어라. 최대 200줄.
        20줄씩 나눠 읽지 말 것."""
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

    tools = [find_definition, find_callers, find_callees, read_code, search_code]
    if store is None or embedder is None:
        return tools

    @tool
    def search_symbols(query: str) -> str:
        """함수·클래스 이름을 모를 때 동작을 설명하는 말로 찾는다(예: "리디렉트 횟수 초과 예외").
        이름을 알면 find_definition 을 먼저 쓴다. query 는 찾는 동작을 설명하는 짧은 문장.
        결과 줄 끝의 read_code(...) 호출을 그대로 쓰면 정의 전체를 한 번에 읽는다."""
        lines = []
        for h in store.search(embedder.embed_query(query), k=MAX_SYMBOL_HITS):
            file, start, end = h["file"], h["line_start"], h["line_end"]
            lines.append(
                f"{h['kind']} {h['name']} — {file}:{start}~{end} (유사도 {h['score']:.2f})"
                f'  → read_code("{file}", {start}, {end})  인용: {file}:{start}'
            )
        return "\n".join(lines) if lines else f"'{query}' 와 비슷한 심볼을 찾지 못함"

    return tools + [search_symbols]
