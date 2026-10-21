"""import 관계로 모듈 그래프를 만들고 PageRank로 핵심 모듈을 고른다 (LLM 미사용)."""

import networkx as nx

from repoute.indexing.parser import ParsedFile

# 패키지 __init__.py 는 하위 모듈을 모두 import 해 재수출하므로 역방향 점수가 부풀려진다
# (gold 5개 레포 모두 상위 5 에 들어감). 0.5 는 효과 없음, 0.2 에서 개선 (static_baseline.py).
INIT_DAMPING = 0.2


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


def modules_of(graph: nx.DiGraph, files: list[str]) -> list[str]:
    """파일 경로 목록 → 모듈 이름 (노드의 file 속성 역조회, 중복 제거·순서 유지)."""
    by_file = {data["file"]: module for module, data in graph.nodes(data=True)}
    return list(dict.fromkeys(by_file[f] for f in files if f in by_file))


def rank_modules_v2(graph: nx.DiGraph, seeds: list[str]) -> list[tuple[str, float]]:
    """진입점 모듈(seeds)에서 출발하는 역방향 개인화 PageRank. 시드가 없으면 rank_modules 와 같다.

    엣지 A→B 는 "A 가 B 를 import". 그대로 걸면 점수가 하부 부품(compat·types)으로 흐르므로
    graph.reverse() 에서 걸어 "부품을 조립하는 상위 흐름 모듈"(sessions·_client·routing)을 올린다.
    질량은 `from . import x` 로 생기는 패키지 __init__ 허브를 거쳐 퍼지고, 그 허브는
    INIT_DAMPING 으로 감쇠한다. 정방향·리프 감점·이름 힌트·시드 가산은 재 보고 채택하지 않았다.
    """
    seeds = [m for m in seeds if m in graph]
    if not seeds:
        return rank_modules(graph)
    scores = nx.pagerank(graph.reverse(), personalization={m: 1 for m in seeds})
    for module, data in graph.nodes(data=True):
        if data["file"].endswith("__init__.py"):
            scores[module] *= INIT_DAMPING
    return sorted(scores.items(), key=lambda item: item[1], reverse=True)
