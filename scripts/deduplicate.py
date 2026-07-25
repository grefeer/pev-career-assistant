#!/usr/bin/env python3
"""deduplicate.py — Normalize, deduplicate, package, and verify candidate JSONs.

Covers capabilities the LLM agent cannot do reliably:
  - NFKC text normalization + core-hash (needs Unicode algorithms)
  - SHA-256 idempotency / similarity keys (agent can't hash)
  - Semantic deduplication (agent loses track with 50+ similar candidates)
  - Evidence quality checks (consistent rules, agent forgets)
  - Title-only echo dropping (depends on exact normalized-title matching)

Usage:
  deduplicate.py <candidate.json...> [--out <file>] [--no-verify]

  # Process all candidates from a batch run and merge into one file:
  python scripts/deduplicate.py output/candidates/*.json --out output/candidates_merged.json

  # Verify+package only (skip output):
  python scripts/deduplicate.py output/candidates/*.json

Output (stdout): JSON object with normalized/deduped candidates and stats.
Exit code 0 always (warnings emitted in output, not exit code).
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any


# ============================================================================
# Normalization (mirrors business code: normalization/jd_normalizer.py)
# ============================================================================

# Zero-width / invisible characters
_INVISIBLE_CHARS = "\u200b\u200c\u200d\u200e\u200f\ufeff\u3000\t"
_ASCII_PUNCT = ",.:;!?()[]\"'<>/\\-~"
_CJK_PUNCT = (
    "\u3010\u3011\u300c\u300d\u300e\u300f\u300a\u300b\u3008\u3009"
    "\u3014\u3015\uff0c\u3002\u3001\uff1b\uff1a\uff01\uff1f\uff08\uff09"
)
_DELETE_TABLE = str.maketrans("", "", _ASCII_PUNCT + _CJK_PUNCT)
_WHITESPACE_RE = re.compile(r"\s+")
_TRAILING_QUALIFIER_RE = re.compile(
    r"(?:\uff08[^\uff08\uff09]*\uff09|\([^()]*\)|\u3010[^\u3010\u3011]*\u3011)\s*$"
)


def _normalize_text(value: str | None) -> str:
    if not value:
        return ""
    s = unicodedata.normalize("NFKC", value)
    for ch in _INVISIBLE_CHARS:
        s = s.replace(ch, "")
    s = s.lower()
    s = _WHITESPACE_RE.sub("", s)
    s = s.translate(_DELETE_TABLE)
    return s


def _normalize_title(title: str | None) -> str:
    if not title:
        return ""
    s = unicodedata.normalize("NFKC", title)
    for ch in _INVISIBLE_CHARS:
        s = s.replace(ch, "")
    while _TRAILING_QUALIFIER_RE.search(s):
        s = _TRAILING_QUALIFIER_RE.sub("", s).rstrip()
    s = s.lower()
    s = _WHITESPACE_RE.sub("", s)
    s = s.translate(_DELETE_TABLE)
    return s


def _core_hash(responsibilities: str, requirements: str) -> str:
    r = _normalize_text(responsibilities)
    q = _normalize_text(requirements)
    raw = f"{r}\n---requirements---\n{q}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ============================================================================
# Packaging keys (mirrors: candidate_packager.py)
# ============================================================================

def _build_idempotency_key(
    company: str, title: str, location: str, apply_url: str, evidence_hash: str
) -> str:
    parts = (
        (company or "").strip().lower(),
        (title or "").strip().lower(),
        (location or "").strip().lower(),
        (apply_url or "").strip().lower(),
        (evidence_hash or "").strip().lower(),
    )
    raw = "::".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _build_similarity_group_key(
    company: str, title: str, recruitment_type: str
) -> str:
    cp = (company or "").strip().lower()[:3]
    tp = (title or "").strip().lower()[:3]
    rt = (recruitment_type or "unknown").strip().lower()
    return f"{cp}::{tp}::{rt}"


# ============================================================================
# Evidence quality checks (mirrors: evidence_verifier.py)
# ============================================================================

_MIN_DESCRIPTION_LENGTH = 50
_STALE_YEAR_THRESHOLD = 2024
_JD_KEYWORDS = [
    "\u5c97\u4f4d", "\u804c\u4f4d", "\u62db\u8058", "\u8981\u6c42", "\u804c\u8d23",
    "job", "position", "requirement", "responsibility", "qualification",
]


def _check_stale(desc: str) -> bool:
    years = re.findall(r"\b(20[0-9]{2})\b", desc or "")
    for y in years:
        try:
            if 2000 < int(y) < _STALE_YEAR_THRESHOLD:
                return True
        except ValueError:
            continue
    return False


def _check_vague(desc: str) -> bool:
    return not desc or len(desc.strip()) < _MIN_DESCRIPTION_LENGTH


def _check_non_jd(desc: str) -> bool:
    if not desc or not desc.strip():
        return True
    if len(desc) <= 100:
        return False
    dl = desc.lower()
    return sum(1 for kw in _JD_KEYWORDS if kw in dl) < 2


def _verify_candidate(c: dict[str, Any]) -> list[str]:
    """Return list of warning strings for a candidate (empty = clean)."""
    warnings: list[str] = []
    desc = c.get("description_text", "") or ""

    if not c.get("title") and not c.get("company_name"):
        warnings.append("MISSING_BOTH_TITLE_AND_COMPANY")
    if not c.get("evidence_refs"):
        warnings.append("MISSING_EVIDENCE_REFS")
    if _check_stale(desc):
        warnings.append(f"POSSIBLE_STALE: references year < {_STALE_YEAR_THRESHOLD}")
    if _check_vague(desc):
        warnings.append(f"VAGUE_DESCRIPTION: < {_MIN_DESCRIPTION_LENGTH} chars")
    if _check_non_jd(desc):
        warnings.append("NON_JD_TEXT: fewer than 2 JD keywords found")
    return warnings


# ============================================================================
# Semantic deduplication (mirrors: canonical_job_deduplicator.py)
# ============================================================================

def _has_jd_body(c: dict[str, Any]) -> bool:
    return bool(
        (c.get("responsibilities") or "").strip()
        or (c.get("requirements") or "").strip()
    )


def _loc_key(c: dict[str, Any]) -> str:
    locations = c.get("locations") or []
    norms: list[str] = []
    for loc in locations:
        raw = str(loc or "")
        for d in ("\u3001", "\uff0c", ",", "/", ";", "\uff1b"):
            raw = raw.replace(d, "\u3001")
        for part in raw.split("\u3001"):
            s = part.strip()
            if not s:
                continue
            for suf in ("\u81ea\u6cbb\u533a", "\u7701", "\u5e02"):
                if s.endswith(suf) and len(s) > len(suf):
                    s = s[:-len(suf)]
                    break
            norms.append(s)
    return "|".join(sorted(set(norms)))


def _identity_key(c: dict[str, Any]) -> tuple:
    if _has_jd_body(c):
        company = _normalize_text(c.get("company_name"))
        ch = _core_hash(
            c.get("responsibilities") or "", c.get("requirements") or ""
        )
        return ("jd", company, ch, _loc_key(c))
    return ("title", _normalize_title(c.get("title")))


def _cluster_by_title_substring(members: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    clusters: list[list] = []
    for m in members:
        t = _normalize_title(m.get("title"))
        placed = False
        for c in clusters:
            if any(t and ct and (t == ct or t in ct or ct in t) for ct in c[0]):
                c[0].add(t)
                c[1].append(m)
                placed = True
                break
        if not placed:
            clusters.append([{t}, [m]])
    return [c[1] for c in clusters]


def _dedupe_preserve(items: list | None) -> list:
    if not items:
        return []
    out: list = []
    seen: set = set()
    for item in items:
        if isinstance(item, dict):
            key = (item.get("url"), item.get("content_hash"), item.get("evidence_type"))
        else:
            key = ("", str(item))
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _merge(dst: dict[str, Any], src: dict[str, Any]) -> None:
    dst["locations"] = _dedupe_preserve(
        [*(dst.get("locations") or []), *(src.get("locations") or [])]
    )
    dst["evidence_refs"] = _dedupe_preserve(
        [*(dst.get("evidence_refs") or []), *(src.get("evidence_refs") or [])]
    )
    dst["recruitment_types"] = _dedupe_preserve(
        [*(dst.get("recruitment_types") or []), *(src.get("recruitment_types") or [])]
    )
    dst["industries"] = _dedupe_preserve(
        [*(dst.get("industries") or []), *(src.get("industries") or [])]
    )
    nw: list = []
    for w in [*(dst.get("normalization_warnings") or []), *(src.get("normalization_warnings") or [])]:
        if w not in nw:
            nw.append(w)
    dst["normalization_warnings"] = nw
    if not dst.get("apply_url") and src.get("apply_url"):
        dst["apply_url"] = src.get("apply_url")
    if not dst.get("company_name") and src.get("company_name"):
        dst["company_name"] = src.get("company_name")
    if not dst.get("description_text") and src.get("description_text"):
        dst["description_text"] = src.get("description_text")


# ============================================================================
# Main pipeline
# ============================================================================

def process(
    candidate_files: list[str],
    verify: bool = True,
) -> dict[str, Any]:
    """Load, normalize, deduplicate, package, verify — return merged result."""

    # --- Load all candidates ---
    raw_candidates: list[dict[str, Any]] = []
    load_errors: list[str] = []
    for fpath in candidate_files:
        try:
            data = json.loads(Path(fpath).read_text(encoding="utf-8"))
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        item["_source_file"] = fpath
                        raw_candidates.append(item)
            elif isinstance(data, dict):
                data["_source_file"] = fpath
                raw_candidates.append(data)
        except (json.JSONDecodeError, OSError) as e:
            load_errors.append(f"{fpath}: {e}")

    if not raw_candidates:
        return {
            "status": "empty",
            "candidates": [],
            "stats": {"input_count": 0, "output_count": 0, "duplicates_removed": 0},
            "load_errors": load_errors,
        }

    input_count = len(raw_candidates)

    # --- Pass 1: Deduplicate by canonical identity ---
    groups: dict[tuple, list[int]] = {}
    order: list[tuple] = []
    for i, c in enumerate(raw_candidates):
        key = _identity_key(c)
        if key not in groups:
            groups[key] = [i]
            order.append(key)
        else:
            groups[key].append(i)

    deduped: list[dict[str, Any]] = []
    for key in order:
        members = [copy.deepcopy(raw_candidates[i]) for i in groups[key]]
        if key[0] == "jd" and len(members) > 1:
            clusters = _cluster_by_title_substring(members)
        else:
            clusters = [members]
        for cluster in clusters:
            dst = cluster[0]
            for src in cluster[1:]:
                _merge(dst, src)
            deduped.append(dst)

    # --- Pass 2: Drop title-only echoes of full-JD candidates ---
    full_jd_titles: set[str] = set()
    for c in deduped:
        if _has_jd_body(c):
            t = _normalize_title(c.get("title"))
            if t:
                full_jd_titles.add(t)
    if full_jd_titles:
        kept: list[dict[str, Any]] = []
        for c in deduped:
            if not _has_jd_body(c):
                t = _normalize_title(c.get("title"))
                if t and t in full_jd_titles:
                    continue
            kept.append(c)
        deduped = kept

    duplicates_removed = input_count - len(deduped)

    # --- Pass 3: Add packaging keys ---
    for c in deduped:
        title = c.get("title") or ""
        company = c.get("company_name") or ""
        locations = c.get("locations") or []
        recruitment_types = c.get("recruitment_types") or []
        evidence_refs = c.get("evidence_refs") or []
        apply_url = c.get("apply_url") or ""

        evidence_hash = ""
        if evidence_refs:
            evidence_hash = evidence_refs[0].get("content_hash", "") if isinstance(evidence_refs[0], dict) else ""

        primary_location = locations[0] if locations else ""
        primary_rec_type = recruitment_types[0] if recruitment_types else ""

        c["idempotency_key"] = _build_idempotency_key(
            company, title, primary_location, apply_url, evidence_hash
        )
        c["similarity_group_key"] = _build_similarity_group_key(
            company, title, primary_rec_type
        )

    # --- Pass 4: Evidence quality checks ---
    verify_warnings: dict[str, list[str]] = {}
    if verify:
        for i, c in enumerate(deduped):
            title = c.get("title", f"candidate_{i}")
            warns = _verify_candidate(c)
            if warns:
                verify_warnings[title] = warns
                existing = c.get("normalization_warnings") or []
                for w in warns:
                    if w not in existing:
                        existing.append(w)
                c["normalization_warnings"] = existing

    # --- Cleanup internal field ---
    for c in deduped:
        c.pop("_source_file", None)

    return {
        "status": "ok",
        "candidates": deduped,
        "stats": {
            "input_count": input_count,
            "output_count": len(deduped),
            "duplicates_removed": duplicates_removed,
        },
        "verify_warnings": verify_warnings if verify else {},
        "load_errors": load_errors,
    }


# ============================================================================
# CLI
# ============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Normalize, deduplicate, package, and verify candidate JSONs"
    )
    parser.add_argument("files", nargs="+", help="One or more candidate JSON files")
    parser.add_argument("--out", default=None, help="Output file (merged JSON)")
    parser.add_argument("--no-verify", action="store_true",
                        help="Skip evidence quality checks")
    args = parser.parse_args()

    result = process(args.files, verify=not args.no_verify)

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(result["candidates"], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # Print summary to stdout
    print(json.dumps({
        "status": result["status"],
        "stats": result["stats"],
        "load_errors": result["load_errors"],
        "verify_warnings_count": len(result.get("verify_warnings", {})),
        "output_file": str(Path(args.out).resolve()) if args.out else None,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
