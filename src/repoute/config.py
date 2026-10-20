"""환경변수에서 LLM 설정을 읽고 모델 객체를 만든다."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv
from langchain.chat_models import init_chat_model

# .env 파일의 값을 환경변수로 올린다 (이미 있는 환경변수는 덮어쓰지 않음)
load_dotenv()


@dataclass(frozen=True)
class Settings:
    """LLM 설정 묶음. frozen이라 만든 뒤에는 바꿀 수 없다."""

    provider: str  # openai | anthropic | ollama
    fast_model: str  # 저비용 모델: 라우팅·요약
    quality_model: str  # 고품질 모델: 최종 합성·설명


def load_settings() -> Settings:
    """환경변수를 읽어 Settings를 만든다. 없으면 기본값."""
    return Settings(
        provider=os.environ.get("LLM_PROVIDER", "openai"),
        fast_model=os.environ.get("FAST_MODEL", "gpt-4o-mini"),
        quality_model=os.environ.get("QUALITY_MODEL", "gpt-4o"),
    )


def get_llm(model: str, settings: Settings | None = None):
    """'provider:model' 문자열 하나로 채팅 모델을 만든다."""
    settings = settings or load_settings()
    # temperature=0: 같은 입력에 최대한 같은 답 (평가 재현성)
    return init_chat_model(f"{settings.provider}:{model}", temperature=0)


def get_fast_llm(settings: Settings | None = None):
    """저비용 모델"""
    settings = settings or load_settings()
    return get_llm(settings.fast_model, settings)


def get_quality_llm(settings: Settings | None = None):
    """고품질 모델"""
    settings = settings or load_settings()
    return get_llm(settings.quality_model, settings)
