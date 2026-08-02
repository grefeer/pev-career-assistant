from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class AuthRequest(BaseModel):
    account: str = Field(..., min_length=3, max_length=120)
    password: str = Field(..., min_length=6, max_length=1024)

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


class AuthResponse(BaseModel):
    ok: bool
    message: str
    token: str | None = None
    profile: UserProfile | None = None
