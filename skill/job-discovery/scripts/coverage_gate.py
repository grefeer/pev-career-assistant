#!/usr/bin/env python3
"""Deterministic completeness gate for a Skill extraction run."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _location_signature(locations: Any) -> str:
    """Return a stable location key for URL-less job rows.

    An apply URL is the strongest identity on a public recruiting site.  Some
    sites, however, publish an application form only at the company level. In
    that case title + department + location is safer than treating every blank
    URL as the same vacancy.  This is deliberately aligned with the evaluator
    rather than making the coverage gate reject valid URL-less listings.
    """
    if not isinstance(locations, list):
        return ""
    values = {str(location or "").strip() for location in locations}
    return "|".join(sorted(value for value in values if value))


def _candidate_identity(candidate: dict[str, Any]) -> tuple[str, ...]:
    """Identify one concrete opening without collapsing shared JD templates."""
    apply_url = str(candidate.get("apply_url") or "").strip()
    if apply_url:
        return ("url", apply_url)
    return (
        "fallback",
        str(candidate.get("title") or "").strip(),
        str(candidate.get("department") or "").strip(),
        _location_signature(candidate.get("locations")),
    )


def _identities(candidates: list[dict[str, Any]]) -> set[tuple[str, ...]]:
    """Use fallback identity for a URL copied across distinct job titles."""
    titles_by_url: dict[str, set[str]] = {}
    for candidate in candidates:
        url = str(candidate.get("apply_url") or "").strip()
        title = str(candidate.get("title") or "").strip()
        if url and title:
            titles_by_url.setdefault(url, set()).add(title)
    shared_urls = {url for url, titles in titles_by_url.items() if len(titles) > 1}
    identities: set[tuple[str, ...]] = set()
    for candidate in candidates:
        url = str(candidate.get("apply_url") or "").strip()
        if url and url in shared_urls:
            copy = dict(candidate)
            copy["apply_url"] = ""
            identities.add(_candidate_identity(copy))
        else:
            identities.add(_candidate_identity(candidate))
    return identities


def evaluate_coverage(
    *, page_files: list[str], candidates: list[dict[str, Any]],
    terminal_evidence: str | None, expected_count: int | None = None,
) -> dict[str, Any]:
    """Return an auditable PASS only when observed evidence is complete."""
    body_count = sum(bool((c.get("responsibilities") or "").strip() or (c.get("requirements") or "").strip()) for c in candidates)
    identities = _identities(candidates)
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
