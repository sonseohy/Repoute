"""clone → 파싱 → SQLite·import 그래프를 한 번에 만들고, 커밋 단위로 캐시한다 (LLM 미사용)."""

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import networkx as nx

from repoute.indexing.clone import RepoInfo, clone_repo
from repoute.indexing.imports import build_import_graph
from repoute.indexing.parser import parse_repo
from repoute.indexing.symbols import SymbolDB

logger = logging.getLogger(__name__)


@dataclass
class RepoIndex:
    """레포 하나의 인덱스. 이후 도구·그래프는 이것만 받아 쓴다."""

    info: RepoInfo
    db: SymbolDB
    graph: nx.DiGraph
    file_count: int
    elapsed: float  # 인덱싱(또는 캐시 로드)에 걸린 초
    cached: bool  # 캐시에서 읽었으면 True


def _index_prefix(info: RepoInfo, cache_dir: Path | str) -> Path:
    """인덱스 파일 경로 접두어. repo_id 의 '/' 는 파일명에 못 쓰므로 '__' 로 바꾼다."""
    return Path(cache_dir) / "index" / f"{info.owner}__{info.name}@{info.commit[:7]}"


def build_index(url: str, cache_dir: Path | str = ".repoute_cache") -> RepoIndex:
    """레포를 인덱싱한다. 같은 커밋의 인덱스 파일이 있으면 다시 만들지 않고 읽는다."""
    start = time.perf_counter()  # 측정 원칙 1: 처음부터 시간을 기록
    info = clone_repo(url, cache_dir)
    prefix = _index_prefix(info, cache_dir)
    sqlite_path = prefix.with_name(prefix.name + ".sqlite")
    graph_path = prefix.with_name(prefix.name + ".graph.json")

    cached = sqlite_path.exists() and graph_path.exists()
    if cached:
        db = SymbolDB(sqlite_path)
        data = json.loads(graph_path.read_text(encoding="utf-8"))
        graph = nx.node_link_graph(data, edges="links")  # directed 여부는 data 안에 저장돼 있다
        file_count = graph.number_of_nodes()  # 노드 = 파싱된 파일(모듈) 하나
    else:
        sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        parsed = parse_repo(info.path)
        db = SymbolDB(sqlite_path)
        db.build(parsed)  # 기존 행을 지우고 넣으므로 중간에 끊겼던 파일도 안전
        graph = build_import_graph(parsed)
        # graph.json 을 마지막에 써야 "두 파일이 다 있음 = 인덱싱 완료" 가 된다
        data = nx.node_link_data(graph, edges="links")
        graph_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        file_count = len(parsed)

    elapsed = time.perf_counter() - start
    symbols, calls = db.count()
    logger.info(
        "%s %s: files=%d symbols=%d calls=%d time=%.2fs",
        "cache hit" if cached else "indexed",
        info.repo_id,
        file_count,
        symbols,
        calls,
        elapsed,
    )
    return RepoIndex(info, db, graph, file_count, elapsed, cached)
