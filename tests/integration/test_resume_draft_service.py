"""Integration tests for ResumeDraftService.

Scenarios from the plan (min 10 required, 12 implemented):

  1. Completed MatchReport -> draft created successfully
  2. Failed/incomplete MatchReport -> ValueError
  3. Invalid op type -> DraftValidationError -> draft finalized as failed
  4. Non-existent fact_ref -> failed
  5. approve: generates ApprovedResumeVersion + 2 attachments (PDF/DOCX)
  6. approve: object_store.put fails -> compensation, draft stays ``draft``
  7. approve: PDF succeeds, DOCX fails -> both objects compensated
  8. approve: objects succeed, DB finalize (IntegrityError) -> compensation
  9. Duplicate approve -> returns existing ARV (idempotency)
  10. Same idempotency key -> returns existing draft
  11. Stale expected_version on approve/reject -> StaleDraftVersionError
  12. Cross-user draft access on approve -> ValueError not_found
"""

from __future__ import annotations

import base64
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from backend.app.db.models import (
    AnalysisSession,
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
    ResumeDraft,
    User,
)
from backend.app.repositories import drafts as drafts_repo
from backend.app.repositories.drafts import StaleDraftVersionError
from backend.app.services.resume_draft_service import ResumeDraftService
from backend.app.services.storage import ENCRYPTION_VERSION, EncryptedObjectStore

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MODEL_VERSION = "1.0"
PROMPT_VERSION = "1.0"
OUTPUT_SCHEMA_VERSION = "1.0"
SCORING_RULE_VERSION = "1.0"

SAMPLE_FACTS: dict = {
    "name": "Alice Zhang",
    "contact": {
        "email": "alice@example.com",
        "phone": "+1-555-0100",
        "location": "San Francisco, CA",
    },
    "summary": "Experienced software engineer.",
    "education": [
        {
            "school": "Stanford University",
            "degree": "M.S.",
            "field": "Computer Science",
            "start_date": "2014-09",
            "end_date": "2016-06",
        },
    ],
    "skills": [
        {"name": "Python", "level": "Expert"},
        {"name": "TypeScript", "level": "Advanced"},
    ],
    "work_experience": [
        {
            "company": "TechCorp",
            "title": "Senior Engineer",
            "location": "San Francisco, CA",
            "start_date": "2018-03",
            "end_date": "Present",
            "highlights": ["Led a team of 5 engineers"],
        },
    ],
    "projects": [],
    "awards": [],
    "certifications": [],
}

SAMPLE_EVIDENCE_REFS: dict[str, list[str]] = {
    "education": ["ev-edu-001"],
    "skills": ["ev-skill-001", "ev-skill-002"],
    "work_experience": ["ev-work-001"],
}

SAMPLE_DIFFS: list[dict] = [
    {
        "op": "rephrase",
        "section": "work_experience",
        "fact_ref": "work_experience",
        "before": "Led a team of 5 engineers",
        "after": "Directed a cross-functional team of 5 engineers",
        "evidence_ids": ["ev-work-001"],
    },
    {
        "op": "omit",
        "section": "education",
        "fact_ref": "education",
        "before": "M.S. in Computer Science",
        "evidence_ids": ["ev-edu-001"],
    },
]

SAMPLE_JOB_SNAPSHOT: dict = {
    "company_name": "TestCorp",
    "title": "Software Engineer",
    "description_text": "We need Python experts.",
}


# ---------------------------------------------------------------------------
# In-memory mock blob store (same pattern as test_attachment_service)
# ---------------------------------------------------------------------------


class _MockBlobStore:
    def __init__(self) -> None:
        self._data: dict[str, bytes] = {}
        self._metadata: dict[str, dict] = {}

    def put_bytes(
        self,
        *,
        key: str,
        body: bytes,
        content_type: str,
        metadata: dict[str, str],
    ) -> None:
        self._data[key] = body
        self._metadata[key] = {
            "content_type": content_type,
            "metadata": metadata,
        }

    def get_bytes(self, *, key: str) -> bytes:
        if key not in self._data:
            raise FileNotFoundError(key)
        return self._data[key]

    def delete(self, *, key: str) -> None:
        self._data.pop(key, None)
        self._metadata.pop(key, None)

    def head(self, *, key: str) -> dict:
        if key not in self._metadata:
            raise FileNotFoundError(key)
        return {
            "ContentType": self._metadata[key]["content_type"],
            "Metadata": self._metadata[key]["metadata"],
            "ContentLength": len(self._data.get(key, b"")),
        }

    def ensure_bucket(self) -> None:
        pass

    def check_bucket(self) -> None:
        pass


