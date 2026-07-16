from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from backend.app.db.models import JobPostingStatus


ReviewQueueStatus = Literal[
    "pending_completion",
    "pending_review",
    "rejected",
]


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class JobSummary(BaseModel):
    id: str
    company_name: str
    title: str
    locations: list[str]
    recruitment_types: list[str]
    industries: list[str]
    apply_url: str
    deadline_text: str | None
    status: JobPostingStatus
    gui_eligible: bool
    source_key: str
    source_name: str
    updated_at: datetime

    _normalize_updated_at = field_validator("updated_at", mode="before")(_as_utc)


class JobDetail(JobSummary):
    description_text: str
    referral_code: str | None
    verified_at: datetime | None

    _normalize_verified_at = field_validator("verified_at", mode="before")(_as_utc)


class JobListResponse(BaseModel):
    total: int
    jobs: list[JobSummary]


class JobSourceCandidate(BaseModel):
    company_name: str | None = None
    title: str | None = None
    locations: list[str] = Field(default_factory=list)
    recruitment_types: list[str] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)
    apply_url: str | None = None
    referral_code: str | None = None
    deadline_text: str | None = None


class AdminJobDetail(JobSummary):
    description_text: str | None
    referral_code: str | None
    source_candidate: JobSourceCandidate
    source_changed_since_review: bool
    review_version: int


class AdminJobListResponse(BaseModel):
    total: int
    jobs: list[AdminJobDetail]


class JobCompletionRequest(BaseModel):
    expected_version: int = Field(ge=0)
    company_name: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=2000)
    description_text: str = Field(min_length=1, max_length=100_000)
    locations: list[str] = Field(max_length=100)
    recruitment_types: list[str] = Field(max_length=20)
    industries: list[str] = Field(max_length=50)
    apply_url: str = Field(min_length=1, max_length=4096)
    referral_code: str | None = Field(default=None, max_length=255)
    deadline_text: str | None = Field(default=None, max_length=255)


class JobDecisionRequest(BaseModel):
    expected_version: int = Field(ge=0)
    decision: Literal["verify", "reject", "expire"]
    gui_eligible: bool = False
    reason_code: str | None = Field(default=None, max_length=80)
