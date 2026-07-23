"""Canonical-job deduplication for the discovery supervisor path.

Collapses duplicate candidates that arise when the same job is captured
across overlapping evidence pages. The baseline ``extract_rendered_job_evidence``
call and the Web Navigation Agent's re-capture frequently produce
near-identical rendered text with *different* content hashes (lazy-load
timing differences), so the same job titles get extracted twice and surface
as duplicate candidates. This module removes those duplicates by canonical
identity **after** the frozen ``verify_evidence`` step and **before**
packaging, so the supervisor's returned candidates are already deduped.

D3 (scoped plan): exact-identity merge only - no fuzzy/Jaccard auto-merge.

  * **Full-JD candidate** (has ``responsibilities`` or ``requirements``):
    identity = ``(normalized_company, core_hash(responsibilities, requirements))``.
    Location / job-code / posting-time are excluded, so the same JD advertised
    in two cities merges.
  * **Title-only candidate** (no JD body - the common case for career-site
    list pages whose detail bodies are gated): identity =
    ``(normalized_company, normalized_title)``. There is no JD body to hash,
    so the title is the only available identity signal.

Merge accumulates ``locations``, ``evidence_refs``, ``recruitment_types``,
``industries``, ``normalization_warnings`` (union, order-preserving, deduped)
and keeps the first non-empty ``apply_url``. First-seen candidate wins its
title / company / description fields.
"""

from __future__ import annotations

import copy

from backend.app.services.job_discovery.normalization.jd_normalizer import (
    core_hash,
    normalize_company,
    normalize_title,
)
from backend.app.services.job_discovery.schemas import NormalizedJobCandidate


def _has_jd_body(candidate: NormalizedJobCandidate) -> bool:
    return bool((getattr(candidate, "responsibilities", "") or "").strip()
                or (getattr(candidate, "requirements", "") or "").strip())


def _identity_key(candidate: NormalizedJobCandidate) -> tuple[str, ...]:
    """Canonical identity: tag + body-or-title hash.

    Full-JD candidates (those with a responsibilities/requirements body) are
    identified by ``("jd", normalized_company, core_hash)`` - company is kept
    because two different companies can post identical JD text.

    Title-only candidates (no JD body) are identified by ``("title",
    normalize_title(title))`` - company is deliberately EXCLUDED. Within a
    single discovery run (one URL) every candidate belongs to the same
    company, but the ``company_name`` field is attributed inconsistently across
    capture paths (the deterministic baseline often leaves it None while the
    Web Navigation Agent's re-capture may populate it). Splitting the key on
    company would therefore record the same posting twice (``None`` vs
    ``"Company"``) instead of merging it. Excluding company collapses those
    duplicates; it is safe because ``deduplicate_candidates`` is always called
    per-URL (one company per call).
    """
    if _has_jd_body(candidate):
        company = normalize_company(candidate.company_name)
        return ("jd", company,
                core_hash(candidate.responsibilities, candidate.requirements))
    return ("title", normalize_title(candidate.title))


def _dedupe_preserve(items: list | None) -> list:
    """Order-preserving dedupe of a list of hashable-ish items."""
    if not items:
        return []
    out: list = []
    seen: set = set()
    for item in items:
        # dicts (evidence_refs) are unhashable; key by a stable tuple.
        if isinstance(item, dict):
            key = (item.get("url"), item.get("content_hash"),
                   item.get("evidence_type"))
        else:
            key = ("", str(item))
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _cluster_by_title_substring(
    members: list[NormalizedJobCandidate],
) -> list[list[NormalizedJobCandidate]]:
    """Partition same-``core_hash`` full-JD candidates into title clusters.

    Two postings are placed in the same cluster iff one's normalized title is a
    substring of the other's (or they are equal). This collapses city / level
    variants of the same role (``算法工程师`` vs ``算法工程师-北京`` -> one job)
    while keeping genuinely different roles separate even when their JD bodies
    are identical copy-paste templates (``算法工程师`` vs ``算法研究员`` -> two
    jobs). Without this, a shared JD template would merge distinct postings and
    under-count. Returns a list of member-lists (one per cluster).
    """
    clusters: list[list] = []  # each entry: [set_of_titles, [members]]
    for m in members:
        t = normalize_title(getattr(m, "title", "") or "")
        placed = False
        for c in clusters:
            if any(
                t and ct and (t == ct or t in ct or ct in t)
                for ct in c[0]
            ):
                c[0].add(t)
                c[1].append(m)
                placed = True
                break
        if not placed:
            clusters.append([{t}, [m]])
    return [c[1] for c in clusters]


def _merge(dst: NormalizedJobCandidate, src: NormalizedJobCandidate) -> None:
    """Fold ``src`` into ``dst`` in place (union list fields, keep first title)."""
    dst.locations = _dedupe_preserve(
        [*(dst.locations or []), *(src.locations or [])])
    dst.evidence_refs = _dedupe_preserve(
        [*(dst.evidence_refs or []), *(src.evidence_refs or [])])
    dst.recruitment_types = _dedupe_preserve(
        [*(dst.recruitment_types or []), *(src.recruitment_types or [])])
    dst.industries = _dedupe_preserve(
        [*(dst.industries or []), *(src.industries or [])])
    dst.normalization_warnings = _dedupe_preserve(
        [*(dst.normalization_warnings or []), *(src.normalization_warnings or [])])
    if not (dst.apply_url or "") and (src.apply_url or ""):
        dst.apply_url = src.apply_url
    if not (dst.description_text or "") and (src.description_text or ""):
        dst.description_text = src.description_text
    # Company is excluded from the title-only identity key, so duplicates may
    # carry different company attributions (None vs the real name). Keep the
    # first non-empty one so the surviving candidate retains the real company.
    if not (dst.company_name or "") and (src.company_name or ""):
        dst.company_name = src.company_name


def deduplicate_candidates(
    candidates: list[NormalizedJobCandidate],
) -> list[NormalizedJobCandidate]:
    """Collapse duplicate candidates by canonical identity (D3 exact merge).

    Returns a new list (inputs untouched) preserving first-seen order.

    Within a full-JD identity group (same company + ``core_hash``) candidates
    are further partitioned by title-similarity via
    :func:`_cluster_by_title_substring`. This keeps a shared JD template from
    merging genuinely distinct roles (``算法工程师`` vs ``算法研究员``) while
    still collapsing city / level variants whose titles differ only by a suffix
    (``算法工程师`` vs ``算法工程师-北京``). Title-only groups already share an
    identical normalized title, so no further partitioning is applied there.
    """
    if not candidates:
        return []
    # Pass 1: bucket candidates by canonical identity, preserving first-seen order.
    groups: dict[tuple, list[int]] = {}
    order: list[tuple] = []
    for i, candidate in enumerate(candidates):
        key = _identity_key(candidate)
        bucket = groups.get(key)
        if bucket is None:
            groups[key] = [i]
            order.append(key)
        else:
            bucket.append(i)

    # Pass 2: collapse each group into one or more merged candidates.
    out: list[NormalizedJobCandidate] = []
    for key in order:
        members = [candidates[i] for i in groups[key]]
        if key[0] == "jd" and len(members) > 1:
            clusters = _cluster_by_title_substring(members)
        else:
            clusters = [members]
        for cluster in clusters:
            dst = copy.deepcopy(cluster[0])
            for src in cluster[1:]:
                _merge(dst, src)
            out.append(dst)
    return out
