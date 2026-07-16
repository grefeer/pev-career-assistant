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


class ExecutorTaskState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol_version: Literal["executor.v1"]
    task_id: str
    status: TaskStatus
    state_version: int
