from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import ANY

import pytest
from sqlalchemy import create_engine, update as sql_update
from sqlalchemy.orm import Session

from backend.app.db.base import Base
from backend.app.db.models import (
    AnalysisSession,
    ApprovedResumeAttachment,
    ApprovedResumeVersion,
    ApplicationSnapshot,
    ConfirmedProfileVersion,
    JobPosting,
    JobPostingStatus,
    JobSource,
    JobSourceProvider,
    JobVerification,
    MatchReport,
    Profile,
    RawJobRecord,
    ResumeDraft,
    User,
)
from backend.app.repositories import matches as match_repo
from backend.app.repositories import drafts as draft_repo
from backend.app.repositories import attachments as attachment_repo
from backend.app.repositories import snapshots as snapshot_repo


# -------- helpers --------


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    yield session
    session.close()
    engine.dispose()


def _user(db: Session, account: str = "alice") -> User:
    u = User(account=account, nickname=account, password_hash="hash")
    db.add(u)
    db.flush()
    return u


def _profile(db: Session, user: User) -> Profile:
    p = Profile(user_id=user.id, version=0, local_sensitive_references={})
    db.add(p)
    db.flush()
    return p


def _confirmed_version(db: Session, profile: Profile) -> ConfirmedProfileVersion:
    cv = ConfirmedProfileVersion(
        profile_id=profile.id,
        version_number=1,
        aggregate_version=1,
        facts_snapshot={},
        evidence_refs={},
        local_sensitive_references={},
    )
    db.add(cv)
    db.flush()
    return cv


def _job_posting(db: Session) -> JobPosting:
    src = JobSource(
        source_key="test",
        provider=JobSourceProvider.TENCENT_SMARTSHEET,
        name="Test",
        file_id="f",
        sheet_id="s",
        mapper_version="v1",
        enabled=True,
    )
    db.add(src)
    db.flush()
    raw = RawJobRecord(
        source_id=src.id,
        external_record_id="ext1",
        payload_hash="a" * 64,
        raw_fields=[],
    )
    db.add(raw)
    db.flush()
    jp = JobPosting(
        source_id=src.id,
        external_record_id="ext1",
        raw_record_id=raw.id,
        status=JobPostingStatus.VERIFIED,
        company_name="TestCo",
        title="Engineer",
        description_text="...",
        locations=[],
        recruitment_types=[],
        industries=[],
        apply_url="https://example.com/apply",
        mapper_version="v1",
        source_candidate={},
    )
    db.add(jp)
    db.flush()
    return jp


