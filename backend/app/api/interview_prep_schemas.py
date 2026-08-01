"""Pydantic schemas for Interview Prep API endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field, field_validator

from backend.app.domain.interview_prep import AGENT_VERSION_MAX_LENGTH


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class CreateInterviewPrepRequest(BaseModel):
    """Request body for generating an interview-prep kit.

    Strict (``extra="forbid"``) so unknown fields are rejected rather than
    silently dropped. The kit is anchored to a completed match report, which
    carries the target job snapshot, the confirmed profile version, and the
    match analysis used to tailor the kit.
    """

    model_config = {"extra": "forbid"}

    match_report_id: str = Field(min_length=1)

    @field_validator("match_report_id")
    @classmethod
    def _normalize_match_report_id(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("match_report_id must not be empty")
        return cleaned


class InterviewPrepKitResponse(BaseModel):
    """API projection of an :class:`InterviewPrepKit` ORM row.

    Built explicitly by the route helper so the ``*_json`` ORM columns map to
    the clean ``content`` / ``preferences`` / ``match_analysis`` API fields.
    ``extra="ignore"`` tolerates forward-compatible columns.
    """

    model_config = {"extra": "ignore"}

    id: str
    user_id: str
    target_job_id: str | None = None
    profile_version_id: str | None = None
    agent_version: str
    status: str
    content: dict[str, Any] | None = None
    preferences: dict[str, Any] | None = None
    match_analysis: dict[str, Any] | None = None
    error_code: str | None = None
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @field_validator("agent_version")
    @classmethod
    def _clamp_agent_version(cls, value: str) -> str:
        # Defense-in-depth: the column is String(32); never trust the ORM
        # value blindly when projecting to the API.
        if len(value) > AGENT_VERSION_MAX_LENGTH:
            return value[:AGENT_VERSION_MAX_LENGTH]
        return value

    _normalize_created_at = field_validator("created_at", mode="before")(_as_utc)
    _normalize_updated_at = field_validator("updated_at", mode="before")(_as_utc)
    _normalize_started_at = field_validator("started_at", mode="before")(_as_utc)
    _normalize_finished_at = field_validator("finished_at", mode="before")(_as_utc)


class InterviewPrepListResponse(BaseModel):
    items: list[InterviewPrepKitResponse]
    total: int
