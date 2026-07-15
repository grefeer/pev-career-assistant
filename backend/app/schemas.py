from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class AuthRequest(BaseModel):
    account: str = Field(..., min_length=3, max_length=120)
    password: str = Field(..., min_length=8, max_length=1024)

    @field_validator("account", mode="before")
    @classmethod
    def normalize_account(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class RegisterRequest(AuthRequest):
    nickname: str = Field(..., min_length=1, max_length=120)

    @field_validator("nickname", mode="before")
    @classmethod
    def normalize_nickname(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class UserProfile(BaseModel):
    account: str
    nickname: str
    role: Literal["student", "admin"]
    created_at: str
    last_login_at: str = ""
    active_thread_id: str = ""
    sessions: list[dict[str, Any]] = Field(default_factory=list)


class AuthResponse(BaseModel):
    ok: bool
    message: str
    token: str | None = None
    profile: UserProfile | None = None


class SessionSummary(BaseModel):
    user_goal: str = ""
    jobs_count: int = 0
    analyses_count: int = 0
    matches_count: int = 0
    optimization_round: int = 0
    has_final_report: bool = False
    shortlist: list[str] = Field(default_factory=list)
    revision_notes: list[str] = Field(default_factory=list)


class SessionStateResponse(BaseModel):
    thread_id: str
    values: dict[str, Any]
    summary: SessionSummary


class HistoryItem(BaseModel):
    created_at: str = ""
    next: list[str] = Field(default_factory=list)
    step: int | None = None
    source: str | None = None
    analyses_count: int = 0
    matches_count: int = 0
    optimization_round: int = 0
    has_final_report: bool = False
    checkpoint_id: str = ""


class SessionListResponse(BaseModel):
    active_thread_id: str = ""
    sessions: list[dict[str, Any]] = Field(default_factory=list)


class AnalysisResponse(BaseModel):
    thread_id: str
    result: dict[str, Any]
    summary: SessionSummary


class ActivateSessionResponse(BaseModel):
    ok: bool
    active_thread_id: str


class JobListResponse(BaseModel):
    total: int
    jobs: list[dict[str, Any]]
