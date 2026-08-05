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
    Float,
    ForeignKey,
    ForeignKeyConstraint,
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

from backend.app.domain.preferences import (
    JobInteractionType,
    WorkModePreference,
)
from backend.app.domain.personalized_discovery import (
    RecommendationPresentationState,
    SourceStatusReason,
)
from backend.app.domain.company_research import (
    CompanyResearchBlockReason,
    CompanyResearchStatus,
)
from backend.app.domain.interview_prep import InterviewPrepKitStatus
from backend.app.domain.application_tracking import ApplicationStatus
from backend.app.domain.agent_runtime import (
    AgentRole,
    ComplexityLevel,
    RunStatus,
    StepStatus,
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
    attachment_refs: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    approval_idempotency_key: Mapped[str] = mapped_column(
        String(96), nullable=False
    )
    approved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    approved_by: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)

    __table_args__ = (
        UniqueConstraint("draft_id"),
        UniqueConstraint(
            "approved_by",
            "approval_idempotency_key",
            name="uq_approved_resume_versions_idempotency",
        ),
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
    adapter_id: Mapped[str | None] = mapped_column(String(64))
    adapter_version: Mapped[str | None] = mapped_column(String(16))
    adapter_status_at_dispatch: Mapped[str | None] = mapped_column(String(24))
    site_adapter_error_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    site_adapter_last_error: Mapped[str | None] = mapped_column(String(256))


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


# ── Site Adapter ───────────────────────────────────────────────────────────────


class SiteAdapterStatus(StrEnum):
    ACTIVE = "active"
    CIRCUIT_BREAKER_OPEN = "circuit_breaker_open"
    DEPRECATED = "deprecated"
    READONLY = "readonly"
    STAGED = "staged"


class SiteAdapter(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "site_adapters"
    __table_args__ = (
        UniqueConstraint("adapter_id", name="uq_site_adapters_adapter_id"),
    )
    adapter_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[str] = mapped_column(String(16), nullable=False)
    supported_domains: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="active"
    )
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    circuit_breaker_open: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    rollout_stage: Mapped[str] = mapped_column(
        String(24), nullable=False, default="readonly"
    )
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_readonly_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class ObservedSite(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "observed_sites"
    __table_args__ = (
        UniqueConstraint("site_code", name="uq_observed_sites_site_code"),
    )
    site_code: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    domains: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    page_samples_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="observing"
    )
    adapter_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="observation"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


# ── Job Discovery Agent ────────────────────────────────────────────────────────


class JobDiscoveryTaskStatus(StrEnum):
    queued = "queued"
    running = "running"
    partial_success = "partial_success"
    succeeded = "succeeded"
    needs_manual_review = "needs_manual_review"
    failed = "failed"
    cancelled = "cancelled"


class DiscoveredJobCandidateStatus(StrEnum):
    pending_review = "pending_review"
    approved = "approved"
    rejected = "rejected"
    merged = "merged"
    needs_manual_review = "needs_manual_review"


class DiscoveryBlockReason(StrEnum):
    login_required = "login_required"
    captcha = "captcha"
    anti_bot = "anti_bot"
    wechat_unavailable = "wechat_unavailable"
    permission_denied = "permission_denied"
    invalid_url = "invalid_url"
    timeout = "timeout"
    budget_exceeded = "budget_exceeded"
    parse_failed = "parse_failed"
    unknown = "unknown"


class JobDiscoveryTask(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "job_discovery_tasks"
    __table_args__ = (
        UniqueConstraint(
            "source_id", "external_record_id", "url_hash", "payload_hash", "agent_version",
            name="uq_job_discovery_tasks_source_record",
        ),
        Index("ix_job_discovery_tasks_status_lease_created", "status", "lease_expires_at", "created_at"),
        Index("ix_job_discovery_tasks_raw_record_id", "raw_record_id"),
    )
    source_id: Mapped[str] = mapped_column(
        ForeignKey("job_sources.id", ondelete="RESTRICT"), nullable=False
    )
    raw_record_id: Mapped[str] = mapped_column(
        ForeignKey("raw_job_records.id", ondelete="RESTRICT"), nullable=False
    )
    external_record_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_key: Mapped[str] = mapped_column(String(64), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    url_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False
    )
    agent_version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[JobDiscoveryTaskStatus] = mapped_column(
        Enum(JobDiscoveryTaskStatus, name="job_discovery_task_status", **enum_kwargs),
        default=JobDiscoveryTaskStatus.queued,
        index=True,
        nullable=False,
    )
    block_reason: Mapped[DiscoveryBlockReason | None] = mapped_column(
        Enum(DiscoveryBlockReason, name="discovery_block_reason", **enum_kwargs),
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    budget_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    result_summary_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class JobDiscoveryEvidence(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "job_discovery_evidence"
    __table_args__ = (
        UniqueConstraint(
            "task_id", "evidence_type", "content_hash",
            name="uq_job_discovery_evidence_task_type_hash",
        ),
        Index("ix_job_discovery_evidence_task_created", "task_id", "created_at"),
    )
    task_id: Mapped[str] = mapped_column(
        ForeignKey("job_discovery_tasks.id", ondelete="CASCADE"), nullable=False
    )
    evidence_type: Mapped[str] = mapped_column(String(32), nullable=False)
    url: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(String(512))
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    text_excerpt: Mapped[str | None] = mapped_column(Text)
    storage_uri: Mapped[str | None] = mapped_column(String(1024))
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class DiscoveredJobCandidate(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "discovered_job_candidates"
    __table_args__ = (
        Index(
            "ix_discovered_job_candidates_status_group_created",
            "status", "similarity_group_key", "created_at",
        ),
        Index(
            "ix_discovered_job_candidates_source_record",
            "source_id", "external_record_id",
        ),
    )
    task_id: Mapped[str] = mapped_column(
        ForeignKey("job_discovery_tasks.id", ondelete="CASCADE"), nullable=False
    )
    source_id: Mapped[str] = mapped_column(
        ForeignKey("job_sources.id", ondelete="RESTRICT"), nullable=False
    )
    raw_record_id: Mapped[str] = mapped_column(
        ForeignKey("raw_job_records.id", ondelete="RESTRICT"), nullable=False
    )
    external_record_id: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False
    )
    similarity_group_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[DiscoveredJobCandidateStatus] = mapped_column(
        Enum(
            DiscoveredJobCandidateStatus,
            name="discovered_job_candidate_status",
            **enum_kwargs,
        ),
        default=DiscoveredJobCandidateStatus.pending_review,
        nullable=False,
    )
    title: Mapped[str | None] = mapped_column(String(512))
    company_name: Mapped[str | None] = mapped_column(String(256))
    department: Mapped[str | None] = mapped_column(String(256))
    description_text: Mapped[str | None] = mapped_column(Text)
    responsibilities: Mapped[str | None] = mapped_column(Text)
    requirements: Mapped[str | None] = mapped_column(Text)
    locations_json: Mapped[list[str] | None] = mapped_column(JSON)
    recruitment_types_json: Mapped[list[str] | None] = mapped_column(JSON)
    industries_json: Mapped[list[str] | None] = mapped_column(JSON)
    apply_url: Mapped[str | None] = mapped_column(String(2048))
    application_channel_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    deadline_text: Mapped[str | None] = mapped_column(String(256))
    referral_code: Mapped[str | None] = mapped_column(String(256))
    confidence: Mapped[float | None] = mapped_column(Float)
    evidence_refs_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    normalization_warnings_json: Mapped[list[str] | None] = mapped_column(JSON)


# ---------------------------------------------------------------------------
# Strategy Router tables
# ---------------------------------------------------------------------------


class JobDiscoveryStrategy(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "job_discovery_strategies"
    __table_args__ = (
        Index("ix_job_discovery_strategies_pattern", "url_pattern"),
        Index("ix_job_discovery_strategies_status", "status"),
    )
    url_pattern: Mapped[str] = mapped_column(String(500), nullable=False)
    site_type: Mapped[str] = mapped_column(
        String(50), default="other", nullable=False, index=True
    )
    description: Mapped[str | None] = mapped_column(Text)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    adapter: Mapped[str | None] = mapped_column(String(500))
    plan_yaml: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default="active", nullable=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    total_runs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    success_runs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    fallback_runs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    consecutive_ok: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error_tool: Mapped[str | None] = mapped_column(String(100))
    last_error_reason: Mapped[str | None] = mapped_column(String(50))
    last_error_message: Mapped[str | None] = mapped_column(Text)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    success_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    avg_duration_s: Mapped[float | None] = mapped_column(Float)
    degradation_threshold: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    recovery_threshold: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    last_health_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    _trajectories: Mapped[list["JobDiscoveryTrajectory"]] = relationship(
        back_populates="_strategy", lazy="raise",
    )


class JobDiscoveryTrajectory(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "job_discovery_trajectories"
    __table_args__ = (
        Index("ix_job_discovery_trajectories_url_pattern", "url_pattern"),
        Index("ix_job_discovery_trajectories_created", "created_at"),
        ForeignKeyConstraint(
            ["task_id"], ["job_discovery_tasks.id"],
            name="fk_job_discovery_trajectories_task_id", ondelete="SET NULL",
        ),
    )
    task_id: Mapped[str | None] = mapped_column(String(36))
    strategy_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("job_discovery_strategies.id", ondelete="SET NULL"),
    )
    executor_type: Mapped[str] = mapped_column(String(20), nullable=False)
    overall_status: Mapped[str] = mapped_column(String(30), nullable=False)
    failed_at_step: Mapped[int | None] = mapped_column(Integer)
    failed_tool: Mapped[str | None] = mapped_column(String(100))
    failed_params: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    failed_error_type: Mapped[str | None] = mapped_column(String(100))
    failed_error_message: Mapped[str | None] = mapped_column(Text)
    failed_error_reason: Mapped[str | None] = mapped_column(String(50))
    completed_steps: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=list)
    fallback_trace: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    clean_path: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    annotations: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    url: Mapped[str | None] = mapped_column(Text)
    url_pattern: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    _strategy: Mapped["JobDiscoveryStrategy | None"] = relationship(
        back_populates="_trajectories", lazy="raise",
    )


# ---------------------------------------------------------------------------
# Personal memory: preferences + interactions + relevance scores
# (personal-mode application assistant). Preferences complement the parsed
# profile/resume facts; interactions feed the relevance feedback loop;
# relevance scores cache the cheap ranker output keyed by profile+pref
# version so the expensive MatchService only runs on ranked top-N.
# ---------------------------------------------------------------------------


class UserPreference(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "user_preferences"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_user_preferences_user"),
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # What the user is looking for (complements who they are, in the profile).
    desired_roles: Mapped[list[str] | None] = mapped_column(JSON)
    target_cities: Mapped[list[str] | None] = mapped_column(JSON)
    salary_min: Mapped[int | None] = mapped_column(Integer)
    salary_max: Mapped[int | None] = mapped_column(Integer)
    excluded_companies: Mapped[list[str] | None] = mapped_column(JSON)
    excluded_industries: Mapped[list[str] | None] = mapped_column(JSON)
    preferred_industries: Mapped[list[str] | None] = mapped_column(JSON)
    preferred_recruitment_types: Mapped[list[str] | None] = mapped_column(JSON)
    work_mode: Mapped[WorkModePreference | None] = mapped_column(
        Enum(WorkModePreference, name="work_mode_preference", **enum_kwargs)
    )
    is_active_search: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    notes: Mapped[str | None] = mapped_column(Text)
    # Personalized discovery v1: synonyms broaden title recall (e.g. "agent" /
    # "Agent开发" map to the AI-application role); excluded roles always win.
    role_synonyms: Mapped[list[str] | None] = mapped_column(JSON)
    excluded_roles: Mapped[list[str] | None] = mapped_column(JSON)
    # User-controlled relevance threshold (0..100) for personalized delivery.
    personalized_discovery_min_score: Mapped[float | None] = mapped_column(Float)
    # Optimistic lock; bumping this invalidates cached relevance scores.
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class UserJobInteraction(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "user_job_interactions"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "job_id", "interaction_type",
            name="uq_user_job_interactions_user_job_type",
        ),
        Index("ix_user_job_interactions_user_created", "user_id", "created_at"),
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    job_id: Mapped[str] = mapped_column(
        ForeignKey("job_postings.id", ondelete="CASCADE"), nullable=False
    )
    interaction_type: Mapped[JobInteractionType] = mapped_column(
        Enum(JobInteractionType, name="job_interaction_type", **enum_kwargs),
        nullable=False,
    )
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class JobRelevanceScore(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "job_relevance_scores"
    __table_args__ = (
        UniqueConstraint(
            "job_id", "profile_version_id", "preferences_version",
            name="uq_job_relevance_scores_job_profile_prefs",
        ),
        Index("ix_job_relevance_scores_user_score", "user_id", "score"),
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    job_id: Mapped[str] = mapped_column(
        ForeignKey("job_postings.id", ondelete="CASCADE"), nullable=False
    )
    profile_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("confirmed_profile_versions.id", ondelete="SET NULL")
    )
    preferences_version: Mapped[int] = mapped_column(Integer, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    matched_signals_json: Mapped[list[str] | None] = mapped_column(JSON)
    scored_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


# ---------------------------------------------------------------------------
# Personalized discovery v1 - owner-scoped pre-review delivery.
#
# A user receives recommendations built from retained SHARED discovery tasks
# (already-completed JobDiscoveryTask rows + their DiscoveredJobCandidate
# evidence). These tables never mutate JobPosting / review_version / the
# verified-only /jobs path; they are a separate user-scoped delivery channel.
# Recommendation candidate/task FKs are RESTRICT so a delivered recommendation
# retains its evidence trace while it exists - retention cleanup deletes these
# rows before deleting the source task/candidate.
# ---------------------------------------------------------------------------


class PersonalizedDiscoveryRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "personalized_discovery_runs"
    __table_args__ = (
        Index("ix_pdr_user_started", "user_id", "started_at"),
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    preference_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class PersonalizedDiscoveryRecommendation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "personalized_discovery_recommendations"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "canonical_job_key",
            name="uq_pdr_user_canonical_job_key",
        ),
        Index("ix_pdr_rec_user_score", "user_id", "relevance_score"),
        Index("ix_pdr_rec_user_state", "user_id", "presentation_state"),
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("discovered_job_candidates.id", ondelete="RESTRICT"),
        nullable=False,
    )
    task_id: Mapped[str] = mapped_column(
        ForeignKey("job_discovery_tasks.id", ondelete="RESTRICT"), nullable=False
    )
    last_run_id: Mapped[str] = mapped_column(
        ForeignKey("personalized_discovery_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    canonical_job_key: Mapped[str] = mapped_column(String(128), nullable=False)
    preference_version: Mapped[int] = mapped_column(Integer, nullable=False)
    relevance_score: Mapped[float] = mapped_column(Float, nullable=False)
    relevance_reason: Mapped[str | None] = mapped_column(Text)
    matched_signals_json: Mapped[list[str] | None] = mapped_column(JSON)
    presentation_state: Mapped[RecommendationPresentationState] = mapped_column(
        Enum(
            RecommendationPresentationState,
            name="recommendation_presentation_state",
            **enum_kwargs,
        ),
        default=RecommendationPresentationState.NEW,
        nullable=False,
    )


class UserDiscoverySourceStatus(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "user_discovery_source_statuses"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "run_id",
            "task_id",
            "reason_code",
            name="uq_udss_user_run_task_reason",
        ),
        Index("ix_udss_user_run", "user_id", "run_id"),
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[str] = mapped_column(
        ForeignKey("personalized_discovery_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    task_id: Mapped[str] = mapped_column(
        ForeignKey("job_discovery_tasks.id", ondelete="CASCADE"), nullable=False
    )
    source_key: Mapped[str] = mapped_column(String(64), nullable=False)
    safe_source_url: Mapped[str] = mapped_column(Text, nullable=False)
    reason_code: Mapped[SourceStatusReason] = mapped_column(
        Enum(SourceStatusReason, name="source_status_reason", **enum_kwargs),
        nullable=False,
    )
    display_text: Mapped[str] = mapped_column(Text, nullable=False)
    retry_guidance: Mapped[str] = mapped_column(Text, nullable=False)


# ---------------------------------------------------------------------------
# Company-research skill
# ---------------------------------------------------------------------------


class CompanyResearchReport(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One user-scoped company-research result.

    Read-only research output: a company profile plus its open positions,
    built from a public careers/about URL.  Never an auto-submit and never a
    JobPosting - the verified-only ``/api/jobs`` path is decoupled from this.
    """

    __tablename__ = "company_research_reports"
    __table_args__ = (
        Index(
            "ix_company_research_user_status_created",
            "user_id",
            "status",
            "created_at",
        ),
        Index("ix_company_research_url_hash", "source_url_hash"),
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    company_name: Mapped[str] = mapped_column(String(256), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    source_url_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[CompanyResearchStatus] = mapped_column(
        Enum(
            CompanyResearchStatus,
            name="company_research_status",
            **enum_kwargs,
        ),
        default=CompanyResearchStatus.queued,
        index=True,
        nullable=False,
    )
    block_reason: Mapped[CompanyResearchBlockReason | None] = mapped_column(
        Enum(
            CompanyResearchBlockReason,
            name="company_research_block_reason",
            **enum_kwargs,
        ),
    )
    profile_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    openings_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    evidence_refs_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    summary: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class InterviewPrepKit(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One user-scoped interview-prep kit.

    Agent-driven generation output: an LLM read the target job snapshot (plus,
    optionally, the user's confirmed profile facts + preferences) and produced
    a structured prep kit.  Read-only study material - never an auto-submit and
    never a JobPosting.
    """

    __tablename__ = "interview_prep_kits"
    __table_args__ = (
        Index(
            "ix_interview_prep_user_status_created",
            "user_id",
            "status",
            "created_at",
        ),
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    target_job_id: Mapped[str | None] = mapped_column(
        ForeignKey("job_postings.id", ondelete="SET NULL"), index=True
    )
    profile_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("confirmed_profile_versions.id", ondelete="SET NULL"),
        index=True,
    )
    job_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    agent_version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[InterviewPrepKitStatus] = mapped_column(
        Enum(
            InterviewPrepKitStatus,
            name="interview_prep_kit_status",
            **enum_kwargs,
        ),
        default=InterviewPrepKitStatus.generating,
        index=True,
        nullable=False,
    )
    content_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    preferences_summary_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    match_analysis_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class ApplicationRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One user-scoped tracked job application (NOT the GUI executor task).

    A personal tracker entry the user maintains by hand: the job they saved or
    applied to, where they found it, and how far it has progressed through the
    state machine.  ``target_job_id`` optionally links to a verified
    ``JobPosting``; off-platform applications carry company/title directly.
    The platform never auto-submits (security gate #1) - status advances are an
    explicit human action, each recorded in :class:`ApplicationRecordEvent`.
    """

    __tablename__ = "application_records"
    __table_args__ = (
        Index(
            "ix_application_records_user_status_created",
            "user_id",
            "status",
            "created_at",
        ),
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    target_job_id: Mapped[str | None] = mapped_column(
        ForeignKey("job_postings.id", ondelete="SET NULL"), index=True
    )
    company_name: Mapped[str] = mapped_column(String(200), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    apply_url: Mapped[str | None] = mapped_column(String(1024))
    source: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[ApplicationStatus] = mapped_column(
        Enum(
            ApplicationStatus,
            name="application_record_status",
            **enum_kwargs,
        ),
        default=ApplicationStatus.saved,
        index=True,
        nullable=False,
    )
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    state_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class ApplicationRecordEvent(Base):
    """Append-only audit row for one application status transition."""

    __tablename__ = "application_record_events"
    __table_args__ = (
        Index(
            "ix_application_record_events_app_created",
            "application_id",
            "created_at",
        ),
    )
    id: Mapped[int] = mapped_column(audit_id_type, primary_key=True, autoincrement=True)
    application_id: Mapped[str] = mapped_column(
        ForeignKey("application_records.id", ondelete="CASCADE"), nullable=False
    )
    from_status: Mapped[str] = mapped_column(String(40), nullable=False)
    to_status: Mapped[str] = mapped_column(String(40), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


# ---------------------------------------------------------------------------
# Adaptive Planner–Executor–Verifier runtime.  These rows are deliberately
# separate from LangGraph/checkpoint state: MySQL is the durable source of
# truth for what a user asked, what autonomous roles observed, and why a run
# ended.  JSON columns only hold schema-whitelisted summaries and artifact refs.
# ---------------------------------------------------------------------------


class AgentRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One user-scoped adaptive PEV run and its terminal summary."""

    __tablename__ = "agent_runs"
    __table_args__ = (
        Index("ix_agent_runs_user_status_created", "user_id", "status", "created_at"),
    )

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    allowed_skills_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    context_summary_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    budget_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    agent_version: Mapped[str] = mapped_column(String(64), nullable=False)
    complexity: Mapped[ComplexityLevel | None] = mapped_column(
        Enum(ComplexityLevel, name="agent_complexity_level", **enum_kwargs)
    )
    status: Mapped[RunStatus] = mapped_column(
        Enum(RunStatus, name="agent_run_status", **enum_kwargs),
        default=RunStatus.queued,
        nullable=False,
        index=True,
    )
    final_summary: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class AgentPlan(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A versioned Planner output for one run."""

    __tablename__ = "agent_plans"
    __table_args__ = (
        UniqueConstraint("run_id", "revision", name="uq_agent_plans_run_revision"),
        Index("ix_agent_plans_run_revision", "run_id", "revision"),
    )

    run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    complexity: Mapped[ComplexityLevel] = mapped_column(
        Enum(ComplexityLevel, name="agent_plan_complexity_level", **enum_kwargs),
        nullable=False,
    )
    plan_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_by: Mapped[AgentRole] = mapped_column(
        Enum(AgentRole, name="agent_role", **enum_kwargs),
        default=AgentRole.planner,
        nullable=False,
    )


class AgentStep(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Durable execution record for one ordered Planner step."""

    __tablename__ = "agent_steps"
    __table_args__ = (
        UniqueConstraint("plan_id", "sequence", name="uq_agent_steps_plan_sequence"),
        Index("ix_agent_steps_run_sequence", "run_id", "sequence"),
    )

    run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("agent_plans.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    allowed_skills_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    status: Mapped[StepStatus] = mapped_column(
        Enum(StepStatus, name="agent_step_status", **enum_kwargs),
        default=StepStatus.planned,
        nullable=False,
    )
    input_summary_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    output_artifact_refs_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(64))


class AgentArtifact(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Immutable tool-observed evidence that later PEV steps can reference."""

    __tablename__ = "agent_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "step_id",
            "artifact_type",
            "content_hash",
            name="uq_agent_artifacts_step_type_content_hash",
        ),
        Index("ix_agent_artifacts_run_created", "run_id", "created_at"),
    )

    run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    step_id: Mapped[str] = mapped_column(
        ForeignKey("agent_steps.id", ondelete="CASCADE"), nullable=False
    )
    artifact_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_url: Mapped[str] = mapped_column(String(2_048), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_by: Mapped[AgentRole] = mapped_column(
        Enum(AgentRole, name="agent_artifact_role", **enum_kwargs),
        default=AgentRole.executor,
        nullable=False,
    )


class AgentTurn(Base):
    """A role decision summary, excluding raw prompts and private material."""

    __tablename__ = "agent_turns"
    __table_args__ = (
        UniqueConstraint("run_id", "role", "turn_index", name="uq_agent_turns_run_role_index"),
        Index("ix_agent_turns_run_created", "run_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(audit_id_type, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[AgentRole] = mapped_column(
        Enum(AgentRole, name="agent_turn_role", **enum_kwargs), nullable=False
    )
    turn_index: Mapped[int] = mapped_column(Integer, nullable=False)
    decision_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    model_name: Mapped[str | None] = mapped_column(String(128))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    context_manifest: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class AgentEvent(Base):
    """Append-only, sequence-addressable trace event for UI and audit."""

    __tablename__ = "agent_events"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_agent_events_run_sequence"),
        Index("ix_agent_events_run_sequence", "run_id", "sequence"),
    )

    id: Mapped[int] = mapped_column(audit_id_type, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
