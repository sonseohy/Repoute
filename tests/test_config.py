"""repoute.config(설정 로딩·LLM 생성) 테스트. 실행: uv run pytest -v tests/test_config.py"""

from repoute.config import (
    DEFAULT_EMBEDDING_MODEL,
    Settings,
    get_embedder,
    get_llm,
    load_settings,
)


def test_load_settings(monkeypatch):
    """환경변수가 없으면 기본값, 있으면 그 값을 읽는다."""
    for name in ("LLM_PROVIDER", "FAST_MODEL", "QUALITY_MODEL", "REPOUTE_EMBEDDING_MODEL"):
        monkeypatch.delenv(name, raising=False)
    assert load_settings() == Settings("openai", "gpt-4o-mini", "gpt-4o")

    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("FAST_MODEL", "fast-x")
    monkeypatch.setenv("QUALITY_MODEL", "quality-y")
    assert load_settings() == Settings("anthropic", "fast-x", "quality-y")


def test_get_llm_builds_model(monkeypatch):
    """문자열 "provider:model" 로 모델 객체가 만들어진다 (호출 전까지 네트워크 없음)."""
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    llm = get_llm("gpt-4o-mini", Settings("openai", "gpt-4o-mini", "gpt-4o"))
    assert hasattr(llm, "invoke")


def test_embedding_model_setting(monkeypatch):
    """임베딩 모델 기본값은 openai:text-embedding-3-small, REPOUTE_EMBEDDING_MODEL 로 바꾼다."""
    monkeypatch.delenv("REPOUTE_EMBEDDING_MODEL", raising=False)
    assert DEFAULT_EMBEDDING_MODEL == "openai:text-embedding-3-small"
    assert load_settings().embedding_model == DEFAULT_EMBEDDING_MODEL
    assert Settings("openai", "a", "b").embedding_model == DEFAULT_EMBEDDING_MODEL
    monkeypatch.setenv("REPOUTE_EMBEDDING_MODEL", "openai:text-embedding-3-large")
    assert load_settings().embedding_model == "openai:text-embedding-3-large"


def test_get_embedder_builds_model(monkeypatch):
    """get_embedder 는 embed_query·embed_documents 를 가진 객체를 만든다 (만들 때 네트워크 없음)."""
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    embedder = get_embedder(Settings("openai", "gpt-4o-mini", "gpt-4o"))
    assert hasattr(embedder, "embed_query") and hasattr(embedder, "embed_documents")
