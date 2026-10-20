from repoute.config import Settings, get_llm, load_settings


def test_load_settings(monkeypatch):
    # 환경변수가 없으면 기본값, 있으면 그 값을 읽는다
    for name in ("LLM_PROVIDER", "FAST_MODEL", "QUALITY_MODEL"):
        monkeypatch.delenv(name, raising=False)
    assert load_settings() == Settings("openai", "gpt-4o-mini", "gpt-4o")

    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("FAST_MODEL", "fast-x")
    monkeypatch.setenv("QUALITY_MODEL", "quality-y")
    assert load_settings() == Settings("anthropic", "fast-x", "quality-y")


def test_get_llm_builds_model(monkeypatch):
    # "provider:model" 문자열로 모델 객체가 만들어진다 (호출 전까지 네트워크 없음)
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    llm = get_llm("gpt-4o-mini", Settings("openai", "gpt-4o-mini", "gpt-4o"))
    assert hasattr(llm, "invoke")
