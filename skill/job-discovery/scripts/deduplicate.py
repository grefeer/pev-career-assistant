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

# ============================================================================
# Garbage / placeholder filtering (deterministic, prompt-independent)
# ============================================================================
# v1.5: the LLM occasionally emits placeholder/test candidates (title like
# `test`, `job1`, `placeholder`, `\u6d4b\u8bd5`, a bare category label `\u7b97\u6cd5\u7ec4`, etc.)
# despite prompt bans. These are NEVER real JDs - drop them deterministically
# so quality does not depend on the model obeying instructions. This is a
# GENERAL rule (same regex for every site), not a per-URL adapter.
#
# Anchored exact-match after normalization: a real title like "Test Engineer"
# or "QA Tester" is NOT matched because the normalized form ("testengineer" /
# "qatester") is not exactly "test". Only bare placeholder tokens are dropped.
_GARBAGE_TITLE_RE = re.compile(
    r"^(?:test\d*|test_clear|job\d*|placeholder|demo\d*|sample\d*|example\d*|"
    r"tmp\d*|foo\d*|bar\d*|"
    r"\u6d4b\u8bd5\d*|\u5360\u4f4d\u7b26|\u793a\u4f8b|"
    r"\u7b97\u6cd5\u7ec4|\u7b97\u6cd5\u7ec4\u7ec4\u957f|\u5f00\u53d1\u7ec4|"
    # bare single CJK category labels with no role
    r"\u5b9e\u4e60\u5c97|\u6821\u62db\u5c97)$"
)
# Placeholder BODY tokens: a candidate whose every text field (desc/resp/req)
# is empty or one of these tokens has no real JD content - drop it. Catches
# candidates whose title looked real but whose body is bare "test" data.
_GARBAGE_BODY_RE = re.compile(
    r"^(?:test\d*|test_clear|placeholder|demo\d*|sample\d*|tmp\d*|"
    r"\u6d4b\u8bd5|\u5360\u4f4d\u7b26|tbd|todo|n/?a|none|null|\.\.\.)$"
)

# Romanization leak detector. v1.5: a pure-ASCII title with no recognized
# English job word is almost always pinyin the model emitted instead of the
# original Chinese title (e.g. "dingjian" for \u9f0e\u5c16, "Guanggao Suanfa
# Gongchengshi" for \u5e7f\u544a\u7b97\u6cd5\u5de5\u7a0b\u5e08). These are REAL jobs with
# mistranslated titles - we FLAG them (not drop, to preserve completeness) so
# they can be re-extracted or reviewed. Recognized English job words exempt a
# title (e.g. "AI Engineer", "Python Developer" are legitimate).
_ASCII_TITLE_RE = re.compile(r"^[A-Za-z0-9\s\-\.\,\(\)/&+]+$")
# After lowercasing, pinyin is one or more bare alphabetic words separated by
# whitespace/hyphens (no digits, no other punctuation). Matches "dingjian",
# "duan", "guanggao suanfa gongchengshi - hulianwang".
_PINYIN_HINT_RE = re.compile(r"^[a-z]+(?:[\s\-]+[a-z]+)*$")
_ENGLISH_JOB_WORDS = {
    "engineer", "developer", "manager", "intern", "analyst", "architect",
    "scientist", "specialist", "consultant", "lead", "senior", "junior",
    "director", "officer", "designer", "product", "program", "software",
    "data", "ai", "ml", "qa", "test", "fullstack", "frontend", "backend",
    "platform", "solution", "solutions", "security", "cloud", "devops", "sre",
    "ios", "android", "web", "ui", "ux", "marketing", "operation",
    "operations", "finance", "hr", "legal", "support", "researcher",
    "trader", "accountant", "auditor", "engineer", "head", "vp", "ceo",
    "cto", "coo", "cfo", "partner", "associate", "assistant", "secretary",
}
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _is_garbage_title(title: str | None) -> bool:
    if not title:
        return False
    return bool(_GARBAGE_TITLE_RE.match(_normalize_title(title)))


def _is_garbage_body(c: dict[str, Any]) -> bool:
    """True if the candidate has SOME body text but ALL of it is placeholder.

    All-empty bodies are NOT garbage here (they may be legitimate title-only
    listings on some sites; Pass 2 / verify handle them as warnings). We only
    drop when the model emitted explicit placeholder tokens ("test"/"tbd"/etc.)
    - a real JD never has every field be a bare placeholder.
    """
    fields = [
        str(c.get("description_text") or "").strip(),
        str(c.get("responsibilities") or "").strip(),
        str(c.get("requirements") or "").strip(),
    ]
    non_empty = [f for f in fields if f]
    if not non_empty:
        return False
    return all(_GARBAGE_BODY_RE.match(f.lower()) for f in non_empty)


def _romanization_warning(c: dict[str, Any]) -> str | None:
    """Return a warning string if the title looks like pinyin romanization."""
    title = c.get("title") or ""
    if not title or not _ASCII_TITLE_RE.match(title):
        return None
    # Exempt titles containing a recognized English job word (any case).
    words = set(re.findall(r"[A-Za-z]+", title.lower()))
    if words & _ENGLISH_JOB_WORDS:
        return None
    # Flag any pinyin-shaped title (lowercased: bare alphabetic words). A real
    # English title almost always contains a job word (exempted above); a bare
    # pinyin string like "dingjian"/"guanggao suanfa gongchengshi" is a
    # romanization leak. WARNING only - does not drop (preserves completeness).
    if _PINYIN_HINT_RE.match(title.strip().lower()):
        body_has_cjk = bool(_CJK_RE.search(" ".join(
            str(c.get(k) or "") for k in ("description_text", "responsibilities", "requirements")
        )))
        kind = "JD body is CJK" if body_has_cjk else "JD body is English-translated"
        return (
            "ROMANIZED_TITLE_SUSPECTED: title is ASCII pinyin but " + kind
            + "; re-extract in original language"
        )
    return None


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


