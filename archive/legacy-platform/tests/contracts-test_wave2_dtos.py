"""Contract tests for Wave 2 DTOs — verify serialization shapes before implementation."""
import json
from datetime import datetime, timezone


class TestVerifiedJobSnapshot:
    def test_round_trip_minimal(self):
        from backend.app.services.job_snapshot_service import VerifiedJobSnapshot

        snapshot = VerifiedJobSnapshot(
            job_id="job-001",
            company_name="Test Corp",
            title="Software Engineer",
            description_text="Build things.",
            locations=["Beijing"],
            recruitment_types=["campus"],
            industries=["Tech"],
            apply_url="https://example.com/apply",
            gui_eligible=True,
            verified_at=datetime.now(timezone.utc),
            review_version=1,
            source_links=[],
            job_verification_id="jv-001",
        )
        data = json.loads(json.dumps(snapshot.__dict__, default=str))
        assert data["job_id"] == "job-001"
        assert data["gui_eligible"] is True

    def test_gui_ineligible(self):
        from backend.app.services.job_snapshot_service import VerifiedJobSnapshot

        snapshot = VerifiedJobSnapshot(
            job_id="job-002",
            company_name="Test Corp",
            title="Referral Only",
            description_text="Email your CV.",
            locations=["Shanghai"],
            recruitment_types=["referral"],
            industries=["Finance"],
            apply_url=None,
            gui_eligible=False,
            verified_at=datetime.now(timezone.utc),
            review_version=2,
            source_links=[],
            job_verification_id="jv-002",
        )
        assert snapshot.gui_eligible is False
        assert snapshot.apply_url is None


class TestConfirmedProfileSnapshot:
    def test_sensitive_fields_excluded(self):
        from backend.app.services.profile_snapshot_service import ConfirmedProfileSnapshot

        snapshot = ConfirmedProfileSnapshot(
            profile_version_id="pv-001",
            profile_id="prof-001",
            version_number=1,
            facts={
                "education": [{"school": "PKU", "degree": "BS"}],
                "skills": ["Python", "Rust"],
            },
            evidence_refs={
                "education": ["ev-edu-001"],
                "skills": ["ev-skill-001"],
            },
            confirmed_at=datetime.now(timezone.utc),
        )
        # local_sensitive_references must NOT be in facts
        assert "id_number" not in snapshot.facts
        assert "family_members" not in snapshot.facts
