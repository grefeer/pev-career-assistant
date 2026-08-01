"""LLM-driven DraftGenerator for the Resume Tailoring skill.

``LLMDraftGenerator`` is the concrete :class:`DraftGenerator` implementation
that ``ResumeDraftService`` has always expected but never had: it turns a
target job snapshot + confirmed profile facts + user preferences + match
analysis into a list of resume diff operations via a DeepSeek (OpenAI-
compatible) LLM.

Responsibility split:

- The generator builds the prompt, invokes the LLM, and best-effort parses the
  JSON response into ``{"diffs": [...]}``. It does **not** validate diffs
  against ``evidence_refs`` (it never receives them) - that authoritative check
  lives in ``ResumeDraftService.create_draft`` via ``validate_draft_diffs``.
- On a parse failure the generator raises :class:`DraftGenerationError`; the
  service catches it and finalizes the draft as ``draft_generation_interrupted``.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from backend.app.config import Settings

logger = logging.getLogger(__name__)


class DraftGenerationError(RuntimeError):
    """Raised when the LLM output cannot be parsed into a diffs list.

    Carries a stable ``code`` so callers can record a meaningful failure reason.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


_SYSTEM_PROMPT = (
    "You are a resume-tailoring assistant. Given a target job snapshot, the "
    "candidate's confirmed profile facts, their stated preferences, and a match "
    "analysis (strengths/gaps), produce a concise list of resume diff operations "
    "that tailor the resume to the job.\n\n"
    "Each diff object MUST have:\n"
    '- "op": one of reorder, rephrase, summarize, omit, highlight\n'
    '- "section": a non-empty resume section name '
    '(e.g. "work_experience", "projects", "skills", "summary")\n'
    '- "fact_ref": a key that EXISTS in profile_facts '
    "(use one of the provided valid_fact_refs verbatim)\n"
    '- "before": the current text (may be empty)\n'
    '- "after": the proposed tailored text (may be empty for "omit")\n'
    '- "evidence_ids": an optional list (may be empty or omitted)\n\n'
    "Respond with ONLY a JSON object of the form:\n"
    '{"diffs": [ { "op": ..., "section": ..., "fact_ref": ..., '
    '"before": ..., "after": ... }, ... ]}\n\n'
    "Do not include prose, markdown fences, or commentary."
)

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class LLMDraftGenerator:
    """Agent-driven DraftGenerator: prompt -> LLM -> parsed diff list.

    Constructed with a ChatOpenAI-like LLM (anything exposing
    ``invoke(messages) -> object-with-``.content``) and an optional
    :class:`Settings` used only to stamp the agent version on the result.
    """

    def __init__(self, llm: Any, settings: Settings | None = None) -> None:
        self.llm = llm
        self.agent_version = (
            settings.resume_tailoring_agent_version if settings is not None else "1.0.0"
        )

    def generate_diffs(
        self,
        *,
        job_snapshot: dict[str, Any],
        profile_facts: dict[str, Any],
        preferences: dict[str, Any] | None = None,
        match_analysis: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Generate resume diff operations for the target job.

        Returns ``{"diffs": [...], "agent_version": str}``. Raises
        :class:`DraftGenerationError` when the LLM output is not parseable; the
        caller treats any other exception (network/auth) as a hard interruption.
        """
        messages = self._build_messages(
            job_snapshot=job_snapshot,
            profile_facts=profile_facts,
            preferences=preferences,
            match_analysis=match_analysis,
        )
        response = self.llm.invoke(messages)
        content = _extract_content(response)
        diffs = _parse_diffs(content)
        return {"diffs": diffs, "agent_version": self.agent_version}

    def _build_messages(
        self,
        *,
        job_snapshot: dict[str, Any],
        profile_facts: dict[str, Any],
        preferences: dict[str, Any] | None,
        match_analysis: dict[str, Any] | None,
    ) -> list[Any]:
        valid_fact_refs = (
            list(profile_facts.keys()) if isinstance(profile_facts, dict) else []
        )
        human_payload = {
            "job_snapshot": job_snapshot,
            "profile_facts": profile_facts,
            "valid_fact_refs": valid_fact_refs,
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


def _extract_content(response: Any) -> str:
    """Coerce an LLM response into a string.

    Handles (a) objects with a ``.content`` string (langchain ``AIMessage``),
    (b) objects whose ``.content`` is a list of content blocks, and (c) plain
    strings passed straight through (useful for tests).
    """
    if response is None:
        return ""
    content = getattr(response, "content", response)
    if isinstance(content, list):
        return "".join(_block_text(block) for block in content)
    return content if isinstance(content, str) else str(content)


def _block_text(block: Any) -> str:
    """Extract text from one content block (string or {"text": ...} dict)."""
    if isinstance(block, str):
        return block
    if isinstance(block, dict):
        return str(block.get("text", ""))
    return str(block)


def _parse_diffs(content: str) -> list[dict[str, Any]]:
    """Parse the LLM response text into a list of diff dicts.

    Raises :class:`DraftGenerationError` when no JSON can be located or the
    JSON does not contain a diffs list.
    """
    payload = _extract_json(content)
    if payload is None:
        raise DraftGenerationError(
            "draft_generation_parse_error",
            "LLM response did not contain a parseable JSON object.",
        )
    diffs = _coerce_diffs(payload)
    if diffs is None:
        raise DraftGenerationError(
            "draft_generation_parse_error",
            "LLM response JSON did not contain a 'diffs' list.",
        )
    return diffs


def _extract_json(content: str) -> Any:
    """Best-effort extraction of the first JSON value from ``content``.

    Tries a fenced ```json block first, then the whole text, then a bracket
    slice for the common "preamble {json} postamble" shape.
    """
    match = _FENCE_RE.search(content)
    candidates = [match.group(1)] if match else []
    candidates.append(content)
    for candidate in candidates:
        result = _try_parse_json(candidate)
        if result is not None:
            return result
    return None


def _try_parse_json(text: str) -> Any:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except (ValueError, TypeError):
        pass
    obj = _slice_between(stripped, "{", "}")
    if obj is not None:
        try:
            return json.loads(obj)
        except (ValueError, TypeError):
            pass
    arr = _slice_between(stripped, "[", "]")
    if arr is not None:
        try:
            return json.loads(arr)
        except (ValueError, TypeError):
            pass
    return None


def _slice_between(text: str, open_ch: str, close_ch: str) -> str | None:
    """Return the substring from the first ``open_ch`` to the last ``close_ch``.

    Returns ``None`` when either bracket is absent or out of order. Using the
    last close bracket correctly captures a single top-level JSON value even
    when it contains nested objects.
    """
    start = text.find(open_ch)
    if start == -1:
        return None
    end = text.rfind(close_ch)
    if end <= start:
        return None
    return text[start : end + 1]


def _coerce_diffs(payload: Any) -> list[dict[str, Any]] | None:
    """Normalize a parsed JSON payload into a list of diff dicts.

    Accepts either a bare list or ``{"diffs": [...]}``. Returns ``None`` when
    the payload has no diffs list, so the caller can raise a parse error.
    Non-dict entries are dropped (they cannot be valid diff operations).
    """
    if isinstance(payload, list):
        return [d for d in payload if isinstance(d, dict)]
    if isinstance(payload, dict):
        diffs = payload.get("diffs")
        if isinstance(diffs, list):
            return [d for d in diffs if isinstance(d, dict)]
    return None
