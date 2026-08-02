"""Environment-only model provider configuration for the production PEV runtime."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values, load_dotenv


_ROOT_DIR = Path(__file__).resolve().parents[4]
_DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"


def load_project_env() -> None:
    """Load only the project-level non-interpolated provider environment values."""
    env_path = _ROOT_DIR / ".env"
    load_dotenv(env_path)
    literal_values = dotenv_values(env_path, interpolate=False)
    for name in ("TENCENT_DOCS_TOKEN", "TEST_TENCENT_DOCS_TOKEN"):
        value = literal_values.get(name)
        if value:
            os.environ[name] = value


def get_api_key() -> str | None:
    """Read an explicitly configured OpenAI-compatible provider key."""
    return os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY")


def get_base_url() -> str:
    """Return the configured endpoint while retaining the documented DeepSeek default."""
    return os.getenv("OPENAI_BASE_URL", _DEFAULT_DEEPSEEK_BASE_URL)
