"""인용한 줄을 감싸는 심볼 찾기(LLM 없음)와 그 심볼 설명(fast 모델 1회, SQLite 캐시)."""

import ast
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field

from repoute.indexing.symbols import SymbolDB
from repoute.prompts.explain_v1 import EXPLAIN
from repoute.tools.code_tools import MAX_READ_LINES

CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS explanations (
    repo_id TEXT, file TEXT, line_start INTEGER, what TEXT, watch_json TEXT,
    docstring_ko TEXT,
    PRIMARY KEY (repo_id, file, line_start)
);
"""


@dataclass(frozen=True)
class SymbolSpan:
    """줄을 감싸는 심볼 하나. 코드 뷰어는 line_start~line_end 를 통째로 보여 준다."""

    name: str
    kind: str  # function | class | method
    file: str
    line_start: int
    line_end: int
    docstring: str | None


class Explanation(BaseModel):
    """fast 모델의 구조화 출력: 이 심볼이 하는 일과 꼭 봐야 할 코드."""

    what: str = Field(description="이 심볼이 하는 일, 한국어 1~2문장")
    watch: list[str] = Field(
        description='꼭 봐야 할 코드 2~4개, 각 항목 끝에 "(줄 N)" 또는 "(줄 N~M)"'
    )
    docstring_ko: str = Field(
        default="",
        description="원문 docstring 의 핵심만 한국어 1~2문장 (무엇을 받아 무엇을 돌려주는지,"
        " 파라미터 나열 금지). docstring 이 없으면 빈 문자열",
    )


def _docstring(path: Path, line_start: int) -> str | None:
    """파일을 ast 로 파싱해 line_start 에서 시작하는 함수·클래스의 docstring. 실패하면 None."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError, ValueError):
        return None
    kinds = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    for node in ast.walk(tree):
        if isinstance(node, kinds) and node.lineno == line_start:
            return ast.get_docstring(node, clean=True)
    return None


def enclosing_symbol(db: SymbolDB, root: Path, file: str, line: int) -> SymbolSpan | None:
    """file 의 line 을 감싸는 가장 안쪽 심볼(메서드 > 클래스). 없으면 None.

    범위가 가장 짧은 심볼이 가장 안쪽이다. docstring 은 root/file 을 ast 로 읽어 뽑는다.
    """
    row = db.conn.execute(
        "SELECT name, kind, file, line_start, line_end FROM symbols"
        " WHERE file = ? AND line_start <= ? AND line_end >= ?"
        " ORDER BY (line_end - line_start) ASC LIMIT 1",
        (file, line, line),
    ).fetchone()
    if row is None:
        return None
    return SymbolSpan(**dict(row), docstring=_docstring(root / file, row["line_start"]))


def _numbered_code(root: Path, span: SymbolSpan) -> tuple[str, bool]:
    """심볼 코드를 줄 번호와 함께 (최대 MAX_READ_LINES 줄). 반환 (코드, 잘렸는지)."""
    lines = (root / span.file).read_text(encoding="utf-8", errors="replace").splitlines()
    end = min(span.line_end, span.line_start + MAX_READ_LINES - 1)
    picked = lines[span.line_start - 1 : end]
    code = "\n".join(f"{n:>5}| {t}" for n, t in enumerate(picked, span.line_start))
    return code, span.line_end > end


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """캐시 테이블을 만들고, 옛 DB 에 docstring_ko 컬럼이 없으면 추가한다 (옛 행은 NULL)."""
    conn.executescript(CACHE_SCHEMA)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(explanations)")}
    if "docstring_ko" not in columns:
        conn.execute("ALTER TABLE explanations ADD COLUMN docstring_ko TEXT")
        conn.commit()


def explain_symbol(
    llm, root: Path, span: SymbolSpan, cache_path: Path, repo_id: str
) -> tuple[Explanation, bool]:
    """심볼 코드를 fast 모델에 보내 설명을 만든다. (repo_id, file, line_start) 로 캐시.

    반환 (설명, cached). 연결은 이 함수 안에서 열고 닫는다 (요청 스레드마다 따로).
    """
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    key = (repo_id, span.file, span.line_start)
    conn = sqlite3.connect(cache_path)
    try:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT what, watch_json, docstring_ko FROM explanations"
            " WHERE repo_id = ? AND file = ? AND line_start = ?",
            key,
        ).fetchone()
        # docstring_ko 가 NULL 인 옛 행은 docstring 이 있을 때만 다시 만든다
        if row is not None and (row[2] is not None or not span.docstring):
            what, watch = row[0], json.loads(row[1])
            return Explanation(what=what, watch=watch, docstring_ko=row[2] or ""), True
        code, truncated = _numbered_code(root, span)
        prompt = EXPLAIN.format(name=span.name, kind=span.kind, file=span.file, code=code)
        result = llm.with_structured_output(Explanation).invoke(prompt)
        if not span.docstring:  # docstring 이 없는데 지어낸 번역은 버린다
            result = result.model_copy(update={"docstring_ko": ""})
        if truncated:  # 긴 심볼은 앞부분만 봤다는 것을 알린다
            what = f"(앞 {MAX_READ_LINES}줄 기준) {result.what}"
            result = Explanation(what=what, watch=result.watch, docstring_ko=result.docstring_ko)
        conn.execute(
            "INSERT OR REPLACE INTO explanations"
            " (repo_id, file, line_start, what, watch_json, docstring_ko)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                *key,
                result.what,
                json.dumps(result.watch, ensure_ascii=False),
                result.docstring_ko,
            ),
        )
        conn.commit()
        return result, False
    finally:
        conn.close()
