#!/usr/bin/env python3
"""write_candidates.py - Persist a batch of extracted candidate JDs to disk.

Why this exists
---------------
Each ``jd_extractor`` sub-agent pulls JDs out of ONE page's text. Its
generation holds a single page's worth of candidates, so the per-page design
sidesteps the 8192-token truncation that loses jobs when one agent tries to
emit 151 candidates in one message.

But a single page can still hold 10-20 verbose JDs - more than fits in one
8192-token generation. So this script supports **append mode** (``--append``):
the sub-agent writes candidates in SMALL batches (<=6 per call), each batch well
under the cap, and the script accumulates them into one file with identity
dedup. This is what keeps a large page from truncating.

Candidates are passed on STDIN (not a CLI arg) to avoid Windows' ~32k
command-line length limit. The stdin is parsed **leniently**: a leading prose
prefix (``Here are the candidates: [...]``), a ```json fence, or surrounding
text are all stripped so a sub-agent that adds narration does not produce a
malformed file. A truncated final batch (the model hit max_tokens mid-object)
yields whatever complete objects the lenient scanner recovers - the bad object
is dropped, not the whole batch.

Usage
-----
  # First batch (creates the file):
  ... | python scripts/write_candidates.py --out output/candidates/page_03.json
  # Subsequent batches (accumulate, dedup by identity):
  ... | python scripts/write_candidates.py --out output/candidates/page_03.json --append

The script reads a JSON document from stdin. Accepted shapes:
  - a JSON array of candidate objects:  [{"title": "...", "company_name": "..."}, ...]
  - a JSON object wrapping one candidate: {"title": "...", ...}
  - a JSON object with a "candidates" array: {"candidates": [...], ...}

It validates the minimum publishable JD shape (non-empty title, company name,
and at least one responsibilities/requirements body), drops items that are not
JSON objects, and (in --append) dedups against the existing file by (company,
normalized title, location). Output
(stdout): a JSON summary with status / written-so-far count / out path. Never
raises - malformed input is reported as a status JSON so the agent gets a clean
tool result instead of a crash.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

_SKILL_ROOT = Path(__file__).resolve().parents[1]
_ALLOWED_ROOT = _SKILL_ROOT / "output"

_INVISIBLE = "​‌‍‎‏﻿　\t"
_WS = re.compile(r"\s+")
# Exact page-file stem (page_01, page_12, ...). Anything else (page_03_batch,
# page_10_new, page_06_temp2, ...) is a suffixed variant the model invented and
# is REDIRECTED to the base page_NN.json so candidates are never lost.
_PAGE_STEM_RE = re.compile(r"^page_\d+$")
# Match the page_<digits> prefix of a suffixed stem (page_03_batch -> page_03).
_PAGE_PREFIX_RE = re.compile(r"^(page_\d+)_.*$")


def _norm_title(title: str | None) -> str:
    if not title:
        return ""
    s = unicodedata.normalize("NFKC", title)
    for ch in _INVISIBLE:
        s = s.replace(ch, "")
    s = s.lower()
    s = _WS.sub("", s)
    return s


def _loc_key(c: dict[str, Any]) -> str:
    locs = c.get("locations") or []
    norms: list[str] = []
    for loc in locs:
        raw = str(loc or "")
        for d in ("、", "，", ",", "/", ";", "；"):
            raw = raw.replace(d, "、")
        for part in raw.split("、"):
            s = part.strip()
            if s:
                norms.append(s)
    return "|".join(sorted(set(norms)))


def _identity(c: dict[str, Any]) -> tuple:
    """Identity for cross-batch dedup (company, normalized title, location)."""
    company = (c.get("company_name") or "").strip().lower()
    return (company, _norm_title(c.get("title")), _loc_key(c))


def _lenient_extract_json(raw: str) -> Any:
    """Parse JSON leniently: strip prose/code-fences, then brace-scan.

    Tries, in order: whole-document json.loads; strip a ```json fence then
    loads; balanced-scan for the outermost ``[...]`` (array) or ``{...}``
    (object); finally brace-scan for individual ``{...}`` objects and return
    them as a list. Returns None if nothing parseable is found.
    """
    if not raw or not raw.strip():
        return None
    s = raw.strip()
    # Strip a leading ```json ... ``` fence if present.
    m = re.search(r"```(?:json)?\s*(.*?)```", s, flags=re.DOTALL)
    if m:
        s = m.group(1).strip()
    # 1. Direct parse.
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # 2. Outermost balanced [...] (array).
    arr = _balanced_span(s, "[", "]")
    if arr is not None:
        try:
            return json.loads(arr)
        except json.JSONDecodeError:
            pass
    # 3. Outermost balanced {...} (single candidate object).
    obj = _balanced_span(s, "{", "}")
    if obj is not None:
        try:
            return json.loads(obj)
        except json.JSONDecodeError:
            pass
    # 4. Brace-scan individual objects (recovers from a truncated array / prose
    # mixed with objects). Drops any malformed object.
    out: list[dict[str, Any]] = []
    for span in _scan_objects(s):
        try:
            v = json.loads(span)
        except json.JSONDecodeError:
            continue
        if isinstance(v, dict):
            out.append(v)
    return out or None


def _balanced_span(s: str, open_ch: str, close_ch: str) -> str | None:
    """Return the outermost balanced span (incl. delimiters) for open/close."""
    start = s.find(open_ch)
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return s[start : i + 1]
    return None  # unbalanced (truncated)


def _scan_objects(s: str) -> list[str]:
    """Yield each top-level balanced ``{...}`` span in ``s``."""
    spans: list[str] = []
    depth = 0
    in_str = False
    esc = False
    start = -1
    for i, ch in enumerate(s):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start != -1:
                spans.append(s[start : i + 1])
                start = -1
    return spans


def _normalize(data: Any) -> list[dict[str, Any]]:
    """Coerce accepted shapes into a flat list of candidate dicts."""
    if isinstance(data, dict):
        if isinstance(data.get("candidates"), list):
            data = data["candidates"]
        else:
            data = [data]
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _valid_candidate(c: dict[str, Any]) -> bool:
    """Accept only a publishable JD, never a title-only listing echo.

    The caller is collecting structured job descriptions, not merely vacancy
    titles. A title-only row can remain in raw browser evidence, but must not
    enter the persisted candidate set: it could otherwise hide a failed detail
    extraction and make a count look complete while JD coverage is not.
    """
    title = (c.get("title") or "").strip() if isinstance(c.get("title"), str) else ""
    company = (c.get("company_name") or "").strip() if isinstance(c.get("company_name"), str) else ""
    responsibilities = (
        (c.get("responsibilities") or "").strip()
        if isinstance(c.get("responsibilities"), str) else ""
    )
    requirements = (
        (c.get("requirements") or "").strip()
        if isinstance(c.get("requirements"), str) else ""
    )
    return bool(title and company and (responsibilities or requirements))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Persist a batch of extracted candidate JDs (stdin JSON -> file)."
    )
    parser.add_argument(
        "--out",
        required=True,
        help="Output path (skill-relative, under output/)",
    )
    parser.add_argument(
        "--append",
        action="store_true",
        help="Accumulate into --out if it exists (identity-dedup). Use this for "
        "2nd+ batches when writing a page in small batches.",
    )
    args = parser.parse_args()

    out_p = Path(args.out)
    if not out_p.is_absolute():
        out_p = (_SKILL_ROOT / out_p).resolve()
    else:
        out_p = out_p.resolve()
    try:
        out_p.relative_to(_ALLOWED_ROOT)
    except ValueError:
        print(json.dumps({
            "status": "error",
            "reason": f"refused: --out must be under output/ (got {out_p})",
        }, ensure_ascii=False))
        return 0

    # REDIRECT any suffixed page path (page_03_batch.json, page_10_new.json,
    # page_06_temp2.json, ...) to the base page_NN.json. v1.1 REFUSED a fixed
    # suffix list and sub-agents lost ~10 candidates when they hit the refusal
    # and failed to retry the exact path; the model also adapted by inventing
    # new suffixes (_batch) the list missed. Redirecting by page_<digits> prefix
    # catches ANY suffix, PRESERVES the candidates (they land in page_NN.json
    # with identity-dedup), and ends the whack-a-mole. Always-append (below)
    # makes this safe regardless of write order.
    redirected_to: str | None = None
    if not _PAGE_STEM_RE.match(out_p.stem):
        pm = _PAGE_PREFIX_RE.match(out_p.stem)
        if pm:
            base = pm.group(1)
            out_p = (out_p.parent / f"{base}.json").resolve()
            try:
                redirected_to = str(out_p.relative_to(_SKILL_ROOT)).replace("\\", "/")
            except ValueError:
                redirected_to = str(out_p)
        else:
            print(json.dumps({
                "status": "error",
                "reason": "refused: --out must be output/candidates/page_NN.json",
                "out": str(out_p.relative_to(_SKILL_ROOT)),
            }, ensure_ascii=False))
            return 0

    raw = sys.stdin.read()
    data = _lenient_extract_json(raw)
    if data is None:
        print(json.dumps({
            "status": "error",
            "reason": "no parseable JSON in stdin (empty or all-malformed)",
            "out": str(out_p.relative_to(_SKILL_ROOT)),
            "redirected_to": redirected_to,
        }, ensure_ascii=False))
        return 0

    batch = _normalize(data)
    kept = [c for c in batch if _valid_candidate(c)]
    dropped = len(batch) - len(kept)

    # ALWAYS accumulate (append semantics): load existing + identity-dedup. This
    # makes redirects and retries safe - a re-written or re-routed batch is
    # deduped, never double-counted, and never overwrites prior batches. The
    # --append flag is accepted for back-compat but no longer gates this.
    existing: list[dict[str, Any]] = []
    if out_p.exists():
        try:
            prev = json.loads(out_p.read_text(encoding="utf-8"))
            if isinstance(prev, list):
                existing = [c for c in prev if isinstance(c, dict)]
        except (json.JSONDecodeError, OSError):
            existing = []

    merged: list[dict[str, Any]] = list(existing)
    seen = {_identity(c) for c in existing}
    added = 0
    for c in kept:
        ident = _identity(c)
        if ident in seen:
            continue
        seen.add(ident)
        merged.append(c)
        added += 1

    out_p.parent.mkdir(parents=True, exist_ok=True)
    out_p.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    result: dict[str, Any] = {
        "status": "ok",
        "out": str(out_p.relative_to(_SKILL_ROOT)),
        "batch_received": len(batch),
        "batch_kept": len(kept),
        "batch_dropped_invalid": dropped,
        "appended": added,
        "total_in_file": len(merged),
        "mode": "append" if existing else "overwrite",
    }
    if redirected_to:
        result["redirected_to"] = redirected_to
        result["note"] = (
            f"suffixed --out was redirected to {redirected_to} (always-append + "
            "identity-dedup; your candidates are safe there)."
        )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
