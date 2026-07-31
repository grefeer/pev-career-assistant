"""Generic, preference-driven recommendation matching for discovery results.

This is deliberately a recommendation layer: it returns the subset of candidates
matching a requested preference, and must never remove raw candidates or evidence
from a discovery run (the caller retains the full set as ``discovered_candidates``)
because coverage is assessed on the complete source, not on a student's current
preference.

Generic over the preference
----------------------------
Search terms, keep tokens, and role markers are all derived FROM the preference
string (see :mod:`preference_expansion`), so this works for ANY preference -
``AI应用开发``, ``AI产品经理``, ``数据分析``, ``芯片设计工程师``, ...  No
AI-dev-specific role lists are hardcoded in the prompt or the filter.  An
``AI产品经理`` preference yields product role markers (keeps product roles,
filters dev roles); an ``AI应用开发`` preference yields dev markers.  The two
invert correctly because both sets are derived from the preference.

Three stages, all driven by preference-derived tokens:

* (a) title/department contains a ``keep_token`` AND a ``role_marker`` -> KEEP
      (deterministic; the domain and the role family both align).
* (b) a ``keep_token`` appears in title/department/body but stage (a) did not
      fire -> a generic LLM semantic judge decides (prompt carries only the
      preference, no role lists).  When no ``llm`` is supplied, stage (b)
      candidates are conservatively FILTERED (precision over recall).
* (c) no ``keep_token`` anywhere -> FILTER.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from backend.app.services.job_discovery.preference_expansion import (
    PreferenceProfile,
    expand_preferences,
)
from backend.app.services.job_discovery.schemas import NormalizedJobCandidate


DEFAULT_ROLE_PREFERENCES: tuple[str, ...] = ("AI应用开发", "Agent开发")

# Cap the JD text handed to the LLM judge - enough to judge relevance, bounded
# so a single batch stays cheap and within token limits.
_JUDGE_BODY_CHAR_CAP = 2000

# Generic judge prompt.  The preference is injected per item; NO role lists or
# AI-dev-specific tokens appear here.  This is the "no-cheating" guarantee: the
# only signal is the preference string the user actually supplied.
_JUDGE_SYSTEM_PROMPT = (
    "You are a precise job-relevance judge. A job seeker's stated preference is "
    "given as PREFERENCE. Decide whether the job's CORE WORK genuinely matches "
    "someone seeking that preference.\n"
    "Rules:\n"
    "- 'match' = the job's primary role IS the preference (building / developing / "
    "designing / researching / doing that kind of work).\n"
    "- A job that only MENTIONS, USES, SUPPORTS, TESTS, or EVALUATES the preference "
    "topic as a side activity is NOT a match.\n"
    "- Judge by the actual responsibilities/requirements, not by a buzzword in the "
    "title alone.\n"
    "Respond with exactly one line of JSON: {\"relevant\": true} or "
    "{\"relevant\": false}."
)


def filter_candidates_for_preferences(
    candidates: Iterable[NormalizedJobCandidate],
    preferences: Iterable[str],
    *,
    llm: Any | None = None,
) -> list[NormalizedJobCandidate]:
    """Return candidates matching a requested preference (recommendation subset).

    ``llm``: an optional chat model exposing ``abatch`` (e.g. ``ChatOpenAI``).
    When provided, stage (b) ambiguous candidates are judged semantically with a
    generic, preference-only prompt.  When ``None``, stage (b) candidates are
    conservatively filtered out.  Unknown preferences expand to no keep tokens
    and match nothing - a typo cannot silently broaden the recommended set.
    """
    profiles = expand_preferences(preferences)
    if not profiles:
        return []

    candidates = list(candidates)
    keep_flags = [False] * len(candidates)
    ambiguous: list[tuple[int, NormalizedJobCandidate, str]] = []

    for idx, cand in enumerate(candidates):
        label = _role_label(cand)
        if _stage_a_keep(label, profiles):
            keep_flags[idx] = True
            continue
        body = _body_text(cand)
        matched = _first_keep_token_match(label, body, profiles)
        if matched is not None:
            # Stage (b): keep_token present, role unclear -> needs semantic judgment.
            ambiguous.append((idx, cand, matched.preference))

    if ambiguous and llm is not None:
        for idx in _judge_relevance_sync(llm, ambiguous):
            keep_flags[idx] = True

    return [cand for idx, cand in enumerate(candidates) if keep_flags[idx]]


# ---------------------------------------------------------------------------
# Stage helpers
# ---------------------------------------------------------------------------

def _role_label(cand: NormalizedJobCandidate) -> str:
    return " ".join(v for v in (cand.title, cand.department) if v).casefold()


def _body_text(cand: NormalizedJobCandidate) -> str:
    parts: list[str] = []
    for field_name in ("responsibilities", "requirements", "description_text"):
        value = getattr(cand, field_name, None) or ""
        if value:
            parts.append(value)
    return "\n".join(parts)


def _stage_a_keep(label: str, profiles: list[PreferenceProfile]) -> bool:
    """Deterministic KEEP: a profile whose keep_token AND role_marker are in label.

    Both sides are compared case-insensitively (label is already casefolded; the
    preference-derived tokens are casefolded here, not at storage, so the
    no-cheating "keep_token is a substring of the preference" invariant holds).
    """
    for profile in profiles:
        if not profile.role_markers:
            continue
        if not any(k.casefold() in label for k in profile.keep_tokens):
            continue
        if any(m.casefold() in label for m in profile.role_markers):
            return True
    return False


def _first_keep_token_match(
    label: str, body: str, profiles: list[PreferenceProfile]
) -> PreferenceProfile | None:
    """First profile with a keep_token anywhere in label or body (stage-b gate)."""
    haystack = f"{label}\n{body}".casefold()
    for profile in profiles:
        if any(k.casefold() in haystack for k in profile.keep_tokens):
            return profile
    return None


# ---------------------------------------------------------------------------
# Generic LLM judge (stage b)
# ---------------------------------------------------------------------------

def _judge_relevance_sync(
    llm: Any, items: list[tuple[int, NormalizedJobCandidate, str]]
) -> set[int]:
    """Run the async judge batch synchronously, robust to a running event loop.

    Returns the set of candidate indices the LLM judged relevant.  On any error
    returns an empty set (conservative: filter all ambiguous).
    """
    coro = _judge_relevance_async(llm, items)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # Already inside an event loop: run in a worker thread with its own loop.
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


async def _judge_relevance_async(
    llm: Any, items: list[tuple[int, NormalizedJobCandidate, str]]
) -> set[int]:
    messages_batch: list[list[Any]] = []
    for _idx, cand, preference in items:
        body = _body_text(cand)[:_JUDGE_BODY_CHAR_CAP]
        human = (
            f"PREFERENCE: {preference}\n\n"
            f"Job title: {cand.title or '(none)'}\n"
            f"Department: {cand.department or '(none)'}\n\n"
            f"Job detail:\n{body or '(no body)'}"
        )
        messages_batch.append([
            SystemMessage(content=_JUDGE_SYSTEM_PROMPT),
            HumanMessage(content=human),
        ])
    try:
        results = await llm.abatch(
            messages_batch, config={"max_concurrency": 16}
        )
    except Exception:
        return set()
    kept: set[int] = set()
    for (idx, _cand, _pref), result in zip(items, results):
        if _parse_relevant(result.content if hasattr(result, "content") else str(result)):
            kept.add(idx)
    return kept


def _parse_relevant(content: Any) -> bool:
    """Parse ``{"relevant": true|false}`` from a model reply; default False."""
    if not isinstance(content, str):
        content = str(content)
    # Find the first JSON object in the reply.
    start = content.find("{")
    if start < 0:
        return False
    end = content.find("}", start)
    if end < 0:
        return False
    try:
        obj = json.loads(content[start : end + 1])
    except json.JSONDecodeError:
        return False
    return bool(obj.get("relevant"))
