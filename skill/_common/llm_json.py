"""Best-effort LLM JSON extraction shared by agent-driven skill scripts.

Each agent-driven skill (``resume-tailoring``, ``interview-prep``, ...) prompts a
DeepSeek LLM for structured JSON.  LLMs occasionally wrap the payload in
`` ```json ``` `` fences or surround it with prose, so extraction must be
tolerant before ``json.loads``.  The helpers here are deliberately generic (no
skill-specific shape knowledge); each skill owns the coercion of the parsed
value into its own contract (a diffs list, a prep-kit content object, ...).

This is the single source for the skill-side scripts.  It used to be mirrored as
inline copies in each ``scripts/generate.py`` (and as the now-removed backend
``backend.app.services.common.llm_json``); both have been consolidated here.

A skill script imports it by resolving this file's location from its own
``__file__`` (see ``scripts/generate.py``) so the import works whether the script
is run as ``python skill/<skill>/scripts/generate.py`` or
``python scripts/generate.py`` with ``cwd=skill/<skill>``.

Public surface:

- :func:`extract_content` - coerce an LLM response (``AIMessage``-like, a list
  of content blocks, or a plain string) into text.
- :func:`extract_json` - locate and parse the first JSON value in a response.
- :func:`try_parse_json` / :func:`slice_between` - the building blocks.
"""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_content(response: Any) -> str:
    """Coerce an LLM response into a string.

    Handles (a) objects with a ``.content`` string (langchain ``AIMessage``),
    (b) objects whose ``.content`` is a list of content blocks, and (c) plain
    strings passed straight through (useful for tests).
    """
    if response is None:
        return ""
    content = getattr(response, "content", response)
    if isinstance(content, list):
        return "".join(block_text(block) for block in content)
    return content if isinstance(content, str) else str(content)


def block_text(block: Any) -> str:
    """Extract text from one content block (string or ``{"text": ...}`` dict)."""
    if isinstance(block, str):
        return block
    if isinstance(block, dict):
        return str(block.get("text", ""))
    return str(block)


def extract_json(content: str) -> Any:
    """Best-effort extraction of the first JSON value from ``content``.

    Tries a fenced `````json````` block first, then the whole text, then a
    bracket slice for the common "preamble {json} postamble" shape.  Returns
    ``None`` when nothing parses.
    """
    match = _FENCE_RE.search(content)
    candidates = [match.group(1)] if match else []
    candidates.append(content)
    for candidate in candidates:
        result = try_parse_json(candidate)
        if result is not None:
            return result
    return None


def try_parse_json(text: str) -> Any:
    """Try to parse ``text`` as JSON, then a bracket slice; ``None`` on failure."""
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except (ValueError, TypeError):
        pass
    obj = slice_between(stripped, "{", "}")
    if obj is not None:
        try:
            return json.loads(obj)
        except (ValueError, TypeError):
            pass
    arr = slice_between(stripped, "[", "]")
    if arr is not None:
        try:
            return json.loads(arr)
        except (ValueError, TypeError):
            pass
    return None


def slice_between(text: str, open_ch: str, close_ch: str) -> str | None:
    """Return the substring from the first ``open_ch`` to the last ``close_ch``.

    Returns ``None`` when either bracket is absent or out of order.  Using the
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
