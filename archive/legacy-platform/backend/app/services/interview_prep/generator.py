"""LLM-driven generator for the Interview Prep skill.

``LLMInterviewPrepGenerator`` turns a target job snapshot (+ confirmed profile
facts + preferences + match analysis) into a structured interview-prep kit via
a DeepSeek (OpenAI-compatible) LLM.  The shared :mod:`backend.app.services.common.llm_json`
helpers do the tolerant JSON extraction; this module owns the prep-content
coercion (the five normalized sections).

Responsibility split:

- The generator builds the prompt, invokes the LLM, and parses the JSON into a
  normalized content dict. It does **not** persist anything - the service owns
  the row write.
- On a parse failure (no JSON, or a dict with no recognized content) the
  generator raises :class:`InterviewPrepGenerationError`; the service catches it
  and finalizes the kit as ``failed``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from backend.app.config import Settings
from backend.app.services.common.llm_json import extract_content, extract_json

logger = logging.getLogger(__name__)


class InterviewPrepGenerationError(RuntimeError):
    """Raised when the LLM output cannot be parsed into prep content.

    Carries a stable ``code`` so the service can record a meaningful failure
    reason on the kit row.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


#: The five normalized sections of an interview-prep kit. The generator
#: guarantees each appears as a list in the output (defaulting to ``[]``).
CONTENT_KEYS: tuple[str, ...] = (
    "technical_questions",
    "behavioral_questions",
    "talking_points",
    "topics_to_review",
    "questions_to_ask",
)


_SYSTEM_PROMPT = (
    "You are an interview-prep coach. Given a target job snapshot, the "
    "candidate's confirmed profile facts, their stated preferences, and a match "
    "analysis (strengths/gaps), produce a structured interview-prep kit.\n\n"
    "Respond with ONLY a JSON object with these keys, each a list of concise "
    "strings:\n"
    '- "technical_questions": likely technical questions for this role\n'
    '- "behavioral_questions": likely behavioral/situational questions\n'
    '- "talking_points": strengths and stories to emphasize, grounded in the '
    "candidate's profile where possible\n"
    '- "topics_to_review": concepts/skills to brush up on before the interview\n'
    '- "questions_to_ask": thoughtful questions the candidate can ask the '
    "interviewer\n\n"
    "Tailor every section to the target job. Do not include prose, markdown "
    "fences, or commentary - only the JSON object."
)


class LLMInterviewPrepGenerator:
    """Agent-driven interview-prep generator: prompt -> LLM -> content dict.

    Constructed with a ChatOpenAI-like LLM (anything exposing
    ``invoke(messages) -> object-with-``.content``) and an optional
    :class:`Settings` used only to stamp the agent version on the result.
    """

    def __init__(self, llm: Any, settings: Settings | None = None) -> None:
        self.llm = llm
        self.agent_version = (
            settings.interview_prep_agent_version if settings is not None else "1.0.0"
        )

    def generate_prep(
        self,
        *,
        job_snapshot: dict[str, Any],
        profile_facts: dict[str, Any] | None = None,
        preferences: dict[str, Any] | None = None,
        match_analysis: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Generate a normalized interview-prep content dict.

        Returns ``{"content": {...}, "agent_version": str}`` where ``content``
        has all :data:`CONTENT_KEYS` as lists. Raises
        :class:`InterviewPrepGenerationError` when the LLM output is not
        parseable or contains no recognized content.
        """
        messages = self._build_messages(
            job_snapshot=job_snapshot,
            profile_facts=profile_facts,
            preferences=preferences,
            match_analysis=match_analysis,
        )
        response = self.llm.invoke(messages)
        content = extract_content(response)
        parsed = _parse_content(content)
        return {"content": parsed, "agent_version": self.agent_version}

    def _build_messages(
        self,
        *,
        job_snapshot: dict[str, Any],
        profile_facts: dict[str, Any] | None,
        preferences: dict[str, Any] | None,
        match_analysis: dict[str, Any] | None,
    ) -> list[Any]:
        human_payload = {
            "job_snapshot": job_snapshot,
            "profile_facts": profile_facts or {},
            "preferences": preferences or {},
            "match_analysis": match_analysis or {},
        }
        return [
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content=json.dumps(human_payload, ensure_ascii=False)),
        ]


# ---------------------------------------------------------------------------
# Response parsing helpers
# ---------------------------------------------------------------------------


def _parse_content(content: str) -> dict[str, list[str]]:
    """Parse the LLM response into a normalized content dict.

    Raises :class:`InterviewPrepGenerationError` when no JSON can be located
    (``interview_prep_parse_error``) or the parsed object has no recognized
    content (``interview_prep_empty_content``).
    """
    payload = extract_json(content)
    if payload is None:
        raise InterviewPrepGenerationError(
            "interview_prep_parse_error",
            "LLM response did not contain a parseable JSON object.",
        )
    normalized = _coerce_content(payload)
    if normalized is None:
        raise InterviewPrepGenerationError(
            "interview_prep_parse_error",
            "LLM response JSON was not an object.",
        )
    if not any(normalized.values()):
        raise InterviewPrepGenerationError(
            "interview_prep_empty_content",
            "LLM response contained no recognized interview-prep content.",
        )
    return normalized


def _coerce_content(payload: Any) -> dict[str, list[str]] | None:
    """Normalize a parsed JSON payload into the five content sections.

    Accepts a JSON object and returns a dict with every :data:`CONTENT_KEYS`
    entry as a list (non-list values are dropped, unknown keys ignored).
    Returns ``None`` when ``payload`` is not a dict so the caller can raise.
    """
    if not isinstance(payload, dict):
        return None
    result: dict[str, list[str]] = {}
    for key in CONTENT_KEYS:
        value = payload.get(key)
        result[key] = [item for item in value if isinstance(item, str)] if isinstance(value, list) else []
    return result
