"""Unit tests for canonical-job deduplication (Phase 4, D3 exact merge)."""

from __future__ import annotations

from backend.app.services.job_discovery.deduplication.canonical_job_deduplicator import (
    canonical_job_key,
    deduplicate_candidates,
)
from backend.app.services.job_discovery.schemas import NormalizedJobCandidate


def _title_only(title: str, company: str | None = None, locations=None,
                evidence_refs=None, apply_url: str | None = None) -> NormalizedJobCandidate:
    return NormalizedJobCandidate(
        title=title,
        company_name=company,
        locations=list(locations or []),
        apply_url=apply_url,
        evidence_refs=list(evidence_refs or []),
    )


def _full_jd(title: str, company: str, responsibilities: str,
             requirements: str, location: str | None = None) -> NormalizedJobCandidate:
    return NormalizedJobCandidate(
        title=title,
        company_name=company,
        responsibilities=responsibilities,
        requirements=requirements,
        locations=[location] if location else [],
        apply_url="https://example.com/detail/1",
    )


class TestTitleOnlyDedup:
    def test_same_company_title_same_location_collapses(self) -> None:
        a = _title_only("算法工程师", "元戎启行", locations=["深圳"],
                        evidence_refs=[{"url": "u1", "content_hash": "h1"}])
        b = _title_only("算法工程师", "元戎启行", locations=["深圳"],
                        evidence_refs=[{"url": "u2", "content_hash": "h2"}])
        out = deduplicate_candidates([a, b])
        assert len(out) == 1
        assert set(out[0].locations) == {"深圳"}
        assert len(out[0].evidence_refs) == 2

    def test_same_company_title_different_location_merges(self) -> None:
        # Title-only: a position advertised in several cities is ONE position
        # the site counts once -> merged by title, locations unioned.
        a = _title_only("算法工程师", "元戎启行", locations=["深圳"],
                        evidence_refs=[{"url": "u1", "content_hash": "h1"}])
        b = _title_only("算法工程师", "元戎启行", locations=["北京"],
                        evidence_refs=[{"url": "u2", "content_hash": "h2"}])
        out = deduplicate_candidates([a, b])
        assert len(out) == 1
        assert set(out[0].locations) == {"深圳", "北京"}
        assert len(out[0].evidence_refs) == 2

    def test_different_title_kept(self) -> None:
        out = deduplicate_candidates([
            _title_only("算法工程师", "元戎启行"),
            _title_only("系统工程师", "元戎启行"),
        ])
        assert len(out) == 2

    def test_evidence_refs_deduped_on_merge(self) -> None:
        ref = {"url": "u1", "content_hash": "h1", "evidence_type": "page_text"}
        a = _title_only("算法工程师", "元戎启行", evidence_refs=[ref])
        b = _title_only("算法工程师", "元戎启行", evidence_refs=[ref])
        out = deduplicate_candidates([a, b])
        assert len(out) == 1
        assert len(out[0].evidence_refs) == 1

    def test_company_none_normalizes_consistently(self) -> None:
        # Both None company + same title + no location -> one candidate.
        out = deduplicate_candidates([
            _title_only("算法工程师", None),
            _title_only("算法工程师", None),
        ])
        assert len(out) == 1