@pytest.fixture
def object_store() -> EncryptedObjectStore:
    key = base64.b64encode(bytes(range(32))).decode("ascii")
    return EncryptedObjectStore(_MockBlobStore(), key)


# ---------------------------------------------------------------------------
# Fixtures: DB entities
# ---------------------------------------------------------------------------


@pytest.fixture
def user(db_session: Session) -> User:
    u = User(
        id=str(uuid.uuid4()),
        account="draft-tester",
        nickname="Draft Tester",
        password_hash="argon2-placeholder",
    )
    db_session.add(u)
    db_session.flush()
    return u


@pytest.fixture
def analysis_session(db_session: Session, user: User) -> AnalysisSession:
    s = AnalysisSession(
        id=str(uuid.uuid4()),
        user_id=user.id,
        thread_id=f"thread-{uuid.uuid4().hex[:8]}",
        label="Test Session",
        activated_at=datetime.now(timezone.utc),
    )
    db_session.add(s)
    db_session.flush()
    return s


@pytest.fixture
def job_source(db_session: Session) -> JobSource:
    src = JobSource(
        id=str(uuid.uuid4()),
        source_key="draft-test-source",
        provider=JobSourceProvider.TENCENT_SMARTSHEET,
        name="Draft Test Source",
        file_id="f1",
        sheet_id="s1",
        mapper_version="v1",
        enabled=True,
    )
    db_session.add(src)
    db_session.flush()
    return src


