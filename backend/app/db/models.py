from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.domain.job_feedback import (
    JobFeedbackAction,
    JobFeedbackCategory,
    JobFeedbackStatus,
)

from backend.app.domain.profiles import (
    EvidenceDecisionAction,
    ResumeAssetStatus,
    ResumeImportStatus,
)

from backend.app.domain.job_submissions import (
    DeduplicationStatus,
    JobSourceLinkType,
    SubmissionInputType,
    SubmissionStatus,
)

from .base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utc_now


class UserRole(StrEnum):
    STUDENT = "student"
    ADMIN = "admin"


class DevicePlatform(StrEnum):
    WINDOWS = "windows"


class DeviceStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class ApplicationTaskStatus(StrEnum):
    CREATED = "created"
    WAITING_FOR_DEVICE = "waiting_for_device"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    WAITING_FOR_HUMAN = "waiting_for_human"
    READY_FOR_REVIEW = "ready_for_review"
    OBSERVING_USER_SUBMISSION = "observing_user_submission"
    SUBMITTED_SUCCESS = "submitted_success"
    SUBMITTED_FAILED = "submitted_failed"
    RESULT_UNKNOWN = "result_unknown"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskActor(StrEnum):
    HUMAN = "human"
    EXECUTOR = "executor"
    SYSTEM = "system"


