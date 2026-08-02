"""Per-page LLM JD-body extractor (PATH C quality port from skill/job-discovery).

When ``settings.job_discovery_llm_extraction_enabled`` is on, the Legacy
Supervisor calls this at the title-only fallback fork inside
``deepagents_runner._extract_and_verify_candidates_from_evidence``: instead of
pulling bare job titles from a rendered list page, ask a small structured-output
LLM to read the page text and emit one ``NormalizedJobCandidate`` per real job
listing found, carrying ``responsibilities``/``requirements`` bodies where the
page actually shows them.

This is a QUALITY port (richer JD bodies via ``count_with_body``), not a speed
port - it runs sequentially per page in v1. The skill's parallel-fetch and
load-more fixes live in the browse layer and are intentionally NOT ported (they
are orthogonal to extraction).

Hard gates honored:

- No secrets / raw payloads logged - only page text + URL enter the prompt.
- Detail pages gated by login/captcha/anti-bot are NEVER circumvented: this
  extractor only reads already-captured public rendered text, exactly like the
  loose title extractor it augments.
- The frozen deterministic tools (``tools/jd_extraction.py`` /
  ``tools/evidence_verifier.py``) are NOT modified; this is a separate,
  additive LLM path that produces ``NormalizedJobCandidate`` objects the same
  downstream verify/dedup pipeline already consumes.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from backend.app.config import Settings
from backend.app.services.job_discovery.schemas import NormalizedJobCandidate

logger = logging.getLogger(__name__)

_PROMPT_DIR = Path(__file__).resolve().parents[1] / "prompts"
_PROMPT_CACHE: str | None = None

# Lenient JSON-array parser ported from the skill v1.x recipe. DeepSeek
# occasionally wraps a JSON array in ```json fences or prepends stray prose,
# and may emit a single object instead of an array. Recover the array robustly.
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

# Verify-retry bound: invoke once; if the parse is empty, retry once. Matches
# the skill v1.x recipe (1 retry round).
_MAX_ATTEMPTS = 2


def _load_prompt() -> str:
    """Load and memoize the extractor system prompt."""
    global _PROMPT_CACHE
    if _PROMPT_CACHE is None:
        _PROMPT_CACHE = (_PROMPT_DIR / "llm_jd_extractor.txt").read_text(
            encoding="utf-8"
        )
    return _PROMPT_CACHE


def _extract_json_array(text: str) -> list[dict[str, Any]]:
    """Parse a possibly-fenced / prose-wrapped JSON array (or single object).

    Returns ``[]`` on any parse failure so the caller degrades to its fallback.
    """
    if not text:
        return []
    raw = text.strip()
    m = _FENCE_RE.search(raw)
    if m:
        raw = m.group(1).strip()
    # Find the first '[' or '{' and last matching close to trim surrounding prose.
    first_bracket = raw.find("[")
    first_brace = raw.find("{")
    starts = [i for i in (first_bracket, first_brace) if i != -1]
    if not starts:
        return []
    start = min(starts)
    close_ch = "]" if raw[start] == "[" else "}"
    end = raw.rfind(close_ch)
    if end == -1 or end <= start:
        return []
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []
    return [d for d in data if isinstance(d, dict)]


def _as_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    return str(v)


def _as_str_list(v: Any) -> list[str]:
    """Coerce a field value into a list of non-empty trimmed strings."""
    if v is None:
        return []
    if isinstance(v, str):
        # "北京、上海" or "北京/上海" -> split on common separators.
        parts = re.split(r"[、/,;；\s]+", v.strip())
        return [p for p in parts if p]
    if isinstance(v, list):
        return [str(x).strip() for x in v if x is not None and str(x).strip()]
    return []


def _to_candidate(
    d: dict[str, Any], url: str | None, ref: dict
) -> NormalizedJobCandidate | None:
    """Build a NormalizedJobCandidate from one parsed LLM object.

    Returns ``None`` for entries with no usable title so they are dropped. A
    title with no JD body is kept (flagged via ``normalization_warnings``) so a
    page that genuinely lists only titles still surfaces them.
    """
    title = _as_str(d.get("title")).strip()
    if not title:
        return None
    resp = _as_str(d.get("responsibilities")).strip()
    req = _as_str(d.get("requirements")).strip()
    warnings: list[str] = []
    if not resp and not req:
        warnings.append(
            "Title-only candidate (LLM): page listed title with no JD body"
        )
    try:
        conf = float(d.get("confidence") or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    return NormalizedJobCandidate(
        title=title,
        company_name=_as_str(d.get("company_name")).strip() or None,
        department=_as_str(d.get("department")).strip() or None,
        description_text=_as_str(d.get("description_text")).strip(),
        responsibilities=resp,
        requirements=req,
        locations=_as_str_list(d.get("locations")),
        recruitment_types=_as_str_list(d.get("recruitment_types")),
        industries=_as_str_list(d.get("industries")),
        apply_url=_as_str(d.get("apply_url")).strip() or url,
        deadline_text=_as_str(d.get("deadline_text")).strip() or None,
        confidence=max(0.0, min(1.0, conf)),
        evidence_refs=[ref],
        normalization_warnings=warnings,
    )


def _build_extractor_llm(settings: Settings) -> Any | None:
    """Build the extractor ChatOpenAI with the proven DeepSeek lenient recipe.

    Mirrors ``deepagents_runner._build_job_discovery_llm`` but adds
    ``max_tokens=8192`` (the recipe that lifted skill v1.x extraction). Returns
    ``None`` when no API key / base url is configured so the caller degrades to
    the deterministic title-only fallback rather than raising.
    """
    try:
        from src.utils import get_api_key, get_base_url

        api_key = get_api_key()
        base_url = get_base_url()
    except Exception:  # noqa: BLE001 - degrade gracefully, never raise
        return None
    if not api_key or not base_url:
        return None
    kwargs: dict[str, Any] = {
        "model": settings.job_discovery_model,
        "temperature": 0,
        "request_timeout": 120,
        "max_retries": 2,
        "max_tokens": 8192,
        "api_key": api_key,
        "base_url": base_url,
    }
    if "deepseek" in base_url.lower() and settings.job_discovery_model.startswith(
        "deepseek-v4"
    ):
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(**kwargs)


def _has_jd_body(c: NormalizedJobCandidate) -> bool:
    """A candidate counts as full-JD when it carries a resp/req body."""
    return bool(
        (c.responsibilities or "").strip() or (c.requirements or "").strip()
    )


def extract_jd_candidates_llm(
    page_text: str,
    url: str | None,
    *,
    settings: Settings,
    model: Any | None = None,
    ref: dict | None = None,
) -> list[NormalizedJobCandidate]:
    """LLM-extract candidates from a single page's rendered text.

    Returns ``[]`` on any error / empty input / flag-off / no-credentials so the
    caller degrades to its deterministic title-only fallback. Bounded
    verify-retry: invoke up to :data:`_MAX_ATTEMPTS` times; return the first
    non-empty parse.
    """
    if not page_text or not page_text.strip():
        return []
    if not getattr(settings, "job_discovery_llm_extraction_enabled", False):
        return []
    llm = model or _build_extractor_llm(settings)
    if llm is None:
        return []
    ref = ref or {"url": url, "content_hash": None, "evidence_type": "page_text"}
    prompt = _load_prompt()
    user_msg = HumanMessage(
        content=f"SOURCE_URL: {url}\n\nPAGE_TEXT:\n{page_text}"
    )
    last: list[NormalizedJobCandidate] = []
    for attempt in range(_MAX_ATTEMPTS):
        try:
            resp = llm.invoke([SystemMessage(content=prompt), user_msg])
        except Exception:  # noqa: BLE001 - never raise; degrade gracefully
            logger.warning(
                "llm_jd_extractor invoke failed (attempt %d/%d)",
                attempt + 1,
                _MAX_ATTEMPTS,
            )
            continue
        content = getattr(resp, "content", None)
        if not content:
            content = str(resp) if resp else ""
        items = _extract_json_array(_as_str(content))
        last = [
            c
            for c in (_to_candidate(it, url, ref) for it in items)
            if c is not None
        ]
        if last:
            return last
    return last
