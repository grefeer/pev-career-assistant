#!/usr/bin/env python3
"""Deterministic completeness gate for a Skill extraction run."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def evaluate_coverage(
    *, page_files: list[str], candidates: list[dict[str, Any]],
    terminal_evidence: str | None, expected_count: int | None = None,
) -> dict[str, Any]:
    """Return an auditable PASS only when observed evidence is complete."""
    body_count = sum(bool((c.get("responsibilities") or "").strip() or (c.get("requirements") or "").strip()) for c in candidates)
    identities = {
        (str(c.get("title") or "").strip(), str(c.get("apply_url") or "").strip())
        for c in candidates
    }
    reasons: list[str] = []
    if not page_files:
        reasons.append("no_page_evidence")
    if not terminal_evidence:
        reasons.append("missing_terminal_evidence")
    if body_count != len(candidates):
        reasons.append("missing_jd_body")
    if len(identities) != len(candidates):
        reasons.append("duplicate_candidate_identity")
    if expected_count is not None and len(candidates) != expected_count:
        reasons.append("expected_count_mismatch")
    return {
        "coverage_verified": not reasons,
        "page_count": len(page_files),
        "candidate_count": len(candidates),
        "body_candidate_count": body_count,
        "unique_listing_count": len(identities),
        "expected_count": expected_count,
        "terminal_evidence": terminal_evidence,
        "reasons": reasons,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify Skill extraction coverage")
    parser.add_argument("candidates", type=Path)
    parser.add_argument("--pages", nargs="+", required=True)
    parser.add_argument("--terminal-evidence")
    parser.add_argument("--expected-count", type=int)
    args = parser.parse_args()
    data = json.loads(args.candidates.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise SystemExit("candidates must be a JSON array")
    print(json.dumps(evaluate_coverage(
        page_files=args.pages, candidates=data, terminal_evidence=args.terminal_evidence,
        expected_count=args.expected_count,
    ), ensure_ascii=False))


if __name__ == "__main__":
    main()