def _clear_shared_listing_apply_urls(candidates: list[dict[str, Any]]) -> int:
    """Remove a shared list-page URL from otherwise distinct job records.

    Some sites expose only one recruiting-list URL and an extractor copies that
    URL into every row. It is not a concrete job application route, so using it
    as identity collapses all positions and sends users to a misleading page.
    A URL shared by two or more distinct normalized titles is treated as such;
    the title/department/location identity remains available downstream.
    """
    titles_by_url: dict[str, set[str]] = {}
    for candidate in candidates:
        url = str(candidate.get("apply_url") or "").strip()
        title = _normalize_title(candidate.get("title"))
        if url and title:
            titles_by_url.setdefault(url, set()).add(title)
    shared_urls = {url for url, titles in titles_by_url.items() if len(titles) > 1}
    cleared = 0
    for candidate in candidates:
        url = str(candidate.get("apply_url") or "").strip()
        if url not in shared_urls:
            continue
        candidate["apply_url"] = ""
        warnings = candidate.setdefault("normalization_warnings", [])
        if isinstance(warnings, list) and "SHARED_LISTING_URL_CLEARED" not in warnings:
            warnings.append("SHARED_LISTING_URL_CLEARED")
        cleared += 1
    return cleared


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
            # A shared JD template is common in campus recruiting.  It is only
            # safe to merge title variants when their concrete apply routes do
            # not contradict each other; otherwise distinct openings with the
            # same template disappear during deduplication.
            cluster_urls = {
                str(item.get("apply_url") or "").strip()
                for item in c[1] if str(item.get("apply_url") or "").strip()
            }
            candidate_url = str(m.get("apply_url") or "").strip()
            routes_compatible = not cluster_urls or not candidate_url or candidate_url in cluster_urls
            if routes_compatible and any(t and ct and (t == ct or t in ct or ct in t) for ct in c[0]):
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
    keep_garbage: bool = False,
) -> dict[str, Any]:
    """Load, normalize, deduplicate, package, verify - return merged result."""

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
            "stats": {
                "input_count": 0,
                "garbage_dropped": 0,
                "garbage_titles": [],
                "output_count": 0,
                "duplicates_removed": 0,
            },
            "load_errors": load_errors,
        }

    input_count = len(raw_candidates)

    # --- Pass 0: Drop garbage/placeholder candidates (deterministic) ---
    # v1.5: removes (a) test/job1/placeholder/测试/算法组-style TITLES and
    # (b) candidates whose body is all placeholder tokens ("test"/"tbd"/etc.)
    # - both are NEVER real JDs. Runs BEFORE identity-dedup so garbage cannot
    # pollute identity keys or merge into real candidates. GENERAL rule (same
    # regexes for every site), not a per-URL adapter.
    garbage_dropped: list[str] = []
    if not keep_garbage:
        filtered: list[dict[str, Any]] = []
        for c in raw_candidates:
            if _is_garbage_title(c.get("title")) or _is_garbage_body(c):
                garbage_dropped.append(
                    f"{c.get('title')!r}"
                    + (" (body=placeholder)" if not _is_garbage_title(c.get("title")) else "")
                )
                continue
            filtered.append(c)
        raw_candidates = filtered

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

    shared_listing_urls_cleared = _clear_shared_listing_apply_urls(deduped)

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
            # v1.5: romanization flag (does not drop - preserves completeness)
            rw = _romanization_warning(c)
            if rw:
                warns.append(rw)
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
            "garbage_dropped": len(garbage_dropped),
            "garbage_titles": garbage_dropped,
            "output_count": len(deduped),
            "duplicates_removed": duplicates_removed,
            "shared_listing_urls_cleared": shared_listing_urls_cleared,
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

    output_root = Path("output").resolve()

    def is_output_json(value: str) -> bool:
        candidate = Path(value)
        if candidate.suffix.lower() != ".json":
            return False
        try:
            candidate.resolve().relative_to(output_root)
        except ValueError:
            return False
        return True

    expanded: list[str] = []
    seen: set[str] = set()
    for a in file_args:
        if any(ch in a for ch in "*?["):
            matches = sorted(glob.glob(a, recursive=True))
            for m in matches:
                if not is_output_json(m):
                    continue
                if m not in seen:
                    seen.add(m)
                    expanded.append(m)
        else:
            if is_output_json(a) and a not in seen:
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
    parser.add_argument("--keep-garbage", action="store_true",
                        help="Do NOT drop placeholder/test titles (default: drop them)")
    args = parser.parse_args()

    output_path = Path(args.out) if args.out else None
    if output_path is not None:
        try:
            output_path.resolve().relative_to(Path("output").resolve())
        except ValueError:
            parser.error("--out must stay under the skill output directory")

    files = _expand_files(args.files)
    if not files:
        # Nothing to merge - emit an empty-but-valid result so the caller (and the
        # harness reading candidates_merged.json) gets a clean empty array, not a
        # missing file.
        if output_path is not None:
            out_path = output_path
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text("[]", encoding="utf-8")
        print(json.dumps({
            "status": "empty",
            "stats": {"input_count": 0, "output_count": 0, "duplicates_removed": 0},
            "load_errors": [],
            "verify_warnings_count": 0,
            "output_file": str(output_path.resolve()) if output_path else None,
        }, ensure_ascii=False))
        return

    result = process(files, verify=not args.no_verify, keep_garbage=args.keep_garbage)

    if output_path is not None:
        out_path = output_path
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
        "output_file": str(output_path.resolve()) if output_path else None,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
