#!/usr/bin/env python3
"""normalize.py — JD text normalization utilities (zero-dependency, pure functions).

Provides the same canonical normalization used by deduplicate.py and the backend's
jd_normalizer.py, but as a standalone utility. Use this when you need to normalize
a single title for comparison, compute a core_hash for identity matching, or
pre-process company names — without loading the full deduplication pipeline.

All functions are importable AND the script works as a CLI.

Usage:
  # CLI: normalize a title
  python scripts/normalize.py --title "AI Infra研发工程师【2027届云弧计划】（上海）"
  # → "aiinfra研发工程师"

  # CLI: normalize a company name
  python scripts/normalize.py --company "深圳市腾讯计算机系统有限公司"
  # → "深圳市腾讯计算机系统有限公司"

  # CLI: compute core_hash of a JD body
  python scripts/normalize.py --hash --resp "设计并实现..." --req "本科及以上..."

  # Import from another script
  from normalize import normalize_title, normalize_company, core_hash
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from typing import Any

# ═══════════════════════════════════════════════════════════════════
# Character tables (mirrors backend: jd_normalizer.py)
# ═══════════════════════════════════════════════════════════════════

_INVISIBLE_CHARS = "\u200b\u200c\u200d\u200e\u200f\ufeff\u3000\t"
_ASCII_PUNCT = ",.:;!?()[]\"'<>/\\-~"
# CJK brackets + full-width punctuation.  +, #, & are intentionally KEPT
# so C++/C#/R&D survive normalization.
_CJK_PUNCT = (
    "\u3010\u3011"  # 【】
    "\u300c\u300d"  # 「」
    "\u300e\u300f"  # 『』
    "\u300a\u300b"  # 《》
    "\u3008\u3009"  # 〈〉
    "\u3014\u3015"  # 〔〕
    "\uff0c\u3002\u3001\uff1b\uff1a\uff01\uff1f\uff08\uff09"
)

_DELETE_TABLE = str.maketrans("", "", _ASCII_PUNCT + _CJK_PUNCT)
_WHITESPACE_RE = re.compile(r"\s+")
_TRAILING_QUALIFIER_RE = re.compile(
    r"(?:\uff08[^\uff08\uff09]*\uff09|\([^()]*\)|\u3010[^\u3010\u3011]*\u3011)\s*$"
)


def normalize_text(value: str | None) -> str:
    """Normalize free-form text for identity comparison.

    NFKC-folds (full-width → half-width), strips zero-width chars, lower-cases,
    deletes all whitespace, and drops structural punctuation. Letters, digits,
    and ``+#&@%`` survive so job titles / JD bodies keep distinguishing markers.
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
    """Repeatedly remove trailing （...）/ (...)/ 【...】 groups.

    Only TRAILING groups are removed — a leading tag like 【2027秋招】算法工程师
    keeps its bracket content (the brackets are later deleted by normalize_text).
    """
    while _TRAILING_QUALIFIER_RE.search(s):
        s = _TRAILING_QUALIFIER_RE.sub("", s).rstrip()
    return s


def normalize_title(title: str | None) -> str:
    """Normalize a job title for identity comparison.

    Like normalize_text but first strips trailing parenthetical / lenticular
    groups (location, specialization, program tags).  This collapses variants:
      「算法工程师（上海）」  and  「算法工程师」
      「AI Infra研发工程师【2027届云弧计划】」 and 「AI Infra研发工程师」

    Only the comparison key is affected — the stored title is unchanged.
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

    Excludes location, job code, and posting time.  Two candidates whose
    responsibilities/requirements normalize to the same bytes produce the same
    hash and are considered the same canonical job.
    """
    r = normalize_text(responsibilities)
    q = normalize_text(requirements)
    raw = f"{r}\n---requirements---\n{q}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ═══════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════

def _cli() -> None:
    parser = argparse.ArgumentParser(
        description="Normalize job titles, company names, and compute JD core hashes"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--title", type=str, help="Normalize a job title")
    group.add_argument("--company", type=str, help="Normalize a company name")
    group.add_argument("--text", type=str, help="Normalize free-form text")
    group.add_argument("--hash", action="store_true", help="Compute core_hash (requires --resp and --req)")
    parser.add_argument("--resp", type=str, help="Responsibilities text (for --hash)")
    parser.add_argument("--req", type=str, help="Requirements text (for --hash)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    result: dict[str, Any] = {}

    if args.title:
        result["input"] = args.title
        result["normalized"] = normalize_title(args.title)
    elif args.company:
        result["input"] = args.company
        result["normalized"] = normalize_company(args.company)
    elif args.text:
        result["input"] = args.text
        result["normalized"] = normalize_text(args.text)
    elif args.hash:
        if not args.resp and not args.req:
            print("ERROR: --hash requires at least one of --resp or --req", file=sys.stderr)
            sys.exit(1)
        result["core_hash"] = core_hash(args.resp or "", args.req or "")
        result["resp_normalized"] = normalize_text(args.resp) if args.resp else ""
        result["req_normalized"] = normalize_text(args.req) if args.req else ""

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for key, val in result.items():
            if key != "input":
                print(f"{key}: {val}")


if __name__ == "__main__":
    _cli()
