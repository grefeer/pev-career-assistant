"""Verify migration 0008 tables exist and ORM models work after upgrade."""
import pytest
import uuid
from datetime import datetime, timezone


@pytest.mark.integration
class TestMatchReportCRUD:
    def test_create_and_read(self, db_session, test_user):
        from backend.app.db.models import MatchReport

        mid = str(uuid.uuid4())
        report = MatchReport(
            id=mid,
            user_id=test_user.id,
            analysis_session_id=str(uuid.uuid4()),
            job_id=str(uuid.uuid4()),
            job_verification_id=str(uuid.uuid4()),
            job_snapshot={"company_name": "Test"},
            profile_version_id=str(uuid.uuid4()),
            request_idempotency_key=f"test-{mid[:8]}",
            request_hash="abc123",
            status="pending",
            scoring_rule_version="1.0",
            model_version="test",
            prompt_version="test-v1",
            output_schema_version="1.0",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(report)
        db_session.commit()

        fetched = db_session.query(MatchReport).filter_by(id=mid).first()
        assert fetched is not None
        assert fetched.status == "pending"
        assert fetched.job_snapshot["company_name"] == "Test"


@pytest.mark.integration
class TestApplicationTaskMigration:
    def test_simulation_task_has_task_kind(self, db_session):
        from backend.app.db.models import ApplicationTask

        tasks = db_session.query(ApplicationTask).filter(
            ApplicationTask.snapshot_id.is_(None)
        ).limit(5).all()
        for t in tasks:
            assert t.task_kind == "simulation"
            assert t.target_job_id is None or t.simulation_scenario is not None
