"""Integration tests for MatchService orchestrating the full 7-step matching flow.

Tests cover:
- Happy path: complete match lifecycle
- Idempotency: dedup on same key+hash, conflict on same key+diff hash
- Error handling: missing/unverified job, missing profile, missing session
- Auto-creation of AnalysisSession when not provided
- Graph failure recovery
- Validation failure recovery
- Stale match recovery
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, update as sql_update
from sqlalchemy.orm import Session

from backend.app.db.base import Base
from backend.app.db.models import (
    AnalysisSession,
    ConfirmedProfileVersion,
    JobPosting,
    JobPostingStatus,
    JobSource,
    JobSourceProvider,
    JobSourceLink,
    JobVerification,
    MatchReport,
    Profile,
    User,
)
from backend.app.repositories import matches as match_repo
from backend.app.services.match_service import (
    MATCH_MODEL_VERSION,
    MATCH_OUTPUT_SCHEMA_VERSION,
    MATCH_PROMPT_VERSION,
    STALE_TIMEOUT_MINUTES,
    MatchService,
)
from backend.app.services.match_scoring import SCORING_RULE_VERSION
from src.evidence_matching.schemas import (
    MatchComputationOutput,
    ReferencedRecommendation,
    RequirementAssessment,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db() -> Session:
    """Provide a clean in-memory SQLite session."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def user(db: Session) -> User:
    u = User(
        id=str(uuid.uuid4()),
        account="match-tester",
        nickname="Match Tester",
        password_hash="argon2-placeholder",
    )
    db.add(u)
    db.flush()
    return u


@pytest.fixture
def analysis_session(db: Session, user: User) -> AnalysisSession:
    s = AnalysisSession(
        id=str(uuid.uuid4()),
        user_id=user.id,
        thread_id=f"thread-{uuid.uuid4().hex[:8]}",
        label="Test Session",
        activated_at=datetime.now(timezone.utc),
    )
    db.add(s)
    db.flush()
    return s


@pytest.fixture
def job_postings(db: Session) -> tuple[JobPosting, JobVerification]:
    """Create a verified JobPosting + JobVerification for match testing."""
    src = JobSource(
        id=str(uuid.uuid4()),
        source_key="test-source",
        provider=JobSourceProvider.TENCENT_SMARTSHEET,
        name="Test Source",
        file_id="f1",
        sheet_id="s1",
        mapper_version="v1",
        enabled=True,
    )
    db.add(src)
    db.flush()

    raw = type("RawRecord", (), {"id": str(uuid.uuid4())})()
    # RawJobRecord has FK to source; create a minimal one
    from backend.app.db.models import RawJobRecord

    raw = RawJobRecord(
        id=str(uuid.uuid4()),
        source_id=src.id,
        external_record_id="ext-1",
        payload_hash="a" * 64,
        raw_fields=[],
    )
    db.add(raw)
    db.flush()

    job = JobPosting(
        id=str(uuid.uuid4()),
        source_id=src.id,
        external_record_id="ext-1",
        raw_record_id=raw.id,
        status=JobPostingStatus.VERIFIED,
        company_name="TestCorp",
        title="Software Engineer",
        description_text="We need someone who knows Python and Django.",
        locations=["Shanghai"],
        recruitment_types=[],
        industries=["Tech"],
        apply_url="https://example.com/apply",
        mapper_version="v1",
        source_candidate={},
        gui_eligible=True,
        review_version=1,
        verified_at=datetime.now(timezone.utc),
    )
    db.add(job)
    db.flush()

    jv = JobVerification(
        id=str(uuid.uuid4()),
        job_id=job.id,
        actor_user_id=None,
        action="verify",
        from_status="pending_completion",
        to_status="verified",
        review_version=1,
        field_snapshot={},
    )
    db.add(jv)
    db.flush()

    # Add source link so snapshot can include it
    link = JobSourceLink(
        id=str(uuid.uuid4()),
        job_id=job.id,
        source_type="tencent_smartsheet",
        source_id=src.id,
        source_record_ref="ext-1",
        submission_id=None,
    )
    db.add(link)
    db.flush()

    return job, jv


@pytest.fixture
def profile_version(db: Session, user: User) -> ConfirmedProfileVersion:
    """Create a Profile + ConfirmedProfileVersion with non-sensitive evidence_refs."""
    profile = Profile(
        id=str(uuid.uuid4()),
        user_id=user.id,
        version=0,
        local_sensitive_references={},
    )
    db.add(profile)
    db.flush()

    cv = ConfirmedProfileVersion(
        id=str(uuid.uuid4()),
        profile_id=profile.id,
        version_number=1,
        aggregate_version=1,
        facts_snapshot={
            "skills": ["Python", "Django"],
            "name": "Alice",
        },
        evidence_refs={
            "skills": ["ev-skills-1", "ev-skills-2"],
        },
        local_sensitive_references={},
    )
    db.add(cv)
    db.flush()
    return cv


