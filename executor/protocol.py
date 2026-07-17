from __future__ import annotations

import sys
from typing import Any, Literal

if sys.version_info >= (3, 11):
    from enum import StrEnum
else:
    from enum import Enum

    class StrEnum(str, Enum):
        """Minimal StrEnum polyfill for Python < 3.11."""
        pass

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


PROTOCOL_V1: Literal["executor.v1"] = "executor.v1"
PROTOCOL_V2: Literal["executor.v2"] = "executor.v2"

PROTOCOL_VERSION: Literal["executor.v1"] = PROTOCOL_V1


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


class ExecutorTaskPayloadV2(BaseModel):
    """v2 payload for task_kind=application.

    Contains only non-sensitive fields and semantic references for
    local-sensitive data.  NEVER holds object keys, full profile
    snapshots, passwords, cookies, captcha, or local-sensitive plaintext.
    """

    model_config = ConfigDict(extra="forbid")
    protocol_version: Literal["executor.v2"] = PROTOCOL_V2
    task_id: str = Field(min_length=36, max_length=36)
    state_version: int = Field(ge=0)
    snapshot_id: str = Field(min_length=36, max_length=36)
    target_url: HttpUrl
    non_sensitive_fields: dict[str, Any] = Field(default_factory=dict)
    local_sensitive_requirements: list[dict[str, Any]] = Field(default_factory=list)
    attachment_ids: list[str] = Field(default_factory=list)


class TaskStatus(StrEnum):
    DISPATCHED = "dispatched"
    RUNNING = "running"
    WAITING_FOR_HUMAN = "waiting_for_human"
    READY_FOR_REVIEW = "ready_for_review"
    OBSERVING_USER_SUBMISSION = "observing_user_submission"
    SUBMITTED_SUCCESS = "submitted_success"
    SUBMITTED_FAILED = "submitted_failed"
    RESULT_UNKNOWN = "result_unknown"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ExecutorTaskSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol_version: Literal["executor.v1"]
    task_id: str
    target_job_id: str
    snapshot_id: str | None
    status: TaskStatus
    state_version: int


class ExecutorTaskListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tasks: list[ExecutorTaskSummary]


class ExecutorTaskDetail(ExecutorTaskSummary):
    payload: ExecutorTaskPayload


class ExecutorTaskDetailV2(ExecutorTaskSummary):
    payload: ExecutorTaskPayloadV2


class ExecutorTaskState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol_version: Literal["executor.v1"]
    task_id: str
    status: TaskStatus
    state_version: int