@pytest.fixture
def verified_job(db_session: Session, job_source: JobSource) -> tuple[JobPosting, JobVerification]:
    from backend.app.db.models import RawJobRecord

    raw = RawJobRecord(
        id=str(uuid.uuid4()),
        source_id=job_source.id,
        external_record_id="ext-draft-1",
        payload_hash="a" * 64,
        raw_fields=[],
    )
    db_session.add(raw)
    db_session.flush()

    job = JobPosting(
        id=str(uuid.uuid4()),
        source_id=job_source.id,
        external_record_id="ext-draft-1",
        raw_record_id=raw.id,
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

    link = JobSourceLink(
        id=str(uuid.uuid4()),
        job_id=job.id,
        source_type="tencent_smartsheet",
        source_id=job_source.id,
        source_record_ref="ext-draft-1",
        submission_id=None,
    )
    db_session.add(link)
    db_session.flush()

    return job, jv


@pytest.fixture
def profile_version(db_session: Session, user: User) -> ConfirmedProfileVersion:
    profile = Profile(
        id=str(uuid.uuid4()),
        user_id=user.id,
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
    return cv


@pytest.fixture
def completed_match_report(
    db_session: Session,
    user: User,
    analysis_session: AnalysisSession,
    verified_job: tuple[JobPosting, JobVerification],
    profile_version: ConfirmedProfileVersion,
) -> MatchReport:
    job, jv = verified_job
    mr = MatchReport(
        id=str(uuid.uuid4()),
        user_id=user.id,
        analysis_session_id=analysis_session.id,
        job_id=job.id,
        job_verification_id=jv.id,
        job_snapshot=SAMPLE_JOB_SNAPSHOT,
        profile_version_id=profile_version.id,
        request_idempotency_key="mr-ik-001",
        request_hash="mr-hash-001",
        status="completed",
        score=85,
        scoring_rule_version=SCORING_RULE_VERSION,
        model_version=MODEL_VERSION,
        prompt_version=PROMPT_VERSION,
        output_schema_version=OUTPUT_SCHEMA_VERSION,
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
    user: User,
    completed_match_report: MatchReport,
) -> ResumeDraft:
    """A ResumeDraft already in 'draft' status (after successful creation)."""
    d = ResumeDraft(
        id=str(uuid.uuid4()),
        user_id=user.id,
        match_report_id=completed_match_report.id,
        profile_version_id=completed_match_report.profile_version_id,
        target_job_id=completed_match_report.job_id,
        request_idempotency_key="approve-draft-ik",
        request_hash="approve-draft-hash",
        diffs=SAMPLE_DIFFS,
        status="draft",
        state_version=0,
    )
    db_session.add(d)
    db_session.flush()
    db_session.commit()
    return d


@pytest.fixture
def mock_generator() -> MagicMock:
    g = MagicMock()
    g.generate_diffs.return_value = {"diffs": SAMPLE_DIFFS}
    return g


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCreateDraft:
    """ResumeDraftService.create_draft tests."""

    def _service(self, mock_generator: MagicMock) -> ResumeDraftService:
        return ResumeDraftService(draft_generator=mock_generator)

    # -- Scenario 1: Happy path --------------------------------------------

    def test_create_draft_happy_path(
        self,
        db_session: Session,
        user: User,
        completed_match_report: MatchReport,
        mock_generator: MagicMock,
    ) -> None:
        """Completed MatchReport -> draft created with diffs."""
        service = self._service(mock_generator)
        draft = service.create_draft(
            db=db_session,
            user_id=user.id,
            match_report_id=completed_match_report.id,
            idempotency_key="ik-happy-1",
        )

        assert draft.status == "draft"
        assert draft.diffs == SAMPLE_DIFFS
        assert draft.match_report_id == completed_match_report.id
        assert draft.user_id == user.id
        assert draft.error_code is None
        assert draft.state_version == 0  # not changed by finalize

    # -- Scenario 2: Failed match report -----------------------------------

    def test_create_draft_incomplete_match_report(
        self,
        db_session: Session,
        user: User,
        completed_match_report: MatchReport,
        mock_generator: MagicMock,
    ) -> None:
        """Pending match report raises ValueError."""
        # Change the match report status to pending
        completed_match_report.status = "pending"
        db_session.flush()

        service = self._service(mock_generator)
        with pytest.raises(ValueError, match="draft_match_not_completed"):
            service.create_draft(
                db=db_session,
                user_id=user.id,
                match_report_id=completed_match_report.id,
                idempotency_key="ik-pending-1",
            )

    def test_create_draft_failed_match_report(
        self,
        db_session: Session,
        user: User,
        completed_match_report: MatchReport,
        mock_generator: MagicMock,
    ) -> None:
        """Match report with status != completed raises ValueError."""
        completed_match_report.status = "failed"
        completed_match_report.error_code = "match_execution_interrupted"
        db_session.flush()

        service = self._service(mock_generator)
        with pytest.raises(ValueError, match="draft_match_not_completed"):
            service.create_draft(
                db=db_session,
                user_id=user.id,
                match_report_id=completed_match_report.id,
                idempotency_key="ik-failed-1",
            )

    def test_create_draft_completed_with_error(
        self,
        db_session: Session,
        user: User,
        completed_match_report: MatchReport,
        mock_generator: MagicMock,
    ) -> None:
        """Completed match report with error_code raises ValueError."""
        completed_match_report.error_code = "some_error"
        db_session.flush()

        service = self._service(mock_generator)
        with pytest.raises(ValueError, match="draft_match_failed"):
            service.create_draft(
                db=db_session,
                user_id=user.id,
                match_report_id=completed_match_report.id,
                idempotency_key="ik-completed-err-1",
            )

    # -- Scenario 3: Invalid op type ---------------------------------------

    def test_create_draft_invalid_op(
        self,
        db_session: Session,
        user: User,
        completed_match_report: MatchReport,
        mock_generator: MagicMock,
    ) -> None:
        """Generator returns invalid op -> draft finalized as failed."""
        invalid_diffs = [
            {
                "op": "invalid_op",
                "section": "work_experience",
                "fact_ref": "senior_engineer",
                "before": "Led",
                "evidence_ids": ["ev-work-001"],
            },
        ]
        mock_generator.generate_diffs.return_value = {"diffs": invalid_diffs}
        service = self._service(mock_generator)

        draft = service.create_draft(
            db=db_session,
            user_id=user.id,
            match_report_id=completed_match_report.id,
            idempotency_key="ik-invalid-op-1",
        )

        assert draft.status == "failed"
        assert draft.error_code is not None
        assert draft.diffs is None

    # -- Scenario 4: Non-existent fact_ref ---------------------------------

    def test_create_draft_invalid_fact_ref(
        self,
        db_session: Session,
        user: User,
        completed_match_report: MatchReport,
        mock_generator: MagicMock,
    ) -> None:
        """Generator returns diff with unknown fact_ref -> draft failed."""
        bad_diffs = [
            {
                "op": "omit",
                "section": "work_experience",
                "fact_ref": "nonexistent_fact",
                "before": "Something",
                "evidence_ids": ["ev-work-001"],
            },
        ]
        mock_generator.generate_diffs.return_value = {"diffs": bad_diffs}
        service = self._service(mock_generator)

        draft = service.create_draft(
            db=db_session,
            user_id=user.id,
            match_report_id=completed_match_report.id,
            idempotency_key="ik-bad-ref-1",
        )

        assert draft.status == "failed"
        assert draft.error_code == "draft_validation_invalid_fact_ref"

    # -- Scenario 10: Idempotency ------------------------------------------

    def test_create_draft_idempotency_returns_existing(
        self,
        db_session: Session,
        user: User,
        completed_match_report: MatchReport,
        mock_generator: MagicMock,
    ) -> None:
        """Same idempotency_key + hash returns existing draft."""
        service = self._service(mock_generator)
        idem_key = "ik-idem-1"

        first = service.create_draft(
            db=db_session,
            user_id=user.id,
            match_report_id=completed_match_report.id,
            idempotency_key=idem_key,
        )
        second = service.create_draft(
            db=db_session,
            user_id=user.id,
            match_report_id=completed_match_report.id,
            idempotency_key=idem_key,
        )

        assert second.id == first.id
        assert second.status == "draft"


class TestApproveDraft:
    """ResumeDraftService.approve_draft tests."""

    def _service(self) -> ResumeDraftService:
        return ResumeDraftService()

    # -- Scenario 5: Happy path approve ------------------------------------

    def test_approve_draft_happy_path(
        self,
        db_session: Session,
        user: User,
        draft_in_draft_status: ResumeDraft,
        object_store: EncryptedObjectStore,
    ) -> None:
        """Full approve flow: ARV + 2 attachments (PDF/DOCX)."""
        service = self._service()
        arv = service.approve_draft(
            db=db_session,
            user_id=user.id,
            draft_id=draft_in_draft_status.id,
            expected_version=0,
            object_store=object_store,
        )

        # Verify ARV
        assert arv.draft_id == draft_in_draft_status.id
        assert arv.approved_by == user.id
        assert arv.approved_facts == SAMPLE_FACTS
        assert arv.approved_diffs == SAMPLE_DIFFS

        # Verify attachments
        attachments = (
            db_session.query(ApprovedResumeAttachment)
            .filter(ApprovedResumeAttachment.draft_id == draft_in_draft_status.id)
            .all()
        )
        assert len(attachments) == 2
        formats = {a.format for a in attachments}
        assert formats == {"pdf", "docx"}
        for att in attachments:
            assert att.status == "ready"
            assert att.approved_resume_version_id == arv.id
            assert att.plaintext_size > 0

        # Verify draft is approved
        draft = drafts_repo.get_by_id(
            db_session, draft_in_draft_status.id, user.id,
        )
        assert draft is not None
        assert draft.status == "approved"
        assert draft.state_version == 1
        assert draft.approved_at is not None

    # -- Scenario 9: Duplicate approve (idempotency) -----------------------

    def test_approve_draft_duplicate(
        self,
        db_session: Session,
        user: User,
        draft_in_draft_status: ResumeDraft,
        object_store: EncryptedObjectStore,
    ) -> None:
        """Approving an already-approved draft returns existing ARV."""
        service = self._service()

        first = service.approve_draft(
            db=db_session,
            user_id=user.id,
            draft_id=draft_in_draft_status.id,
            expected_version=0,
            object_store=object_store,
        )

        # Draft is now "approved". Call again.
        second = service.approve_draft(
            db=db_session,
            user_id=user.id,
            draft_id=draft_in_draft_status.id,
            expected_version=1,
            object_store=object_store,
        )

        assert second.id == first.id

    # -- Scenario 6: object_store.put fails --------------------------------

    def test_approve_draft_storage_failure(
        self,
        db_session: Session,
        user: User,
        draft_in_draft_status: ResumeDraft,
        object_store: EncryptedObjectStore,
    ) -> None:
        """When object store put fails, draft stays ``draft`` and no ARV."""
        service = self._service()

        # Patch put to fail on first call
        original_put = object_store.put

        def failing_put(*, key, plaintext, content_type):
            raise RuntimeError("storage unavailability")

        with patch.object(object_store, "put", side_effect=failing_put):
            with pytest.raises(RuntimeError, match="storage unavailability"):
                service.approve_draft(
                    db=db_session,
                    user_id=user.id,
                    draft_id=draft_in_draft_status.id,
                    expected_version=0,
                    object_store=object_store,
                )

        # Verify draft unchanged
        draft = drafts_repo.get_by_id(
            db_session, draft_in_draft_status.id, user.id,
        )
        assert draft is not None
        assert draft.status == "draft"

        # No ARV created
        arv = (
            db_session.query(ApprovedResumeVersion)
            .filter(ApprovedResumeVersion.draft_id == draft_in_draft_status.id)
            .first()
        )
        assert arv is None

    # -- Scenario 7: PDF succeeds, DOCX fails ------------------------------

    def test_approve_draft_pdf_succeeds_docx_fails(
        self,
        db_session: Session,
        user: User,
        draft_in_draft_status: ResumeDraft,
        object_store: EncryptedObjectStore,
    ) -> None:
        """PDF succeeds, DOCX generation fails -> both objects compensated."""
        service = self._service()

        real_generate_docx = (
            "backend.app.services.resume_draft_service.generate_resume_docx"
        )

        def failing_docx(*args, **kwargs):
            raise RuntimeError("docx generation failed")

        with patch(real_generate_docx, side_effect=failing_docx):
            with pytest.raises(RuntimeError, match="docx generation failed"):
                service.approve_draft(
                    db=db_session,
                    user_id=user.id,
                    draft_id=draft_in_draft_status.id,
                    expected_version=0,
                    object_store=object_store,
                )

        # Verify draft unchanged
        draft = drafts_repo.get_by_id(
            db_session, draft_in_draft_status.id, user.id,
        )
        assert draft is not None
        assert draft.status == "draft"

        # PDF and DOCX attachments should be marked failed
        attachments = (
            db_session.query(ApprovedResumeAttachment)
            .filter(ApprovedResumeAttachment.draft_id == draft_in_draft_status.id)
            .all()
        )
        assert len(attachments) == 2
        for att in attachments:
            assert att.status == "failed"

        # PDF object should be deleted from store
        pdf_key = f"resumes/{user.id}/{draft_in_draft_status.id}/pdf"
        docx_key = f"resumes/{user.id}/{draft_in_draft_status.id}/docx"
        with pytest.raises(FileNotFoundError):
            object_store.get(key=pdf_key)
        with pytest.raises(FileNotFoundError):
            object_store.get(key=docx_key)

    # -- Scenario 8: Objects stored, DB finalize fails ---------------------

    def test_approve_draft_integrity_error_race(
        self,
        db_session: Session,
        user: User,
        draft_in_draft_status: ResumeDraft,
        object_store: EncryptedObjectStore,
    ) -> None:
        """Objects stored, but concurrent ARV exists -> rollback + compensate."""
        service = self._service()

        # Manually create an ARV for this draft_id (simulating concurrent approve).
        # Commit so the row survives any rollback the service may perform.
        existing_arv = ApprovedResumeVersion(
            id=str(uuid.uuid4()),
            draft_id=draft_in_draft_status.id,
            profile_version_id=draft_in_draft_status.profile_version_id,
            target_job_id=draft_in_draft_status.target_job_id,
            approved_facts=SAMPLE_FACTS,
            approved_diffs=[],
            approved_by=user.id,
        )
        db_session.add(existing_arv)
        db_session.commit()

        # The draft is still "draft" (inconsistent state, as in a race),
        # so the service will try to create another ARV and hit IntegrityError
        result = service.approve_draft(
            db=db_session,
            user_id=user.id,
            draft_id=draft_in_draft_status.id,
            expected_version=0,
            object_store=object_store,
        )

        # Should return the existing ARV, not raise
        assert result.id == existing_arv.id

        # Stored objects should be deleted (compensated)
        pdf_key = f"resumes/{user.id}/{draft_in_draft_status.id}/pdf"
        docx_key = f"resumes/{user.id}/{draft_in_draft_status.id}/docx"
        with pytest.raises(FileNotFoundError):
            object_store.get(key=pdf_key)
        with pytest.raises(FileNotFoundError):
            object_store.get(key=docx_key)

    # -- Scenario 11: Stale version on approve -----------------------------

    def test_approve_draft_stale_version(
        self,
        db_session: Session,
        user: User,
        draft_in_draft_status: ResumeDraft,
        object_store: EncryptedObjectStore,
    ) -> None:
        """Wrong expected_version raises StaleDraftVersionError."""
        service = self._service()

        with pytest.raises(StaleDraftVersionError):
            service.approve_draft(
                db=db_session,
                user_id=user.id,
                draft_id=draft_in_draft_status.id,
                expected_version=99,  # wrong version
                object_store=object_store,
            )

    # -- Scenario 12: Cross-user access ------------------------------------

    def test_approve_draft_cross_user(
        self,
        db_session: Session,
        user: User,
        draft_in_draft_status: ResumeDraft,
        object_store: EncryptedObjectStore,
    ) -> None:
        """Other user cannot approve this draft."""
        other_user = User(
            id=str(uuid.uuid4()),
            account="other-tester",
            nickname="Other Tester",
            password_hash="argon2-placeholder",
        )
        db_session.add(other_user)
        db_session.flush()

        service = self._service()
        with pytest.raises(ValueError, match="not_found"):
            service.approve_draft(
                db=db_session,
                user_id=other_user.id,
                draft_id=draft_in_draft_status.id,
                expected_version=0,
                object_store=object_store,
            )


class TestRejectDraft:
    """ResumeDraftService.reject_draft tests."""

    def _service(self) -> ResumeDraftService:
        return ResumeDraftService()

    def test_reject_draft_happy_path(
        self,
        db_session: Session,
        user: User,
        draft_in_draft_status: ResumeDraft,
    ) -> None:
        """Reject a draft -> status='rejected', version bumps."""
        service = self._service()
        rejected = service.reject_draft(
            db=db_session,
            user_id=user.id,
            draft_id=draft_in_draft_status.id,
            expected_version=0,
        )

        assert rejected.status == "rejected"
        assert rejected.state_version == 1
        assert rejected.rejected_at is not None

    def test_reject_draft_stale_version(
        self,
        db_session: Session,
        user: User,
        draft_in_draft_status: ResumeDraft,
    ) -> None:
        """Wrong expected_version raises StaleDraftVersionError."""
        service = self._service()

        with pytest.raises(StaleDraftVersionError):
            service.reject_draft(
                db=db_session,
                user_id=user.id,
                draft_id=draft_in_draft_status.id,
                expected_version=99,
            )

    def test_reject_draft_cross_user(
        self,
        db_session: Session,
        user: User,
        draft_in_draft_status: ResumeDraft,
    ) -> None:
        """Other user cannot reject this draft."""
        other_user = User(
            id=str(uuid.uuid4()),
            account="other-rejecter",
            nickname="Other Rejecter",
            password_hash="argon2-placeholder",
        )
        db_session.add(other_user)
        db_session.flush()

        service = self._service()
        with pytest.raises(ValueError, match="not_found"):
            service.reject_draft(
                db=db_session,
                user_id=other_user.id,
                draft_id=draft_in_draft_status.id,
                expected_version=0,
            )

    def test_reject_already_approved_draft(
        self,
        db_session: Session,
        user: User,
        draft_in_draft_status: ResumeDraft,
        object_store: EncryptedObjectStore,
    ) -> None:
        """Cannot reject an already approved draft."""
        # First approve it
        service = self._service()
        service.approve_draft(
            db=db_session,
            user_id=user.id,
            draft_id=draft_in_draft_status.id,
            expected_version=0,
            object_store=object_store,
        )

        # Now try to reject
        with pytest.raises(ValueError, match="draft_cannot_reject_status_approved"):
            service.reject_draft(
                db=db_session,
                user_id=user.id,
                draft_id=draft_in_draft_status.id,
                expected_version=1,
            )
