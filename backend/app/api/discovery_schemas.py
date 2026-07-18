from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class JobDiscoveryTaskResponse(BaseModel):
    id: str
    source_key: str
    source_name: str | None = None
    source_url: str
    status: str
    block_reason: str | None = None
    attempt_count: int
    result_summary_json: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime


class JobDiscoveryTaskListResponse(BaseModel):
    tasks: list[JobDiscoveryTaskResponse]


class DiscoveredJobCandidateResponse(BaseModel):
    id: str
    task_id: str
    similarity_group_key: str
    status: str
    title: str | None = None
    company_name: str | None = None
    description_text: str | None = None
    locations_json: list[str] | None = None
    apply_url: str | None = None
    confidence: float | None = None
    evidence_refs_json: list[dict[str, Any]] | None = None
    normalization_warnings_json: list[str] | None = None
    created_at: datetime


class JobDiscoveryReviewGroupResponse(BaseModel):
    similarity_group_key: str
    candidates: list[DiscoveredJobCandidateResponse]


class JobDiscoveryRetryRequest(BaseModel):
    """No additional params needed — retry resets attempt_count and status."""
    pass
