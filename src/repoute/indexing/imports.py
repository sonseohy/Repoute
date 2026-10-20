"""import 관계로 모듈 그래프를 만들고 PageRank로 핵심 모듈을 고른다 (LLM 미사용)."""

import networkx as nx

from repoute.indexing.parser import ParsedFile


def _match(name: str, known: set[str]) -> str | None:
    """정확히 같은 모듈, 없으면 이름이 "." + name 으로 끝나는 모듈 (여러 개면 정렬해 첫 번째)."""
    if name in known:
        return name
    suffixed = sorted(m for m in known if m.endswith("." + name))
    return suffixed[0] if suffixed else None


def resolve_module(name: str, known: set[str]) -> str | None:
    """import 이름 → 레포 안 모듈 이름. 외부 패키지(os, langchain)는 None."""
    found = _match(name, known)  # "repoute.config" → "src.repoute.config" 같은 접두어 차이도 흡수
    if found is None and "." in name:
        found = _match(name.rsplit(".", 1)[0], known)  # from a.b import c 의 c 가 심볼일 수 있다
    return found


def build_import_graph(parsed: list[ParsedFile]) -> nx.DiGraph:
    """노드 = 모듈, 엣지 A→B = "A 가 B 를 import 한다"."""
    graph = nx.DiGraph()
    for p in parsed:
        graph.add_node(p.module, file=p.file)  # import 가 없는 고립 모듈도 노드로 둔다
    known = set(graph.nodes)
    for p in parsed:
        for name in p.imports:
            target = resolve_module(name, known)
            if target is not None and target != p.module:  # 외부 패키지·자기 자신은 건너뜀
                graph.add_edge(p.module, target)
    return graph


def rank_modules(graph: nx.DiGraph) -> list[tuple[str, float]]:
    """PageRank 점수 내림차순. 많이 import 되는 모듈일수록 위로 (= 핵심 모듈)."""
    if graph.number_of_nodes() == 0:
        return []
    # 참고: graph.in_degree(m) 은 "몇 개 모듈이 m 을 참조하는지"
    scores = nx.pagerank(graph)  # alpha=0.85 기본값, scipy 필요
    return sorted(scores.items(), key=lambda item: item[1], reverse=True)
