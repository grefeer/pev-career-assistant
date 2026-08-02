import src.utils as utils
from src.utils import get_api_key, get_base_url, get_model_name


def test_deepseek_defaults_are_used_when_openai_compatible_env_is_unset(monkeypatch):
    for name in [
        "OPENAI_BASE_URL",
        "OPENAI_MODEL",
        "SUPERVISOR_MODEL",
        "DEEPSEEK_API_KEY",
        "OPENAI_API_KEY",
    ]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(utils, "_get_windows_user_env", lambda name: None)

    assert get_base_url() == "https://api.deepseek.com"
    assert get_model_name() == "deepseek-v4-flash"
    assert get_model_name("SUPERVISOR_MODEL") == "deepseek-v4-flash"
    assert get_api_key() is None


def test_deepseek_api_key_takes_precedence(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-key")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")

    assert get_api_key() == "deepseek-key"


def test_openai_compatible_overrides_still_work(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "fallback-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("OPENAI_MODEL", "custom-model")
    monkeypatch.setenv("SUPERVISOR_MODEL", "custom-supervisor")

    assert get_api_key() == "fallback-key"
    assert get_base_url() == "https://example.test/v1"
    assert get_model_name() == "custom-model"
    assert get_model_name("SUPERVISOR_MODEL") == "custom-supervisor"


def test_windows_user_deepseek_key_is_used_as_fallback(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        utils, "_get_windows_user_env", lambda name: "user-key" if name == "DEEPSEEK_API_KEY" else None
    )

    assert get_api_key() == "user-key"
