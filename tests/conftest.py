"""pytest 공통 설정. 테스트는 가짜 LLM 으로 돌므로 LangSmith 에 기록하지 않는다 (측정 잡음 방지)."""

import importlib
import os
import re
import sys
import types
import zlib
from typing import Literal

import pytest
from pydantic import BaseModel

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


ISSUE_MODULE = "repoute.graphs.issue_matcher"


def _fake_issue_module() -> types.ModuleType:
    """issue_matcher 와 같은 모양의 Profile·IssueMatch·IssueMatches 만 든 가짜 모듈.

    체크포인터가 pydantic 객체를 "모듈 이름 + 클래스 이름" 으로 되살리므로 __module__ 을
    진짜 모듈 이름으로 맞춘다 (sys.modules 에 넣으면 import 가 이 모듈을 돌려준다).
    """
    from repoute.validation.citations import CodeRef

    class Profile(BaseModel):
        interests: str = ""
        level: Literal["beginner", "intermediate", "advanced"] = "beginner"

    class IssueMatch(BaseModel):
        number: int
        title: str
        url: str
        labels: list[str]
        why: str
        modules: list[str]
        refs: list[CodeRef]
        difficulty: Literal["easy", "medium", "hard"]

    class IssueMatches(BaseModel):
        repo: str
        profile: Profile
        matches: list[IssueMatch]
        considered: int

    module = types.ModuleType(ISSUE_MODULE)
    for cls in (Profile, IssueMatch, IssueMatches):
        cls.__module__ = ISSUE_MODULE
        setattr(module, cls.__name__, cls)
    return module


@pytest.fixture
def issue_mod(monkeypatch) -> types.ModuleType:
    """Issue Matcher 모듈. 아직 없거나 import 가 깨지면 가짜를 sys.modules 에 넣어 돌려준다.

    Supervisor 는 이 모듈을 지연 import 하므로, 모듈 작업과 따로 진행해도 테스트가 돈다.
    """
    try:
        return importlib.import_module(ISSUE_MODULE)
    except Exception:
        module = _fake_issue_module()
        monkeypatch.setitem(sys.modules, ISSUE_MODULE, module)
        return module