enum_kwargs = {
    "native_enum": False,
    "create_constraint": True,
    "validate_strings": True,
    "values_callable": lambda enum_type: [item.value for item in enum_type],
}
audit_id_type = BigInteger().with_variant(Integer, "sqlite")


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"
    account: Mapped[str] = mapped_column(
        String(120), unique=True, index=True, nullable=False
    )
    nickname: Mapped[str] = mapped_column(String(120), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, **enum_kwargs), default=UserRole.STUDENT, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sessions: Mapped[list["AnalysisSession"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class AnalysisSession(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "analysis_sessions"
    __table_args__ = (
        UniqueConstraint("thread_id", name="uq_analysis_sessions_thread_id"),
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    thread_id: Mapped[str] = mapped_column(String(96), nullable=False)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    activated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True, nullable=False
    )
    user: Mapped[User] = relationship(back_populates="sessions")


class Device(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "devices"
    __table_args__ = (UniqueConstraint("token_hash", name="uq_devices_token_hash"),)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    platform: Mapped[DevicePlatform] = mapped_column(
        Enum(DevicePlatform, **enum_kwargs), nullable=False
    )
    status: Mapped[DeviceStatus] = mapped_column(
        Enum(DeviceStatus, **enum_kwargs),
        default=DeviceStatus.ACTIVE,
        index=True,
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    public_key_pem: Mapped[str] = mapped_column(Text, nullable=False)
    paired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: utc_now() + timedelta(days=90),
        index=True,
        nullable=False,
    )
    credential_rotated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[str | None] = mapped_column(String(40))


class MatchReport(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "match_reports"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    analysis_session_id: Mapped[str] = mapped_column(
        ForeignKey("analysis_sessions.id"), nullable=False
    )
    job_id: Mapped[str] = mapped_column(ForeignKey("job_postings.id"), nullable=False)
    job_verification_id: Mapped[str] = mapped_column(
        ForeignKey("job_verifications.id"), nullable=False
    )
    job_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    profile_version_id: Mapped[str] = mapped_column(
        ForeignKey("confirmed_profile_versions.id"), nullable=False
    )
    request_idempotency_key: Mapped[str] = mapped_column(String(96), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    score: Mapped[int | None] = mapped_column(Integer)
    score_components: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    scoring_rule_version: Mapped[str] = mapped_column(String(64), nullable=False)
    strengths: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    gaps: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    unknowns: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    risks: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    application_priority: Mapped[str | None] = mapped_column(String(20))
    recommendation: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    output_schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    error_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("user_id", "request_idempotency_key"),
        Index("ix_match_reports_analysis_session_id", "analysis_session_id"),
        Index("ix_match_reports_job_id", "job_id"),
        Index("ix_match_reports_profile_version_id", "profile_version_id"),
        Index("ix_match_reports_user_id_created_at", "user_id", "created_at"),
    )


class ResumeDraft(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "resume_drafts"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    match_report_id: Mapped[str] = mapped_column(
        ForeignKey("match_reports.id"), nullable=False
    )
    profile_version_id: Mapped[str] = mapped_column(
        ForeignKey("confirmed_profile_versions.id"), nullable=False
    )
    target_job_id: Mapped[str] = mapped_column(
        ForeignKey("job_postings.id"), nullable=False
    )
    request_idempotency_key: Mapped[str] = mapped_column(String(96), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    diffs: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="generating")
    error_code: Mapped[str | None] = mapped_column(String(80))
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("user_id", "request_idempotency_key"),
    )


class ApprovedResumeVersion(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "approved_resume_versions"

    draft_id: Mapped[str] = mapped_column(
        ForeignKey("resume_drafts.id"), nullable=False
    )
    profile_version_id: Mapped[str] = mapped_column(
        ForeignKey("confirmed_profile_versions.id"), nullable=False
    )
    target_job_id: Mapped[str] = mapped_column(
        ForeignKey("job_postings.id"), nullable=False
    )
    approved_facts: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    approved_diffs: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    approved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    approved_by: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)

    __table_args__ = (
        UniqueConstraint("draft_id"),
    )


class ApprovedResumeAttachment(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "approved_resume_attachments"

    draft_id: Mapped[str] = mapped_column(
        ForeignKey("resume_drafts.id"), nullable=False
    )
    approved_resume_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("approved_resume_versions.id")
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    format: Mapped[str] = mapped_column(String(16), nullable=False)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    content_type: Mapped[str] = mapped_column(String(120), nullable=False)
    plaintext_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    encryption_version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )

    __table_args__ = (
        UniqueConstraint("draft_id", "format"),
    )


class ApplicationSnapshot(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "application_snapshots"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    job_id: Mapped[str] = mapped_column(ForeignKey("job_postings.id"), nullable=False)
    approved_resume_version_id: Mapped[str] = mapped_column(
        ForeignKey("approved_resume_versions.id"), nullable=False
    )
    profile_version_id: Mapped[str] = mapped_column(
        ForeignKey("confirmed_profile_versions.id"), nullable=False
    )
    job_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    profile_facts: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    request_idempotency_key: Mapped[str] = mapped_column(String(96), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    dynamic_answers: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    local_sensitive_requirements: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False
    )
    attachment_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    gui_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    job_status_at_snapshot: Mapped[str] = mapped_column(String(20), nullable=False)
    job_review_version_at_snapshot: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "request_idempotency_key"),
    )


class ApplicationTask(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "application_tasks"
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    target_job_id: Mapped[str | None] = mapped_column(
        ForeignKey("job_postings.id"), index=True
    )
    snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("application_snapshots.id"), index=True
    )
    device_id: Mapped[str | None] = mapped_column(
        ForeignKey("devices.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[ApplicationTaskStatus] = mapped_column(
        Enum(ApplicationTaskStatus, **enum_kwargs),
        default=ApplicationTaskStatus.CREATED,
        index=True,
        nullable=False,
    )
    state_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    current_page: Mapped[int | None] = mapped_column(Integer)
    page_count: Mapped[int | None] = mapped_column(Integer)
    last_error_code: Mapped[str | None] = mapped_column(String(80))
    task_kind: Mapped[str] = mapped_column(String(20), nullable=False, default="simulation")
    simulation_scenario: Mapped[str | None] = mapped_column(String(100))
    request_idempotency_key: Mapped[str | None] = mapped_column(String(96))


class ApplicationEvent(Base):
    __tablename__ = "application_events"
    __table_args__ = (
        Index("ix_application_events_task_created", "task_id", "created_at"),
    )
    id: Mapped[int] = mapped_column(audit_id_type, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("application_tasks.id", ondelete="CASCADE"), nullable=False
    )
    actor: Mapped[TaskActor] = mapped_column(
        Enum(TaskActor, **enum_kwargs), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    from_status: Mapped[str] = mapped_column(String(40), nullable=False)
    to_status: Mapped[str] = mapped_column(String(40), nullable=False)
    redacted_payload: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        Index(
            "ix_audit_events_entity_created",
            "entity_type",
            "entity_id",
            "created_at",
        ),
    )
    id: Mapped[int] = mapped_column(audit_id_type, primary_key=True, autoincrement=True)
    actor_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    actor_device_id: Mapped[str | None] = mapped_column(
        ForeignKey("devices.id", ondelete="SET NULL"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    entity_type: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(80), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    redacted_payload: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class JobSourceProvider(StrEnum):
    TENCENT_SMARTSHEET = "tencent_smartsheet"
    USER_SUBMISSION = "user_submission"


class JobSyncRunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"


class JobPostingStatus(StrEnum):
    PENDING_COMPLETION = "pending_completion"
    PENDING_REVIEW = "pending_review"
    VERIFIED = "verified"
    EXPIRED = "expired"
    REJECTED = "rejected"


class JobSource(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "job_sources"
    __table_args__ = (
        UniqueConstraint("source_key", name="uq_job_sources_source_key"),
        UniqueConstraint(
            "provider", "file_id", "sheet_id", name="uq_job_sources_location"
        ),
    )
    source_key: Mapped[str] = mapped_column(String(100), nullable=False)
    provider: Mapped[JobSourceProvider] = mapped_column(
        Enum(JobSourceProvider, name="job_source_provider", **enum_kwargs),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    file_id: Mapped[str] = mapped_column(String(100), nullable=False)
    sheet_id: Mapped[str] = mapped_column(String(100), nullable=False)
    mapper_version: Mapped[str] = mapped_column(String(40), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_successful_sync_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    active_sync_run_id: Mapped[str | None] = mapped_column(String(36), index=True)
    sync_lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )


class JobSyncRun(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "job_sync_runs"
    __table_args__ = (
        Index("ix_job_sync_runs_source_started", "source_id", "started_at"),
    )
    source_id: Mapped[str] = mapped_column(
        ForeignKey("job_sources.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[JobSyncRunStatus] = mapped_column(
        Enum(JobSyncRunStatus, name="job_sync_run_status", **enum_kwargs),
        nullable=False,
    )
    pages_read: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    records_read: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    raw_snapshots_created: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    postings_created: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    postings_updated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    records_skipped_incomplete: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    error_code: Mapped[str | None] = mapped_column(String(80))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RawJobRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "raw_job_records"
    __table_args__ = (
        UniqueConstraint(
            "source_id",
            "external_record_id",
            "payload_hash",
            name="uq_raw_job_records_snapshot",
        ),
        Index("ix_raw_job_records_source_record", "source_id", "external_record_id"),
    )
    source_id: Mapped[str] = mapped_column(
        ForeignKey("job_sources.id", ondelete="RESTRICT"), nullable=False
    )
    external_record_id: Mapped[str] = mapped_column(String(100), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_fields: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class JobPosting(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "job_postings"
    __table_args__ = (
        UniqueConstraint(
            "source_id", "external_record_id", name="uq_job_postings_source_record"
        ),
        Index("ix_job_postings_status_updated", "status", "updated_at"),
    )
    source_id: Mapped[str] = mapped_column(
        ForeignKey("job_sources.id", ondelete="RESTRICT"), nullable=False
    )
    external_record_id: Mapped[str] = mapped_column(String(100), nullable=False)
    raw_record_id: Mapped[str] = mapped_column(
        ForeignKey("raw_job_records.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[JobPostingStatus] = mapped_column(
        Enum(JobPostingStatus, name="job_posting_status", **enum_kwargs),
        default=JobPostingStatus.PENDING_COMPLETION,
        nullable=False,
    )
    company_name: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description_text: Mapped[str | None] = mapped_column(Text)
    locations: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    recruitment_types: Mapped[list[str]] = mapped_column(
        JSON, default=list, nullable=False
    )
    industries: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    apply_url: Mapped[str] = mapped_column(Text, nullable=False)
    referral_code: Mapped[str | None] = mapped_column(String(255))
    deadline_text: Mapped[str | None] = mapped_column(String(255))
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    mapper_version: Mapped[str] = mapped_column(String(40), nullable=False)
    source_candidate: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    source_changed_since_review: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    gui_eligible: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    review_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class JobVerification(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "job_verifications"
    __table_args__ = (
        Index("ix_job_verifications_job_created", "job_id", "created_at"),
    )
    job_id: Mapped[str] = mapped_column(
        ForeignKey("job_postings.id", ondelete="CASCADE"), nullable=False
    )
    actor_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    from_status: Mapped[str] = mapped_column(String(40), nullable=False)
    to_status: Mapped[str] = mapped_column(String(40), nullable=False)
    review_version: Mapped[int] = mapped_column(Integer, nullable=False)
    field_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class UserJobSubmission(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "user_job_submissions"
    __table_args__ = (
        Index("ix_user_job_submissions_user_status_updated", "user_id", "status", "updated_at"),
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    input_type: Mapped[SubmissionInputType] = mapped_column(
        Enum(SubmissionInputType, name="job_submission_input_type", **enum_kwargs),
        nullable=False,
    )
    original_url: Mapped[str | None] = mapped_column(Text)
    original_jd: Mapped[str | None] = mapped_column(Text)
    input_preview: Mapped[str] = mapped_column(String(240), nullable=False)
    normalized_url: Mapped[str | None] = mapped_column(Text)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[SubmissionStatus] = mapped_column(
        Enum(SubmissionStatus, name="job_submission_status", **enum_kwargs),
        default=SubmissionStatus.DRAFT,
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    deduplication_status: Mapped[DeduplicationStatus] = mapped_column(
        Enum(DeduplicationStatus, name="job_deduplication_status", **enum_kwargs),
        default=DeduplicationStatus.PENDING,
        nullable=False,
    )
    deduplication_error_code: Mapped[str | None] = mapped_column(String(80))
    promoted_job_id: Mapped[str | None] = mapped_column(
        ForeignKey("job_postings.id", ondelete="SET NULL"), index=True
    )
    rejected_reason_code: Mapped[str | None] = mapped_column(String(80))


class JobDuplicateCandidate(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "job_duplicate_candidates"
    __table_args__ = (
        UniqueConstraint(
            "submission_id", "candidate_job_id", "generated_for_version",
            "algorithm_version", name="uq_job_duplicate_candidate_version",
        ),
        CheckConstraint(
            "score_basis_points >= 0 AND score_basis_points <= 10000",
            name="ck_job_duplicate_candidate_score",
        ),
        Index("ix_job_duplicate_candidates_submission_version", "submission_id", "generated_for_version"),
    )
    submission_id: Mapped[str] = mapped_column(
        ForeignKey("user_job_submissions.id", ondelete="CASCADE"), nullable=False
    )
    candidate_job_id: Mapped[str] = mapped_column(
        ForeignKey("job_postings.id", ondelete="CASCADE"), nullable=False
    )
    generated_for_version: Mapped[int] = mapped_column(Integer, nullable=False)
    score_basis_points: Mapped[int] = mapped_column(Integer, nullable=False)
    reasons: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    score_components: Mapped[dict[str, int]] = mapped_column(JSON, nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class JobSourceLink(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "job_source_links"
    __table_args__ = (
        UniqueConstraint(
            "job_id", "source_type", "source_record_ref", name="uq_job_source_link_record"
        ),
        CheckConstraint(
            "(source_type = 'tencent_smartsheet' AND source_id IS NOT NULL AND submission_id IS NULL) OR "
            "(source_type = 'user_submission' AND source_id IS NULL AND submission_id IS NOT NULL)",
            name="ck_job_source_link_reference",
        ),
        Index("ix_job_source_links_job_created", "job_id", "created_at"),
    )
    job_id: Mapped[str] = mapped_column(
        ForeignKey("job_postings.id", ondelete="CASCADE"), nullable=False
    )
    source_type: Mapped[JobSourceLinkType] = mapped_column(
        Enum(JobSourceLinkType, name="job_source_link_type", **enum_kwargs), nullable=False
    )
    source_id: Mapped[str | None] = mapped_column(
        ForeignKey("job_sources.id", ondelete="RESTRICT")
    )
    submission_id: Mapped[str | None] = mapped_column(
        ForeignKey("user_job_submissions.id", ondelete="RESTRICT")
    )
    source_record_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    normalized_url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class Profile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "profiles"
    __table_args__ = (UniqueConstraint("user_id", name="uq_profiles_user_id"),)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    local_sensitive_references: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )


class ResumeAsset(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "resume_assets"
    __table_args__ = (
        UniqueConstraint("object_key", name="uq_resume_assets_object_key"),
        Index("ix_resume_assets_profile_created", "profile_id", "created_at"),
    )
    profile_id: Mapped[str] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(120), nullable=False)
    plaintext_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    plaintext_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    encryption_version: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[ResumeAssetStatus] = mapped_column(
        Enum(ResumeAssetStatus, name="resume_asset_status", **enum_kwargs),
        nullable=False,
    )
    error_code: Mapped[str | None] = mapped_column(String(80))


class ResumeImport(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "resume_imports"
    __table_args__ = (
        Index("ix_resume_imports_profile_created", "profile_id", "created_at"),
    )
    profile_id: Mapped[str] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )
    asset_id: Mapped[str] = mapped_column(
        ForeignKey("resume_assets.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    parser_version: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[ResumeImportStatus] = mapped_column(
        Enum(ResumeImportStatus, name="resume_import_status", **enum_kwargs),
        nullable=False,
    )
    error_code: Mapped[str | None] = mapped_column(String(80))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProfileFieldEvidence(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "profile_field_evidence"
    __table_args__ = (
        Index(
            "ix_evidence_profile_field_created",
            "profile_id",
            "field_path",
            "created_at",
        ),
        UniqueConstraint(
            "resume_import_id", "sequence", name="uq_evidence_import_sequence"
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 100",
            name="ck_profile_field_evidence_confidence",
        ),
    )
    profile_id: Mapped[str] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )
    resume_import_id: Mapped[str] = mapped_column(
        ForeignKey("resume_imports.id", ondelete="CASCADE"), nullable=False
    )
    field_path: Mapped[str] = mapped_column(String(255), nullable=False)
    candidate_value: Mapped[dict[str, Any] | list[Any] | str | int | float | bool | None] = mapped_column(  # noqa: E501
        JSON, nullable=False
    )
    evidence_excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[int] = mapped_column(Integer, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class ProfileFieldDecision(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "profile_field_decisions"
    __table_args__ = (
        Index("ix_decisions_evidence_created", "evidence_id", "created_at"),
    )
    profile_id: Mapped[str] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )
    evidence_id: Mapped[str] = mapped_column(
        ForeignKey("profile_field_evidence.id", ondelete="CASCADE"), nullable=False
    )
    actor_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    action: Mapped[EvidenceDecisionAction] = mapped_column(
        Enum(EvidenceDecisionAction, name="profile_evidence_decision_action", **enum_kwargs),
        nullable=False,
    )
    resolved_value: Mapped[dict[str, Any] | list[Any] | str | int | float | bool | None] = mapped_column(  # noqa: E501
        JSON, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class ConfirmedProfileVersion(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "confirmed_profile_versions"
    __table_args__ = (
        UniqueConstraint(
            "profile_id", "version_number", name="uq_confirmed_version_number"
        ),
        Index("ix_confirmed_versions_profile_created", "profile_id", "created_at"),
    )
    profile_id: Mapped[str] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    aggregate_version: Mapped[int] = mapped_column(Integer, nullable=False)
    facts_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    evidence_refs: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    local_sensitive_references: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class JobFeedback(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "job_feedback"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "job_id", "category", name="uq_job_feedback_user_job_category"
        ),
        Index("ix_job_feedback_job_status_updated", "job_id", "status", "updated_at"),
        Index("ix_job_feedback_status_updated", "status", "updated_at"),
    )
    job_id: Mapped[str] = mapped_column(
        ForeignKey("job_postings.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    category: Mapped[JobFeedbackCategory] = mapped_column(
        Enum(JobFeedbackCategory, name="job_feedback_category", **enum_kwargs),
        nullable=False,
    )
    status: Mapped[JobFeedbackStatus] = mapped_column(
        Enum(JobFeedbackStatus, name="job_feedback_status", **enum_kwargs),
        default=JobFeedbackStatus.OPEN,
        nullable=False,
    )
    note: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class JobFeedbackEvent(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "job_feedback_events"
    __table_args__ = (
        UniqueConstraint(
            "actor_user_id", "idempotency_key",
            name="uq_job_feedback_events_actor_key",
        ),
        Index("ix_job_feedback_events_feedback_created", "feedback_id", "created_at"),
    )
    feedback_id: Mapped[str] = mapped_column(
        ForeignKey("job_feedback.id", ondelete="CASCADE"), nullable=False
    )
    actor_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    action: Mapped[JobFeedbackAction] = mapped_column(
        Enum(JobFeedbackAction, name="job_feedback_action", **enum_kwargs),
        nullable=False,
    )
    from_status: Mapped[str | None] = mapped_column(String(40))
    to_status: Mapped[str] = mapped_column(String(40), nullable=False)
    feedback_version: Mapped[int] = mapped_column(Integer, nullable=False)
    redacted_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
