"""Public DTOs for adaptive PEV runs; no raw prompt/context/artifact leakage."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class CreateAgentRunRequest(BaseModel):
    """User goal plus the installed Skill authority the Planner may use."""

    model_config = {"extra": "forbid"}

    goal: str = Field(min_length=1, max_length=8_000)
    allowed_skills: list[str] = Field(min_length=1, max_length=4)
    context: dict[str, Any] = Field(default_factory=dict)

    @field_validator("goal")
    @classmethod
    def normalize_goal(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("goal must not be empty")
        return cleaned


class AgentRunCreatedResponse(BaseModel):
    id: str
    status: str
    summary: str | None
    error_code: str | None


class AgentRunResponse(BaseModel):
    id: str
    goal: str
    status: str
    complexity: str | None
    summary: str | None
    error_code: str | None
    created_at: datetime
    updated_at: datetime


class AgentRunListResponse(BaseModel):
    """Recent owner-safe task summaries for the personal workspace."""

    items: list[AgentRunResponse]


class AgentEventResponse(BaseModel):
    sequence: int
    event_type: str
    payload: dict[str, Any]
    created_at: datetime


class AgentEventListResponse(BaseModel):
    items: list[AgentEventResponse]


class AgentArtifactResponse(BaseModel):
    """Public, owner-safe projection of a persisted PEV output artifact."""

    id: str
    artifact_type: str
    source_url: str
    content_hash: str
    content: dict[str, Any]
    created_at: datetime


class AgentArtifactListResponse(BaseModel):
    items: list[AgentArtifactResponse]
