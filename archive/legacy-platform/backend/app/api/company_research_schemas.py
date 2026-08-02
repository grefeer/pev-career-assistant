"""Pydantic schemas for Company Research API endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field, field_validator

from backend.app.domain.company_research import (
    COMPANY_NAME_MAX_LENGTH,
    SOURCE_URL_MAX_LENGTH,
)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class CreateCompanyResearchRequest(BaseModel):
    """Request body for starting a company research run.

    Strict (``extra="forbid"``) so unknown fields are rejected rather than
    silently dropped.
    """

    model_config = {"extra": "forbid"}

    company_name: str = Field(min_length=1, max_length=COMPANY_NAME_MAX_LENGTH)
    source_url: str = Field(min_length=1, max_length=SOURCE_URL_MAX_LENGTH)

    @field_validator("company_name")
    @classmethod
    def _normalize_company_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("company_name must not be empty")
        return cleaned

    @field_validator("source_url")
    @classmethod
    def _normalize_source_url(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("source_url must not be empty")
        if not (cleaned.startswith("http://") or cleaned.startswith("https://")):
            raise ValueError("source_url must start with http:// or https://")
        return cleaned


class CompanyResearchReportResponse(BaseModel):
    """API projection of a :class:`CompanyResearchReport` ORM row.

    Built explicitly by the route helper so the ``*_json`` ORM columns map to
    the clean ``profile`` / ``openings`` / ``evidence_refs`` API fields.
    ``extra="ignore"`` tolerates forward-compatible columns.
    """

    model_config = {"extra": "ignore"}

    id: str
    user_id: str
    company_name: str
    source_url: str
    agent_version: str
    status: str
    block_reason: str | None = None
    profile: dict[str, Any] | None = None
    openings: list[dict[str, Any]] = Field(default_factory=list)
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)
    summary: str | None = None
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None

    _normalize_created_at = field_validator("created_at", mode="before")(_as_utc)
    _normalize_updated_at = field_validator("updated_at", mode="before")(_as_utc)
    _normalize_started_at = field_validator("started_at", mode="before")(_as_utc)
    _normalize_finished_at = field_validator("finished_at", mode="before")(_as_utc)


class CompanyResearchListResponse(BaseModel):
    items: list[CompanyResearchReportResponse]
    total: int
