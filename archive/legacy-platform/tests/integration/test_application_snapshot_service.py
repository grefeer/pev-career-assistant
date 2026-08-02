"""Integration tests for ApplicationSnapshotService.

Scenarios covered:

  ``create_snapshot`` (12 scenarios):
    1.  Happy path — creates snapshot with correct fields
    2.  Idempotency — same key + same data returns existing snapshot
    3.  Idempotency conflict — same key + different data raises ``ValueError``
    4.  Job not found — raises ``ValueError``
    5.  ARV not found — raises ``ValueError``
    6.  Cross-user ARV — raises ``ValueError``
    7.  Invalid dynamic answers (non-sensitive field missing) — raises
        ``SnapshotValidationError``
    8.  Invalid dynamic answers (local-sensitive field) — raises
        ``SnapshotValidationError``
    9.  Invalid local-sensitive requirements (plaintext value) — raises
        ``SnapshotValidationError``
   10.  Invalid local-sensitive requirements (wrong classification) — raises
        ``SnapshotValidationError``
   11.  No attachments — raises ``SnapshotValidationError``
   12.  Unique-constraint race — rollback + reload + return existing

  ``create_application_task`` (6 scenarios):
   13.  Happy path — creates task with ``CREATED`` status
   14.  Snapshot not found — raises ``ValueError``
   15.  Cross-user snapshot — raises ``ValueError``
   16.  Idempotency — same key returns existing task
   17.  Eligibility failure — ``gui_eligible=False`` raises ``ValueError``
   18.  Task fields correct — task_kind, snapshot_id, etc.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import pytest
from sqlalchemy.orm import Session

from backend.app.db.models import (
    AnalysisSession,
    ApplicationSnapshot,
    ApplicationTaskStatus,
    ApprovedResumeAttachment,
    ApprovedResumeVersion,
    ConfirmedProfileVersion,
    JobPosting,
    JobPostingStatus,
    JobSource,
    JobSourceProvider,
    JobSourceLink,
    JobVerification,
    MatchReport,
    Profile,
    RawJobRecord,
    ResumeDraft,
    User,
)
from backend.app.services.application_snapshot_service import (
    create_application_task,
    create_snapshot,
)
from backend.app.services.snapshot_validators import (
    SnapshotValidationError,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SNAPSHOT_SCHEMA_VERSION = "1.0"

SAMPLE_FACTS: dict[str, Any] = {
    "name": "Alice Zhang",
    "email": "alice@example.com",
    "skills": ["Python", "TypeScript"],
    "work_experience": [
        {
            "company": "TechCorp",
            "title": "Senior Engineer",
            "start_date": "2018-03",
            "end_date": "Present",
        },
    ],
}

SAMPLE_EVIDENCE_REFS: dict[str, list[str]] = {
    "skills": ["ev-skill-001"],
    "work_experience": ["ev-work-001"],
}

SAMPLE_DIFFS: list[dict[str, Any]] = [
    {
        "op": "rephrase",
        "section": "work_experience",
        "fact_ref": "work_experience",
        "before": "Led a team",
        "after": "Directed a team",
        "evidence_ids": ["ev-work-001"],
    },
]

SAMPLE_JOB_SNAPSHOT: dict[str, Any] = {
    "company_name": "TestCorp",
    "title": "Software Engineer",
    "description_text": "We need Python experts.",
}

VALID_DYNAMIC_ANSWERS: list[dict[str, Any]] = [
    {
        "field_key": "expected_salary",
        "classification": "non_sensitive",
        "answer": "10000-15000",
    },
    {
        "field_key": "expected_city",
        "classification": "non_sensitive",
        "answer": "Shanghai",
    },
]

VALID_LOCAL_SENSITIVE_REQS: list[dict[str, Any]] = [
    {
        "field_key": "id_number",
        "category": "government_id",
        "local_reference": "lsr:v1:abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
    },
]

SAMPLE_ATTACHMENT_OBJECT_KEY = "resumes/test-user/test-draft/pdf"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def job_source(db_session: Session) -> JobSource:
    src = JobSource(
        id=str(uuid.uuid4()),
        source_key="snapshot-test-source",
        provider=JobSourceProvider.TENCENT_SMARTSHEET,
        name="Snapshot Test Source",
        file_id="f1",
        sheet_id="s1",
        mapper_version="v1",
        enabled=True,
    )
    db_session.add(src)
    db_session.flush()
    return src


@pytest.fixture
def raw_job_record(db_session: Session, job_source: JobSource) -> RawJobRecord:
    rec = RawJobRecord(
        id=str(uuid.uuid4()),
        source_id=job_source.id,
        external_record_id="ext-snapshot-1",
        payload_hash="a" * 64,
        raw_fields=[],
    )
    db_session.add(rec)
    db_session.flush()
    return rec


@pytest.fixture
def verified_job(
    db_session: Session,
    job_source: JobSource,
    raw_job_record: RawJobRecord,
) -> JobPosting:
    job = JobPosting(
        id=str(uuid.uuid4()),
        source_id=job_source.id,
        external_record_id="ext-snapshot-1",
        raw_record_id=raw_job_record.id,
        status=JobPostingStatus.VERIFIED,
        company_name="TestCorp",
        title="Software Engineer",
        description_text="We need Python experts.",
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
    db_session.add(job)
    db_session.flush()

    link = JobSourceLink(
        id=str(uuid.uuid4()),
        job_id=job.id,
        source_type="tencent_smartsheet",
        source_id=job_source.id,
        source_record_ref="ext-snapshot-1",
        submission_id=None,
    )
    db_session.add(link)
    db_session.flush()

    return job


@pytest.fixture
def profile_and_version(
    db_session: Session, test_user: User,
) -> tuple[Profile, ConfirmedProfileVersion]:
    profile = Profile(
        id=str(uuid.uuid4()),
        user_id=test_user.id,
        version=0,
        local_sensitive_references={},
    )
    db_session.add(profile)
    db_session.flush()

    cv = ConfirmedProfileVersion(
        id=str(uuid.uuid4()),
        profile_id=profile.id,
        version_number=1,
        aggregate_version=1,
        facts_snapshot=SAMPLE_FACTS,
        evidence_refs=SAMPLE_EVIDENCE_REFS,
        local_sensitive_references={},
    )
    db_session.add(cv)
    db_session.flush()
    return profile, cv


@pytest.fixture
def analysis_session(
    db_session: Session, test_user: User,
) -> AnalysisSession:
    s = AnalysisSession(
        id=str(uuid.uuid4()),
        user_id=test_user.id,
        thread_id=f"thread-{uuid.uuid4().hex[:8]}",
        label="Snapshot Test Session",
        activated_at=datetime.now(timezone.utc),
    )
    db_session.add(s)
    db_session.flush()
    return s


@pytest.fixture
def completed_match_report(
    db_session: Session,
    test_user: User,
    analysis_session: AnalysisSession,
    verified_job: JobPosting,
    profile_and_version: tuple[Profile, ConfirmedProfileVersion],
) -> MatchReport:
    _, cv = profile_and_version
    job = verified_job

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
    db_session.add(jv)
    db_session.flush()

    mr = MatchReport(
        id=str(uuid.uuid4()),
        user_id=test_user.id,
        analysis_session_id=analysis_session.id,
        job_id=job.id,
        job_verification_id=jv.id,
        job_snapshot=SAMPLE_JOB_SNAPSHOT,
        profile_version_id=cv.id,
        request_idempotency_key="mr-snapshot-ik",
        request_hash="mr-snapshot-hash",
        status="completed",
        score=85,
        scoring_rule_version="1.0",
        model_version="1.0",
        prompt_version="1.0",
        output_schema_version="1.0",
        strengths=[{"requirement_id": "req-1", "verdict": "satisfied"}],
        gaps=[],
        unknowns=[],
        risks=[],
        recommendation={"text": "Proceed."},
        completed_at=datetime.now(timezone.utc),
    )
    db_session.add(mr)
    db_session.flush()
    return mr


@pytest.fixture
def draft_in_draft_status(
    db_session: Session,
    test_user: User,
    completed_match_report: MatchReport,
) -> ResumeDraft:
    d = ResumeDraft(
        id=str(uuid.uuid4()),
        user_id=test_user.id,
        match_report_id=completed_match_report.id,
        profile_version_id=completed_match_report.profile_version_id,
        target_job_id=completed_match_report.job_id,
        request_idempotency_key="arv-draft-ik",
        request_hash="arv-draft-hash",
        diffs=SAMPLE_DIFFS,
        status="draft",
        state_version=0,
    )
    db_session.add(d)
    db_session.flush()
    return d


@pytest.fixture
def approved_resume_version(
    db_session: Session,
    test_user: User,
    draft_in_draft_status: ResumeDraft,
    profile_and_version: tuple[Profile, ConfirmedProfileVersion],
) -> ApprovedResumeVersion:
    _, cv = profile_and_version
    arv = ApprovedResumeVersion(
        id=str(uuid.uuid4()),
        draft_id=draft_in_draft_status.id,
        profile_version_id=cv.id,
        target_job_id=draft_in_draft_status.target_job_id,
        approved_facts=SAMPLE_FACTS,
        approved_diffs=SAMPLE_DIFFS,
        approval_idempotency_key=str(uuid.uuid4()),
        approved_by=test_user.id,
    )
    db_session.add(arv)
    db_session.flush()
    return arv


@pytest.fixture
def attachment_for_arv(
    db_session: Session,
    test_user: User,
    draft_in_draft_status: ResumeDraft,
    approved_resume_version: ApprovedResumeVersion,
) -> ApprovedResumeAttachment:
    att = ApprovedResumeAttachment(
        id=str(uuid.uuid4()),
        draft_id=draft_in_draft_status.id,
        approved_resume_version_id=approved_resume_version.id,
        user_id=test_user.id,
        format="pdf",
        object_key=SAMPLE_ATTACHMENT_OBJECT_KEY,
        content_type="application/pdf",
        plaintext_size=4096,
        encryption_version="v1",
        status="ready",
    )
    db_session.add(att)
    db_session.flush()
    return att


# ---------------------------------------------------------------------------
# Tests: create_snapshot
# ---------------------------------------------------------------------------


class TestCreateSnapshot:
    """``create_snapshot`` test cases."""

    # -- Scenario 1: Happy path ------------------------------------------------

    def test_happy_path(
        self,
        db_session: Session,
        test_user: User,
        verified_job: JobPosting,
        approved_resume_version: ApprovedResumeVersion,
        attachment_for_arv: ApprovedResumeAttachment,
    ) -> None:
        """Creates snapshot with correct fields."""
        snapshot = create_snapshot(
            db=db_session,
            user_id=test_user.id,
            job_id=verified_job.id,
            approved_resume_version_id=approved_resume_version.id,
            dynamic_answers=VALID_DYNAMIC_ANSWERS,
            local_sensitive_requirements=VALID_LOCAL_SENSITIVE_REQS,
            idempotency_key="snapshot-happy-1",
        )

        assert snapshot.user_id == test_user.id
        assert snapshot.job_id == verified_job.id
        assert snapshot.approved_resume_version_id == approved_resume_version.id
        assert snapshot.gui_eligible is True
        assert snapshot.job_status_at_snapshot == "verified"
        assert snapshot.job_review_version_at_snapshot == 1
        assert snapshot.created_by == test_user.id
        assert snapshot.schema_version == SNAPSHOT_SCHEMA_VERSION
        assert snapshot.profile_version_id == approved_resume_version.profile_version_id
        assert snapshot.job_snapshot is not None
        assert snapshot.job_snapshot["company_name"] == "TestCorp"
        assert snapshot.profile_facts == SAMPLE_FACTS
        assert snapshot.dynamic_answers == VALID_DYNAMIC_ANSWERS
        assert snapshot.local_sensitive_requirements == VALID_LOCAL_SENSITIVE_REQS
        assert snapshot.attachment_ids == [attachment_for_arv.id]

    # -- Scenario 2: Idempotency -----------------------------------------------

    def test_idempotency_returns_existing(
        self,
        db_session: Session,
        test_user: User,
        verified_job: JobPosting,
        approved_resume_version: ApprovedResumeVersion,
        attachment_for_arv: ApprovedResumeAttachment,
    ) -> None:
        """Same idempotency key + same data returns existing snapshot."""
        ik = "snapshot-idem-1"
        first = create_snapshot(
            db=db_session,
            user_id=test_user.id,
            job_id=verified_job.id,
            approved_resume_version_id=approved_resume_version.id,
            dynamic_answers=VALID_DYNAMIC_ANSWERS,
            local_sensitive_requirements=VALID_LOCAL_SENSITIVE_REQS,
            idempotency_key=ik,
        )
        second = create_snapshot(
            db=db_session,
            user_id=test_user.id,
            job_id=verified_job.id,
            approved_resume_version_id=approved_resume_version.id,
            dynamic_answers=VALID_DYNAMIC_ANSWERS,
            local_sensitive_requirements=VALID_LOCAL_SENSITIVE_REQS,
            idempotency_key=ik,
        )

        assert second.id == first.id
        assert second.gui_eligible == first.gui_eligible

    # -- Scenario 3: Idempotency conflict --------------------------------------

    def test_idempotency_conflict(
        self,
        db_session: Session,
        test_user: User,
        verified_job: JobPosting,
        approved_resume_version: ApprovedResumeVersion,
        attachment_for_arv: ApprovedResumeAttachment,
    ) -> None:
        """Same key + different data raises ValueError."""
        ik = "snapshot-conflict-1"
        create_snapshot(
            db=db_session,
            user_id=test_user.id,
            job_id=verified_job.id,
            approved_resume_version_id=approved_resume_version.id,
            dynamic_answers=VALID_DYNAMIC_ANSWERS,
            local_sensitive_requirements=VALID_LOCAL_SENSITIVE_REQS,
            idempotency_key=ik,
        )

        with pytest.raises(ValueError, match="idempotency_key_conflict"):
            create_snapshot(
                db=db_session,
                user_id=test_user.id,
                job_id=verified_job.id,
                approved_resume_version_id=approved_resume_version.id,
                dynamic_answers=[],  # different data -> different hash
                local_sensitive_requirements=VALID_LOCAL_SENSITIVE_REQS,
                idempotency_key=ik,
            )

    # -- Scenario 4: Job not found ---------------------------------------------

    def test_job_not_found(
        self,
        db_session: Session,
        test_user: User,
        approved_resume_version: ApprovedResumeVersion,
        attachment_for_arv: ApprovedResumeAttachment,
    ) -> None:
        """Invalid job_id raises ValueError."""
        with pytest.raises(ValueError, match="job_not_found"):
            create_snapshot(
                db=db_session,
                user_id=test_user.id,
                job_id=str(uuid.uuid4()),
                approved_resume_version_id=approved_resume_version.id,
                dynamic_answers=VALID_DYNAMIC_ANSWERS,
                local_sensitive_requirements=VALID_LOCAL_SENSITIVE_REQS,
                idempotency_key="snapshot-no-job-1",
            )

    # -- Scenario 5: ARV not found ---------------------------------------------

    def test_arv_not_found(
        self,
        db_session: Session,
        test_user: User,
        verified_job: JobPosting,
    ) -> None:
        """Invalid ARV id raises ValueError."""
        with pytest.raises(ValueError, match="approved_resume_version_not_found"):
            create_snapshot(
                db=db_session,
                user_id=test_user.id,
                job_id=verified_job.id,
                approved_resume_version_id=str(uuid.uuid4()),
                dynamic_answers=VALID_DYNAMIC_ANSWERS,
                local_sensitive_requirements=VALID_LOCAL_SENSITIVE_REQS,
                idempotency_key="snapshot-no-arv-1",
            )

    # -- Scenario 6: Cross-user ARV --------------------------------------------

    def test_cross_user_arv(
        self,
        db_session: Session,
        test_user: User,
        verified_job: JobPosting,
        draft_in_draft_status: ResumeDraft,
        profile_and_version: tuple[Profile, ConfirmedProfileVersion],
    ) -> None:
        """ARV owned by another user raises ValueError."""
        other_user = User(
            id=str(uuid.uuid4()),
            account="arv-other-tester",
            nickname="Other Tester",
            password_hash="argon2-placeholder",
        )
        db_session.add(other_user)
        db_session.flush()

        _, cv = profile_and_version

        # Create ARV for other_user
        other_draft = ResumeDraft(
            id=str(uuid.uuid4()),
            user_id=other_user.id,
            match_report_id=draft_in_draft_status.match_report_id,
            profile_version_id=cv.id,
            target_job_id=verified_job.id,
            request_idempotency_key="other-arv-ik",
            request_hash="other-arv-hash",
            diffs=SAMPLE_DIFFS,
            status="draft",
            state_version=0,
        )
        db_session.add(other_draft)
        db_session.flush()

        other_arv = ApprovedResumeVersion(
            id=str(uuid.uuid4()),
            draft_id=other_draft.id,
            profile_version_id=cv.id,
            target_job_id=verified_job.id,
            approved_facts=SAMPLE_FACTS,
            approved_diffs=SAMPLE_DIFFS,
            approval_idempotency_key=str(uuid.uuid4()),
            approved_by=other_user.id,
        )
        db_session.add(other_arv)
        db_session.flush()

        # test_user tries to use other_user's ARV
        with pytest.raises(ValueError, match="approved_resume_version_not_found"):
            create_snapshot(
                db=db_session,
                user_id=test_user.id,
                job_id=verified_job.id,
                approved_resume_version_id=other_arv.id,
                dynamic_answers=VALID_DYNAMIC_ANSWERS,
                local_sensitive_requirements=VALID_LOCAL_SENSITIVE_REQS,
                idempotency_key="snapshot-cross-arv-1",
            )

    # -- Scenario 7: Invalid dynamic answers (missing classification) ----------

    def test_invalid_dynamic_answers_missing_classification(
        self,
        db_session: Session,
        test_user: User,
        verified_job: JobPosting,
        approved_resume_version: ApprovedResumeVersion,
        attachment_for_arv: ApprovedResumeAttachment,
    ) -> None:
        """Dynamic answer missing classification raises SnapshotValidationError."""
        bad_answers = [
            {"field_key": "expected_salary", "answer": "10000"},
            # missing "classification"
        ]
        with pytest.raises(SnapshotValidationError, match="missing 'classification' field"):
            create_snapshot(
                db=db_session,
                user_id=test_user.id,
                job_id=verified_job.id,
                approved_resume_version_id=approved_resume_version.id,
                dynamic_answers=bad_answers,
                local_sensitive_requirements=VALID_LOCAL_SENSITIVE_REQS,
                idempotency_key="snapshot-bad-da-1",
            )

    # -- Scenario 8: Invalid dynamic answers (local-sensitive field) -----------

    def test_invalid_dynamic_answers_local_sensitive(
        self,
        db_session: Session,
        test_user: User,
        verified_job: JobPosting,
        approved_resume_version: ApprovedResumeVersion,
        attachment_for_arv: ApprovedResumeAttachment,
    ) -> None:
        """Dynamic answer with local-sensitive field raises SnapshotValidationError."""
        bad_answers = [
            {
                "field_key": "id_number",
                "classification": "non_sensitive",
                "answer": "123456",
            },
        ]
        with pytest.raises(
            SnapshotValidationError, match="classifies as 'local_sensitive'",
        ):
            create_snapshot(
                db=db_session,
                user_id=test_user.id,
                job_id=verified_job.id,
                approved_resume_version_id=approved_resume_version.id,
                dynamic_answers=bad_answers,
                local_sensitive_requirements=VALID_LOCAL_SENSITIVE_REQS,
                idempotency_key="snapshot-bad-da-2",
            )

    # -- Scenario 9: Invalid local-sensitive (plaintext value) -----------------

    def test_invalid_lsr_plaintext(
        self,
        db_session: Session,
        test_user: User,
        verified_job: JobPosting,
        approved_resume_version: ApprovedResumeVersion,
        attachment_for_arv: ApprovedResumeAttachment,
    ) -> None:
        """LSR with plaintext value raises SnapshotValidationError."""
        bad_reqs = [
            {
                "field_key": "id_number",
                "category": "government_id",
                "local_reference": "lsr:v1:abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
                "value": "420106199001011234",  # plaintext!
            },
        ]
        with pytest.raises(
            SnapshotValidationError, match="must not contain plaintext value",
        ):
            create_snapshot(
                db=db_session,
                user_id=test_user.id,
                job_id=verified_job.id,
                approved_resume_version_id=approved_resume_version.id,
                dynamic_answers=VALID_DYNAMIC_ANSWERS,
                local_sensitive_requirements=bad_reqs,
                idempotency_key="snapshot-bad-lsr-1",
            )

    # -- Scenario 10: Invalid LSR (wrong classification) -----------------------

    def test_invalid_lsr_wrong_classification(
        self,
        db_session: Session,
        test_user: User,
        verified_job: JobPosting,
        approved_resume_version: ApprovedResumeVersion,
        attachment_for_arv: ApprovedResumeAttachment,
    ) -> None:
        """LSR field that is not local-sensitive raises SnapshotValidationError."""
        bad_reqs = [
            {
                "field_key": "email",  # NON_SENSITIVE, not LOCAL_SENSITIVE
                "category": "government_id",
                "local_reference": "lsr:v1:abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
            },
        ]
        with pytest.raises(
            SnapshotValidationError, match="classifies as 'non_sensitive'",
        ):
            create_snapshot(
                db=db_session,
                user_id=test_user.id,
                job_id=verified_job.id,
                approved_resume_version_id=approved_resume_version.id,
                dynamic_answers=VALID_DYNAMIC_ANSWERS,
                local_sensitive_requirements=bad_reqs,
                idempotency_key="snapshot-bad-lsr-2",
            )

    # -- Scenario 11: No attachments --------------------------------------------

    def test_no_attachments_raises_error(
        self,
        db_session: Session,
        test_user: User,
        verified_job: JobPosting,
        approved_resume_version: ApprovedResumeVersion,
    ) -> None:
        """Missing attachments raises SnapshotValidationError (no attachment_ids)."""
        # No attachment fixture added, so attachment_ids will be empty
        with pytest.raises(
            SnapshotValidationError, match="at least one attachment",
        ):
            create_snapshot(
                db=db_session,
                user_id=test_user.id,
                job_id=verified_job.id,
                approved_resume_version_id=approved_resume_version.id,
                dynamic_answers=VALID_DYNAMIC_ANSWERS,
                local_sensitive_requirements=VALID_LOCAL_SENSITIVE_REQS,
                idempotency_key="snapshot-no-att-1",
            )

    # -- Scenario 12: Unique-constraint race ------------------------------------

    def test_unique_constraint_race(
        self,
        db_session: Session,
        test_user: User,
        verified_job: JobPosting,
        approved_resume_version: ApprovedResumeVersion,
        attachment_for_arv: ApprovedResumeAttachment,
    ) -> None:
        """Concurrent duplicate insertion returns existing snapshot."""
        ik = "snapshot-race-1"

        first = create_snapshot(
            db=db_session,
            user_id=test_user.id,
            job_id=verified_job.id,
            approved_resume_version_id=approved_resume_version.id,
            dynamic_answers=VALID_DYNAMIC_ANSWERS,
            local_sensitive_requirements=VALID_LOCAL_SENSITIVE_REQS,
            idempotency_key=ik,
        )

        # Simulate a concurrent creation by inserting the same snapshot
        # directly (bypassing the service) with the same key
        from sqlalchemy.exc import IntegrityError

        dup_snapshot = ApplicationSnapshot(
            id=str(uuid.uuid4()),
            user_id=test_user.id,
            job_id=verified_job.id,
            approved_resume_version_id=approved_resume_version.id,
            profile_version_id=approved_resume_version.profile_version_id,
            job_snapshot={"company_name": "TestCorp"},
            profile_facts=SAMPLE_FACTS,
            request_idempotency_key=ik,
            request_hash="different-hash",
            dynamic_answers=VALID_DYNAMIC_ANSWERS,
            local_sensitive_requirements=VALID_LOCAL_SENSITIVE_REQS,
            attachment_ids=[attachment_for_arv.id],
            gui_eligible=True,
            job_status_at_snapshot="verified",
            job_review_version_at_snapshot=1,
            created_by=test_user.id,
            schema_version=SNAPSHOT_SCHEMA_VERSION,
        )
        db_session.add(dup_snapshot)
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()

        # Now call the service again with matching hash
        second = create_snapshot(
            db=db_session,
            user_id=test_user.id,
            job_id=verified_job.id,
            approved_resume_version_id=approved_resume_version.id,
            dynamic_answers=VALID_DYNAMIC_ANSWERS,
            local_sensitive_requirements=VALID_LOCAL_SENSITIVE_REQS,
            idempotency_key=ik,
        )

        assert second.id == first.id


# ---------------------------------------------------------------------------
# Tests: create_application_task
# ---------------------------------------------------------------------------


class TestCreateApplicationTask:
    """``create_application_task`` test cases."""

    @pytest.fixture
    def snapshot(
        self,
        db_session: Session,
        test_user: User,
        verified_job: JobPosting,
        approved_resume_version: ApprovedResumeVersion,
        attachment_for_arv: ApprovedResumeAttachment,
    ) -> ApplicationSnapshot:
        return create_snapshot(
            db=db_session,
            user_id=test_user.id,
            job_id=verified_job.id,
            approved_resume_version_id=approved_resume_version.id,
            dynamic_answers=VALID_DYNAMIC_ANSWERS,
            local_sensitive_requirements=VALID_LOCAL_SENSITIVE_REQS,
            idempotency_key="task-snapshot-1",
        )

    # -- Scenario 13: Happy path ------------------------------------------------

    def test_happy_path(
        self,
        db_session: Session,
        test_user: User,
        snapshot: ApplicationSnapshot,
    ) -> None:
        """Creates an ApplicationTask with CREATED status."""
        task = create_application_task(
            db=db_session,
            user_id=test_user.id,
            snapshot_id=snapshot.id,
            idempotency_key="task-happy-1",
        )

        assert task.status == ApplicationTaskStatus.CREATED
        assert task.snapshot_id == snapshot.id
        assert task.user_id == test_user.id
        assert task.target_job_id == snapshot.job_id
        assert task.task_kind == "application"
        assert task.state_version == 0
        assert task.device_id is None

    # -- Scenario 14: Snapshot not found ---------------------------------------

    def test_snapshot_not_found(
        self,
        db_session: Session,
        test_user: User,
    ) -> None:
        """Non-existent snapshot raises ValueError."""
        with pytest.raises(ValueError, match="snapshot_not_found"):
            create_application_task(
                db=db_session,
                user_id=test_user.id,
                snapshot_id=str(uuid.uuid4()),
                idempotency_key="task-no-snap-1",
            )

    # -- Scenario 15: Cross-user snapshot --------------------------------------

    def test_cross_user_snapshot(
        self,
        db_session: Session,
        test_user: User,
        snapshot: ApplicationSnapshot,
    ) -> None:
        """Other user's snapshot raises ValueError."""
        other_user = User(
            id=str(uuid.uuid4()),
            account="snapshot-other-tester",
            nickname="Other Tester",
            password_hash="argon2-placeholder",
        )
        db_session.add(other_user)
        db_session.flush()

        with pytest.raises(ValueError, match="snapshot_not_found"):
            create_application_task(
                db=db_session,
                user_id=other_user.id,
                snapshot_id=snapshot.id,
                idempotency_key="task-cross-1",
            )

    # -- Scenario 16: Idempotency -----------------------------------------------

    def test_idempotency_returns_existing(
        self,
        db_session: Session,
        test_user: User,
        snapshot: ApplicationSnapshot,
    ) -> None:
        """Same idempotency key returns existing task."""
        ik = "task-idem-1"

        first = create_application_task(
            db=db_session,
            user_id=test_user.id,
            snapshot_id=snapshot.id,
            idempotency_key=ik,
        )
        second = create_application_task(
            db=db_session,
            user_id=test_user.id,
            snapshot_id=snapshot.id,
            idempotency_key=ik,
        )

        assert second.id == first.id
        assert second.status == ApplicationTaskStatus.CREATED

    # -- Scenario 17: Eligibility failure ---------------------------------------

    def test_eligibility_failure(
        self,
        db_session: Session,
        test_user: User,
        snapshot: ApplicationSnapshot,
        verified_job: JobPosting,
    ) -> None:
        """Disabling gui_eligible on the job raises ValueError."""
        # Snapshot already created with gui_eligible=True at snapshot time.
        # But the eligibility check also verifies the job's current state.
        # Set the job to not gui_eligible
        verified_job.gui_eligible = False
        db_session.flush()

        with pytest.raises(ValueError, match="task_not_eligible"):
            create_application_task(
                db=db_session,
                user_id=test_user.id,
                snapshot_id=snapshot.id,
                idempotency_key="task-no-elig-1",
            )

    # -- Scenario 18: Task fields correctness -----------------------------------

    def test_task_fields_correct(
        self,
        db_session: Session,
        test_user: User,
        snapshot: ApplicationSnapshot,
    ) -> None:
        """Task has correct kind, snapshot_id, and status."""
        task = create_application_task(
            db=db_session,
            user_id=test_user.id,
            snapshot_id=snapshot.id,
            idempotency_key="task-fields-1",
            device_id=str(uuid.uuid4()),
        )

        assert task.task_kind == "application"
        assert task.snapshot_id == snapshot.id
        assert task.status == ApplicationTaskStatus.CREATED
        assert task.device_id is not None
        assert task.state_version == 0
        assert task.request_idempotency_key == "task-fields-1"
