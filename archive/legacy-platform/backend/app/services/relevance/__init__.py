"""Relevance ranking subsystem for the personal-mode application assistant.

Sits UPSTREAM of the expensive per-job MatchService: a single batched LLM call
scores many candidates against the user's profile + preferences so only a
ranked top-N ever reaches MatchService (or admin review).
"""

from __future__ import annotations

from typing import Any

from langchain_openai import ChatOpenAI


def build_relevance_llm(settings: Any) -> ChatOpenAI:
    """Build the cheap batched ranker LLM.

    Mirrors ``deepagents_runner._build_job_discovery_llm`` but uses
    ``settings.relevance_model`` (same DeepSeek family by default).
    """
    from src.utils import get_api_key, get_base_url

    kwargs: dict[str, Any] = {
        "model": settings.relevance_model,
        "temperature": 0,
        "request_timeout": 120,
        "max_retries": 2,
    }
    api_key = get_api_key()
    if api_key:
        kwargs["api_key"] = api_key
    base_url = get_base_url()
    kwargs["base_url"] = base_url
    if "deepseek" in base_url.lower() and settings.relevance_model.startswith("deepseek-v4"):
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    return ChatOpenAI(**kwargs)
