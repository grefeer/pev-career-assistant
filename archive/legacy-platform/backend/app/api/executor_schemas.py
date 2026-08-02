from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


# ── Protocol version literals ──────────────────────────────────────────────────

PROTOCOL_V1: Literal["executor.v1"] = "executor.v1"
PROTOCOL_V2: Literal["executor.v2"] = "executor.v2"

ProtocolVersion = Literal["executor.v1", "executor.v2"]


# ── Adapter reference ────────────────────────────────────────────────────────────


class AdapterRef(BaseModel):
    """Identifies the site adapter and version frozen at dispatch time."""

    model_config = ConfigDict(extra="forbid")
    adapter_id: str = Field(min_length=1, max_length=64)
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    min_engine_version: str = Field(default="0.1.0", pattern=r"^\d+\.\d+\.\d+$")


# ── Shared field DTO ───────────────────────────────────────────────────────────


class ExecutorField(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field_key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,79}$")
    label: str = Field(min_length=1, max_length=120)
    value: str | None = Field(default=None, max_length=4000)
    confidence: Literal["confirmed", "low", "missing"]
    required: bool
    sensitive: Literal[False] = False


# ── v1 Payload (simulation) ────────────────────────────────────────────────────


class ExecutorTaskPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol_version: Literal["executor.v1"] = PROTOCOL_V1
    task_id: str = Field(min_length=36, max_length=36)
    state_version: int = Field(ge=0)
    target_url: HttpUrl
    fields: list[ExecutorField] = Field(max_length=100)


# ── v2 Payload (application) ───────────────────────────────────────────────────


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
    adapter: AdapterRef | None = None


# ── Discriminated union ────────────────────────────────────────────────────────


ExecutorTaskPayloadAny = Annotated[
    Union[ExecutorTaskPayload, ExecutorTaskPayloadV2],
    Field(discriminator="protocol_version"),
]


# ── Request / response DTOs ────────────────────────────────────────────────────


class ExecutorTaskSummary(BaseModel):
    protocol_version: ProtocolVersion = PROTOCOL_V1
    task_id: str
    target_job_id: str
    snapshot_id: str | None
    status: str
    state_version: int


class ExecutorTaskListResponse(BaseModel):
    tasks: list[ExecutorTaskSummary]


class ExecutorTaskDetail(BaseModel):
    """Detail view of an executor task.

    Defined as a standalone model (not inheriting from ExecutorTaskSummary)
    to avoid Pydantic v2.12 discriminator resolution failures when
    ExecutorTaskPayloadAny (a discriminated union keyed on protocol_version)
    is used in a subclass whose parent already carries protocol_version.
    """

    protocol_version: ProtocolVersion = PROTOCOL_V1
    task_id: str
    target_job_id: str
    snapshot_id: str | None
    status: str
    state_version: int
    payload: ExecutorTaskPayloadAny


class FieldCounts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed: int = Field(ge=0, le=100)
    defaulted: int = Field(ge=0, le=100)
    missing: int = Field(ge=0, le=100)
    low: int = Field(ge=0, le=100)


class ExecutorProgressRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol_version: ProtocolVersion
    expected_version: int = Field(ge=0)
    target_status: Literal[
        "running", "waiting_for_human", "ready_for_review", "failed"
    ]
    page_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{6,64}$")
    page_index: int | None = Field(default=None, ge=1, le=100)
    reason_code: str | None = Field(default=None, pattern=r"^[a-z0-9_]{1,80}$")
    field_counts: FieldCounts


class ExecutorResultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol_version: ProtocolVersion
    expected_version: int = Field(ge=0)
    target_status: Literal[
        "submitted_success", "submitted_failed", "result_unknown"
    ]
    page_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{6,64}$")
    reason_code: str = Field(pattern=r"^[a-z0-9_]{1,80}$")


class ExecutorTaskState(BaseModel):
    protocol_version: ProtocolVersion = PROTOCOL_V1
    task_id: str
    status: str
    state_version: int
