"""Tests for match_validators — validates LangGraph structured output before persisting MatchReport."""
from types import SimpleNamespace

import pytest

from backend.app.services.match_validators import (
    validate_match_output,
    MatchValidationError,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def sample_job_snapshot():
    """Minimal VerifiedJobSnapshot-like object; only fields used by validators."""
    return SimpleNamespace(
        job_id="job-001",
        company_name="Test Corp",
        title="Software Engineer",
    )


@pytest.fixture
def sample_profile_snapshot():
    """Minimal ConfirmedProfileSnapshot-like object with evidence_refs."""
    return SimpleNamespace(
        profile_version_id="pv-001",
        profile_id="prof-001",
        version_number=1,
        facts={
            "education": [{"school": "PKU", "degree": "BS"}],
            "skills": ["Python", "Rust"],
        },
        evidence_refs={
            "education": ["ev-edu-001"],
            "skills": ["ev-skill-001", "ev-skill-002"],
        },
    )


@pytest.fixture
def sample_valid_output():
    """A fully valid MatchComputationOutput dict."""
    return {
        "strengths": [
            {
                "requirement_id": "req-edu",
                "verdict": "satisfied",
                "rationale": "Education matches.",
                "profile_field_path": "education",
                "evidence_ids": ["ev-edu-001"],
            },
            {
                "requirement_id": "req-skill",
                "verdict": "satisfied",
                "rationale": "Skills match.",
                "profile_field_path": "skills",
                "evidence_ids": ["ev-skill-001", "ev-skill-002"],
            },
        ],
        "gaps": [
            {
                "requirement_id": "req-lang",
                "verdict": "gap",
                "rationale": "Missing language requirement.",
            },
        ],
        "unknowns": [
            {
                "requirement_id": "req-unknown",
                "verdict": "unknown",
                "rationale": "Cannot determine from profile.",
            },
        ],
        "risks": [
            {
                "requirement_id": "risk-1",
                "verdict": "risk",
                "rationale": "Potential overqualification.",
                "requirement_ids": ["req-edu"],
            },
        ],
        "recommendation": {
            "decision": "proceed",
            "rationale": "Candidate is a strong match.",
            "requirement_ids": ["req-edu", "req-skill", "req-lang", "req-unknown"],
        },
    }


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestValidateMatchOutput:
    def test_valid_output_passes(self, sample_job_snapshot, sample_profile_snapshot, sample_valid_output):
        result = validate_match_output(sample_valid_output, sample_job_snapshot, sample_profile_snapshot)
        assert result == sample_valid_output

    def test_missing_requirement_id_fails(self, sample_job_snapshot, sample_profile_snapshot, sample_valid_output):
        sample_valid_output["strengths"][0].pop("requirement_id")
        with pytest.raises(MatchValidationError, match="requirement_id"):
            validate_match_output(sample_valid_output, sample_job_snapshot, sample_profile_snapshot)

    def test_satisfied_without_evidence_fails(self, sample_job_snapshot, sample_profile_snapshot, sample_valid_output):
        sample_valid_output["strengths"][0]["evidence_ids"] = []
        with pytest.raises(MatchValidationError, match="evidence"):
            validate_match_output(sample_valid_output, sample_job_snapshot, sample_profile_snapshot)

    def test_unknown_with_fabricated_evidence_fails(self, sample_job_snapshot, sample_profile_snapshot, sample_valid_output):
        sample_valid_output["unknowns"][0]["evidence_ids"] = ["fake-ev-001"]
        with pytest.raises(MatchValidationError, match="unknown.*evidence"):
            validate_match_output(sample_valid_output, sample_job_snapshot, sample_profile_snapshot)

    def test_invalid_evidence_ref_fails(self, sample_job_snapshot, sample_profile_snapshot, sample_valid_output):
        sample_valid_output["strengths"][0]["evidence_ids"] = ["ev-nonexistent"]
        with pytest.raises(MatchValidationError, match="evidence.*not found"):
            validate_match_output(sample_valid_output, sample_job_snapshot, sample_profile_snapshot)

    def test_risk_without_requirement_ref_fails(self, sample_job_snapshot, sample_profile_snapshot, sample_valid_output):
        sample_valid_output["risks"][0]["requirement_ids"] = []
        with pytest.raises(MatchValidationError, match="requirement.*ref"):
            validate_match_output(sample_valid_output, sample_job_snapshot, sample_profile_snapshot)

    def test_verdict_mismatch_fails(self, sample_job_snapshot, sample_profile_snapshot, sample_valid_output):
        sample_valid_output["strengths"][0]["verdict"] = "gap"
        with pytest.raises(MatchValidationError, match="verdict.*strengths"):
            validate_match_output(sample_valid_output, sample_job_snapshot, sample_profile_snapshot)
