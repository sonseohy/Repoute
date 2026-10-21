"""심볼 단위 임베딩: 심볼 하나를 짧은 텍스트로 만들어 벡터로 저장하고 코사인 유사도로 찾는다.

RAG 1단계. 함수 이름이 없는 질문("리디렉트를 너무 많이 따라가면 어디서 예외가 나?")에서
출발점 심볼을 찾는 용도라, 파일 조각이 아니라 심볼(함수·메서드·클래스) 하나를 단위로 삼는다.
결과가 곧 "kind name — file:줄" 이 되어 read_code 로 바로 이어진다.
"""

import logging
import sqlite3
import time
from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager
from pathlib import Path

import numpy as np

from repoute.indexing.clone import RepoInfo
from repoute.indexing.index import RepoIndex, _index_prefix
from repoute.indexing.symbols import SymbolDB

logger = logging.getLogger(__name__)

KINDS = ("function", "method", "class")  # 임베딩할 심볼 종류 (symbols 테이블 전부)
SIGNATURE_LIMIT = 200  # def 줄 최대 글자 수
DOCSTRING_LIMIT = 300  # docstring 최대 글자 수
SCHEMA = """
CREATE TABLE IF NOT EXISTS embeddings (
    name TEXT, kind TEXT, file TEXT, line_start INTEGER, line_end INTEGER,
    model TEXT, vector BLOB
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def _docstring(lines: list[str], def_index: int) -> str:
    """def 줄(0부터 센 번호) 바로 아래 docstring 을 ast 없이 꺼낸다. 없으면 빈 문자열.

    시그니처가 여러 줄이면 ':' 로 끝나는 줄까지 건너뛰고(최대 10줄), 다음 줄이 \"\"\"·'''
    로 시작할 때만 닫는 따옴표까지 모은다. 공백은 한 칸으로 줄여 토큰을 아낀다.
    """
    i = def_index
    while i < min(len(lines) - 1, def_index + 10) and not lines[i].rstrip().endswith(":"):
        i += 1
    rest = "\n".join(lines[i + 1 : i + 40]).lstrip()  # 40줄이면 300자 docstring 엔 충분
    quote = rest[:3]
    if quote not in ('"""', "'''"):
        return ""
    end = rest.find(quote, 3)
    body = rest[3:end] if end != -1 else rest[3:]
    return " ".join(body.split())[:DOCSTRING_LIMIT]


def symbol_text(sym_row: Mapping, source_lines: list[str]) -> str:
    """심볼 하나의 임베딩 입력: "{kind} {name}\\n{def 줄}\\n{docstring}\\n{file}".

    sym_row 는 symbols 테이블의 행(name·kind·file·line_start 를 키로 꺼낼 수 있으면 된다).
    본문 전체가 아니라 이름·시그니처·docstring 만 넣어 비용을 심볼 수에 비례하게 묶는다.
    """
    index = sym_row["line_start"] - 1
    signature = source_lines[index].strip()[:SIGNATURE_LIMIT] if index < len(source_lines) else ""
    doc = _docstring(source_lines, index) if signature else ""
    return f"{sym_row['kind']} {sym_row['name']}\n{signature}\n{doc}\n{sym_row['file']}"


def embedding_path(info: RepoInfo, cache_dir: Path | str) -> Path:
    """임베딩 파일 경로: 인덱스와 같은 접두어 + ".emb.sqlite" (커밋이 바뀌면 파일도 바뀐다)."""
    prefix = _index_prefix(info, cache_dir)
    return prefix.with_name(prefix.name + ".emb.sqlite")


