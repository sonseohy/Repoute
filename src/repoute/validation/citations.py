"""LLM 이 낸 `파일:줄` 인용이 실제 코드와 맞는지 검사한다 (LLM 미사용, 환각 방지)."""

from pathlib import Path

from pydantic import BaseModel, Field

from repoute.indexing.symbols import SymbolDB


class CodeRef(BaseModel):
    """코드 인용 하나. LLM 구조화 출력 스키마로도 그대로 쓴다."""

    file: str = Field(description="레포 루트 기준 상대 경로, 예: src/app/main.py")
    line: int = Field(description="1부터 세는 줄 번호")
    symbol: str | None = Field(default=None, description="그 줄이 속한 함수·클래스 이름 (알면)")


def _line_count(path: Path, cache: dict[Path, int]) -> int:
    """파일 줄 수. 같은 파일은 한 번만 읽는다."""
    if path not in cache:
        cache[path] = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    return cache[path]


def _check(ref: CodeRef, db: SymbolDB, root: Path, cache: dict[Path, int]) -> str | None:
    """인용 하나를 검사해 오류 메시지를, 맞으면 None 을 돌려준다."""
    where = f"{ref.file}:{ref.line}"
    path = (root / ref.file).resolve()
    if not path.is_relative_to(root) or not path.is_file():  # "../x.py" 같은 레포 밖 경로도 차단
        return f"{where} — 파일이 없음"
    n = _line_count(path, cache)
    if not 1 <= ref.line <= n:
        return f"{where} — 파일은 {n}줄까지"
    if ref.symbol:
        rel = path.relative_to(root).as_posix()  # "./src/a.py" 같은 표기도 DB 경로와 맞춘다
        hits = [
            s
            for s in db.find_definition(ref.symbol)
            if s.file == rel and s.line_start <= ref.line <= s.line_end
        ]
        if not hits:
            return f"{where} — '{ref.symbol}' 이(가) 그 줄에 없음"
    return None


def validate_citations(refs: list[CodeRef], db: SymbolDB, root: Path) -> list[str]:
    """틀린 인용마다 오류 메시지 한 줄. 빈 리스트면 전부 통과."""
    root = root.resolve()
    cache: dict[Path, int] = {}
    errors = [_check(ref, db, root, cache) for ref in refs]
    return [e for e in errors if e is not None]


def citation_validity(refs: list[CodeRef], db: SymbolDB, root: Path) -> float:
    """유효한 인용 비율 (0.0~1.0). 인용이 하나도 없으면 0.0 (근거 없는 답은 0점)."""
    if not refs:
        return 0.0
    wrong = len(validate_citations(refs, db, root))
    return (len(refs) - wrong) / len(refs)
