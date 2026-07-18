"""Unit tests for the DJI Moka site adapter.

These tests validate the adapter independent of a real browser by testing
the pure-logic portions (topology classification, field mapping) and
structural invariants (Protocol compliance, version format).
"""

from __future__ import annotations


from executor.adapters.base import (
    PageClass,
    PageFingerprint,
    SiteAdapter,
)
from executor.adapters.moka.adapter import MokaSiteAdapter
from executor.adapters.moka.topology import (
    MOKA_TOPOLOGY,
    find_page_entry,
)


class TestMokaAdapterProtocol:
    """Verify structural compliance with SiteAdapter Protocol."""

    def test_is_runtime_checkable(self):
        adapter = MokaSiteAdapter()
        assert isinstance(adapter, SiteAdapter)

    def test_has_required_attributes(self):
        adapter = MokaSiteAdapter()
        assert adapter.adapter_id == "moka.dji"
        assert len(adapter.supported_domains) > 0
        assert "." in adapter.version  # semver-like

    def test_version_is_semver(self):
        adapter = MokaSiteAdapter()
        parts = adapter.version.split(".")
        assert len(parts) == 3
        assert all(p.isdigit() for p in parts)

    def test_supported_domains_non_empty(self):
        adapter = MokaSiteAdapter()
        for domain in adapter.supported_domains:
            assert "." in domain or domain == "localhost"


class TestTopologyClassification:
    """Test page classification logic."""

    def test_find_page_entry_by_index(self):
        entry = find_page_entry(page_index=1)
        assert entry is not None
        assert entry["page_class"] == PageClass.MULTI_PAGE_FIRST
        assert entry["label"] == "basic_info"

    def test_find_page_entry_by_index_2(self):
        entry = find_page_entry(page_index=2)
        assert entry is not None
        assert entry["page_class"] == PageClass.MULTI_PAGE_MIDDLE
        assert entry["repeat_section"] == "education"

    def test_find_page_entry_last_page(self):
        entry = find_page_entry(page_index=6)
        assert entry is not None
        assert entry["page_class"] == PageClass.MULTI_PAGE_LAST

    def test_find_page_entry_out_of_range(self):
        entry = find_page_entry(page_index=99)
        assert entry is None

    def test_fingerprint_classify_unknown(self):
        adapter = MokaSiteAdapter()
        fp = PageFingerprint(
            url_pattern="https://unknown.example.com",
            dom_hash="sha256:" + "ab" * 32,
            page_index=999,
            fields_detected=[],
        )
        result = adapter.classify_topology(fp)
        assert result == PageClass.UNKNOWN

    def test_fingerprint_last_page_heuristic(self):
        adapter = MokaSiteAdapter()
        fp = PageFingerprint(
            url_pattern="https://moka.com/form/last",
            dom_hash="sha256:" + "ab" * 32,
            page_index=7,
            has_submit_button=True,
            fields_detected=["name"],
        )
        result = adapter.classify_topology(fp)
        assert result == PageClass.MULTI_PAGE_LAST

    def test_topology_page_count(self):
        assert len(MOKA_TOPOLOGY) == 6
        indices = [p["index"] for p in MOKA_TOPOLOGY]
        assert indices == [1, 2, 3, 4, 5, 6]

    def test_topology_first_page_is_multi_page_first(self):
        assert MOKA_TOPOLOGY[0]["page_class"] == PageClass.MULTI_PAGE_FIRST

    def test_topology_last_page_is_multi_page_last(self):
        assert MOKA_TOPOLOGY[-1]["page_class"] == PageClass.MULTI_PAGE_LAST


class TestFingerprintStructure:
    """Test PageFingerprint invariants."""

    def test_fingerprint_contains_dom_hash(self):
        fp = PageFingerprint(
            url_pattern="https://moka.com/page",
            dom_hash="sha256:" + "cd" * 32,
        )
        assert fp.dom_hash.startswith("sha256:")

    def test_fingerprint_fields_detected_list(self):
        fp = PageFingerprint(
            url_pattern="https://moka.com/page",
            dom_hash="sha256:" + "ef" * 32,
            fields_detected=["name", "email"],
        )
        assert "name" in fp.fields_detected
        assert "email" in fp.fields_detected


class TestBlockerDetection:
    """Test blocker detection logic patterns."""

    def test_blocker_info_structure(self):
        from executor.adapters.base import BlockerInfo
        blocker = BlockerInfo(blocker_type="login", detail="需要登录")
        assert blocker.blocker_type == "login"
        assert blocker.requires_human is True

    def test_blocker_types_are_documented(self):
        valid_types = {"login", "captcha", "risk_warning", "page_changed", "unknown_button"}
        # Verify our BlockerInfo uses one of the valid types
        from executor.adapters.base import BlockerInfo
        b = BlockerInfo(blocker_type="page_changed", detail="DOM changed")
        assert b.blocker_type in valid_types


class TestMokaTopologyControlsMapping:
    """Verify that each topology page has a valid control mapping."""

    def test_basic_info_has_controls(self):
        page = MOKA_TOPOLOGY[0]
        assert len(page["controls"]) >= 6
        assert "name" in page["controls"]
        assert page["controls"]["name"] == "text_input"

    def test_skills_has_multi_select(self):
        page = MOKA_TOPOLOGY[4]  # page 5
        assert page["controls"]["skills"] == "multi_select"

    def test_attachments_has_file_upload(self):
        page = MOKA_TOPOLOGY[5]  # page 6
        assert page["controls"]["resume_upload"] == "file_upload"
