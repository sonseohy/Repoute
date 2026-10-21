"""그래프 노드가 쓰는 의존성 묶음 (wave13 패턴)."""

from dataclasses import dataclass
from typing import Any

from repoute.indexing.index import RepoIndex


@dataclass
class Deps:
    """그래프 노드가 쓰는 의존성 묶음. 테스트에서는 가짜 LLM 을 꽂는다 (wave13 패턴)."""

    fast_llm: Any  # 모듈 요약 (저비용)
    quality_llm: Any  # 읽기 순서 합성 (고품질)
    index: RepoIndex
    # 옵트인 심볼 임베딩 검색(search_symbols). 둘 다 None 이면 도구 5개 그대로 (기본 동작 불변)
    embedder: Any = None  # embed_query 를 가진 임베딩 모델
    embedding_store: Any = None  # SymbolEmbeddingStore
