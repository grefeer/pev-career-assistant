"""Pydantic schemas for the application-tracking API endpoints."""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator

from backend.app.domain.application_tracking import (
    APPLY_URL_MAX_LENGTH,
    COMPANY_NAME_MAX_LENGTH,
    NOTE_MAX_LENGTH,
    SOURCE_MAX_LENGTH,
    TITLE_MAX_LENGTH,
    ApplicationStatus,
)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class CreateApplicationRequest(BaseModel):
    """Request body for recording a new tracked application.

    Strict (``extra="forbid"``) so unknown fields are rejected.  ``target_job_id``
    optionally links to a verified ``JobPosting``; off-platform applications carry
    company/title directly.  All applications start in ``saved``.
    """

    model_config = {"extra": "forbid"}

    company_name: str = Field(min_length=1, max_length=COMPANY_NAME_MAX_LENGTH)
    title: str = Field(min_length=1, max_length=TITLE_MAX_LENGTH)
    apply_url: str | None = Field(default=None, max_length=APPLY_URL_MAX_LENGTH)
    source: str | None = Field(default=None, max_length=SOURCE_MAX_LENGTH)
    notes: str | None = Field(default=None, max_length=NOTE_MAX_LENGTH)
    target_job_id: str | None = Field(default=None, min_length=1)

    @field_validator("company_name", "title")
    @classmethod
    def _strip_required_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must not be empty")
        return cleaned

    @field_validator("apply_url", "source", "notes", "target_job_id")
    @classmethod
    def _strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class TransitionRequest(BaseModel):
    """Request body for advancing an application through the state machine."""

    model_config = {"extra": "forbid"}

    to_status: ApplicationStatus
    note: str | None = Field(default=None, max_length=NOTE_MAX_LENGTH)
    expected_version: int | None = Field(default=None, ge=0)

    @field_validator("note")
    @classmethod
    def _strip_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class UpdateApplicationRequest(BaseModel):
    """Request body for patching editable fields (notes / apply_url).

    Strict (``extra="forbid"``); the route uses ``model_dump(exclude_unset=True)``
    so omitted fields are left unchanged while explicit ``null`` clears them.
    """

    model_config = {"extra": "forbid"}

    notes: str | None = Field(default=None, max_length=NOTE_MAX_LENGTH)
    apply_url: str | None = Field(default=None, max_length=APPLY_URL_MAX_LENGTH)

    @field_validator("notes", "apply_url")
    @classmethod
    def _strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class ApplicationEventResponse(BaseModel):
    """API projection of one :class:`ApplicationRecordEvent` audit row."""

    model_config = {"extra": "ignore"}

    id: int
    application_id: str
    from_status: str
    to_status: str
    note: str | None = None
    created_at: datetime

    _normalize_created_at = field_validator("created_at", mode="before")(_as_utc)


class ApplicationRecordResponse(BaseModel):
    """API projection of an :class:`ApplicationRecord` ORM row."""

    model_config = {"extra": "ignore"}

    id: str
    user_id: str
    target_job_id: str | None = None
    company_name: str
    title: str
    apply_url: str | None = None
    source: str | None = None
    status: str
    applied_at: datetime | None = None
    notes: str | None = None
    state_version: int
    created_at: datetime
    updated_at: datetime

    _normalize_created_at = field_validator("created_at", mode="before")(_as_utc)
    _normalize_updated_at = field_validator("updated_at", mode="before")(_as_utc)
    _normalize_applied_at = field_validator("applied_at", mode="before")(_as_utc)


class ApplicationListResponse(BaseModel):
    items: list[ApplicationRecordResponse]
    total: int


class ApplicationEventListResponse(BaseModel):
    items: list[ApplicationEventResponse]
