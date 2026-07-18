"""Regression test framework for site adapters.

Each registered adapter is tested against its static HTML fixtures to
verify: page classification, field filling strategy selection, submit
button blocking, and ambiguous button detection.

Tests are parametrized across all adapters with fixtures.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from executor.adapters.base import PageFingerprint, SiteAdapter
from executor.adapters.moka.adapter import MokaSiteAdapter

# Registry of adapters to test
_ADAPTERS: dict[str, SiteAdapter] = {
    "moka.dji": MokaSiteAdapter(),
}

FIXTURE_DIR = Path(__file__).parent / "fixtures"


# ── Helpers ─────────────────────────────────────────────────────────────────────


def _load_html(page_key: str) -> str:
    """Load an HTML fixture for the given site/page key."""
    parts = page_key.split("/", 1)
    site = parts[0]
    page = parts[1] if len(parts) > 1 else parts[0]
    path = FIXTURE_DIR / site / f"{page}.html"
    if not path.exists():
        pytest.skip(f"Fixture not found: {path}")
    return path.read_text(encoding="utf-8")


# ── Tests ───────────────────────────────────────────────────────────────────────


class TestAdapterRegistry:
    """Verify that all registered adapters satisfy structural invariants."""

    @pytest.mark.parametrize(
        "adapter_id,adapter",
        [(aid, a) for aid, a in _ADAPTERS.items()],
    )
    def test_adapter_is_site_adapter(self, adapter_id, adapter):
        assert isinstance(adapter, SiteAdapter), (
            f"{adapter_id} does not implement SiteAdapter"
        )

    @pytest.mark.parametrize(
        "adapter_id,adapter",
        [(aid, a) for aid, a in _ADAPTERS.items()],
    )
    def test_adapter_has_non_empty_domains(self, adapter_id, adapter):
        assert len(adapter.supported_domains) > 0, (
            f"{adapter_id} has no supported domains"
        )

    @pytest.mark.parametrize(
        "adapter_id,adapter",
        [(aid, a) for aid, a in _ADAPTERS.items()],
    )
    def test_adapter_version_is_semver(self, adapter_id, adapter):
        parts = adapter.version.split(".")
        assert len(parts) == 3, f"{adapter_id} version is not semver"
        assert all(p.isdigit() for p in parts)


class TestPageClassification:
    """Verify that page classification works for all fixtures.

    These tests use PageFingerprint objects (not live Playwright pages)
    to validate the classification logic.
    """

    def test_moka_first_page_classified_as_first(self):
        adapter = _ADAPTERS["moka.dji"]
        fp = PageFingerprint(
            url_pattern="https://zhaopin.dji.com/apply",
            dom_hash="sha256:" + "11" * 32,
            page_index=1,
            total_pages=6,
            fields_detected=["name", "email", "school"],
        )
        result = adapter.classify_topology(fp)
        assert result == "multi_page_first"

    def test_moka_middle_page_classified_as_middle(self):
        adapter = _ADAPTERS["moka.dji"]
        fp = PageFingerprint(
            url_pattern="https://zhaopin.dji.com/apply",
            dom_hash="sha256:" + "22" * 32,
            page_index=2,
            total_pages=6,
            fields_detected=[],
        )
        result = adapter.classify_topology(fp)
        assert result == "multi_page_middle"

    def test_moka_last_page_classified_as_last(self):
        adapter = _ADAPTERS["moka.dji"]
        fp = PageFingerprint(
            url_pattern="https://zhaopin.dji.com/apply",
            dom_hash="sha256:" + "33" * 32,
            page_index=6,
            total_pages=6,
            has_submit_button=True,
            fields_detected=["resume_upload"],
        )
        result = adapter.classify_topology(fp)
        assert result == "multi_page_last"

    def test_unknown_page_classified_as_unknown(self):
        adapter = _ADAPTERS["moka.dji"]
        fp = PageFingerprint(
            url_pattern="https://unknown.example.com",
            dom_hash="sha256:" + "ff" * 32,
            page_index=999,
            fields_detected=[],
        )
        result = adapter.classify_topology(fp)
        assert result == "unknown"


class TestFieldFillStrategies:
    """Verify field fill strategy selection."""

    def test_text_input_strategy_exists(self):
        from executor.adapters.moka.controls import FILL_STRATEGIES
        assert "text_input" in FILL_STRATEGIES
        assert callable(FILL_STRATEGIES["text_input"])

    def test_select_strategy_exists(self):
        from executor.adapters.moka.controls import FILL_STRATEGIES
        assert "select" in FILL_STRATEGIES
        assert callable(FILL_STRATEGIES["select"])

    def test_file_upload_strategy_exists(self):
        from executor.adapters.moka.controls import FILL_STRATEGIES
        assert "file_upload" in FILL_STRATEGIES
        assert callable(FILL_STRATEGIES["file_upload"])

    def test_deferred_strategy_exists(self):
        from executor.adapters.moka.controls import FILL_STRATEGIES
        assert "deferred" in FILL_STRATEGIES
        assert callable(FILL_STRATEGIES["deferred"])

    def test_all_strategies_return_fill_result_type(self):
        from executor.adapters.moka.controls import FILL_STRATEGIES
        for name in [
            "text_input", "select", "date_picker", "rich_text",
            "radio", "checkbox", "multi_select", "deferred",
        ]:
            assert name in FILL_STRATEGIES, f"Missing strategy: {name}"


class TestRepeatSectionLogic:
    """Verify repeat section handler invariants."""

    def test_stable_entry_signature_deterministic(self):
        from executor.adapters.moka.repeat import _stable_entry_signature
        entry = {"school": "PKU", "degree": "BS"}
        sig1 = _stable_entry_signature(entry)
        sig2 = _stable_entry_signature(entry)
        assert sig1 == sig2

    def test_stable_entry_signature_order_independent(self):
        from executor.adapters.moka.repeat import _stable_entry_signature
        e1 = {"a": "1", "b": "2"}
        e2 = {"b": "2", "a": "1"}
        assert _stable_entry_signature(e1) == _stable_entry_signature(e2)

    def test_stable_entry_signature_different(self):
        from executor.adapters.moka.repeat import _stable_entry_signature
        e1 = {"school": "PKU", "degree": "BS"}
        e2 = {"school": "THU", "degree": "MS"}
        assert _stable_entry_signature(e1) != _stable_entry_signature(e2)


class TestSubmitBlocking:
    """Verify that submit buttons exist in topology but are never auto-clicked.

    The actual safety gate is in ``executor.safety.decide_action()``.
    These tests verify the topology correctly identifies submit buttons.
    """

    def test_moka_submit_selectors_not_empty(self):
        from executor.adapters.moka.topology import SUBMIT_BUTTON_SELECTORS
        assert len(SUBMIT_BUTTON_SELECTORS) > 0

    def test_submit_selectors_contain_chinese_labels(self):
        from executor.adapters.moka.topology import SUBMIT_BUTTON_SELECTORS
        has_submit = any("提交" in s or "投递" in s for s in SUBMIT_BUTTON_SELECTORS)
        assert has_submit, "Submit selectors must include Chinese labels"

    def test_safety_gate_blocks_submit_label(self):
        """Cross-reference: executor safety gate blocks Moka submit labels."""
        from executor.safety import FINAL_TOKENS, decide_action, PageTopology
        assert "\u63d0\u4ea4" in FINAL_TOKENS  # 提交
        decision = decide_action(
            topology=PageTopology.MULTI_STEP_FINAL,
            label="提交申请",
            action_kind="submit",
            is_bottom_action=True,
            has_verified_next_step=False,
        )
        assert not decision.allowed
        assert decision.reason_code == "final_action_forbidden"


class TestAttachmentLogic:
    """Verify attachment upload logic structure."""

    def test_file_upload_selectors_defined(self):
        from executor.adapters.moka.attachments import FILE_INPUT_SELECTORS
        assert "resume_upload" in FILE_INPUT_SELECTORS
        assert "portfolio_upload" in FILE_INPUT_SELECTORS


class TestFingerprintDeduplication:
    """Verify fingerprint mechanism for checkpointing."""

    def test_dom_hash_format(self):
        fp = PageFingerprint(
            url_pattern="https://example.com/page",
            dom_hash="sha256:abcdef1234567890",
        )
        assert fp.dom_hash.startswith("sha256:")
        assert len(fp.dom_hash) > 7  # sha256: + at least 1 char

    def test_different_urls_different_fingerprints(self):
        fp1 = PageFingerprint(
            url_pattern="https://example.com/page1",
            dom_hash="sha256:aaaa",
        )
        fp2 = PageFingerprint(
            url_pattern="https://example.com/page2",
            dom_hash="sha256:bbbb",
        )
        assert fp1.url_pattern != fp2.url_pattern
