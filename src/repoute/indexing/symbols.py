"""심볼·호출 관계를 SQLite에 저장하고 이름으로 찾는다 (LLM 미사용)."""

import sqlite3
from pathlib import Path

from repoute.indexing.parser import Call, ParsedFile, Symbol

# 테이블과 인덱스. IF NOT EXISTS라서 기존 파일을 다시 열어도 안전하다
SCHEMA = """
CREATE TABLE IF NOT EXISTS symbols (
    name TEXT, kind TEXT, file TEXT, line_start INTEGER, line_end INTEGER
);
CREATE TABLE IF NOT EXISTS calls (caller TEXT, callee TEXT, file TEXT, line INTEGER);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_calls_callee ON calls(callee);
CREATE INDEX IF NOT EXISTS idx_calls_caller ON calls(caller);
"""


class SymbolDB:
    """심볼·호출 관계를 SQLite에 넣고 이름으로 찾는다 (LLM 미사용)."""

    def __init__(self, path: Path | str = ":memory:"):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row  # 행을 dict처럼 꺼낼 수 있게
        self.conn.executescript(SCHEMA)

    def build(self, parsed: list[ParsedFile]) -> None:
        """기존 행을 지우고 파싱 결과 전체를 다시 넣는다."""
        self.conn.execute("DELETE FROM symbols")
        self.conn.execute("DELETE FROM calls")
        self.conn.executemany(
            "INSERT INTO symbols VALUES (?, ?, ?, ?, ?)",
            [(s.name, s.kind, s.file, s.line_start, s.line_end) for p in parsed for s in p.symbols],
        )
        self.conn.executemany(
            "INSERT INTO calls VALUES (?, ?, ?, ?)",
            [(c.caller, c.callee, c.file, c.line) for p in parsed for c in p.calls],
        )
        self.conn.commit()

    def find_definition(self, name: str) -> list[Symbol]:
        """이름이 같거나 ".name"으로 끝나는 심볼. "run" → "App.run"도 찾는다."""
        rows = self.conn.execute(
            "SELECT * FROM symbols WHERE name = ? OR name LIKE ? ORDER BY file, line_start",
            (name, "%." + name),
        )
        return [Symbol(**dict(row)) for row in rows]

    def find_callers(self, name: str) -> list[Call]:
        """name을 부르는 호출들 (어디서 이 함수를 부르나)."""
        rows = self.conn.execute(
            "SELECT * FROM calls WHERE callee = ? ORDER BY file, line", (name,)
        )
        return [Call(**dict(row)) for row in rows]

    def find_callees(self, symbol_name: str) -> list[Call]:
        """symbol_name 안에서 일어나는 호출들 (이 함수가 무엇을 부르나)."""
        rows = self.conn.execute(
            "SELECT * FROM calls WHERE caller = ? ORDER BY file, line", (symbol_name,)
        )
        return [Call(**dict(row)) for row in rows]

    def count(self) -> tuple[int, int]:
        """(심볼 수, 호출 수). 인덱싱 결과 보고용."""
        symbols = self.conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
        calls = self.conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
        return symbols, calls

    def close(self) -> None:
        self.conn.close()
