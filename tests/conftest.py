"""pytest 공통 설정. 테스트는 가짜 LLM 으로 돌므로 LangSmith 에 기록하지 않는다 (측정 잡음 방지)."""

import os
import re
import zlib

import pytest

# config.py 의 load_dotenv 는 이미 있는 환경변수를 덮어쓰지 않으므로, import 전에 먼저 꺼 둔다
os.environ["LANGSMITH_TRACING"] = "false"


class FakeEmbedder:
    """단어 집합을 해시로 칸에 떨어뜨리는 결정론 임베더 (실제 임베딩 API 호출 금지).

    겹치는 단어가 많을수록 코사인 유사도가 커서 "단어가 겹치는 심볼이 1위" 를 검사할 수 있다.
    calls 에 embed_documents 배치 크기를 남겨 캐시로 API 를 건너뛰었는지 확인한다.
    """

    DIM = 64

    def __init__(self):
        self.calls: list[int] = []

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.DIM
        for word in set(re.findall(r"[a-z0-9]+|[가-힣]+", text.lower())):
            vector[zlib.crc32(word.encode()) % self.DIM] += 1.0  # hash() 는 실행마다 달라 crc32
        return vector

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(len(texts))
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


@pytest.fixture
def fake_embedder() -> FakeEmbedder:
    """테스트마다 새 가짜 임베더."""
    return FakeEmbedder()
