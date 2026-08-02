"""Unit tests for TaskEligibilityService — check_task_eligibility gates."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db.base import Base
from backend.app.db.models import (
    ApplicationSnapshot,
    ApprovedResumeAttachment,
    ApprovedResumeVersion,
    JobPosting,
    JobPostingStatus,
    JobSource,
    JobSourceProvider,
    RawJobRecord,
    ResumeDraft,
    User,
    UserRole,
)
from backend.app.services.task_eligibility_service import check_task_eligibility


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def db() -> Session:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        yield session


@pytest.fixture
def user(db: Session) -> User:
    value = User(
        account="alice",
        nickname="Alice",
        password_hash="hash",
        role=UserRole.STUDENT,
        is_active=True,
    )
    db.add(value)
    db.commit()
    return value


@pytest.fixture
def other_user(db: Session) -> User:
    value = User(
        account="bob",
        nickname="Bob",
        password_hash="hash",
        role=UserRole.STUDENT,
        is_active=True,
    )
    db.add(value)
    db.commit()
    return value


@pytest.fixture
def job_source(db: Session) -> JobSource:
    value = JobSource(
        source_key="elig-source",
        provider=JobSourceProvider.TENCENT_SMARTSHEET,
        name="Eligibility Source",
        file_id="f1",
        sheet_id="s1",
        mapper_version="v1",
        enabled=True,
    )
    db.add(value)
    db.commit()
    return value


@pytest.fixture
def raw_job_record(db: Session, job_source: JobSource) -> RawJobRecord:
    value = RawJobRecord(
        source_id=job_source.id,
        external_record_id="ext-1",
        payload_hash="a" * 64,
        raw_fields=[],
    )
    db.add(value)
    db.commit()
    return value


@pytest.fixture
def verified_job(db: Session, job_source: JobSource, raw_job_record: RawJobRecord) -> JobPosting:
    value = JobPosting(
        source_id=job_source.id,
        external_record_id="ext-1",
        raw_record_id=raw_job_record.id,
        status=JobPostingStatus.VERIFIED,
        company_name="Test Corp",
        title="Engineer",
        locations=[],
        recruitment_types=[],
        industries=[],
        apply_url="https://example.com/apply",
        mapper_version="v1",
        gui_eligible=True,
        review_version=1,
    )
    db.add(value)
    db.commit()
    return value


@pytest.fixture
def resume_draft(db: Session, user: User, verified_job: JobPosting) -> ResumeDraft:
    value = ResumeDraft(
        user_id=user.id,
        match_report_id="00000000-0000-0000-0000-000000000001",
        profile_version_id="00000000-0000-0000-0000-000000000002",
        target_job_id=verified_job.id,
        request_idempotency_key="ik-draft",
        request_hash="b" * 64,
        diffs={},
        status="approved",
        state_version=1,
    )
    db.add(value)
    db.commit()
    return value


@pytest.fixture
def approved_resume_version(db: Session, resume_draft: ResumeDraft) -> ApprovedResumeVersion:
    value = ApprovedResumeVersion(
        draft_id=resume_draft.id,
        profile_version_id="00000000-0000-0000-0000-000000000002",
        target_job_id=resume_draft.target_job_id,
        approved_facts={},
        approved_diffs={},
        approval_idempotency_key="ik-arv-elig",
        approved_by=resume_draft.user_id,
    )
    db.add(value)
    db.commit()
    return value


@pytest.fixture
def ready_attachment_pdf(
    db: Session, resume_draft: ResumeDraft, approved_resume_version: ApprovedResumeVersion, user: User
) -> ApprovedResumeAttachment:
    value = ApprovedResumeAttachment(
        draft_id=resume_draft.id,
        approved_resume_version_id=approved_resume_version.id,
        user_id=user.id,
        format="pdf",
        object_key="resumes/abc.pdf",
        content_type="application/pdf",
        plaintext_size=1000,
        encryption_version="v1",
        status="ready",
    )
    db.add(value)
    db.commit()
    return value


@pytest.fixture
def ready_attachment_docx(
    db: Session, resume_draft: ResumeDraft, approved_resume_version: ApprovedResumeVersion, user: User
) -> ApprovedResumeAttachment:
    value = ApprovedResumeAttachment(
        draft_id=resume_draft.id,
        approved_resume_version_id=approved_resume_version.id,
        user_id=user.id,
        format="docx",
        object_key="resumes/abc.docx",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        plaintext_size=800,
        encryption_version="v1",
        status="ready",
    )
    db.add(value)
    db.commit()
    return value


@pytest.fixture
def eligible_snapshot(
    db: Session,
    user: User,
    verified_job: JobPosting,
    approved_resume_version: ApprovedResumeVersion,
) -> ApplicationSnapshot:
    value = ApplicationSnapshot(
        user_id=user.id,
        job_id=verified_job.id,
        approved_resume_version_id=approved_resume_version.id,
        profile_version_id="00000000-0000-0000-0000-000000000002",
        job_snapshot={},
        profile_facts={},
        request_idempotency_key="ik-snap-1",
        request_hash="c" * 64,
        dynamic_answers={},
        local_sensitive_requirements={},
        attachment_ids=[],
        gui_eligible=True,
        job_status_at_snapshot="verified",
        job_review_version_at_snapshot=1,
        created_by=user.id,
        schema_version="v1",
    )
    db.add(value)
    db.commit()
    return value


# ── Tests: Gate 1 – snapshot exists + ownership ───────────────────────────────


class TestGateSnapshotOwnership:
    """Gate 1: snapshot must exist and belong to the requesting user."""

    def test_snapshot_not_found_returns_not_found(self, db: Session, user: User):
        can_create, reason = check_task_eligibility(
            db, user.id, "00000000-0000-0000-0000-000000000000"
        )
        assert can_create is False
        assert reason == "not_found"

    def test_wrong_user_returns_not_found(
        self, db: Session, user: User, other_user: User, eligible_snapshot: ApplicationSnapshot
    ):
        """A snapshot owned by a different user is treated as not_found."""
        can_create, reason = check_task_eligibility(
            db, other_user.id, eligible_snapshot.id
        )
        assert can_create is False
        assert reason == "not_found"


# ── Tests: Gate 2 – snapshot gui_eligible ─────────────────────────────────────


class TestGateSnapshotGuiEligible:
    """Gate 2: the snapshot-level gui_eligible flag must be True."""

    def test_snapshot_gui_not_eligible_returns_error(
        self, db: Session, user: User, verified_job: JobPosting, approved_resume_version: ApprovedResumeVersion
    ):
        snapshot = ApplicationSnapshot(
            user_id=user.id,
            job_id=verified_job.id,
            approved_resume_version_id=approved_resume_version.id,
            profile_version_id="00000000-0000-0000-0000-000000000002",
            job_snapshot={},
            profile_facts={},
            request_idempotency_key="ik-snap-gui",
            request_hash="d" * 64,
            dynamic_answers={},
            local_sensitive_requirements={},
            attachment_ids=[],
            gui_eligible=False,
            job_status_at_snapshot="verified",
            job_review_version_at_snapshot=verified_job.review_version,
            created_by=user.id,
            schema_version="v1",
        )
        db.add(snapshot)
        db.commit()

        can_create, reason = check_task_eligibility(db, user.id, snapshot.id)
        assert can_create is False
        assert reason == "snapshot_gui_not_eligible"


# ── Tests: Gate 3 – job exists and is verified ────────────────────────────────


class TestGateJobVerified:
    """Gate 3: the referenced job must exist and have status 'verified'."""

    def test_job_none_returns_job_expired(
        self, db: Session, user: User, eligible_snapshot: ApplicationSnapshot
    ):
        """Delete the job so the query returns None."""
        db.query(JobPosting).delete()
        db.commit()

        can_create, reason = check_task_eligibility(db, user.id, eligible_snapshot.id)
        assert can_create is False
        assert reason == "snapshot_job_expired"

    def test_job_not_verified_returns_job_expired(
        self,
        db: Session,
        user: User,
        job_source: JobSource,
        raw_job_record: RawJobRecord,
        eligible_snapshot: ApplicationSnapshot,
    ):
        db.query(JobPosting).delete()
        db.commit()
        pending_job = JobPosting(
            source_id=job_source.id,
            external_record_id="ext-pending",
            raw_record_id=raw_job_record.id,
            status=JobPostingStatus.PENDING_REVIEW,
            company_name="Pending Corp",
            title="Pending Role",
            locations=[],
            recruitment_types=[],
            industries=[],
            apply_url="https://example.com/apply",
            mapper_version="v1",
            gui_eligible=True,
            review_version=1,
        )
        db.add(pending_job)
        db.commit()
        eligible_snapshot.job_id = pending_job.id
        db.commit()

        can_create, reason = check_task_eligibility(db, user.id, eligible_snapshot.id)
        assert can_create is False
        assert reason == "snapshot_job_expired"


# ── Tests: Gate 4 – job gui_eligible ──────────────────────────────────────────


class TestGateJobGuiEligible:
    """Gate 4: the job-level gui_eligible flag must be True."""

    def test_job_gui_not_eligible_returns_error(
        self,
        db: Session,
        user: User,
        job_source: JobSource,
        raw_job_record: RawJobRecord,
        eligible_snapshot: ApplicationSnapshot,
    ):
        db.query(JobPosting).delete()
        db.commit()
        non_gui_job = JobPosting(
            source_id=job_source.id,
            external_record_id="ext-nongui",
            raw_record_id=raw_job_record.id,
            status=JobPostingStatus.VERIFIED,
            company_name="NoGui Corp",
            title="NoGui Role",
            locations=[],
            recruitment_types=[],
            industries=[],
            apply_url="https://example.com/apply",
            mapper_version="v1",
            gui_eligible=False,
            review_version=1,
        )
        db.add(non_gui_job)
        db.commit()
        eligible_snapshot.job_id = non_gui_job.id
        db.commit()

        can_create, reason = check_task_eligibility(db, user.id, eligible_snapshot.id)
        assert can_create is False
        assert reason == "snapshot_gui_not_eligible"


# ── Tests: Gate 5 – review version match ──────────────────────────────────────


class TestGateReviewVersion:
    """Gate 5: job.review_version must match snapshot.job_review_version_at_snapshot."""

    def test_version_mismatch_returns_stale(
        self,
        db: Session,
        user: User,
        job_source: JobSource,
        raw_job_record: RawJobRecord,
        eligible_snapshot: ApplicationSnapshot,
    ):
        db.query(JobPosting).delete()
        db.commit()
        bumped_job = JobPosting(
            source_id=job_source.id,
            external_record_id="ext-bumped",
            raw_record_id=raw_job_record.id,
            status=JobPostingStatus.VERIFIED,
            company_name="Bumped Corp",
            title="Bumped Role",
            locations=[],
            recruitment_types=[],
            industries=[],
            apply_url="https://example.com/apply",
            mapper_version="v1",
            gui_eligible=True,
            review_version=2,
        )
        db.add(bumped_job)
        db.commit()
        eligible_snapshot.job_id = bumped_job.id
        db.commit()

        can_create, reason = check_task_eligibility(db, user.id, eligible_snapshot.id)
        assert can_create is False
        assert reason == "snapshot_version_stale"


# ── Tests: Gate 6 – approved resume version exists ────────────────────────────


class TestGateApprovedResumeVersion:
    """Gate 6: the approved_resume_version referenced by the snapshot must exist."""

    def test_arv_missing_returns_stale(
        self,
        db: Session,
        user: User,
        eligible_snapshot: ApplicationSnapshot,
    ):
        """Delete the approved resume version so the join returns None."""
        db.query(ApprovedResumeVersion).delete()
        db.commit()

        can_create, reason = check_task_eligibility(db, user.id, eligible_snapshot.id)
        assert can_create is False
        assert reason == "snapshot_version_stale"


# ── Tests: Gate 7 – all attachments ready ─────────────────────────────────────


class TestGateAttachmentsReady:
    """Gate 7: all ApprovedResumeAttachment rows for the version must have status 'ready'."""

    def test_attachment_not_ready_returns_stale(
        self,
        db: Session,
        user: User,
        resume_draft: ResumeDraft,
        approved_resume_version: ApprovedResumeVersion,
        eligible_snapshot: ApplicationSnapshot,
    ):
        """Single attachment with status=error makes the gate fail."""
        bad_attachment = ApprovedResumeAttachment(
            draft_id=resume_draft.id,
            approved_resume_version_id=approved_resume_version.id,
            user_id=user.id,
            format="pdf",
            object_key="resumes/bad.pdf",
            content_type="application/pdf",
            plaintext_size=500,
            encryption_version="v1",
            status="error",
            error_code="render_failed",
        )
        db.add(bad_attachment)
        db.commit()

        can_create, reason = check_task_eligibility(db, user.id, eligible_snapshot.id)
        assert can_create is False
        assert reason == "snapshot_version_stale"

    def test_some_attachments_not_ready_returns_stale(
        self,
        db: Session,
        user: User,
        resume_draft: ResumeDraft,
        approved_resume_version: ApprovedResumeVersion,
        ready_attachment_pdf: ApprovedResumeAttachment,
        eligible_snapshot: ApplicationSnapshot,
    ):
        """Mix of ready + non-ready attachments fails."""
        pending_attachment = ApprovedResumeAttachment(
            draft_id=resume_draft.id,
            approved_resume_version_id=approved_resume_version.id,
            user_id=user.id,
            format="docx",
            object_key="resumes/pending.docx",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            plaintext_size=600,
            encryption_version="v1",
            status="pending",
        )
        db.add(pending_attachment)
        db.commit()

        can_create, reason = check_task_eligibility(db, user.id, eligible_snapshot.id)
        assert can_create is False
        assert reason == "snapshot_version_stale"


# ── Tests: happy path ─────────────────────────────────────────────────────────


class TestEligible:
    """All gates pass — task creation should be allowed."""

    def test_all_gates_pass(
        self,
        db: Session,
        user: User,
        eligible_snapshot: ApplicationSnapshot,
        ready_attachment_pdf: ApprovedResumeAttachment,
        ready_attachment_docx: ApprovedResumeAttachment,
    ):
        can_create, reason = check_task_eligibility(db, user.id, eligible_snapshot.id)
        assert can_create is True
        assert reason is None

    def test_no_attachments_allowed(
        self,
        db: Session,
        user: User,
        eligible_snapshot: ApplicationSnapshot,
    ):
        """Zero attachments is valid — no attachment fails the ready check."""
        can_create, reason = check_task_eligibility(db, user.id, eligible_snapshot.id)
        assert can_create is True
        assert reason is None
