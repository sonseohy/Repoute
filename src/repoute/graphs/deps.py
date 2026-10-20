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
