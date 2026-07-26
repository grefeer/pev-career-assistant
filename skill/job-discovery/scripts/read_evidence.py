#!/usr/bin/env python3
"""read_evidence.py - Read a stashed evidence/page file into the agent context.

Why this exists
---------------
``browse.py`` runs as a subprocess (cwd = skill dir) and writes page text to the
REAL filesystem under ``output/evidence/pages/page_NN.txt``. The agent runs on a
``FilesystemBackend(virtual_mode=True)`` whose virtual FS is seeded from the
skill source tree at init time, so it CANNOT see files that browse.py creates at
runtime. The agent therefore has no way to read a single page's text on demand
via ``read_file``.

This script is the real-disk -> agent bridge for per-page progressive
disclosure: a ``jd_extractor`` sub-agent reads ONE small page file (instead of
the parent holding all 16 pages' text in its own context) and extracts JDs from
just that page. This is the on-disk backing for parallel per-page extraction.

Usage
-----
  python scripts/read_evidence.py <path> [--max-chars N]

``<path>`` may be relative to the skill dir (e.g.
``output/evidence/pages/page_03.txt``) or absolute. Only paths under the skill's
``output/`` or ``references/`` trees are readable - anything else is refused so
a misbehaving prompt cannot exfiltrate arbitrary files.

Output: the file's text on stdout (capped at ``--max-chars``, default 50000),
followed by a single JSON summary line beginning ``[READ_SUMMARY]`` with the
path, full length, and a truncated flag. Empty/missing files are reported, not
raised, so the agent gets a clean message instead of a crashed ToolMessage.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_SKILL_ROOT = Path(__file__).resolve().parents[1]
_ALLOWED_SUBDIRS = ("output", "references")
_DEFAULT_MAX_CHARS = 50_000


def _resolve_and_check(raw_path: str) -> tuple[Path | None, str]:
    """Resolve ``raw_path`` against the skill root and enforce the sandbox.

    Returns ``(path, reason)``. On success ``path`` is the resolved file and
    ``reason`` is "". On failure ``path`` is None and ``reason`` explains why.
    """
    p = Path(raw_path)
    if not p.is_absolute():
        p = (_SKILL_ROOT / p).resolve()
    else:
        p = p.resolve()
    try:
        p.relative_to(_SKILL_ROOT)
    except ValueError:
        return None, f"path outside skill root: {p}"
    # Must live under one of the allowed subtrees.
    rel = p.relative_to(_SKILL_ROOT)
    parts = rel.parts
    if not parts or parts[0] not in _ALLOWED_SUBDIRS:
        return None, f"path not under {('/'.join(_ALLOWED_SUBDIRS))}: {rel}"
    if not p.exists():
        return None, f"file not found: {rel}"
    if not p.is_file():
        return None, f"not a file: {rel}"
    return p, ""


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read a stashed evidence/page file into the agent context."
    )
    parser.add_argument("path", help="Path to read (skill-relative or absolute)")
    parser.add_argument(
        "--max-chars",
        type=int,
        default=_DEFAULT_MAX_CHARS,
        help=f"Cap output chars (default {_DEFAULT_MAX_CHARS})",
    )
    args = parser.parse_args()

    p, reason = _resolve_and_check(args.path)
    if p is None:
        print(f"[READ_SUMMARY] {json.dumps({'status': 'error', 'reason': reason}, ensure_ascii=False)}")
        return 0  # exit 0 so the agent gets the message, not a crashed tool

    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"[READ_SUMMARY] {json.dumps({'status': 'error', 'reason': f'read error: {exc}'}, ensure_ascii=False)}")
        return 0

    full_len = len(text)
    truncated = full_len > args.max_chars
    out = text[: args.max_chars]
    if truncated:
        out += f"\n\n... [TRUNCATED: showed {args.max_chars}/{full_len} chars]"

    sys.stdout.write(out)
    if not out.endswith("\n"):
        sys.stdout.write("\n")
    summary = {
        "status": "ok",
        "path": str(p.relative_to(_SKILL_ROOT)),
        "full_length": full_len,
        "shown_length": len(out),
        "truncated": truncated,
    }
    print(f"[READ_SUMMARY] {json.dumps(summary, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
