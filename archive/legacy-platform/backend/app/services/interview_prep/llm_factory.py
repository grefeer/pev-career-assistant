"""LLM factory for the Interview Prep generator.

Mirrors the resume-tailoring LLM factory (DeepSeek, OpenAI-compatible) and
raises a typed config error when no API key is resolvable, so the application
lifespan can fall back to a generator-less ``InterviewPrepService`` instead of
crashing.
"""

from __future__ import annotations

from typing import Any

from langchain_openai import ChatOpenAI

from backend.app.config import Settings


class InterviewPrepConfigError(RuntimeError):
    """Raised when the interview-prep LLM cannot be constructed.

    Carries a stable ``code`` so the lifespan can distinguish a missing-key from
    a genuine construction failure.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def build_interview_prep_llm(settings: Settings) -> ChatOpenAI:
    """Build the bounded LLM used by :class:`LLMInterviewPrepGenerator`.

    Raises :class:`InterviewPrepConfigError` (code ``missing_api_key``) when no
    DeepSeek/OpenAI key is resolvable; callers treat this as a graceful
    "interview prep unavailable" signal.
    """
    from src.utils import get_api_key, get_base_url

    api_key = get_api_key()
    if not api_key:
        raise InterviewPrepConfigError(
            "missing_api_key",
            "DEEPSEEK_API_KEY/OPENAI_API_KEY is not set; cannot build the "
            "interview-prep generator LLM.",
        )

    base_url = get_base_url()
    model = settings.interview_prep_model

    kwargs: dict[str, Any] = {
        "model": model,
        "temperature": 0.3,
        "request_timeout": 120,
        "max_retries": 2,
        "api_key": api_key,
        "base_url": base_url,
    }
    # deepseek-v4 models expose a "thinking" mode whose interleaved reasoning
    # tags break JSON parsing; disable it for reliable structured output.
    if "deepseek" in base_url.lower() and model.startswith("deepseek-v4"):
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    return ChatOpenAI(**kwargs)