def _make_valid_output() -> MatchComputationOutput:
    """Build a MatchComputationOutput that passes validation.

    The output references:
      - requirement_id "req-python"
      - evidence_ids ["ev-skills-1", "ev-skills-2"] (defined in profile_version fixture)
    """
    req_id = "req-python"
    return MatchComputationOutput(
        strengths=[
            RequirementAssessment(
                requirement_id=req_id,
                requirement="Must know Python",
                job_field_path="description_text",
                profile_field_path="skills",
                verdict="satisfied",
                evidence_ids=["ev-skills-1", "ev-skills-2"],
                detail="Candidate has Python expertise.",
            ),
        ],
        gaps=[],
        unknowns=[],
        risks=[],
        recommendation=ReferencedRecommendation(
            text="Proceed with interview.",
            requirement_ids=[req_id],
        ),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMatchService:
    """Tests for the MatchService orchestration."""

    # -- helpers -----------------------------------------------------------

    def _make_service(self, mock_graph) -> MatchService:
        return MatchService(match_graph=mock_graph)

    # -- happy path --------------------------------------------------------

    def test_happy_path(
        self,
        db: Session,
        user: User,
        analysis_session: AnalysisSession,
        job_postings: tuple[JobPosting, JobVerification],
        profile_version: ConfirmedProfileVersion,
    ) -> None:
        """Full successful match: creates pending, runs graph, finalizes completed."""
        job, jv = job_postings
        mock_graph = MagicMock()
        mock_graph.arun_sync.return_value = {
            "result": _make_valid_output(),
        }
        service = self._make_service(mock_graph)

        report = service.create_match(
            db=db,
            user_id=user.id,
            job_id=job.id,
            profile_version_id=profile_version.id,
            idempotency_key="ik-happy-1",
            analysis_session_id=analysis_session.id,
        )

        assert report.status == "completed"
        assert report.score is not None
        assert report.score >= 0
        assert report.strengths is not None
        assert len(report.strengths) == 1
        assert report.gaps == []
        assert report.unknowns == []
        assert report.risks == []
        assert report.recommendation is not None
        assert report.application_priority is not None
        assert report.completed_at is not None
        assert report.error_code is None
        assert report.model_version == MATCH_MODEL_VERSION
        assert report.prompt_version == MATCH_PROMPT_VERSION
        assert report.output_schema_version == MATCH_OUTPUT_SCHEMA_VERSION
        assert report.scoring_rule_version == SCORING_RULE_VERSION

    # -- idempotency -------------------------------------------------------

    def test_idempotency_returns_existing(
        self,
        db: Session,
        user: User,
        analysis_session: AnalysisSession,
        job_postings: tuple[JobPosting, JobVerification],
        profile_version: ConfirmedProfileVersion,
    ) -> None:
        """Same idempotency_key + request_hash returns existing completed report."""
        job, jv = job_postings
        mock_graph = MagicMock()
        mock_graph.arun_sync.return_value = {"result": _make_valid_output()}
        service = self._make_service(mock_graph)
        idem_key = "ik-dup-1"

        first = service.create_match(
            db=db,
            user_id=user.id,
            job_id=job.id,
            profile_version_id=profile_version.id,
            idempotency_key=idem_key,
            analysis_session_id=analysis_session.id,
        )
        second = service.create_match(
            db=db,
            user_id=user.id,
            job_id=job.id,
            profile_version_id=profile_version.id,
            idempotency_key=idem_key,
            analysis_session_id=analysis_session.id,
        )

        assert second.id == first.id
        assert second.status == "completed"

    def test_idempotency_conflict_raises(
        self,
        db: Session,
        user: User,
        analysis_session: AnalysisSession,
        job_postings: tuple[JobPosting, JobVerification],
        profile_version: ConfirmedProfileVersion,
    ) -> None:
        """Same idempotency_key but different request_hash raises ValueError."""
        job, jv = job_postings
        mock_graph = MagicMock()
        mock_graph.arun_sync.return_value = {"result": _make_valid_output()}
        service = self._make_service(mock_graph)
        idem_key = "ik-conflict-1"

        # First call with job_id=A
        service.create_match(
            db=db,
            user_id=user.id,
            job_id=job.id,
            profile_version_id=profile_version.id,
            idempotency_key=idem_key,
            analysis_session_id=analysis_session.id,
        )

        # Second call with different job_id (different hash) using same key
        job2, _ = job_postings  # need a second job; reuse fixture helper
        # Actually create a second job for this scenario
        src = JobSource(
            id=str(uuid.uuid4()),
            source_key="test-source-2",
            provider=JobSourceProvider.TENCENT_SMARTSHEET,
            name="Test Source 2",
            file_id="f2",
            sheet_id="s2",
            mapper_version="v1",
            enabled=True,
        )
        db.add(src)
        db.flush()
        raw2 = type("RawRecord", (), {"id": str(uuid.uuid4())})()
        from backend.app.db.models import RawJobRecord

        raw2 = RawJobRecord(
            id=str(uuid.uuid4()),
            source_id=src.id,
            external_record_id="ext-2",
            payload_hash="b" * 64,
            raw_fields=[],
        )
        db.add(raw2)
        db.flush()
        job2 = JobPosting(
            id=str(uuid.uuid4()),
            source_id=src.id,
            external_record_id="ext-2",
            raw_record_id=raw2.id,
            status=JobPostingStatus.VERIFIED,
            company_name="OtherCorp",
            title="Backend Dev",
            description_text="...",
            locations=[],
            recruitment_types=[],
            industries=[],
            apply_url="https://other.com/apply",
            mapper_version="v1",
            source_candidate={},
            verified_at=datetime.now(timezone.utc),
        )
        db.add(job2)
        db.flush()

        with pytest.raises(ValueError, match="idempotency_key_conflict"):
            service.create_match(
                db=db,
                user_id=user.id,
                job_id=job2.id,
                profile_version_id=profile_version.id,
                idempotency_key=idem_key,
                analysis_session_id=analysis_session.id,
            )

    # -- error: job --------------------------------------------------------

    def test_unverified_job_raises(
        self,
        db: Session,
        user: User,
        analysis_session: AnalysisSession,
        profile_version: ConfirmedProfileVersion,
    ) -> None:
        """Job with status != verified raises ValueError."""
        # Create an unverified job
        src = JobSource(
            id=str(uuid.uuid4()),
            source_key="test-src-unv",
            provider=JobSourceProvider.TENCENT_SMARTSHEET,
            name="Unverified Source",
            file_id="f3",
            sheet_id="s3",
            mapper_version="v1",
            enabled=True,
        )
        db.add(src)
        db.flush()
        from backend.app.db.models import RawJobRecord

        raw = RawJobRecord(
            id=str(uuid.uuid4()),
            source_id=src.id,
            external_record_id="ext-unv",
            payload_hash="c" * 64,
            raw_fields=[],
        )
        db.add(raw)
        db.flush()
        job = JobPosting(
            id=str(uuid.uuid4()),
            source_id=src.id,
            external_record_id="ext-unv",
            raw_record_id=raw.id,
            status=JobPostingStatus.PENDING_REVIEW,
            company_name="UnverifiedCo",
            title="Unknown",
            description_text="",
            locations=[],
            recruitment_types=[],
            industries=[],
            apply_url="https://unv.com/apply",
            mapper_version="v1",
            source_candidate={},
        )
        db.add(job)
        db.flush()

        mock_graph = MagicMock()
        service = self._make_service(mock_graph)

        with pytest.raises(ValueError, match="match_not_verified_job"):
            service.create_match(
                db=db,
                user_id=user.id,
                job_id=job.id,
                profile_version_id=profile_version.id,
                idempotency_key="ik-unv-job-1",
                analysis_session_id=analysis_session.id,
            )

    def test_missing_job_raises(
        self,
        db: Session,
        user: User,
        analysis_session: AnalysisSession,
        profile_version: ConfirmedProfileVersion,
    ) -> None:
        """Non-existent job_id raises ValueError."""
        mock_graph = MagicMock()
        service = self._make_service(mock_graph)

        with pytest.raises(ValueError, match="not_found"):
            service.create_match(
                db=db,
                user_id=user.id,
                job_id="nonexistent-job-id",
                profile_version_id=profile_version.id,
                idempotency_key="ik-miss-job-1",
                analysis_session_id=analysis_session.id,
            )

    # -- error: profile ----------------------------------------------------

    def test_missing_profile_raises(
        self,
        db: Session,
        user: User,
        analysis_session: AnalysisSession,
        job_postings: tuple[JobPosting, JobVerification],
    ) -> None:
        """Non-existent profile_version_id raises ValueError."""
        job, jv = job_postings
        mock_graph = MagicMock()
        service = self._make_service(mock_graph)

        with pytest.raises(ValueError, match="match_no_confirmed_profile"):
            service.create_match(
                db=db,
                user_id=user.id,
                job_id=job.id,
                profile_version_id="nonexistent-profile-ver",
                idempotency_key="ik-miss-prof-1",
                analysis_session_id=analysis_session.id,
            )

    # -- error: session ----------------------------------------------------

    def test_missing_session_raises(
        self,
        db: Session,
        user: User,
        job_postings: tuple[JobPosting, JobVerification],
        profile_version: ConfirmedProfileVersion,
    ) -> None:
        """Non-existent session_id for this user raises ValueError."""
        job, jv = job_postings
        mock_graph = MagicMock()
        service = self._make_service(mock_graph)

        with pytest.raises(ValueError, match="not_found"):
            service.create_match(
                db=db,
                user_id=user.id,
                job_id=job.id,
                profile_version_id=profile_version.id,
                idempotency_key="ik-miss-sess-1",
                analysis_session_id="nonexistent-session-id",
            )

    # -- auto-create session -----------------------------------------------

    def test_auto_create_session(
        self,
        db: Session,
        user: User,
        job_postings: tuple[JobPosting, JobVerification],
        profile_version: ConfirmedProfileVersion,
    ) -> None:
        """When analysis_session_id is None, a new session is created."""
        job, jv = job_postings
        mock_graph = MagicMock()
        mock_graph.arun_sync.return_value = {"result": _make_valid_output()}
        service = self._make_service(mock_graph)

        report = service.create_match(
            db=db,
            user_id=user.id,
            job_id=job.id,
            profile_version_id=profile_version.id,
            idempotency_key="ik-auto-sess-1",
            analysis_session_id=None,
        )

        assert report.status == "completed"
        # Verify a session was created
        session = (
            db.query(AnalysisSession)
            .filter(AnalysisSession.id == report.analysis_session_id)
            .first()
        )
        assert session is not None
        assert session.user_id == user.id
        assert session.label == "Match Session"

    # -- graph failure handling --------------------------------------------

    def test_graph_failure(
        self,
        db: Session,
        user: User,
        analysis_session: AnalysisSession,
        job_postings: tuple[JobPosting, JobVerification],
        profile_version: ConfirmedProfileVersion,
    ) -> None:
        """When the graph raises an exception, the match is finalized as failed."""
        job, jv = job_postings
        mock_graph = MagicMock()
        mock_graph.arun_sync.side_effect = RuntimeError("LLM timeout")
        service = self._make_service(mock_graph)

        report = service.create_match(
            db=db,
            user_id=user.id,
            job_id=job.id,
            profile_version_id=profile_version.id,
            idempotency_key="ik-graph-fail-1",
            analysis_session_id=analysis_session.id,
        )

        assert report.status == "failed"
        assert report.error_code == "match_execution_interrupted"
        assert report.completed_at is not None

    def test_graph_no_result(
        self,
        db: Session,
        user: User,
        analysis_session: AnalysisSession,
        job_postings: tuple[JobPosting, JobVerification],
        profile_version: ConfirmedProfileVersion,
    ) -> None:
        """When the graph returns no result, the match is finalized as failed."""
        job, jv = job_postings
        mock_graph = MagicMock()
        # Graph returns state dict with result=None
        mock_graph.arun_sync.return_value = {"result": None}
        service = self._make_service(mock_graph)

        report = service.create_match(
            db=db,
            user_id=user.id,
            job_id=job.id,
            profile_version_id=profile_version.id,
            idempotency_key="ik-no-result-1",
            analysis_session_id=analysis_session.id,
        )

        assert report.status == "failed"
        assert report.error_code == "match_model_validation_failed"

    def test_final_status_is_committed(
        self,
        db: Session,
        user: User,
        analysis_session: AnalysisSession,
        job_postings: tuple[JobPosting, JobVerification],
        profile_version: ConfirmedProfileVersion,
    ) -> None:
        """Finalized reports remain visible after a caller-side rollback."""
        job, _ = job_postings
        mock_graph = MagicMock()
        mock_graph.arun_sync.return_value = {"result": None}
        service = self._make_service(mock_graph)

        report = service.create_match(
            db=db,
            user_id=user.id,
            job_id=job.id,
            profile_version_id=profile_version.id,
            idempotency_key="ik-final-commit-1",
            analysis_session_id=analysis_session.id,
        )

        db.rollback()
        persisted = db.get(MatchReport, report.id)

        assert persisted is not None
        assert persisted.status == "failed"
        assert persisted.error_code == "match_model_validation_failed"

    # -- validation failure handling ---------------------------------------

    def test_validation_failure(
        self,
        db: Session,
        user: User,
        analysis_session: AnalysisSession,
        job_postings: tuple[JobPosting, JobVerification],
        profile_version: ConfirmedProfileVersion,
    ) -> None:
        """When validation fails, the match is finalized with the validator error."""
        job, jv = job_postings
        # Output without requirement_id -> validator raises MatchValidationError
        bad_output = MatchComputationOutput(
            strengths=[
                RequirementAssessment(
                    requirement_id="",  # empty -> will fail validation
                    requirement="test",
                    job_field_path="test",
                    verdict="satisfied",
                    evidence_ids=[],
                    detail="test",
                ),
            ],
            gaps=[],
            unknowns=[],
            risks=[],
            recommendation=ReferencedRecommendation(
                text="test", requirement_ids=[]
            ),
        )
        # The output is already invalid because requirement_id is empty
        # but the validator checks for missing/empty requirement_id

        mock_graph = MagicMock()
        mock_graph.arun_sync.return_value = {"result": bad_output}
        service = self._make_service(mock_graph)

        report = service.create_match(
            db=db,
            user_id=user.id,
            job_id=job.id,
            profile_version_id=profile_version.id,
            idempotency_key="ik-val-fail-1",
            analysis_session_id=analysis_session.id,
        )

        assert report.status == "failed"
        # The validator raises for missing requirement_id
        assert report.error_code == "match_validation_missing_requirement_id"

    # -- stale recovery ----------------------------------------------------

    def test_recover_stale(
        self,
        db: Session,
        user: User,
        analysis_session: AnalysisSession,
        job_postings: tuple[JobPosting, JobVerification],
        profile_version: ConfirmedProfileVersion,
    ) -> None:
        """Stale pending/running matches are recovered by recover_stale()."""
        job, jv = job_postings

        # Create a pending match directly (without going through the service)
        report = match_repo.create(
            db,
            id=str(uuid.uuid4()),
            user_id=user.id,
            analysis_session_id=analysis_session.id,
            job_id=job.id,
            job_verification_id=jv.id,
            job_snapshot={"company_name": "TestCo"},
            profile_version_id=profile_version.id,
            request_idempotency_key="ik-stale-1",
            request_hash="stale-hash",
            status="pending",
            scoring_rule_version=SCORING_RULE_VERSION,
            model_version=MATCH_MODEL_VERSION,
            prompt_version=MATCH_PROMPT_VERSION,
            output_schema_version=MATCH_OUTPUT_SCHEMA_VERSION,
        )
        # Push created_at into the past
        past = datetime.now(timezone.utc) - timedelta(minutes=STALE_TIMEOUT_MINUTES + 5)
        db.execute(
            sql_update(MatchReport)
            .where(MatchReport.id == report.id)
            .values(created_at=past),
        )
        db.commit()

        mock_graph = MagicMock()
        service = self._make_service(mock_graph)

        recovered_count = service.recover_stale(db)
        assert recovered_count >= 1

        # Verify the report is now failed
        recovered = match_repo.get_by_id(db, report.id, user.id)
        assert recovered is not None
        assert recovered.status == "failed"
        assert recovered.completed_at is not None

    def test_recover_stale_recent_ignored(
        self,
        db: Session,
        user: User,
        analysis_session: AnalysisSession,
        job_postings: tuple[JobPosting, JobVerification],
        profile_version: ConfirmedProfileVersion,
    ) -> None:
        """Recent pending matches are not recovered."""
        job, jv = job_postings

        match_repo.create(
            db,
            id=str(uuid.uuid4()),
            user_id=user.id,
            analysis_session_id=analysis_session.id,
            job_id=job.id,
            job_verification_id=jv.id,
            job_snapshot={"company_name": "TestCo"},
            profile_version_id=profile_version.id,
            request_idempotency_key="ik-stale-fresh-1",
            request_hash="fresh-hash",
            status="pending",
            scoring_rule_version=SCORING_RULE_VERSION,
            model_version=MATCH_MODEL_VERSION,
            prompt_version=MATCH_PROMPT_VERSION,
            output_schema_version=MATCH_OUTPUT_SCHEMA_VERSION,
        )
        db.commit()

        mock_graph = MagicMock()
        service = self._make_service(mock_graph)

        recovered_count = service.recover_stale(db)
        assert recovered_count == 0
