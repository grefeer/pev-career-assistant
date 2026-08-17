"""Shared ``StrEnum`` contracts for persistence models.

These enums were previously scattered across per-skill domain modules that
also carried retired transition helpers.  They are kept in one place because
the ORM models (and only the ORM models) consume them; business transitions
for live skills live in the respective service/skill layers.
"""

from __future__ import annotations

from enum import StrEnum


class ApplicationStatus(StrEnum):
    """Lifecycle of one tracked application."""

    saved = "saved"
    applied = "applied"
    screening = "screening"
    interview = "interview"
    offer = "offer"
    rejected = "rejected"
    withdrawn = "withdrawn"


class CompanyResearchBlockReason(StrEnum):
    """Why a company-research report could not complete autonomously."""

    anti_bot = "anti_bot"
    login_required = "login_required"
    captcha = "captcha"
    no_evidence = "no_evidence"
    artifact_error = "artifact_error"


class CompanyResearchStatus(StrEnum):
    """Lifecycle of one company-research report."""

    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    needs_manual_review = "needs_manual_review"
    failed = "failed"
    cancelled = "cancelled"


class DeduplicationStatus(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class InterviewPrepKitStatus(StrEnum):
    """Lifecycle of one interview-prep kit."""

    generating = "generating"
    ready = "ready"
    failed = "failed"


class JobFeedbackAction(StrEnum):
    SUBMITTED = "submitted"
    UPDATED = "updated"
    WITHDRAWN = "withdrawn"
    ACCEPTED = "accepted"
    RESOLVED = "resolved"
    REJECTED = "rejected"


class JobFeedbackCategory(StrEnum):
    CLOSED = "closed"
    APPLICATION_CHANNEL_UNAVAILABLE = "application_channel_unavailable"
    CONTENT_CHANGED = "content_changed"
    INCORRECT_INFORMATION = "incorrect_information"


class JobFeedbackStatus(StrEnum):
    OPEN = "open"
    ACCEPTED = "accepted"
    RESOLVED = "resolved"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


class JobSourceLinkType(StrEnum):
    TENCENT_SMARTSHEET = "tencent_smartsheet"
    USER_SUBMISSION = "user_submission"


class SubmissionInputType(StrEnum):
    URL = "url"
    JD_TEXT = "jd_text"


class SubmissionStatus(StrEnum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    PROMOTED = "promoted"
    REJECTED = "rejected"
