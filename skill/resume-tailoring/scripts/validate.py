#!/usr/bin/env python3
"""validate.py - validate resume diff operations for the resume-tailoring skill.

Self-contained, agent-callable mirror of
``backend.app.services.draft_validators.validate_draft_diffs``. Same rule set,
same error codes, so a draft produced by ``generate.py`` (or hand-written by the
agent) can be checked against the candidate's confirmed facts + evidence refs
before anyone applies it.

Each diff must have:
  - ``op`` in {reorder, rephrase, summarize, omit, highlight}
  - ``section`` non-empty
  - ``fact_ref`` present in confirmed_facts
  - every ``evidence_ids`` entry present in evidence_refs values

Usage:
  python scripts/validate.py --input output/draft_diffs.json \
      --facts output/profile_facts.json --evidence output/evidence_refs.json
  python scripts/validate.py --input diffs.json --facts facts.json

Exit code is always 0 (soft errors are reported in the output JSON, exit 0 so
the calling runtime gets a clean message instead of a crash). The valid set is
written to ``--out`` (default ``output/validation.json``) and a one-line
summary is printed to stdout.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

# Mirrors backend.app.services.draft_validators
VALID_OPS = frozenset({"reorder", "rephrase", "summarize", "omit", "highlight"})


class DraftValidationError(ValueError):
    """Raised when a draft diff fails validation. Carries a stable error_code."""

    def __init__(self, error_code: str, message: str, *, index: int | None = None):
        super().__init__(message)
        self.error_code = error_code
        self.index = index


def validate_diffs(
    diffs: list[dict[str, Any]],
    confirmed_facts: dict[str, Any],
    evidence_refs: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    """Validate a list of resume diff operations against confirmed facts.

    Returns the original list if valid; raises ``DraftValidationError`` on the
    first failure. ``evidence_refs`` may be empty/None (diffs with no
    ``evidence_ids`` still validate).
    """
    evidence_refs = evidence_refs or {}
    valid_evidence_ids: set[str] = set()
    for ev_ids in evidence_refs.values():
        if ev_ids:
            valid_evidence_ids.update(ev_ids)

    for idx, diff in enumerate(diffs):
        op = diff.get("op")
        if not op:
            raise DraftValidationError(
                "draft_validation_missing_op",
                f"Diff at index {idx} is missing 'op' field",
                index=idx,
            )
        if op not in VALID_OPS:
            raise DraftValidationError(
                "draft_validation_invalid_op",
                f"Diff at index {idx} has invalid op '{op}'; expected one of {sorted(VALID_OPS)}",
                index=idx,
            )

        section = diff.get("section")
        if not section:
            raise DraftValidationError(
                "draft_validation_empty_section",
                f"Diff at index {idx} has empty or missing 'section' field",
                index=idx,
            )

        fact_ref = diff.get("fact_ref")
        if fact_ref not in confirmed_facts:
            raise DraftValidationError(
                "draft_validation_invalid_fact_ref",
                f"Diff at index {idx} references unknown fact_ref '{fact_ref}'",
                index=idx,
            )

        for eid in diff.get("evidence_ids", []):
            if eid not in valid_evidence_ids:
                raise DraftValidationError(
                    "draft_validation_invalid_evidence",
                    f"Diff at index {idx} references unknown evidence_id '{eid}'",
                    index=idx,
                )

    return diffs


# ═══════════════════════════════════════════════════════════════════
# I/O helpers (mirror generate.py conventions)
# ═══════════════════════════════════════════════════════════════════

def _read_json(path: str) -> Any:
    import pathlib

    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


def _write_json(path: str, payload: dict[str, Any]) -> None:
    import pathlib

    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _load_diffs(args: argparse.Namespace) -> list[dict[str, Any]]:
    raw = _read_json(args.input) if args.input else json.loads(sys.stdin.read())
    diffs = raw.get("diffs") if isinstance(raw, dict) else raw
    if not isinstance(diffs, list):
        raise ValueError("input must be a list of diffs or {'diffs': [...]}")
    return diffs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate resume diff operations against confirmed facts + evidence."
    )
    parser.add_argument("--input", help="Path to diffs JSON (default: stdin)")
    parser.add_argument("--facts", required=True, help="Path to confirmed profile facts JSON")
    parser.add_argument("--evidence", help="Path to evidence_refs JSON (default: {})")
    parser.add_argument("--out", default="output/validation.json", help="Output path")
    args = parser.parse_args(argv)

    try:
        diffs = _load_diffs(args)
        confirmed_facts = _read_json(args.facts)
        if not isinstance(confirmed_facts, dict):
            raise ValueError("--facts must be a JSON object")
        evidence_refs = _read_json(args.evidence) if args.evidence else {}
        if not isinstance(evidence_refs, dict):
            raise ValueError("--evidence must be a JSON object")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        result = {"status": "failed", "code": "bad_input", "last_error": str(exc)[:500]}
        _write_json(args.out, result)
        print(json.dumps({**result, "out": args.out}, ensure_ascii=False))
        return 0

    try:
        validate_diffs(diffs, confirmed_facts, evidence_refs)
    except DraftValidationError as exc:
        result = {
            "status": "failed",
            "code": exc.error_code,
            "index": exc.index,
            "last_error": str(exc)[:500],
        }
        _write_json(args.out, result)
        print(json.dumps({**result, "out": args.out}, ensure_ascii=False))
        return 0

    result = {"status": "ok", "diff_count": len(diffs)}
    _write_json(args.out, result)
    print(json.dumps({**result, "out": args.out}, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover - script entry
    sys.exit(main())
