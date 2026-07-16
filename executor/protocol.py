from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


PROTOCOL_VERSION: Literal["executor.v1"] = "executor.v1"


class FieldConfidence(StrEnum):
    CONFIRMED = "confirmed"
    LOW = "low"
    MISSING = "missing"


class ExecutorField(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field_key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,79}$")
    label: str = Field(min_length=1, max_length=120)
    value: str | None = Field(default=None, max_length=4000)
    confidence: FieldConfidence
    required: bool
    sensitive: Literal[False] = False


class ExecutorTaskPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol_version: Literal["executor.v1"] = PROTOCOL_VERSION
    task_id: str = Field(min_length=36, max_length=36)
    state_version: int = Field(ge=0)
    target_url: HttpUrl
    fields: list[ExecutorField] = Field(max_length=100)
