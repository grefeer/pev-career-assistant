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

def _build_job_identity_key(
    company: str, title: str, location: str, apply_url: str, recruitment_type: str
) -> str:
    """Stable business identity — does NOT include evidence hash.

    Survives content re-extraction (new content_hash → same identity).
    """
    parts = (
        (company or "").strip().lower(),
        (title or "").strip().lower(),
        (location or "").strip().lower(),
        (apply_url or "").strip().lower(),
        (recruitment_type or "").strip().lower(),
    )
    return hashlib.sha256("::".join(parts).encode("utf-8")).hexdigest()


def _build_idempotency_key(
    company: str, title: str, location: str, apply_url: str,
    recruitment_type: str, evidence_hash: str,
) -> str:
    """Identity + evidence hash — changes when page content changes.

    Use for database upsert version tracking. A new content_hash on the same
    job produces a NEW idempotency_key, creating a new version row.
    """
    parts = (
        (company or "").strip().lower(),
        (title or "").strip().lower(),
        (location or "").strip().lower(),
        (apply_url or "").strip().lower(),
        (recruitment_type or "").strip().lower(),
        (evidence_hash or "").strip().lower(),
    )
    return hashlib.sha256("::".join(parts).encode("utf-8")).hexdigest()


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
    """Build a canonical identity key that prevents cross-company collisions.

    For full-JD candidates: company + JD body hash + location.
    For title-only candidates: company + normalized title + location + recruitment_type.

    This ensures:
      - Same title, different companies → different identities
      - Same company+title, campus vs intern → different identities
      - Company A's full JD won't shadow-delete Company B's title-only entry
    """
    company = _normalize_text(c.get("company_name"))
    loc = _loc_key(c)
    rt_raw = (c.get("recruitment_types") or [None])[0]
    rt = _normalize_text(str(rt_raw)) if rt_raw else ""

    if _has_jd_body(c):
        ch = _core_hash(
            c.get("responsibilities") or "", c.get("requirements") or ""
        )
        return ("jd", company, ch, loc, rt)
    return ("title", company, _normalize_title(c.get("title")), loc, rt)


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
    """Merge src into dst.  Accumulates lists; prefers NEWER values for content fields.

    List fields (locations, evidence_refs, recruitment_types, industries):
      deduplicated union — both old and new values preserved.

    Scalar content fields (description_text, responsibilities, requirements):
      NEW values replace old — an updated JD should show current content.

    Identity fields (company_name, title, apply_url):
      dst values preserved; src fills gaps only.
    """
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

    # Identity/gap-fill fields (only fill if dst is missing)
    if not dst.get("apply_url") and src.get("apply_url"):
        dst["apply_url"] = src.get("apply_url")
    if not dst.get("company_name") and src.get("company_name"):
        dst["company_name"] = src.get("company_name")

    # Content fields: prefer NEWER (src) values — an updated JD should show
    # current content, not stale first-seen content.
    if src.get("description_text"):
        dst["description_text"] = src.get("description_text")
    if src.get("responsibilities"):
        dst["responsibilities"] = src.get("responsibilities")
    if src.get("requirements"):
        dst["requirements"] = src.get("requirements")
    if src.get("department"):
        dst["department"] = src.get("department")
    if src.get("deadline_text"):
        dst["deadline_text"] = src.get("deadline_text")


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

        # Stable business identity (no evidence hash)
        c["job_identity_key"] = _build_job_identity_key(
            company, title, primary_location, apply_url, primary_rec_type
        )
        # Version-tracking key (identity + evidence hash)
        c["idempotency_key"] = _build_idempotency_key(
            company, title, primary_location, apply_url,
            primary_rec_type, evidence_hash,
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

def _expand_files(file_args: list[str]) -> list[str]:
    """Expand shell-style globs in file args (run_skill_script uses no shell, so
    ``output/candidates/*.json`` would otherwise be a literal nonexistent path).

    Args containing ``*``/``?``/``[`` are globbed relative to the cwd (the skill
    dir); literal args are kept as-is. Result is de-duplicated and sorted. If a
    glob matches nothing it is silently dropped (the caller's ``process`` reports
    an empty status if NO files resolve at all).
    """
    import glob

    expanded: list[str] = []
    seen: set[str] = set()
    for a in file_args:
        if any(ch in a for ch in "*?["):
            matches = sorted(glob.glob(a, recursive=True))
            for m in matches:
                if m not in seen:
                    seen.add(m)
                    expanded.append(m)
        else:
            if a not in seen:
                seen.add(a)
                expanded.append(a)
    return expanded


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Normalize, deduplicate, package, and verify candidate JSONs"
    )
    parser.add_argument("files", nargs="+", help="One or more candidate JSON files "
                        "(shell globs like output/candidates/*.json are expanded)")
    parser.add_argument("--out", default=None, help="Output file (merged JSON)")
    parser.add_argument("--no-verify", action="store_true",
                        help="Skip evidence quality checks")
    args = parser.parse_args()

    files = _expand_files(args.files)
    if not files:
        # Nothing to merge - emit an empty-but-valid result so the caller (and the
        # harness reading candidates_merged.json) gets a clean empty array, not a
        # missing file.
        if args.out:
            out_path = Path(args.out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text("[]", encoding="utf-8")
        print(json.dumps({
            "status": "empty",
            "stats": {"input_count": 0, "output_count": 0, "duplicates_removed": 0},
            "load_errors": [],
            "verify_warnings_count": 0,
            "output_file": str(Path(args.out).resolve()) if args.out else None,
        }, ensure_ascii=False))
        return

    result = process(files, verify=not args.no_verify)

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