class SymbolEmbeddingStore:
    """심볼 벡터를 SQLite 파일 하나에 넣고, 메모리 행렬로 올려 코사인 유사도로 찾는다."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.cached: bool | None = None  # 마지막 build 가 캐시로 건너뛰었는지
        self._rows: list[dict] | None = None  # search 용 메모리 사본 (build 하면 비운다)
        self._matrix: np.ndarray | None = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """쓸 때마다 열고 닫는다. SQLite 연결은 만든 스레드에서만 쓸 수 있는데(troubleshooting 04),
        이 저장소는 API 요청(다른 스레드)마다 재사용될 수 있어 연결을 들고 있지 않는다."""
        with closing(sqlite3.connect(self.path)) as conn:
            conn.row_factory = sqlite3.Row
            yield conn
            conn.commit()

    def meta(self) -> dict[str, str]:
        """저장된 model·dim·count. 아직 build 전이면 빈 dict."""
        with self._connect() as conn:
            return {row["key"]: row["value"] for row in conn.execute("SELECT * FROM meta")}

    def count(self) -> int:
        """저장된 벡터 수."""
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]

    def build(
        self, db: SymbolDB, root: Path, embedder, model_name: str, batch_size: int = 100
    ) -> bool:
        """symbols 의 함수·메서드·클래스를 임베딩해 저장한다. 캐시로 건너뛰었으면 True.

        같은 model 로 같은 건수가 이미 있으면 API 를 부르지 않는다 (임베딩은 유료 호출).
        벡터를 다 받은 뒤 한 번에 쓰므로, 중간에 API 가 실패하면 기존 파일은 그대로다.
        """
        placeholders = ", ".join("?" * len(KINDS))
        rows = db.conn.execute(
            f"SELECT * FROM symbols WHERE kind IN ({placeholders}) ORDER BY file, line_start",
            KINDS,
        ).fetchall()
        meta = self.meta()
        if meta.get("model") == model_name and meta.get("count") == str(len(rows)):
            self.cached = True
            logger.info("embeddings cache hit %s: count=%d", self.path.name, len(rows))
            return True

        sources: dict[str, list[str]] = {}  # 파일마다 한 번만 읽는다
        texts = []
        for row in rows:
            if row["file"] not in sources:
                path = Path(root) / row["file"]
                text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
                sources[row["file"]] = text.splitlines()
            texts.append(symbol_text(row, sources[row["file"]]))

        start = time.perf_counter()  # 측정 원칙: 건수·토큰·시간을 함께 남긴다
        vectors: list[list[float]] = []
        for i in range(0, len(texts), batch_size):
            vectors += embedder.embed_documents(texts[i : i + batch_size])
        matrix = np.zeros((len(texts), 0), dtype=np.float32)  # 심볼이 없는 레포
        if texts:
            matrix = np.asarray(vectors, dtype=np.float32).reshape(len(texts), -1)

        with self._connect() as conn:
            conn.execute("DELETE FROM embeddings")
            conn.execute("DELETE FROM meta")
            conn.executemany(
                "INSERT INTO embeddings VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        r["name"],
                        r["kind"],
                        r["file"],
                        r["line_start"],
                        r["line_end"],
                        model_name,
                        vec.tobytes(),
                    )
                    for r, vec in zip(rows, matrix, strict=True)
                ],
            )
            meta_rows = {"model": model_name, "dim": matrix.shape[1], "count": len(rows)}
            conn.executemany(
                "INSERT INTO meta VALUES (?, ?)", [(k, str(v)) for k, v in meta_rows.items()]
            )

        chars = sum(len(t) for t in texts)
        logger.info(
            "embedded %s: count=%d chars=%d tokens~%d time=%.2fs",
            self.path.name,
            len(texts),
            chars,
            chars // 4,  # 토큰 추정: 글자 수 / 4 (실제 토큰은 제공자 대시보드에서 확인)
            time.perf_counter() - start,
        )
        self.cached, self._rows, self._matrix = False, None, None
        return False

    def _load(self) -> tuple[list[dict], np.ndarray]:
        """전체 벡터를 한 번 읽어 행 길이 1로 정규화한 행렬로 둔다 (내적 = 코사인 유사도)."""
        if self._rows is None:
            with self._connect() as conn:
                rows = conn.execute("SELECT * FROM embeddings").fetchall()
            self._rows = [{k: r[k] for k in r.keys() if k != "vector"} for r in rows]
            vectors = [np.frombuffer(r["vector"], dtype=np.float32) for r in rows]
            matrix = np.vstack(vectors) if vectors else np.zeros((0, 0), dtype=np.float32)
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            self._matrix = matrix / np.where(norms == 0, 1, norms)  # 0 벡터는 그대로 0
        return self._rows, self._matrix

    def search(self, query_vector, k: int = 5) -> list[dict]:
        """코사인 유사도 상위 k 개. 각 dict 는 name·kind·file·line_start·line_end·model·score.

        벡터 DB·근사 검색(ANN) 없이 전체를 훑는다. 레포 하나의 심볼은 수천 개라
        (수천 × 1536 차원) 행렬 곱 한 번이 수 ms 이고, 정확한 상위 k 를 얻는다.
        """
        rows, matrix = self._load()
        if not rows:
            return []
        query = np.asarray(query_vector, dtype=np.float32)
        scores = matrix @ (query / (np.linalg.norm(query) or 1.0))
        top = np.argsort(-scores, kind="stable")[:k]  # 동점이면 저장 순서(file, line) 유지
        return [{**rows[i], "score": float(scores[i])} for i in top]


def ensure_embeddings(
    index: RepoIndex, embedder, model_name: str, cache_dir: Path | str
) -> SymbolEmbeddingStore:
    """레포 인덱스의 심볼 임베딩을 준비한다. 같은 커밋·모델이면 파일 캐시를 쓴다.

    첫 호출은 임베딩 API 비용이 든다. 캐시 여부는 돌려준 store.cached 로 안다.
    """
    store = SymbolEmbeddingStore(embedding_path(index.info, cache_dir))
    store.build(index.db, index.info.path, embedder, model_name)
    return store