class TestFullJdDedup:
    def test_same_jd_same_location_merges(self) -> None:
        # True duplicate capture (same body, same city) -> merge.
        a = _full_jd("算法工程师", "小米", "负责A", "要求B", location="北京")
        b = _full_jd("算法工程师", "小米", "负责A", "要求B", location="北京")
        out = deduplicate_candidates([a, b])
        assert len(out) == 1

    def test_same_jd_different_location_kept_separate(self) -> None:
        # City variants (same role, same JD body, different city) are DISTINCT
        # listings the site counts separately -> kept separate, NOT merged.
        a = _full_jd("算法工程师", "小米", "负责A", "要求B", location="北京")
        b = _full_jd("算法工程师", "小米", "负责A", "要求B", location="上海")
        out = deduplicate_candidates([a, b])
        assert len(out) == 2
        locs = {out[0].locations[0], out[1].locations[0]}
        assert locs == {"北京", "上海"}

    def test_different_jd_kept(self) -> None:
        out = deduplicate_candidates([
            _full_jd("算法工程师", "小米", "负责A", "要求B"),
            _full_jd("系统工程师", "小米", "负责C", "要求D"),
        ])
        assert len(out) == 2

    def test_title_only_echo_of_full_jd_dropped(self) -> None:
        # A title-only candidate whose title matches a full-JD candidate's
        # title is a list-page echo of the same posting (the list-page title
        # re-captured while the detail-page full JD already exists). It has no
        # body and is not actionable, so it is dropped to avoid double-counting
        # (e.g. mokahr "#/home" list-page titles echoing "#/job/<uuid>" full
        # JDs). The full-JD candidate survives.
        t = _title_only("算法工程师", "小米")
        f = _full_jd("算法工程师", "小米", "负责A", "要求B")
        out = deduplicate_candidates([t, f])
        assert len(out) == 1
        assert out[0].responsibilities == "负责A"  # the full-JD candidate won

    def test_title_only_kept_when_no_full_jd_same_title(self) -> None:
        # No-op guard: uniformly title-only runs (e.g. pdd) are unaffected -
        # a title-only candidate is only dropped when a full-JD candidate of
        # the SAME title exists.
        out = deduplicate_candidates([
            _title_only("算法工程师", "拼多多"),
            _title_only("系统工程师", "拼多多"),
        ])
        assert len(out) == 2


class TestFullJdTitleSubstringClustering:
    """Within a same-identity group, only titles with a substring relation
    merge; genuinely different roles sharing a JD template stay separate.

    This prevents a copy-paste JD template (identical responsibilities +
    requirements) from collapsing distinct postings such as ``算法工程师`` vs
    ``算法研究员`` (engineer vs researcher) while still merging level / suffix
    variants whose titles differ only by a suffix (``算法工程师`` vs
    ``算法工程师-应届``). City variants are already kept separate by
    ``loc_key``, so they never reach this clustering.
    """

    def test_same_jd_different_role_not_merged(self) -> None:
        # 工程师 (engineer) vs 研究员 (researcher) - neither title is a
        # substring of the other, so two distinct postings survive even though
        # their JD body is an identical template.
        out = deduplicate_candidates([
            _full_jd("感知大模型算法工程师", "小米", "负责A", "要求B"),
            _full_jd("感知大模型算法研究员", "小米", "负责A", "要求B"),
        ])
        assert len(out) == 2

    def test_same_jd_duplicate_capture_collapses(self) -> None:
        # Same title captured twice (e.g. across overlapping pages) -> one job.
        out = deduplicate_candidates([
            _full_jd("端侧大模型算法工程师", "小米", "负责A", "要求B"),
            _full_jd("端侧大模型算法工程师", "小米", "负责A", "要求B"),
        ])
        assert len(out) == 1

    def test_same_jd_suffix_variant_same_city_collapses(self) -> None:
        # Same city, title differs only by a level suffix -> one cluster, merged.
        out = deduplicate_candidates([
            _full_jd("算法工程师", "小米", "负责A", "要求B", location="北京"),
            _full_jd("算法工程师-应届", "小米", "负责A", "要求B", location="北京"),
        ])
        assert len(out) == 1
        assert "北京" in out[0].locations

    def test_same_jd_two_distinct_city_variants_kept_separate(self) -> None:
        # Distinct cities -> distinct loc_key -> 3 separate listings.
        out = deduplicate_candidates([
            _full_jd("算法工程师", "小米", "负责A", "要求B", location="深圳"),
            _full_jd("算法工程师", "小米", "负责A", "要求B", location="北京"),
            _full_jd("算法工程师", "小米", "负责A", "要求B", location="上海"),
        ])
        assert len(out) == 3
        locs = {c.locations[0] for c in out}
        assert locs == {"深圳", "北京", "上海"}

    def test_same_jd_mixed_roles_and_dupes(self) -> None:
        # 2 distinct roles x2 duplicate captures each (same city) -> 2 jobs.
        out = deduplicate_candidates([
            _full_jd("决策规划大模型算法工程师", "小米", "负责A", "要求B"),
            _full_jd("决策规划大模型算法研究员", "小米", "负责A", "要求B"),
            _full_jd("决策规划大模型算法工程师", "小米", "负责A", "要求B"),
            _full_jd("决策规划大模型算法研究员", "小米", "负责A", "要求B"),
        ])
        assert len(out) == 2


