from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from backend.app.domain.feedbacks import JobFeedbackCategory


def _normalize_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class FeedbackCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str = Field(min_length=36, max_length=36)
    category: JobFeedbackCategory
    note: str | None = Field(default=None, max_length=2000)


class FeedbackResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    job_id: str
    category: JobFeedbackCategory
    note: str | None = None
    created_at: datetime


class AdminFeedbackResponse(BaseModel):
    """Admin DTO must NOT expose user_id, account, nickname, or idempotency_key."""
    model_config = ConfigDict(extra="ignore")
    id: str
    job_id: str
    category: JobFeedbackCategory
    note: str | None = None
    created_at: datetime

    @classmethod
    def from_orm_model(cls, obj: Any) -> AdminFeedbackResponse:
        return cls(
            id=obj.id,
            job_id=obj.job_id,
            category=obj.category.value if hasattr(obj.category, "value") else str(obj.category),
            note=obj.note,
            created_at=_normalize_utc(obj.created_at),
        )


class FeedbackListResponse(BaseModel):
    total: int
    feedbacks: list[FeedbackResponse]


class AdminFeedbackListResponse(BaseModel):
    total: int
    feedbacks: list[AdminFeedbackResponse]
