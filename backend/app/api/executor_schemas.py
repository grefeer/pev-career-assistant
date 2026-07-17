from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from backend.app.db.models import ApplicationTaskStatus


class ExecutorField(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field_key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,79}$")
    label: str = Field(min_length=1, max_length=120)
    value: str | None = Field(default=None, max_length=4000)
    confidence: Literal["confirmed", "low", "missing"]
    required: bool
    sensitive: Literal[False] = False


class ExecutorTaskPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol_version: Literal["executor.v1"] = "executor.v1"
    task_id: str = Field(min_length=36, max_length=36)
    state_version: int = Field(ge=0)
    target_url: HttpUrl
    fields: list[ExecutorField] = Field(max_length=100)


class ExecutorTaskSummary(BaseModel):
    protocol_version: Literal["executor.v1"] = "executor.v1"
    task_id: str
    target_job_id: str
    snapshot_id: str | None
    status: ApplicationTaskStatus
    state_version: int


class ExecutorTaskListResponse(BaseModel):
    tasks: list[ExecutorTaskSummary]


class ExecutorTaskDetail(ExecutorTaskSummary):
    payload: ExecutorTaskPayload


class FieldCounts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed: int = Field(ge=0, le=100)
    defaulted: int = Field(ge=0, le=100)
    missing: int = Field(ge=0, le=100)
    low: int = Field(ge=0, le=100)


class ExecutorProgressRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol_version: Literal["executor.v1"]
    expected_version: int = Field(ge=0)
    target_status: Literal["running", "waiting_for_human", "ready_for_review", "failed"]
    page_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{6,64}$")
    page_index: int | None = Field(default=None, ge=1, le=100)
    reason_code: str | None = Field(default=None, pattern=r"^[a-z0-9_]{1,80}$")
    field_counts: FieldCounts


class ExecutorResultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol_version: Literal["executor.v1"]
    expected_version: int = Field(ge=0)
    target_status: Literal["submitted_success", "submitted_failed", "result_unknown"]
    page_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{6,64}$")
    reason_code: str = Field(pattern=r"^[a-z0-9_]{1,80}$")


class ExecutorTaskState(BaseModel):
    protocol_version: Literal["executor.v1"] = "executor.v1"
    task_id: str
    status: ApplicationTaskStatus
    state_version: int
