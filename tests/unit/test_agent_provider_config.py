"""Provider environment handling owned by the personal PEV runtime."""

from __future__ import annotations

from backend.app.services.agent_runtime import provider_config


def test_provider_config_loads_project_env_and_prefers_explicit_provider_settings(
    tmp_path, monkeypatch
) -> None:
    (tmp_path / ".env").write_text(
        "DEEPSEEK_API_KEY=project-key\n"
        "OPENAI_BASE_URL=https://provider.example\n"
        "TEST_TENCENT_DOCS_TOKEN=literal-token\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(provider_config, "_ROOT_DIR", tmp_path)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("TEST_TENCENT_DOCS_TOKEN", raising=False)

    provider_config.load_project_env()

    assert provider_config.get_api_key() == "project-key"
    assert provider_config.get_base_url() == "https://provider.example"


def test_provider_config_uses_openai_key_and_documented_base_url_default(monkeypatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    assert provider_config.get_api_key() == "openai-key"
    assert provider_config.get_base_url() == "https://api.deepseek.com"
