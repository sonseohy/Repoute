"""인용한 줄을 감싸는 심볼 찾기(LLM 없음)와 그 심볼 설명(fast 모델 1회, SQLite 캐시)."""

import ast
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field

from repoute.indexing.symbols import SymbolDB
from repoute.prompts import explain_v2
from repoute.tools.code_tools import MAX_READ_LINES

# watch 항목의 "(줄 N)" 또는 "(줄 N~M)". evals/experiments/explain_quality.py 의 것과 같다.
LINE_REF_RE = re.compile(r"\(줄\s*(\d+)(?:\s*[~\-–]\s*(\d+))?\)")
WATCH_MAX = 4  # 프롬프트 규칙은 2~4개. 첫 항목이 핵심 로직이므로 넘치면 앞쪽을 남긴다

CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS explanations (
    repo_id TEXT, file TEXT, line_start INTEGER, what TEXT, watch_json TEXT,
    docstring_ko TEXT, prompt_version TEXT,
    PRIMARY KEY (repo_id, file, line_start, prompt_version)
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


def _fix_thousands(n: int, line_start: int, line_end: int) -> int:
    """범위 밖 줄 번호 n 에 천 단위(line_start 의 천 자리)를 더해 범위 안이 되면 그 값, 아니면 n.

    모델이 "1390" 을 "390" 처럼 앞자리를 잘라 쓰는 결함 보정. 범위 안이 안 되면 지어내지 않는다.
    """
    if line_start <= n <= line_end:
        return n
    fixed = n + (line_start // 1000) * 1000
    return fixed if line_start <= fixed <= line_end else n


def normalize_watch(watch: list[str], line_start: int, line_end: int) -> list[str]:
    """LLM 이 만든 watch 의 형식 결함을 결정론적으로 고친다 (LLM 없음).

    (a) "(줄 N)" 만 있는 항목은 바로 앞 항목 끝에 붙인다 (앞 항목이 없으면 버린다).
    (b) 범위 밖 줄 번호는 천 단위를 더해 범위 안이 되면 바꾼다 (N, M 각각).
    (c) 그래도 범위 밖인 참조는 지우지 않는다 (watch_ref_issues 로 셀 수 있다).
    (d) WATCH_MAX 개를 넘으면 앞쪽만 남긴다.
    """
    merged: list[str] = []
    for item in watch:
        ref_only = LINE_REF_RE.search(item) and not LINE_REF_RE.sub("", item).strip()
        if not ref_only:
            merged.append(item)
        elif merged:  # 앞 항목이 없으면 이 참조는 버린다
            merged[-1] = f"{merged[-1].rstrip()} {item.strip()}"

    def fix(m: re.Match) -> str:
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        fa, fb = _fix_thousands(a, line_start, line_end), _fix_thousands(b, line_start, line_end)
        if (fa, fb) == (a, b):
            return m.group(0)  # 바꿀 게 없으면 원문 그대로
        return f"(줄 {fa})" if m.group(2) is None else f"(줄 {fa}~{fb})"

    return [LINE_REF_RE.sub(fix, item) for item in merged][:WATCH_MAX]


def watch_ref_issues(watch: list[str], line_start: int, line_end: int) -> int:
    """줄 참조가 없거나 참조 중 하나라도 범위 [line_start, line_end] 밖인 항목 수.

    explain_quality.py 의 ref_validity 와 같은 기준(항목 단위)이다.
    """
    issues = 0
    for item in watch:
        refs = [(int(a), int(b or a)) for a, b in LINE_REF_RE.findall(item)]
        if not refs or not all(line_start <= a <= b <= line_end for a, b in refs):
            issues += 1
    return issues


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """캐시 테이블을 만든다. 옛 DB(prompt_version 컬럼 없음)는 새 PK 로 옮긴다.

    PK 를 바꾸려면 ALTER 로는 안 되므로 이름을 바꿔 두고 새 테이블에 복사한 뒤 지운다.
    옛 행은 모두 v1 프롬프트 결과이므로 'v1' 로 둔다. docstring_ko 컬럼조차 없는
    더 옛날 DB 는 docstring_ko 를 NULL 로 옮긴다 (explain_symbol 이 필요할 때 다시 만든다).
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(explanations)")}
    if not columns or "prompt_version" in columns:  # 테이블이 없거나 이미 새 스키마
        conn.executescript(CACHE_SCHEMA)
        return
    doc = "docstring_ko" if "docstring_ko" in columns else "NULL"
    conn.execute("ALTER TABLE explanations RENAME TO explanations_old")
    conn.executescript(CACHE_SCHEMA)
    conn.execute(
        "INSERT INTO explanations"
        " (repo_id, file, line_start, what, watch_json, docstring_ko, prompt_version)"
        f" SELECT repo_id, file, line_start, what, watch_json, {doc}, 'v1'"
        " FROM explanations_old"
    )
    conn.execute("DROP TABLE explanations_old")
    conn.commit()


def explain_symbol(
    llm,
    root: Path,
    span: SymbolSpan,
    cache_path: Path,
    repo_id: str,
    prompts=explain_v2,
) -> tuple[Explanation, bool]:
    """심볼 코드를 fast 모델에 보내 설명을 만든다. (repo_id, file, line_start, 버전) 으로 캐시.

    prompts 는 EXPLAIN·VERSION 을 가진 프롬프트 모듈(기본 explain_v2). 프롬프트가 바뀌면
    버전이 달라 옛 결과를 재사용하지 않는다. 반환 (설명, cached).
    연결은 이 함수 안에서 열고 닫는다 (요청 스레드마다 따로).
    """
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    key = (repo_id, span.file, span.line_start, prompts.VERSION)
    conn = sqlite3.connect(cache_path)
    try:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT what, watch_json, docstring_ko FROM explanations"
            " WHERE repo_id = ? AND file = ? AND line_start = ? AND prompt_version = ?",
            key,
        ).fetchone()
        # docstring_ko 가 NULL 인 옛 행은 docstring 이 있을 때만 다시 만든다
        if row is not None and (row[2] is not None or not span.docstring):
            what, watch = row[0], json.loads(row[1])
            # 후처리 도입 전에 저장된 행도 같은 모양으로 (멱등이라 안전)
            watch = normalize_watch(watch, span.line_start, span.line_end)
            return Explanation(what=what, watch=watch, docstring_ko=row[2] or ""), True
        code, truncated = _numbered_code(root, span)
        prompt = prompts.EXPLAIN.format(name=span.name, kind=span.kind, file=span.file, code=code)
        result = llm.with_structured_output(Explanation).invoke(prompt)
        watch = normalize_watch(result.watch, span.line_start, span.line_end)
        result = result.model_copy(update={"watch": watch})
        if not span.docstring:  # docstring 이 없는데 지어낸 번역은 버린다
            result = result.model_copy(update={"docstring_ko": ""})
        if truncated:  # 긴 심볼은 앞부분만 봤다는 것을 알린다
            what = f"(앞 {MAX_READ_LINES}줄 기준) {result.what}"
            result = Explanation(what=what, watch=result.watch, docstring_ko=result.docstring_ko)
        conn.execute(
            "INSERT OR REPLACE INTO explanations"
            " (repo_id, file, line_start, prompt_version, what, watch_json, docstring_ko)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
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
