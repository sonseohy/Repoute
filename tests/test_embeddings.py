"""repoute.indexing.embeddings(심볼 임베딩 저장·검색) 테스트. 가짜 임베더만 쓴다(API 호출 없음).

실행: uv run pytest -v tests/test_embeddings.py
"""

from pathlib import Path

from repoute.indexing.clone import RepoInfo
from repoute.indexing.embeddings import SymbolEmbeddingStore, embedding_path, symbol_text
from repoute.indexing.parser import parse_repo
from repoute.indexing.symbols import SymbolDB

SOURCE = '''\
class Session:
    """HTTP 세션."""

    def resolve_redirects(self, resp):
        """Follow redirects until too many redirects raise TooManyRedirects."""
        return resp


def parse_header(
    value,
):
    \'\'\'Parse a header
    value into tokens.\'\'\'
    return value.split(";")


def no_doc():
    return 1
'''

FILE = "pkg/sessions.py"


def _repo(tmp_path) -> tuple[SymbolDB, Path]:
    """심볼 4개(클래스 1·메서드 1·함수 2)짜리 작은 레포를 인덱싱한다."""
    root = tmp_path / "repo"
    (root / FILE).parent.mkdir(parents=True)
    (root / FILE).write_text(SOURCE, encoding="utf-8")
    db = SymbolDB()
    db.build(parse_repo(root))
    return db, root


def test_build_saves_all_symbols_then_uses_cache(tmp_path, fake_embedder):
    """build 는 심볼 수만큼 저장하고(배치로 나눠 호출), 같은 모델로 다시 부르면 API 없이 캐시."""
    db, root = _repo(tmp_path)
    store = SymbolEmbeddingStore(tmp_path / "emb.sqlite")
    assert store.build(db, root, fake_embedder, "fake", batch_size=3) is False
    assert store.count() == db.count()[0] == 4
    assert fake_embedder.calls == [3, 1]  # 4개를 3 + 1 배치로
    assert store.meta() == {"model": "fake", "dim": "64", "count": "4"}

    again = SymbolEmbeddingStore(tmp_path / "emb.sqlite")  # 새 객체여도 파일 캐시를 본다
    assert again.build(db, root, fake_embedder, "fake") is True and again.cached is True
    assert fake_embedder.calls == [3, 1]  # 추가 호출 없음

    assert again.build(db, root, fake_embedder, "other-model") is False  # 모델이 바뀌면 다시
    assert again.count() == 4


def test_search_ranks_overlapping_symbol_first(tmp_path, fake_embedder):
    """쿼리와 단어가 겹치는 심볼이 1위이고, 결과에 위치·유사도가 들어 있다."""
    db, root = _repo(tmp_path)
    store = SymbolEmbeddingStore(tmp_path / "emb.sqlite")
    store.build(db, root, fake_embedder, "fake")
    hits = store.search(fake_embedder.embed_query("too many redirects"), k=2)
    assert len(hits) == 2
    top = hits[0]
    assert (top["name"], top["kind"], top["file"]) == ("Session.resolve_redirects", "method", FILE)
    assert (top["line_start"], top["line_end"]) == (4, 6)
    assert top["score"] > hits[1]["score"]


def test_symbol_text_has_signature_docstring_and_file(tmp_path):
    """symbol_text 는 kind·name, def 줄, (여러 줄 시그니처 뒤) docstring, 파일 경로를 담는다."""
    lines = SOURCE.splitlines()
    row = {"name": "parse_header", "kind": "function", "file": FILE, "line_start": 9}
    text = symbol_text(row, lines)
    assert text.splitlines() == [
        "function parse_header",
        "def parse_header(",
        "Parse a header value into tokens.",  # 줄바꿈·들여쓰기는 한 칸 공백으로
        FILE,
    ]
    plain = {"name": "no_doc", "kind": "function", "file": FILE, "line_start": 17}
    assert symbol_text(plain, lines) == f"function no_doc\ndef no_doc():\n\n{FILE}"


def test_embedding_path_follows_index_prefix(tmp_path):
    """임베딩 파일은 인덱스와 같은 접두어(owner__name@sha7)에 .emb.sqlite 를 붙인다."""
    info = RepoInfo("owner", "name", "abcdef1234567", tmp_path)
    assert embedding_path(info, tmp_path) == tmp_path / "index" / "owner__name@abcdef1.emb.sqlite"