def _job_verification(db: Session, job: JobPosting) -> JobVerification:
    jv = JobVerification(
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
    return jv


def _session(db: Session, user: User, thread_id: str = "thread-1") -> AnalysisSession:
    s = AnalysisSession(
        user_id=user.id,
        thread_id=thread_id,
        label="test",
        activated_at=datetime.now(timezone.utc),
    )
    db.add(s)
    db.flush()
    return s


def _report_kwargs(
    user_id: str, session_id: str, job_id: str,
    job_verification_id: str, profile_version_id: str,
    **overrides: Any,
) -> dict[str, Any]:
    base = {
        "user_id": user_id,
        "analysis_session_id": session_id,
        "job_id": job_id,
        "job_verification_id": job_verification_id,
        "job_snapshot": {"company_name": "TestCo"},
        "profile_version_id": profile_version_id,
        "request_idempotency_key": "ik-1",
        "request_hash": "abc123",
        "scoring_rule_version": "1.0",
        "model_version": "test-v1",
        "prompt_version": "prompt-v1",
        "output_schema_version": "1.0",
        "status": "pending",
    }
    base.update(overrides)
    return base


def _draft_kwargs(
    user_id: str, match_report_id: str,
    profile_version_id: str, target_job_id: str,
    **overrides: Any,
) -> dict[str, Any]:
    base = {
        "user_id": user_id,
        "match_report_id": match_report_id,
        "profile_version_id": profile_version_id,
        "target_job_id": target_job_id,
        "request_idempotency_key": "ik-draft-1",
        "request_hash": "def456",
        "status": "generating",
    }
    base.update(overrides)
    return base


def _snapshot_kwargs(
    user_id: str, job_id: str,
    approved_resume_version_id: str, profile_version_id: str,
    **overrides: Any,
) -> dict[str, Any]:
    base = {
        "user_id": user_id,
        "job_id": job_id,
        "approved_resume_version_id": approved_resume_version_id,
        "profile_version_id": profile_version_id,
        "job_snapshot": {"company_name": "TestCo"},
        "profile_facts": {"skills": ["Python"]},
        "request_idempotency_key": "ik-snap-1",
        "request_hash": "ghi789",
        "dynamic_answers": {},
        "local_sensitive_requirements": {},
        "attachment_ids": [],
        "gui_eligible": True,
        "job_status_at_snapshot": "verified",
        "job_review_version_at_snapshot": 1,
        "created_by": user_id,
        "schema_version": "1.0",
    }
    base.update(overrides)
    return base


def _seed_match_scaffold(
    db: Session, user: User,
) -> tuple[AnalysisSession, JobPosting, JobVerification, ConfirmedProfileVersion, MatchReport]:
    profile = _profile(db, user)
    cv = _confirmed_version(db, profile)
    job = _job_posting(db)
    jv = _job_verification(db, job)
    sess = _session(db, user)
    report = match_repo.create(
        db,
        **_report_kwargs(user.id, sess.id, job.id, jv.id, cv.id),
    )
    return sess, job, jv, cv, report


def _seed_draft_scaffold(
    db: Session, user: User,
) -> tuple[JobPosting, ConfirmedProfileVersion, MatchReport, ResumeDraft]:
    _, job, _jv, cv, report = _seed_match_scaffold(db, user)
    draft = draft_repo.create(
        db,
        **_draft_kwargs(user.id, report.id, cv.id, job.id),
    )
    return job, cv, report, draft


# -------- MatchRepository --------


class TestMatchRepository:
    def test_create_and_get_owned(self, db: Session) -> None:
        user = _user(db)
        _sess, _job, _jv, _cv, report = _seed_match_scaffold(db, user)
        fetched = match_repo.get_by_id(db, report.id, user.id)
        assert fetched is not None
        assert fetched.id == report.id
        assert fetched.status == "pending"

    def test_get_by_id_cross_user_returns_none(self, db: Session) -> None:
        alice = _user(db, "alice")
        bob = _user(db, "bob")
        _sess, _job, _jv, _cv, report = _seed_match_scaffold(db, alice)
        assert match_repo.get_by_id(db, report.id, bob.id) is None

    def test_list_by_user(self, db: Session) -> None:
        user = _user(db)
        _sess, job, jv, cv, r1 = _seed_match_scaffold(db, user)
        # Create a second report for same user
        r2 = match_repo.create(
            db,
            **_report_kwargs(
                user.id, _sess.id, job.id, jv.id, cv.id,
                **{"request_idempotency_key": "ik-2", "request_hash": "def"},
            ),
        )
        items = match_repo.list_by_user(db, user.id)
        assert len(items) == 2

    def test_list_by_user_other_user_empty(self, db: Session) -> None:
        alice = _user(db, "alice")
        bob = _user(db, "bob")
        _sess, _job, _jv, _cv, _report = _seed_match_scaffold(db, alice)
        assert match_repo.list_by_user(db, bob.id) == []

    def test_list_by_thread(self, db: Session) -> None:
        user = _user(db)
        sess = _session(db, user, thread_id="thread-A")
        profile = _profile(db, user)
        cv = _confirmed_version(db, profile)
        job = _job_posting(db)
        jv = _job_verification(db, job)
        report = match_repo.create(
            db,
            **_report_kwargs(user.id, sess.id, job.id, jv.id, cv.id),
        )
        items = match_repo.list_by_thread(db, "thread-A", user.id)
        assert len(items) == 1
        assert items[0].id == report.id

    def test_list_by_thread_cross_user_returns_empty(self, db: Session) -> None:
        alice = _user(db, "alice")
        bob = _user(db, "bob")
        sess = _session(db, alice, thread_id="thread-A")
        profile = _profile(db, alice)
        cv = _confirmed_version(db, profile)
        job = _job_posting(db)
        jv = _job_verification(db, job)
        match_repo.create(
            db,
            **_report_kwargs(alice.id, sess.id, job.id, jv.id, cv.id),
        )
        assert match_repo.list_by_thread(db, "thread-A", bob.id) == []

    def test_finalize_sets_score(self, db: Session) -> None:
        user = _user(db)
        _sess, _job, _jv, _cv, report = _seed_match_scaffold(db, user)
        finalized = match_repo.finalize(
            db, report.id, status="completed",
            score=85,
            score_components={"match": 85},
            strengths=[{"s": "good"}],
            gaps=[{"g": "bad"}],
            unknowns=[],
            risks=[],
        )
        assert finalized.status == "completed"
        assert finalized.score == 85
        assert finalized.completed_at is not None

    def test_finalize_sets_error(self, db: Session) -> None:
        user = _user(db)
        _sess, _job, _jv, _cv, report = _seed_match_scaffold(db, user)
        finalized = match_repo.finalize(db, report.id, status="failed", error_code="timeout")
        assert finalized.status == "failed"
        assert finalized.error_code == "timeout"
        assert finalized.completed_at is not None

    def test_recover_stale(self, db: Session) -> None:
        user = _user(db)
        _sess, _job, _jv, _cv, report = _seed_match_scaffold(db, user)
        # Stretch created_at into the past
        past = datetime.now(timezone.utc) - timedelta(minutes=30)
        db.execute(
            sql_update(MatchReport)
            .where(MatchReport.id == report.id)
            .values(created_at=past),
        )
        db.flush()

        count = match_repo.recover_stale(db, timeout_minutes=5)
        assert count >= 1

        fetched = match_repo.get_by_id(db, report.id, user.id)
        assert fetched is not None
        assert fetched.status == "failed"
        assert fetched.error_code == "stale"

    def test_recover_stale_ignores_recent(self, db: Session) -> None:
        user = _user(db)
        _sess, _job, _jv, _cv, report = _seed_match_scaffold(db, user)
        count = match_repo.recover_stale(db, timeout_minutes=10)
        assert count == 0


# -------- DraftRepository --------


class TestDraftRepository:
    def test_create_and_get_owned(self, db: Session) -> None:
        user = _user(db)
        _job, _cv, _report, draft = _seed_draft_scaffold(db, user)
        fetched = draft_repo.get_by_id(db, draft.id, user.id)
        assert fetched is not None
        assert fetched.id == draft.id

    def test_get_by_id_cross_user_returns_none(self, db: Session) -> None:
        alice = _user(db, "alice")
        bob = _user(db, "bob")
        _job, _cv, _report, draft = _seed_draft_scaffold(db, alice)
        assert draft_repo.get_by_id(db, draft.id, bob.id) is None

    def test_list_by_user(self, db: Session) -> None:
        user = _user(db)
        _job, _cv, report, d1 = _seed_draft_scaffold(db, user)
        d2 = draft_repo.create(
            db,
            **_draft_kwargs(
                user.id, report.id, _cv.id, _job.id,
                **{"request_idempotency_key": "ik-d2", "request_hash": "h2"},
            ),
        )
        items = draft_repo.list_by_user(db, user.id)
        assert len(items) == 2

    def test_list_by_user_other_user_empty(self, db: Session) -> None:
        alice = _user(db, "alice")
        bob = _user(db, "bob")
        _job, _cv, _report, _draft = _seed_draft_scaffold(db, alice)
        assert draft_repo.list_by_user(db, bob.id) == []

    def test_approve_optimistic_lock_success(self, db: Session) -> None:
        user = _user(db)
        _job, _cv, _report, draft = _seed_draft_scaffold(db, user)
        assert draft.state_version == 0

        approved = draft_repo.approve(db, draft.id, expected_version=0)
        assert approved.status == "approved"
        assert approved.state_version == 1
        assert approved.approved_at is not None

    def test_approve_optimistic_lock_failure(self, db: Session) -> None:
        user = _user(db)
        _job, _cv, _report, draft = _seed_draft_scaffold(db, user)
        with pytest.raises(RuntimeError, match="stale"):
            draft_repo.approve(db, draft.id, expected_version=999)

    def test_reject_optimistic_lock_success(self, db: Session) -> None:
        user = _user(db)
        _job, _cv, _report, draft = _seed_draft_scaffold(db, user)
        assert draft.state_version == 0

        rejected = draft_repo.reject(db, draft.id, expected_version=0)
        assert rejected.status == "rejected"
        assert rejected.state_version == 1
        assert rejected.rejected_at is not None

    def test_reject_optimistic_lock_failure(self, db: Session) -> None:
        user = _user(db)
        _job, _cv, _report, draft = _seed_draft_scaffold(db, user)
        with pytest.raises(RuntimeError, match="stale"):
            draft_repo.reject(db, draft.id, expected_version=999)

    def test_finalize_success(self, db: Session) -> None:
        user = _user(db)
        _job, _cv, _report, draft = _seed_draft_scaffold(db, user)
        finalized = draft_repo.finalize(
            db, draft.id, status="completed", diffs_or_error={"changes": []},
        )
        assert finalized.status == "completed"
        assert finalized.diffs == {"changes": []}
        assert finalized.error_code is None

    def test_finalize_error(self, db: Session) -> None:
        user = _user(db)
        _job, _cv, _report, draft = _seed_draft_scaffold(db, user)
        finalized = draft_repo.finalize(
            db, draft.id, status="failed", diffs_or_error="generation_error",
        )
        assert finalized.status == "failed"
        assert finalized.error_code == "generation_error"


# -------- AttachmentRepository --------


class TestAttachmentRepository:
    def test_reserve_or_reset_pending_creates_new(self, db: Session) -> None:
        user = _user(db)
        _job, _cv, _report, draft = _seed_draft_scaffold(db, user)
        att = attachment_repo.reserve_or_reset_pending(
            db, draft.id, user.id, format="pdf",
            object_key="resumes/test.pdf",
            content_type="application/pdf",
            encryption_version="v1",
        )
        assert att.status == "pending"
        assert att.format == "pdf"
        assert att.object_key == "resumes/test.pdf"
        assert att.draft_id == draft.id
        assert att.plaintext_size == 0

    def test_reserve_or_reset_reuses_failed_row(self, db: Session) -> None:
        user = _user(db)
        _job, _cv, _report, draft = _seed_draft_scaffold(db, user)
        att = attachment_repo.reserve_or_reset_pending(
            db, draft.id, user.id, format="pdf",
            object_key="resumes/test.pdf",
            content_type="application/pdf",
            encryption_version="v1",
        )
        # Mark it failed
        attachment_repo.mark_failed(db, att.id, error_code="conversion_error")

        # Reserve again — should reuse the same row
        att2 = attachment_repo.reserve_or_reset_pending(
            db, draft.id, user.id, format="pdf",
            object_key="resumes/test_v2.pdf",
            content_type="application/pdf",
            encryption_version="v2",
        )
        assert att2.id == att.id
        assert att2.status == "pending"
        assert att2.object_key == "resumes/test_v2.pdf"
        assert att2.encryption_version == "v2"

    def test_mark_ready(self, db: Session) -> None:
        user = _user(db)
        _job, _cv, _report, draft = _seed_draft_scaffold(db, user)
        att = attachment_repo.reserve_or_reset_pending(
            db, draft.id, user.id, format="pdf",
            object_key="resumes/test.pdf",
            content_type="application/pdf",
            encryption_version="v1",
        )
        ready = attachment_repo.mark_ready(
            db, att.id,
            approved_version_id="ver-123",
            plaintext_size=4096,
        )
        assert ready.status == "ready"
        assert ready.approved_resume_version_id == "ver-123"
        assert ready.plaintext_size == 4096

    def test_mark_failed(self, db: Session) -> None:
        user = _user(db)
        _job, _cv, _report, draft = _seed_draft_scaffold(db, user)
        att = attachment_repo.reserve_or_reset_pending(
            db, draft.id, user.id, format="pdf",
            object_key="resumes/test.pdf",
            content_type="application/pdf",
            encryption_version="v1",
        )
        failed = attachment_repo.mark_failed(db, att.id, error_code="conversion_error")
        assert failed.status == "failed"
        assert failed.error_code == "conversion_error"

    def test_get_by_draft(self, db: Session) -> None:
        user = _user(db)
        _job, _cv, _report, draft = _seed_draft_scaffold(db, user)
        attachment_repo.reserve_or_reset_pending(
            db, draft.id, user.id, format="pdf",
            object_key="resumes/test.pdf",
            content_type="application/pdf",
            encryption_version="v1",
        )
        attachment_repo.reserve_or_reset_pending(
            db, draft.id, user.id, format="docx",
            object_key="resumes/test.docx",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            encryption_version="v1",
        )
        items = attachment_repo.get_by_draft(db, draft.id, user.id)
        assert len(items) == 2

    def test_get_by_draft_cross_user_empty(self, db: Session) -> None:
        alice = _user(db, "alice")
        bob = _user(db, "bob")
        _job, _cv, _report, draft = _seed_draft_scaffold(db, alice)
        attachment_repo.reserve_or_reset_pending(
            db, draft.id, alice.id, format="pdf",
            object_key="resumes/test.pdf",
            content_type="application/pdf",
            encryption_version="v1",
        )
        assert attachment_repo.get_by_draft(db, draft.id, bob.id) == []


# -------- SnapshotRepository --------


class TestSnapshotRepository:
    def test_create_and_get_owned(self, db: Session) -> None:
        user = _user(db)
        _job, cv, _report, draft = _seed_draft_scaffold(db, user)
        arv = ApprovedResumeVersion(
            draft_id=draft.id,
            profile_version_id=cv.id,
            target_job_id=_job.id,
            approved_facts={},
            approved_diffs={},
            approved_by=user.id,
        )
        db.add(arv)
        db.flush()

        snap = snapshot_repo.create(
            db,
            **_snapshot_kwargs(user.id, _job.id, arv.id, cv.id),
        )
        fetched = snapshot_repo.get_by_id(db, snap.id, user.id)
        assert fetched is not None
        assert fetched.id == snap.id

    def test_get_by_id_cross_user_returns_none(self, db: Session) -> None:
        alice = _user(db, "alice")
        bob = _user(db, "bob")
        _job, cv, _report, draft = _seed_draft_scaffold(db, alice)
        arv = ApprovedResumeVersion(
            draft_id=draft.id,
            profile_version_id=cv.id,
            target_job_id=_job.id,
            approved_facts={},
            approved_diffs={},
            approved_by=alice.id,
        )
        db.add(arv)
        db.flush()

        snap = snapshot_repo.create(
            db,
            **_snapshot_kwargs(alice.id, _job.id, arv.id, cv.id),
        )
        assert snapshot_repo.get_by_id(db, snap.id, bob.id) is None

    def test_list_by_user(self, db: Session) -> None:
        user = _user(db)
        _job, cv, _report, draft = _seed_draft_scaffold(db, user)
        arv = ApprovedResumeVersion(
            draft_id=draft.id,
            profile_version_id=cv.id,
            target_job_id=_job.id,
            approved_facts={},
            approved_diffs={},
            approved_by=user.id,
        )
        db.add(arv)
        db.flush()

        s1 = snapshot_repo.create(
            db,
            **_snapshot_kwargs(
                user.id, _job.id, arv.id, cv.id,
                **{"request_idempotency_key": "ik-s1", "request_hash": "h1"},
            ),
        )
        s2 = snapshot_repo.create(
            db,
            **_snapshot_kwargs(
                user.id, _job.id, arv.id, cv.id,
                **{"request_idempotency_key": "ik-s2", "request_hash": "h2"},
            ),
        )
        items = snapshot_repo.list_by_user(db, user.id)
        assert len(items) == 2

    def test_list_by_user_other_user_empty(self, db: Session) -> None:
        alice = _user(db, "alice")
        bob = _user(db, "bob")
        _job, cv, _report, draft = _seed_draft_scaffold(db, alice)
        arv = ApprovedResumeVersion(
            draft_id=draft.id,
            profile_version_id=cv.id,
            target_job_id=_job.id,
            approved_facts={},
            approved_diffs={},
            approved_by=alice.id,
        )
        db.add(arv)
        db.flush()

        snapshot_repo.create(
            db,
            **_snapshot_kwargs(alice.id, _job.id, arv.id, cv.id),
        )
        assert snapshot_repo.list_by_user(db, bob.id) == []
