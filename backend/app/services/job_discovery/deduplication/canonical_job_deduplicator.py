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
    identity = ``(normalized_company, core_hash(responsibilities, requirements), loc_key)``.
    Location is INCLUDED so the same JD advertised in two cities stays as two
    distinct postings (a city variant) - the site counts each (role, city)
    listing separately. Job-code / posting-time remain excluded.
  * **Title-only candidate** (no JD body - the common case for career-site
    list pages whose detail bodies are gated): identity =
    ``(normalized_company, normalized_title)``. There is no JD body to hash, so
    the title is the only available identity signal; location is excluded so a
    position advertised in several cities counts as one (the site counts
    positions, not per-city listings).

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


def _loc_key(candidate: NormalizedJobCandidate) -> str:
    """Stable location signature for the identity key.

    A role advertised in two different cities is two DISTINCT postings (a city
    variant), not a duplicate - the site counts each (role, city) listing
    separately. Two re-captures of the *same* listing share the same city, so
    they get the same key and merge. Including the location in the identity is
    what stops the body-hash from collapsing city variants into one.

    Empty / missing locations collapse to ``""`` (candidates with no extractable
    city still merge by body alone, preserving prior behavior for sites that do
    not expose city in their XHR).
    """
    locations = getattr(candidate, "locations", None) or []
    norms: list[str] = []
    for loc in locations:
        # A single location string may join multiple cities (e.g. "上海、深圳"
        # from rendered list text vs ["上海","深圳"] from an XHR list). Split on
        # common delimiters so both capture forms produce the same key and merge,
        # instead of surviving as distinct (un-mergeable) loc_key values that
        # surface as duplicate candidates (e.g. feishu multi-city postings).
        _raw = str(loc or "")
        for _d in ("、", "，", ",", "/", ";", "；"):
            _raw = _raw.replace(_d, "、")
        for part in _raw.split("、"):
            s = part.strip()
            if not s:
                continue
            for suf in ("自治区", "省", "市"):
                if s.endswith(suf) and len(s) > len(suf):
                    s = s[: -len(suf)]
                    break
            norms.append(s)
    return "|".join(sorted(set(norms)))


def _identity_key(candidate: NormalizedJobCandidate) -> tuple[str, ...]:
    """Canonical identity: tag + body-or-title hash (+ location for full-JD).

    Full-JD candidates (those with a responsibilities/requirements body) are
    identified by ``("jd", normalized_company, core_hash, loc_key)`` - company
    is kept because two different companies can post identical JD text, and
    location is kept because each (role, city) is a DISTINCT posting with its
    own JD (the site counts each city variant as a separate listing, e.g.
    Mioffice/xiaomi).

    Title-only candidates (no JD body) are identified by ``("title",
    normalize_title(title))`` - company AND location are deliberately EXCLUDED.
    Within a single discovery run (one URL) every candidate belongs to the same
    company, but the ``company_name`` field is attributed inconsistently across
    capture paths (the deterministic baseline often leaves it None while the
    Web Navigation Agent's re-capture may populate it). Splitting the key on
    company would therefore record the same posting twice (``None`` vs
    ``"Company"``) instead of merging it. Excluding company collapses those
    duplicates; it is safe because ``deduplicate_candidates`` is always called
    per-URL (one company per call). Location is also excluded for title-only
    candidates because a list-page position advertised in several cities is one
    POSITION the site counts once (city is an attribute, not a separate
    listing) - merging city variants here matches how title-only career sites
    (e.g. PDD) count positions, in contrast to full-JD sites that count each
    (role, city) listing.
    """
    if _has_jd_body(candidate):
        company = normalize_company(candidate.company_name)
        return ("jd", company,
                core_hash(candidate.responsibilities, candidate.requirements),
                _loc_key(candidate))
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
    """Partition same-identity full-JD candidates into title clusters.

    Members here already share ``company + core_hash + loc_key`` (i.e. they are
    re-captures of the same listing in the same city). Two such captures are
    placed in the same cluster iff one's normalized title is a substring of the
    other's (or they are equal). This collapses level / suffix variants of the
    same role that arise from slightly-different captures (``算法工程师`` vs
    ``算法工程师-应届`` -> one job) while keeping genuinely different roles
    separate even when their JD bodies are identical copy-paste templates
    (``算法工程师`` vs ``算法研究员`` -> two jobs). Without this, a shared JD
    template would merge distinct postings and under-count. Returns a list of
    member-lists (one per cluster).
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


def _drop_title_only_echoes(
    candidates: list[NormalizedJobCandidate],
) -> list[NormalizedJobCandidate]:
    """Drop title-only candidates that echo a kept full-JD candidate's title.

    A title-only candidate (no JD body) whose normalized title matches a
    full-JD candidate's title is a list-page echo of an already-captured real
    posting - e.g. mokahr's ``#/home`` list-page titles re-extracted as
    title-only while the ``#/job/<uuid>`` full JDs (with body + per-job URL)
    are captured via XHR. It has no body and typically a list-page URL, so it
    is not a usable posting once the full JD exists; keeping it both inflates
    the count and leaves a same-titled pair that the eval flags as a duplicate.

    Returns a new list (inputs untouched) preserving first-seen order. A no-op
    when no full-JD candidate shares a title with a title-only one - so sites
    whose candidates are uniformly title-only (e.g. pdd) are unaffected.
    """
    if not candidates:
        return []
    full_jd_titles: set[str] = set()
    for c in candidates:
        if _has_jd_body(c):
            t = normalize_title(getattr(c, "title", "") or "")
            if t:
                full_jd_titles.add(t)
    if not full_jd_titles:
        return list(candidates)
    out: list[NormalizedJobCandidate] = []
    for c in candidates:
        if not _has_jd_body(c):
            t = normalize_title(getattr(c, "title", "") or "")
            if t and t in full_jd_titles:
                continue
        out.append(c)
    return out


def deduplicate_candidates(
    candidates: list[NormalizedJobCandidate],
) -> list[NormalizedJobCandidate]:
    """Collapse duplicate candidates by canonical identity (D3 exact merge).

    Returns a new list (inputs untouched) preserving first-seen order.

    Within a full-JD identity group (same company + ``core_hash`` + ``loc_key``)
    candidates are further partitioned by title-similarity via
    :func:`_cluster_by_title_substring`. This keeps a shared JD template from
    merging genuinely distinct roles (``算法工程师`` vs ``算法研究员``) while
    still collapsing level / suffix variants whose titles differ only by a
    suffix (``算法工程师`` vs ``算法工程师-应届``). City variants (same role,
    different city) are kept separate because ``loc_key`` differs. Title-only
    groups already share an identical normalized title, so no further
    partitioning is applied there.
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
    # Drop title-only list-page echoes of full-JD candidates (see
    # ``_drop_title_only_echoes``). Run AFTER identity clustering so it sees
    # the merged survivors, and so it is applied exactly once per run.
    return _drop_title_only_echoes(out)
