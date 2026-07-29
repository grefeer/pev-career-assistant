"""Model construction shared by legacy and Skill discovery runtimes."""

from __future__ import annotations

from typing import Any

from langchain_openai import ChatOpenAI

from backend.app.config import Settings


def build_job_discovery_llm(settings: Settings) -> ChatOpenAI:
    """Build the bounded, non-thinking model used by job discovery."""
    from src.utils import get_api_key, get_base_url

    kwargs: dict[str, Any] = {
        "model": settings.job_discovery_model,
        "temperature": 0,
        "request_timeout": 120,
        "max_retries": 2,
    }
    api_key = get_api_key()
    if api_key:
        kwargs["api_key"] = api_key
    base_url = get_base_url()
    kwargs["base_url"] = base_url
    if "deepseek" in base_url.lower() and settings.job_discovery_model.startswith("deepseek-v4"):
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    return ChatOpenAI(**kwargs)
