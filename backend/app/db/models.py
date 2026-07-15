from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
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


class ApplicationTask(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "application_tasks"
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    target_job_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    snapshot_id: Mapped[str | None] = mapped_column(String(36), index=True)
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


class JobSyncRunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"


class JobPostingStatus(StrEnum):
    PENDING_COMPLETION = "pending_completion"


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
