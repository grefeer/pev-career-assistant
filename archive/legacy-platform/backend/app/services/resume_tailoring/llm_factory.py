"""LLM factory for the Resume Tailoring generator.

Mirrors the job-discovery LLM factory pattern (DeepSeek, OpenAI-compatible) but
uses a small non-zero temperature (drafting rewards some variation) and raises a
typed config error when no API key is resolvable, so the application lifespan
can fall back to a generator-less ``ResumeDraftService`` instead of crashing.
"""

from __future__ import annotations

from typing import Any

from langchain_openai import ChatOpenAI

from backend.app.config import Settings


class DraftGeneratorConfigError(RuntimeError):
    """Raised when the draft-generator LLM cannot be constructed.

    Carries a stable ``code`` so callers (notably the lifespan) can distinguish
    a missing-key from a genuine construction failure.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def build_draft_generator_llm(settings: Settings) -> ChatOpenAI:
    """Build the bounded LLM used by :class:`LLMDraftGenerator`.

    Raises :class:`DraftGeneratorConfigError` (code ``missing_api_key``) when no
    DeepSeek/OpenAI key is resolvable from the environment; callers should treat
    this as a graceful "tailoring unavailable" signal rather than a hard fault.
    """
    from src.utils import get_api_key, get_base_url

    api_key = get_api_key()
    if not api_key:
        raise DraftGeneratorConfigError(
            "missing_api_key",
            "DEEPSEEK_API_KEY/OPENAI_API_KEY is not set; cannot build the "
            "resume-tailoring generator LLM.",
        )

    base_url = get_base_url()
    model = settings.resume_tailoring_model

    kwargs: dict[str, Any] = {
        "model": model,
        "temperature": 0.2,
        "request_timeout": 120,
        "max_retries": 2,
        "api_key": api_key,
        "base_url": base_url,
    }
    # deepseek-v4 models expose a "thinking" mode whose interleaved reasoning
    # tags break JSON parsing; disable it for reliable structured output, the
    # same convention used by the job-discovery LLM.
    if "deepseek" in base_url.lower() and model.startswith("deepseek-v4"):
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    return ChatOpenAI(**kwargs)