class TestEdgeCases:
    def test_empty_list(self) -> None:
        assert deduplicate_candidates([]) == []

    def test_inputs_not_mutated(self) -> None:
        a = _title_only("算法工程师", "小米", locations=["北京"],
                        evidence_refs=[{"url": "u1", "content_hash": "h1"}])
        b = _title_only("算法工程师", "小米", locations=["上海"])
        deduplicate_candidates([a, b])
        # Original objects unchanged.
        assert a.locations == ["北京"]
        assert len(a.evidence_refs) == 1

    def test_apply_url_kept_from_first_non_empty(self) -> None:
        a = _title_only("算法工程师", "小米", apply_url=None)
        b = _title_only("算法工程师", "小米", apply_url="https://example.com/job/1")
        out = deduplicate_candidates([a, b])
        assert len(out) == 1
        assert out[0].apply_url == "https://example.com/job/1"


class TestCanonicalJobKey:
    """``canonical_job_key`` must agree with ``deduplicate_candidates``:

    two candidates that the deduper would merge (same identity) must produce
    the same key so personalized-discovery upserts replace rather than duplicate.
    """

    def test_full_jd_same_body_different_city_order_matches(self) -> None:
        a = _full_jd("AI工程师", "某公司", "职责", "要求", "上海、深圳")
        b = _full_jd("AI工程师", "某公司", "职责", "要求", "深圳、上海")
        assert canonical_job_key(a) == canonical_job_key(b)

    def test_full_jd_different_body_differs(self) -> None:
        a = _full_jd("AI工程师", "某公司", "职责A", "要求", "上海")
        b = _full_jd("AI工程师", "某公司", "职责B", "要求", "上海")
        assert canonical_job_key(a) != canonical_job_key(b)

    def test_full_jd_different_city_differs(self) -> None:
        # A city variant is a DISTINCT posting -> distinct canonical key.
        a = _full_jd("AI工程师", "某公司", "职责", "要求", "上海")
        b = _full_jd("AI工程师", "某公司", "职责", "要求", "深圳")
        assert canonical_job_key(a) != canonical_job_key(b)

    def test_title_only_matches_when_deduper_merges(self) -> None:
        # Deduper merges title-only candidates by normalized title (company/
        # location excluded). The canonical key must follow the same rule.
        a = _title_only("算法工程师", "小米", locations=["北京"])
        b = _title_only("算法工程师", None, locations=["上海"])
        assert canonical_job_key(a) == canonical_job_key(b)

    def test_key_is_versioned_sha256_hex(self) -> None:
        a = _full_jd("AI工程师", "某公司", "职责", "要求", "上海")
        key = canonical_job_key(a)
        assert key.startswith("v1:")
        assert len(key) == 3 + 64
        int(key[3:], 16)  # valid hex

    def test_merged_candidate_key_matches_original_identity(self) -> None:
        """End-to-end: after dedupe, the survivor's key equals the pre-dedupe
        identity key of every member it merged (so an upsert keyed on the
        survivor would have matched any of its duplicates)."""
        a = _full_jd("AI工程师", "某公司", "职责", "要求", "上海、深圳")
        b = _full_jd("AI工程师-应届", "某公司", "职责", "要求", "深圳、上海")
        out = deduplicate_candidates([a, b])
        assert len(out) == 1
        survivor_key = canonical_job_key(out[0])
        assert survivor_key == canonical_job_key(a)
        assert survivor_key == canonical_job_key(b)
