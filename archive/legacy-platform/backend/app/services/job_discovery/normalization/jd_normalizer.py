"""JD text normalization and ``core_hash`` for canonical-job deduplication.

Pure helpers (no I/O, no LLM). Consumed by the deduplicator and by the
supervisor extraction path to collapse duplicate candidates that arise when
the same job is captured across overlapping evidence pages.

D3 (scoped plan): the canonical identity for a *full-JD* candidate is
``normalized_company + core_hash(responsibilities, requirements)``. The
``core_hash`` deliberately excludes location, job code, and posting time so
the same JD advertised in two cities merges. For *title-only* candidates
(list pages whose detail bodies are not captured) there is no JD body, so
the identity falls back to ``normalized_company + normalized_title``.

Note: all non-ASCII characters below are written as ``\\u`` escapes so the
source is encoding-safe on Windows consoles (literal CJK / zero-width bytes
get mojibake'd when written through a GBK shell).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

# Zero-width / invisible chars that leak into scraped titles.
# U+200B ZWSP, U+200C ZWNJ, U+200D ZWJ, U+200E LRM, U+200F RLM, U+FEFF BOM,
# U+3000 ideographic space, plus tab.
_INVISIBLE_CHARS = "​‌‍‎‏﻿　\t"

# ASCII structural punctuation removed before hashing/identity.
_ASCII_PUNCT = ",.:;!?()[]\"'<>/\\-~"
# CJK brackets and full-width punctuation. NFKC folds the full-width
# punctuation to ASCII (already covered above), but the brackets survive
# NFKC, so they are listed explicitly. ``+``, ``#``, ``&`` are intentionally
# KEPT so programming-language markers (C++, C#, R&D) survive normalization
# and distinct titles do not collide.
_CJK_PUNCT = (
    "【】"   # lenticular brackets
    "「」"   # corner brackets
    "『』"   # white corner brackets
    "〈〉"   # angle brackets
    "《》"   # double angle brackets
    "〔〕"   # tortoise-shell brackets
    "，。、；：！？（）"
)

_DELETE_TABLE = str.maketrans("", "", _ASCII_PUNCT + _CJK_PUNCT)
_WHITESPACE_RE = re.compile(r"\s+")

# Trailing qualifier group: full-width （...）, ASCII (...), or lenticular 【...】.
# Used to strip location / specialization / program suffixes from titles for
# identity comparison so the same job captured with and without its ``（上海）``
# or ``【2027届云弧计划】`` suffix collapses: the deterministic page-text
# extractor strips these suffixes while the XHR-payload extractor preserves
# them, so the same job surfaces once as ``AI Infra研发工程师`` and once as
# ``AI Infra研发工程师【2027届云弧计划】``. Content inside a group must not
# itself contain the same bracket type (no nesting); a LEADING tag
# (``【2027秋招】算法工程师``) is NOT stripped - only trailing groups.
_TRAILING_QUALIFIER_RE = re.compile(r"(?:[（(][^（）()]*[）)]|【[^【】]*】)\s*$")


def normalize_text(value: str | None) -> str:
    """Normalize free-form text for identity comparison.

    NFKC-folds (full-width -> half-width), strips zero-width chars, lower-cases,
    drops all whitespace, and deletes structural punctuation. Letters, digits,
    and ``+#&@%`` survive so job titles/JD bodies keep their distinguishing
    markers.
    """
    if not value:
        return ""
    s = unicodedata.normalize("NFKC", value)
    for ch in _INVISIBLE_CHARS:
        s = s.replace(ch, "")
    s = s.lower()
    s = _WHITESPACE_RE.sub("", s)
    s = s.translate(_DELETE_TABLE)
    return s


def normalize_company(name: str | None) -> str:
    """Normalize a company name for identity comparison."""
    return normalize_text(name)


def _strip_trailing_qualifiers(s: str) -> str:
    """Repeatedly remove trailing ``（...）`` / ``(...)`` / ``【...】`` groups.

    A title may carry several trailing qualifiers
    (``算法工程师（北京）（校招）`` or ``AI Infra研发工程师【2027届云弧计划】``);
    each pass peels one until none remain. Only TRAILING groups are removed - a
    leading tag (``【2027秋招】算法工程师``) is kept (its bracket chars are later
    deleted by ``normalize_text`` but the tag content survives).
    """
    while _TRAILING_QUALIFIER_RE.search(s):
        s = _TRAILING_QUALIFIER_RE.sub("", s).rstrip()
    return s


def normalize_title(title: str | None) -> str:
    """Normalize a job title for identity comparison.

    Like :func:`normalize_text` but first strips trailing parenthetical /
    lenticular groups (location / specialization / program tags such as
    ``（上海）``, ``（多语种优势-上海）`` or ``【2027届云弧计划】``). The
    deterministic page-text extractor strips these suffixes while the
    XHR-payload extractor preserves them, so the same job surfaces once as
    ``产品管培生`` and once as ``产品管培生（上海）``, or once as
    ``AI Infra研发工程师`` and once as ``AI Infra研发工程师【2027届云弧计划】``;
    without this step they survive as duplicate candidates because
    ``normalize_text`` only deletes the bracket *characters* (keeping their
    content). Only the comparison key is affected - the candidate's stored
    title is unchanged.
    """
    if not title:
        return ""
    s = unicodedata.normalize("NFKC", title)
    for ch in _INVISIBLE_CHARS:
        s = s.replace(ch, "")
    s = _strip_trailing_qualifiers(s)
    s = s.lower()
    s = _WHITESPACE_RE.sub("", s)
    s = s.translate(_DELETE_TABLE)
    return s


def core_hash(responsibilities: str | None, requirements: str | None) -> str:
    """SHA-256 of the normalized JD body (responsibilities + requirements).

    Excludes location, job code, and posting time (D3). Two candidates whose
    responsibilities/requirements normalize to the same bytes produce the same
    hash and are considered the same canonical job.
    """
    r = normalize_text(responsibilities)
    q = normalize_text(requirements)
    raw = f"{r}\n---requirements---\n{q}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
