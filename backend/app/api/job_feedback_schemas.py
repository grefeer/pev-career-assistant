from __future__ import annotations

from datetime import datetime, timezone
from typing import Self

from pydantic import BaseModel, Field, field_validator, model_validator

from backend.app.domain.job_feedback import (
    FEEDBACK_NOTE_MAX_LENGTH,
    FeedbackAdminDecision,
    FeedbackStudentAction,
    JobFeedbackCategory,
    JobFeedbackStatus,
)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class StudentFeedbackItem(BaseModel):
    id: str
    job_id: str
    category: JobFeedbackCategory
    status: JobFeedbackStatus
    note: str | None
    version: int
    created_at: datetime
    updated_at: datetime

    _normalise_times = field_validator("created_at", "updated_at", mode="before")(_as_utc)


class StudentFeedbackListResponse(BaseModel):
    feedback: list[StudentFeedbackItem]


class FeedbackMutationRequest(BaseModel):
    action: FeedbackStudentAction
    category: JobFeedbackCategory
    expected_version: int | None = Field(default=None, ge=0)
    note: str | None = Field(default=None, max_length=FEEDBACK_NOTE_MAX_LENGTH)

    @model_validator(mode="after")
    def require_version_for_withdraw(self) -> Self:
        if self.action is FeedbackStudentAction.WITHDRAW and self.expected_version is None:
            raise ValueError("withdraw requires expected_version")
        if self.action is FeedbackStudentAction.WITHDRAW and self.note is not None:
            raise ValueError("withdraw does not accept note")
        return self


class FeedbackMutationResponse(BaseModel):
    id: str
    job_id: str
    category: JobFeedbackCategory
    status: JobFeedbackStatus
    version: int
    updated_at: datetime

    _normalise_updated_at = field_validator("updated_at", mode="before")(_as_utc)


class AdminFeedbackDetail(BaseModel):
    id: str
    job_id: str
    company_name: str
    title: str
    job_status: str
    job_review_version: int
    category: JobFeedbackCategory
    status: JobFeedbackStatus
    note: str | None
    version: int
    created_at: datetime
    updated_at: datetime

    _normalise_times = field_validator("created_at", "updated_at", mode="before")(_as_utc)


class AdminFeedbackAggregate(BaseModel):
    job_id: str
    company_name: str
    title: str
    category: JobFeedbackCategory
    open_count: int
    accepted_count: int
    total_count: int
    latest_updated_at: datetime

    _normalise_latest = field_validator("latest_updated_at", mode="before")(_as_utc)


class AdminFeedbackQueueResponse(BaseModel):
    total: int
    feedback: list[AdminFeedbackDetail]
    aggregates: list[AdminFeedbackAggregate]


class AdminFeedbackDecisionRequest(BaseModel):
    decision: FeedbackAdminDecision
    expected_version: int = Field(ge=0)
