#!/usr/bin/env python3
"""validate.py — Validate extracted job candidates against the schema.

Usage:
  validate.py <candidates.json> [--strict] [--package] [--verify]

Reads a JSON file containing a list of NormalizedJobCandidate objects, validates
each against constraints, and reports errors/warnings to stdout. Exit code 0
if all candidates pass basic checks, 1 if any fail.

Flags:
  --strict    Also check confidence >= 0.6 and description_text non-empty.
  --package   Add idempotency_key and similarity_group_key to each candidate.
  --verify    Run evidence quality checks (staleness, vagueness, non-JD text).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


_REQUIRED_FIELDS = ["title", "company_name"]
_STRING_FIELDS = [
    "title", "company_name", "department", "description_text",
    "responsibilities", "requirements", "apply_url", "deadline_text",
    "referral_code",
]
_LIST_FIELDS = ["locations", "recruitment_types", "industries", "evidence_refs", "normalization_warnings"]
_FLOAT_FIELDS = ["confidence"]

_VALID_RECRUITMENT_TYPES = {
    "校园招聘", "社会招聘", "实习", "博士专项", "提前批", "内推",
    "应届生", "校招", "社招", "campus", "experienced", "intern",
    "管培生", "博士后",
}

# Evidence quality thresholds
_MIN_DESCRIPTION_LENGTH = 50
_STALE_YEAR_THRESHOLD = 2024
_JD_KEYWORDS = [
    "岗位", "职位", "招聘", "要求", "职责",
    "job", "position", "requirement", "responsibility", "qualification",
]


def _validate_candidate(cand: dict[str, Any], idx: int, strict: bool) -> list[str]:
    """Validate a single candidate dict. Returns a list of error messages."""
    errors: list[str] = []
    prefix = f"[candidate {idx}]"

    # Required fields
    for field in _REQUIRED_FIELDS:
        val = cand.get(field)
        if not val or (isinstance(val, str) and not val.strip()):
            errors.append(f"{prefix} Missing required field: {field}")

    # String fields must be strings if present
    for field in _STRING_FIELDS:
        val = cand.get(field)
        if val is not None and not isinstance(val, str):
            errors.append(f"{prefix} Field '{field}' must be a string, got {type(val).__name__}")

    # List fields must be lists if present
    for field in _LIST_FIELDS:
        val = cand.get(field)
        if val is not None and not isinstance(val, list):
            errors.append(f"{prefix} Field '{field}' must be a list, got {type(val).__name__}")

    # Confidence must be float
    val = cand.get("confidence")
    if val is not None and not isinstance(val, (int, float)):
        errors.append(f"{prefix} Field 'confidence' must be a number")

    # Recruitment types should use standard values
    for rt in (cand.get("recruitment_types") or []):
        if rt not in _VALID_RECRUITMENT_TYPES:
            # Not an error, just note it
            pass

    # Strict checks
    if strict:
        if not cand.get("description_text", "").strip():
            errors.append(f"{prefix} (strict) description_text is empty")
        conf = cand.get("confidence", 0)
        if isinstance(conf, (int, float)) and conf < 0.6:
            errors.append(f"{prefix} (strict) confidence {conf} < 0.6")

    return errors


import re


def _add_packaging_keys(cand: dict[str, Any]) -> None:
    """Add idempotency_key and similarity_group_key to a candidate dict (mutates in place)."""
    title = cand.get("title") or ""
    company = cand.get("company_name") or ""
    locations = cand.get("locations") or []
    recruitment_types = cand.get("recruitment_types") or []
    evidence_refs = cand.get("evidence_refs") or []
    apply_url = cand.get("apply_url") or ""

    evidence_hash = ""
    if evidence_refs and isinstance(evidence_refs[0], dict):
        evidence_hash = evidence_refs[0].get("content_hash", "")

    primary_location = locations[0] if locations else ""
    primary_rec_type = recruitment_types[0] if recruitment_types else ""

    # Idempotency key: SHA-256 of canonical fields
    parts = (
        company.strip().lower(),
        title.strip().lower(),
        primary_location.strip().lower(),
        apply_url.strip().lower(),
        evidence_hash.strip().lower(),
    )
    cand["idempotency_key"] = hashlib.sha256("::".join(parts).encode("utf-8")).hexdigest()

    # Similarity group key: first-3 of company + title + recruitment_type
    cp = company.strip().lower()[:3]
    tp = title.strip().lower()[:3]
    rt = primary_rec_type.strip().lower() or "unknown"
    cand["similarity_group_key"] = f"{cp}::{tp}::{rt}"


def _check_evidence_quality(cand: dict[str, Any]) -> list[str]:
    """Check evidence quality and return warning strings."""
    warnings: list[str] = []
    desc = cand.get("description_text") or ""

    # Staleness
    years = re.findall(r"\b(20[0-9]{2})\b", desc)
    for y in years:
        try:
            if 2000 < int(y) < _STALE_YEAR_THRESHOLD:
                warnings.append(f"POSSIBLE_STALE: references year {y} (threshold: {_STALE_YEAR_THRESHOLD})")
                break
        except ValueError:
            continue

    # Vagueness
    if len(desc.strip()) < _MIN_DESCRIPTION_LENGTH:
        warnings.append(f"VAGUE_DESCRIPTION: {len(desc.strip())} chars (min: {_MIN_DESCRIPTION_LENGTH})")

    # Non-JD text
    if len(desc) > 100:
        dl = desc.lower()
        kw_count = sum(1 for kw in _JD_KEYWORDS if kw in dl)
        if kw_count < 2:
            warnings.append(f"NON_JD_TEXT: only {kw_count} JD keywords found")

    return warnings


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate job candidate JSON against schema")
    parser.add_argument("file", help="Path to candidates JSON file (list of objects)")
    parser.add_argument("--strict", action="store_true",
                        help="Enforce stricter quality checks (confidence, description_text)")
    parser.add_argument("--package", action="store_true",
                        help="Add idempotency_key and similarity_group_key to each candidate")
    parser.add_argument("--verify", action="store_true",
                        help="Run evidence quality checks (staleness, vagueness, non-JD)")
    args = parser.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"ERROR: File not found: {path}")
        sys.exit(1)

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON: {e}")
        sys.exit(1)

    if not isinstance(data, list):
        print("ERROR: Root element must be a JSON array")
        sys.exit(1)

    total = len(data)
    all_errors: list[str] = []
    all_warnings: list[str] = []

    for i, cand in enumerate(data):
        if not isinstance(cand, dict):
            all_errors.append(f"[candidate {i}] Not a JSON object")
            continue
        errors = _validate_candidate(cand, i, args.strict)
        all_errors.extend(errors)

        # Packaging keys
        if args.package:
            _add_packaging_keys(cand)

        # Evidence quality checks
        if args.verify:
            q_warns = _check_evidence_quality(cand)
            for w in q_warns:
                all_warnings.append(f"[candidate {i}] {w}")
            existing = cand.get("normalization_warnings") or []
            for w in q_warns:
                if w not in existing:
                    existing.append(w)
            cand["normalization_warnings"] = existing

    # Rewrite file if packaging keys were added
    if args.package:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # Report
    print(f"Validated {total} candidate(s)")
    print(f"Errors: {len(all_errors)}")
    mode_parts = []
    if args.strict:
        mode_parts.append("strict")
    if args.package:
        mode_parts.append("package")
    if args.verify:
        mode_parts.append("verify")
    print(f"Mode: {', '.join(mode_parts) if mode_parts else 'basic'}")

    if all_errors:
        print("\n--- ERRORS ---")
        for e in all_errors:
            print(f"  FAIL: {e}")

    if all_warnings:
        print("\n--- QUALITY WARNINGS ---")
        for w in all_warnings:
            print(f"  WARN: {w}")

    if not all_errors:
        print("\nAll candidates passed validation.")

    sys.exit(1 if all_errors else 0)


if __name__ == "__main__":
    main()
