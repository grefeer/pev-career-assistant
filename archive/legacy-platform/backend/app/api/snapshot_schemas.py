"""Pydantic schemas for ApplicationSnapshot API endpoints."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class CreateSnapshotRequest(BaseModel):
    """Request body for POST /api/application-snapshots."""

    job_id: str = Field(min_length=1)
    approved_resume_version_id: str = Field(min_length=1)
    dynamic_answers: list[dict[str, Any]] = Field(default_factory=list)
    local_sensitive_requirements: list[dict[str, Any]] = Field(default_factory=list)


class SnapshotResponse(BaseModel):
    """Public response for a single ApplicationSnapshot.

    Excludes sensitive fields (``profile_facts``, ``dynamic_answers``,
    ``local_sensitive_requirements``, internal storage refs).
    """

    id: str
    job_id: str
    approved_resume_version_id: str
    profile_version_id: str
    company_name: str
    title: str
    gui_eligible: bool
    job_status_at_snapshot: str
    job_review_version_at_snapshot: int
    created_at: str
    schema_version: str


class SnapshotListResponse(BaseModel):
    """Response body for GET /api/application-snapshots."""

    items: list[SnapshotResponse]
    total: int


class CreateTaskRequest(BaseModel):
    """Request body for POST /api/application-snapshots/{id}/create-task."""

    device_id: str | None = None


class TaskEligibilityResponse(BaseModel):
    """Response body for GET /api/application-snapshots/{id}/task-eligibility."""

    can_create_task: bool
    reason_code: str | None = None


class DispatchTaskRequest(BaseModel):
    """Request body for POST /api/application-tasks/{task_id}/dispatch."""

    device_id: str = Field(min_length=1)
    expected_version: int = Field(ge=0)


class DispatchTaskResponse(BaseModel):
    """Response body for POST /api/application-tasks/{task_id}/dispatch."""

    task_id: str
    status: str
    state_version: int
