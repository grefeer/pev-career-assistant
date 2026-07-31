#!/usr/bin/env python3
"""Deterministic completeness gate for a Skill extraction run."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
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
        _body_signature(candidate),
    )


def _body_signature(candidate: dict[str, Any]) -> str:
    """Identity signal for URL-less roles sharing a title.

    A campus portal can legitimately publish two openings with the same title
    under different teams while omitting both location and detail URL.  Their
    full JD body is the only auditable discriminator.  Formatting/punctuation
    differences are ignored so the same posting captured twice still merges.
    """
    body = str(candidate.get("responsibilities") or candidate.get("requirements") or "")
    return re.sub(r"[\W_]", "", body.casefold())


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


def evaluate_manifest_coverage(
    *, candidates_path: Path, manifest_path: Path, skill_root: Path,
) -> dict[str, Any]:
    """Verify browser-produced coverage data, never model-supplied values."""
    try:
        candidates = json.loads(candidates_path.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"coverage_verified": False, "reasons": ["manifest_or_candidates_unreadable"]}
    if not isinstance(candidates, list) or not isinstance(manifest, dict):
        return {"coverage_verified": False, "reasons": ["manifest_or_candidates_invalid"]}

    evidence_root = (skill_root / "output" / "evidence").resolve()
    pages = manifest.get("page_files")
    valid_pages: list[str] = []
    valid_page_hashes: set[str] = set()
    reasons: list[str] = []
    if not isinstance(pages, list):
        reasons.append("manifest_pages_missing")
        pages = []
    for value in pages:
        if not isinstance(value, str):
            reasons.append("manifest_page_invalid")
            continue
        path = Path(value)
        resolved = (path if path.is_absolute() else skill_root / path).resolve()
        try:
            resolved.relative_to(evidence_root)
        except ValueError:
            reasons.append("manifest_page_outside_evidence")
            continue
        if not resolved.is_file() or resolved.stat().st_size == 0:
            reasons.append("manifest_page_missing")
            continue
        valid_pages.append(str(resolved))
        valid_page_hashes.add(hashlib.sha256(resolved.read_bytes()).hexdigest())

    if manifest.get("truncated_by_max_pages"):
        reasons.append("browse_truncated_by_max_pages")
    declared_pages = manifest.get("declared_total_pages")
    collected_pages = manifest.get("pages_collected")
    if (
        isinstance(declared_pages, int) and isinstance(collected_pages, int)
        and declared_pages > collected_pages
    ):
        reasons.append("browse_truncated_by_max_pages")
    expected_count = manifest.get("listing_count")
    if not isinstance(expected_count, int):
        expected_count = None
    result = evaluate_coverage(
        page_files=valid_pages,
        candidates=candidates,
        terminal_evidence=(
            manifest.get("terminal_evidence")
            if isinstance(manifest.get("terminal_evidence"), str) else None
        ),
        expected_count=expected_count,
    )
    # A coverage pass must establish a provenance chain: every extracted JD
    # cites one actual page in the browser manifest.  Page existence alone is
    # insufficient because a model could write a plausible candidate without
    # consuming the captured evidence.
    for candidate in candidates:
        if not isinstance(candidate, dict):
            reasons.append("candidate_invalid")
            continue
        refs = candidate.get("evidence_refs")
        if not isinstance(refs, list) or not refs:
            reasons.append("candidate_evidence_missing")
            continue
        hashes = {
            str(ref.get("content_hash") or "")
            for ref in refs if isinstance(ref, dict)
        }
        if not hashes.intersection(valid_page_hashes):
            reasons.append("candidate_evidence_not_in_manifest")
    result["reasons"] = list(dict.fromkeys([*reasons, *result["reasons"]]))
    result["coverage_verified"] = not result["reasons"]
    result["manifest_path"] = str(manifest_path)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify Skill extraction coverage")
    parser.add_argument("candidates", type=Path)
    parser.add_argument("--pages", nargs="+")
    parser.add_argument("--terminal-evidence")
    parser.add_argument("--expected-count", type=int)
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    if args.manifest is not None:
        print(json.dumps(evaluate_manifest_coverage(
            candidates_path=args.candidates, manifest_path=args.manifest,
            skill_root=Path.cwd(),
        ), ensure_ascii=False))
        return
    data = json.loads(args.candidates.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise SystemExit("candidates must be a JSON array")
    if not args.pages:
        raise SystemExit("--pages is required without --manifest")
    print(json.dumps(evaluate_coverage(
        page_files=args.pages, candidates=data, terminal_evidence=args.terminal_evidence,
        expected_count=args.expected_count,
    ), ensure_ascii=False))


if __name__ == "__main__":
    main()
