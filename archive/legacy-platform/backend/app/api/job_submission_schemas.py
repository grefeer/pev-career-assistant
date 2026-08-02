from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.app.domain.job_submissions import (
    DeduplicationStatus,
    SubmissionInputType,
    SubmissionStatus,
)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class JobSubmissionCreateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    input_type: SubmissionInputType
    url: str | None = Field(default=None, max_length=4096)
    jd_text: str | None = Field(default=None, max_length=100_000)

    @model_validator(mode="after")
    def validate_matching_input(self) -> Self:
        if self.input_type is SubmissionInputType.URL and self.url and self.jd_text is None:
            return self
        if self.input_type is SubmissionInputType.JD_TEXT and self.jd_text and self.url is None:
            return self
        raise ValueError("input_type must select exactly one non-empty input")


class JobSubmissionUpdateRequest(JobSubmissionCreateRequest):
    expected_version: int = Field(ge=0)


class JobSubmissionSubmitRequest(BaseModel):
    expected_version: int = Field(ge=0)


class JobSubmissionResponse(BaseModel):
    id: str
    input_type: SubmissionInputType
    input_preview: str
    normalized_url: str | None
    status: SubmissionStatus
    version: int
    deduplication_status: DeduplicationStatus
    deduplication_error_code: str | None
    promoted_job_id: str | None
    created_at: datetime
    updated_at: datetime

    _normalize_created = field_validator("created_at", mode="before")(_as_utc)
    _normalize_updated = field_validator("updated_at", mode="before")(_as_utc)


class AdminJobSubmissionResponse(JobSubmissionResponse):
    content_sha256: str


class JobSubmissionListResponse(BaseModel):
    total: int
    submissions: list[JobSubmissionResponse]


class AdminJobSubmissionListResponse(BaseModel):
    total: int
    submissions: list[AdminJobSubmissionResponse]


class DuplicateJobSummary(BaseModel):
    id: str
    company_name: str
    title: str
    status: str
    apply_url: str


class DuplicateCandidateResponse(BaseModel):
    job: DuplicateJobSummary
    score_basis_points: int = Field(ge=0, le=10_000)
    reasons: list[str]
    score_components: dict[str, int]
    algorithm_version: Literal["manual-job-dedup-v1"]


class DuplicateCandidateListResponse(BaseModel):
    candidates: list[DuplicateCandidateResponse]


class AdminJobSubmissionDecisionRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    expected_version: int = Field(ge=0)
    action: Literal["link_existing", "create_pending", "reject"]
    job_id: str | None = Field(default=None, min_length=36, max_length=36)
    company_name: str | None = Field(default=None, min_length=1, max_length=255)
    title: str | None = Field(default=None, min_length=1, max_length=2000)
    apply_url: str | None = Field(default=None, max_length=4096)
    reason_code: Literal[
        "not_a_job", "insufficient_evidence", "unsafe_link", "duplicate_submission"
    ] | None = None

    @model_validator(mode="after")
    def validate_action_fields(self) -> Self:
        if self.action == "link_existing" and self.job_id and not any(
            (self.company_name, self.title, self.apply_url, self.reason_code)
        ):
            return self
        if self.action == "create_pending" and self.company_name and self.title:
            if self.job_id is None and self.reason_code is None:
                return self
        if self.action == "reject" and self.reason_code and not any(
            (self.job_id, self.company_name, self.title, self.apply_url)
        ):
            return self
        raise ValueError("decision fields do not match action")
