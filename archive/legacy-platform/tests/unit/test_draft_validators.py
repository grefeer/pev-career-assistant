"""Tests for draft_validators — validates resume diff operations."""
import pytest

from backend.app.services.draft_validators import (
    validate_draft_diffs,
    DraftValidationError,
)


class TestValidateDraftDiffs:
    """validate_draft_diffs(diffs, confirmed_facts, evidence_refs)"""

    def test_valid_diffs_pass(self):
        diffs = [
            {"op": "reorder", "section": "education", "fact_ref": "edu-001", "evidence_ids": ["ev-001"]},
            {"op": "rephrase", "section": "skills", "fact_ref": "skill-001", "evidence_ids": ["ev-002"]},
            {"op": "summarize", "section": "experience", "fact_ref": "exp-001", "evidence_ids": []},
            {"op": "omit", "section": "awards", "fact_ref": "award-001", "evidence_ids": []},
            {"op": "highlight", "section": "projects", "fact_ref": "proj-001", "evidence_ids": ["ev-003"]},
        ]
        confirmed_facts = {
            "edu-001": {"school": "PKU"},
            "skill-001": {"name": "Python"},
            "exp-001": {"company": "ACME"},
            "award-001": {"title": "Best"},
            "proj-001": {"name": "Project X"},
        }
        evidence_refs = {
            "education": ["ev-001"],
            "skills": ["ev-002"],
            "projects": ["ev-003"],
        }
        result = validate_draft_diffs(diffs, confirmed_facts, evidence_refs)
        assert result == diffs

    def test_invalid_op_fails(self):
        diffs = [
            {"op": "invalid_op", "section": "education", "fact_ref": "edu-001", "evidence_ids": []},
        ]
        with pytest.raises(DraftValidationError, match="op"):
            validate_draft_diffs(diffs, {"edu-001": {}}, {})

    def test_empty_section_fails(self):
        diffs = [
            {"op": "reorder", "section": "", "fact_ref": "edu-001", "evidence_ids": []},
        ]
        with pytest.raises(DraftValidationError, match="section"):
            validate_draft_diffs(diffs, {"edu-001": {}}, {})

    def test_missing_fact_ref_fails(self):
        diffs = [
            {"op": "reorder", "section": "education", "fact_ref": "nonexistent", "evidence_ids": []},
        ]
        with pytest.raises(DraftValidationError, match="fact_ref"):
            validate_draft_diffs(diffs, {"edu-001": {}}, {})

    def test_invalid_evidence_id_fails(self):
        diffs = [
            {"op": "reorder", "section": "education", "fact_ref": "edu-001", "evidence_ids": ["ev-invalid"]},
        ]
        with pytest.raises(DraftValidationError, match="evidence"):
            validate_draft_diffs(diffs, {"edu-001": {}}, {"education": ["ev-001"]})

    def test_missing_section_key_fails(self):
        diffs = [
            {"op": "reorder", "fact_ref": "edu-001", "evidence_ids": []},
        ]
        with pytest.raises(DraftValidationError, match="section"):
            validate_draft_diffs(diffs, {"edu-001": {}}, {})

    def test_missing_op_key_fails(self):
        diffs = [
            {"section": "education", "fact_ref": "edu-001", "evidence_ids": []},
        ]
        with pytest.raises(DraftValidationError, match="op"):
            validate_draft_diffs(diffs, {"edu-001": {}}, {})

    def test_valid_diff_with_empty_evidence_ids_passes(self):
        diffs = [
            {"op": "omit", "section": "awards", "fact_ref": "award-001", "evidence_ids": []},
        ]
        result = validate_draft_diffs(diffs, {"award-001": {}}, {})
        assert result == diffs
