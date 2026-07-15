# Real Job Sync Vertical Slice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a read-only Tencent Smartsheet synchronization path that preserves immutable raw records in MySQL, creates only eligible `PENDING_COMPLETION` job postings, and exposes authenticated synchronization and query APIs.

**Architecture:** A fixed-endpoint MCP Gateway returns typed Tencent field and record pages. Source-specific mappers convert those records into normalized candidates, while a transaction-oriented sync service coordinates MySQL leases, per-page commits, immutable snapshots, posting upserts, audit events, and stable failures. FastAPI routes remain thin and receive the service through an injectable dependency.

**Tech Stack:** Python 3.12, FastAPI 0.117, SQLAlchemy 2.0, Alembic 1.16, MySQL 8.4, Pydantic 2, MCP Python SDK 1.x, httpx 0.28, pytest 8.4, Ruff 0.15.

## Global Constraints

- MySQL remains the sole authority for sync runs, raw snapshots, and job postings; Redis is not a source of job truth.
- Tencent access is read-only and limited to `smartsheet.list_fields` and `smartsheet.list_records` against `https://docs.qq.com/openapi/mcp`.
- Read `TENCENT_DOCS_TOKEN` and `TEST_TENCENT_DOCS_TOKEN` only from environment-backed settings; never log, persist, commit, or pass either token in argv.
- A posting requires a company name, a non-empty title, and an HTTP(S) URL with a hostname, no userinfo, no control characters, and at most 4,096 Unicode code points.
- New postings have only `pending_completion` status; no code in this plan may mark them verified or make them eligible for GUI Agent execution.
- A raw snapshot is unique on `(source_id, external_record_id, payload_hash)` and is never updated after insertion.
- Sync requests use a 10-minute MySQL-backed lease, refresh it after every committed page, and use a 15-second timeout per Gateway call.
- Each Tencent call makes at most three attempts for timeout, connection, 429, and temporary 5xx failures; authentication, schema, and protocol failures are not retried.
- One run may read at most 1,000 pages or 100,000 records, with a requested page size of 100.
- Missing upstream rows never delete, expire, or overwrite an existing posting in this slice.
- Keep the existing `AGENTS.md` working-tree/index state and the untracked handover summary untouched.

## File Structure

| File | Responsibility |
| --- | --- |
| `alembic/versions/20260715_0003_real_job_sync.py` | Create and remove the four job-sync tables and their constraints. |
| `backend/app/db/models.py` | Declare job enums and ORM entities without synchronization behavior. |
| `backend/app/services/tencent_smartsheet.py` | Fixed-endpoint MCP transport, DTO parsing, retry classification, and pagination response validation. |
| `backend/app/services/job_mappers.py` | Built-in source definitions, field extraction, schema validation, URL validation, and source-specific mapping. |
| `backend/app/repositories/jobs.py` | Source initialization, lease/run persistence, immutable snapshot insertion, posting upsert, and filtered queries. |
| `backend/app/services/job_sync.py` | Synchronization orchestration, page transactions, audit events, counters, and stable domain exceptions. |
| `backend/app/api/routes/jobs.py` | Pydantic response models and authenticated HTTP endpoints. |
| `backend/app/api/dependencies.py` | Injectable `JobSyncService` construction. |
| `backend/app/api/router.py` | Register the jobs router. |
| `backend/app/config.py` | Optional secret Tencent token setting that does not affect startup validation. |
| `requirements.txt` / `requirements-dev.txt` | Add stable MCP 1.x and production httpx dependencies without duplicate pins. |
| `tests/unit/test_tencent_smartsheet.py` | Gateway DTO, protocol, retry, and pagination behavior. |
| `tests/unit/test_job_mappers.py` | Both source schemas and mapping eligibility rules. |
| `tests/unit/test_job_repository.py` | Leases, immutable snapshots, posting upsert, and exact-label filtering. |
| `tests/unit/test_job_sync_service.py` | Full, partial, failed, idempotent, and missing-row synchronization behavior. |
| `tests/contract/test_jobs_api.py` | Authentication, authorization, response whitelists, and stable HTTP errors. |
| `tests/integration/test_mysql_migration.py` | Migration round trip and expected schema. |
| `tests/integration/test_job_sync_mysql.py` | MySQL constraints, lease conflict, and JSON label filtering. |
| `tests/integration/test_tencent_smartsheet_live.py` | Opt-in read-only live source pagination. |
| `tests/security/test_no_sensitive_logging.py` | Token, raw payload, and upstream response redaction. |
| `docs/runbooks/platform-foundation.md` | Token setup, manual sync, failure recovery, and readiness boundary. |

---

### Task 1: Add authoritative job-sync schema

**Files:**
- Create: `alembic/versions/20260715_0003_real_job_sync.py`
- Modify: `backend/app/db/models.py`
- Create: `tests/unit/test_job_models.py`
- Modify: `tests/integration/test_mysql_migration.py`

**Interfaces:**
- Consumes: `Base`, `UUIDPrimaryKeyMixin`, `TimestampMixin`, `utc_now`, and existing `enum_kwargs`.
- Produces: `JobSourceProvider`, `JobSyncRunStatus`, `JobPostingStatus`, `JobSource`, `JobSyncRun`, `RawJobRecord`, and `JobPosting`.

- [ ] **Step 1: Write failing ORM metadata and migration expectations**

Create `tests/unit/test_job_models.py`:

```python
from sqlalchemy import UniqueConstraint

from backend.app.db.base import Base
from backend.app.db.models import JobPostingStatus


def test_job_sync_tables_and_status_exist() -> None:
    assert {
        "job_sources",
        "job_sync_runs",
        "raw_job_records",
        "job_postings",
    } <= set(Base.metadata.tables)
    assert JobPostingStatus.PENDING_COMPLETION.value == "pending_completion"


def test_raw_snapshot_and_posting_identity_are_unique() -> None:
    raw = Base.metadata.tables["raw_job_records"]
    posting = Base.metadata.tables["job_postings"]
    raw_unique = {
        tuple(constraint.columns.keys())
        for constraint in raw.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    posting_unique = {
        tuple(constraint.columns.keys())
        for constraint in posting.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("source_id", "external_record_id", "payload_hash") in raw_unique
    assert ("source_id", "external_record_id") in posting_unique
```

In `tests/integration/test_mysql_migration.py`, set `HEAD_REVISION = "20260715_0003"`, add the four table names to `BUSINESS_TABLES`, and assert the unique constraint/index column sets after `upgrade head`.

- [ ] **Step 2: Run the focused tests and verify the schema is absent**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_job_models.py tests/integration/test_mysql_migration.py::test_alembic_offline_accepts_percent_encoded_database_url -v`

Expected: FAIL during collection because the job model classes do not exist.

- [ ] **Step 3: Add the ORM enums and entities**

Append these declarations to `backend/app/db/models.py`, using the existing SQLAlchemy imports and adding `LargeBinary` only if needed by the migration implementation:

```python
class JobSourceProvider(StrEnum):
    TENCENT_SMARTSHEET = "tencent_smartsheet"


class JobSyncRunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"


class JobPostingStatus(StrEnum):
    PENDING_COMPLETION = "pending_completion"


class JobSource(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "job_sources"
    __table_args__ = (
        UniqueConstraint("source_key", name="uq_job_sources_source_key"),
        UniqueConstraint(
            "provider", "file_id", "sheet_id", name="uq_job_sources_location"
        ),
    )
    source_key: Mapped[str] = mapped_column(String(100), nullable=False)
    provider: Mapped[JobSourceProvider] = mapped_column(
        Enum(JobSourceProvider, name="job_source_provider", **enum_kwargs),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    file_id: Mapped[str] = mapped_column(String(100), nullable=False)
    sheet_id: Mapped[str] = mapped_column(String(100), nullable=False)
    mapper_version: Mapped[str] = mapped_column(String(40), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_successful_sync_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    active_sync_run_id: Mapped[str | None] = mapped_column(String(36), index=True)
    sync_lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )


class JobSyncRun(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "job_sync_runs"
    __table_args__ = (Index("ix_job_sync_runs_source_started", "source_id", "started_at"),)
    source_id: Mapped[str] = mapped_column(
        ForeignKey("job_sources.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[JobSyncRunStatus] = mapped_column(
        Enum(JobSyncRunStatus, name="job_sync_run_status", **enum_kwargs),
        nullable=False,
    )
    pages_read: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    records_read: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    raw_snapshots_created: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    postings_created: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    postings_updated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    records_skipped_incomplete: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    error_code: Mapped[str | None] = mapped_column(String(80))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RawJobRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "raw_job_records"
    __table_args__ = (
        UniqueConstraint(
            "source_id",
            "external_record_id",
            "payload_hash",
            name="uq_raw_job_records_snapshot",
        ),
        Index("ix_raw_job_records_source_record", "source_id", "external_record_id"),
    )
    source_id: Mapped[str] = mapped_column(
        ForeignKey("job_sources.id", ondelete="RESTRICT"), nullable=False
    )
    external_record_id: Mapped[str] = mapped_column(String(100), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_fields: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class JobPosting(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "job_postings"
    __table_args__ = (
        UniqueConstraint(
            "source_id", "external_record_id", name="uq_job_postings_source_record"
        ),
        Index("ix_job_postings_status_updated", "status", "updated_at"),
    )
    source_id: Mapped[str] = mapped_column(
        ForeignKey("job_sources.id", ondelete="RESTRICT"), nullable=False
    )
    external_record_id: Mapped[str] = mapped_column(String(100), nullable=False)
    raw_record_id: Mapped[str] = mapped_column(
        ForeignKey("raw_job_records.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[JobPostingStatus] = mapped_column(
        Enum(JobPostingStatus, name="job_posting_status", **enum_kwargs),
        default=JobPostingStatus.PENDING_COMPLETION,
        nullable=False,
    )
    company_name: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    locations: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    recruitment_types: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    industries: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    apply_url: Mapped[str] = mapped_column(Text, nullable=False)
    referral_code: Mapped[str | None] = mapped_column(String(255))
    deadline_text: Mapped[str | None] = mapped_column(String(255))
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    mapper_version: Mapped[str] = mapped_column(String(40), nullable=False)
```

- [ ] **Step 4: Create migration `20260715_0003_real_job_sync.py`**

Set `revision = "20260715_0003"` and `down_revision = "20260715_0002"`. Create tables in this exact order: `job_sources`, `job_sync_runs`, `raw_job_records`, `job_postings`; create the named constraints and indexes declared above. In `downgrade()`, drop them in reverse order. Use non-native string enums named `job_source_provider`, `job_sync_run_status`, and `job_posting_status`, JSON columns for array/raw fields, and `RESTRICT` foreign keys.

The migration must not insert either built-in source and must not read any Tencent token.

- [ ] **Step 5: Run schema tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_job_models.py tests/integration/test_mysql_migration.py -v`

Expected without `TEST_MYSQL_URL`: metadata and offline tests PASS; the real MySQL round-trip test SKIPS with `requires TEST_MYSQL_URL`.

- [ ] **Step 6: Commit the schema**

```powershell
git add backend/app/db/models.py alembic/versions/20260715_0003_real_job_sync.py tests/unit/test_job_models.py tests/integration/test_mysql_migration.py
git commit -m "feat: add authoritative job sync schema"
```

### Task 2: Add the fixed-endpoint Tencent MCP Gateway

**Files:**
- Modify: `requirements.txt`
- Modify: `requirements-dev.txt`
- Modify: `backend/app/config.py`
- Create: `backend/app/services/tencent_smartsheet.py`
- Create: `tests/unit/test_tencent_smartsheet.py`

**Interfaces:**
- Consumes: optional `Settings.tencent_docs_token: SecretStr | None`.
- Produces: `TencentField`, `TencentRecord`, `TencentRecordPage`, `SmartsheetGateway`, `TencentSmartsheetGateway`, `TencentGatewayError`, `TencentTokenMissingError`, `TencentAuthError`, `TencentRateLimitError`, `TencentTimeoutError`, `TencentUnavailableError`, and `TencentProtocolError`.

- [ ] **Step 1: Write failing Gateway parsing and retry tests**

Create `tests/unit/test_tencent_smartsheet.py` with a callable fake tool transport and these cases:

```python
import pytest

from backend.app.services.tencent_smartsheet import (
    TencentProtocolError,
    TencentSmartsheetGateway,
)


def test_list_records_parses_a_page() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def call(tool: str, arguments: dict[str, object]) -> dict[str, object]:
        calls.append((tool, arguments))
        return {
            "error": "",
            "total": 1,
            "has_more": False,
            "next": 0,
            "records": [
                {
                    "record_id": "r1",
                    "field_values": [
                        {
                            "field": "公司名称",
                            "text_value": {"items": [{"text": "示例公司", "type": "text"}]},
                        }
                    ],
                }
            ],
        }

    gateway = TencentSmartsheetGateway(token="secret", tool_caller=call)
    page = gateway.list_records("file", "sheet", offset=0, limit=100)
    assert page.total == 1
    assert page.records[0].record_id == "r1"
    assert calls == [
        (
            "smartsheet.list_records",
            {"file_id": "file", "sheet_id": "sheet", "offset": 0, "limit": 100},
        )
    ]


def test_list_records_rejects_non_advancing_cursor() -> None:
    gateway = TencentSmartsheetGateway(
        token="secret",
        tool_caller=lambda *_: {
            "error": "",
            "total": 2,
            "has_more": True,
            "next": 0,
            "records": [{"record_id": "r1", "field_values": []}],
        },
    )
    with pytest.raises(TencentProtocolError):
        gateway.list_records("file", "sheet", offset=0, limit=100)


def test_temporary_failure_retries_at_most_three_attempts() -> None:
    attempts = 0

    def call(_tool: str, _arguments: dict[str, object]) -> dict[str, object]:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise TimeoutError("secret upstream detail")
        return {"error": "", "total": 0, "has_more": False, "next": 0, "records": []}

    gateway = TencentSmartsheetGateway(
        token="secret", tool_caller=call, sleeper=lambda _seconds: None
    )
    assert gateway.list_records("file", "sheet", offset=0, limit=100).total == 0
    assert attempts == 3
```

Also test missing token, error codes `400006`, `400007`, and `400008`, malformed content, more records than limit, a record missing `record_id`, and `list_fields` parsing.

- [ ] **Step 2: Run the tests and verify the Gateway module is absent**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_tencent_smartsheet.py -v`

Expected: FAIL during collection with `ModuleNotFoundError`.

- [ ] **Step 3: Add stable production dependencies and optional secret config**

Add these production pins to `requirements.txt`:

```text
httpx==0.28.1
mcp>=1.27,<2
```

Remove the duplicate `httpx==0.28.1` line from `requirements-dev.txt`; it remains available through `-r requirements.txt`.

In `backend/app/config.py`, import `SecretStr` and add:

```python
tencent_docs_token: SecretStr | None = Field(default=None, repr=False)
```

Do not add a production validator requiring this value.

- [ ] **Step 4: Implement typed DTOs, errors, and tool calling**

Create `backend/app/services/tencent_smartsheet.py` with these public interfaces:

```python
from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import json
import time
from typing import Any, Protocol

import httpx
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client


TENCENT_MCP_ENDPOINT = "https://docs.qq.com/openapi/mcp"
PAGE_SIZE = 100
MAX_PAGES = 1_000
MAX_RECORDS = 100_000


class TencentGatewayError(RuntimeError):
    error_code = "tencent_unavailable"


class TencentTokenMissingError(TencentGatewayError):
    error_code = "tencent_token_missing"


class TencentAuthError(TencentGatewayError):
    error_code = "tencent_auth_failed"


class TencentRateLimitError(TencentGatewayError):
    error_code = "tencent_rate_limited"


class TencentTimeoutError(TencentGatewayError):
    error_code = "tencent_timeout"


class TencentUnavailableError(TencentGatewayError):
    error_code = "tencent_unavailable"


class TencentProtocolError(TencentGatewayError):
    error_code = "tencent_protocol_error"


@dataclass(frozen=True)
class TencentField:
    field_id: str
    title: str
    field_type: str


@dataclass(frozen=True)
class TencentRecord:
    record_id: str
    field_values: list[dict[str, Any]]


@dataclass(frozen=True)
class TencentRecordPage:
    records: list[TencentRecord]
    total: int
    has_more: bool
    next_offset: int


class SmartsheetGateway(Protocol):
    def list_fields(self, file_id: str, sheet_id: str) -> list[TencentField]:
        raise NotImplementedError

    def list_records(
        self, file_id: str, sheet_id: str, *, offset: int, limit: int = PAGE_SIZE
    ) -> TencentRecordPage:
        raise NotImplementedError
```

`TencentSmartsheetGateway.__init__` must accept `token: str | None`, optional `tool_caller: Callable[[str, dict[str, object]], dict[str, object]]`, and optional `sleeper`. Its default caller runs this coroutine with `asyncio.run` from the existing synchronous FastAPI worker thread:

```python
async def _mcp_call(self, tool: str, arguments: dict[str, object]) -> dict[str, object]:
    if not self.token:
        raise TencentTokenMissingError("Tencent Docs token is not configured")
    headers = {"Authorization": self.token}
    async with httpx.AsyncClient(headers=headers, timeout=15.0) as client:
        async with streamable_http_client(
            TENCENT_MCP_ENDPOINT, http_client=client
        ) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(tool, arguments=arguments)
    if result.isError:
        text = " ".join(
            block.text for block in result.content if isinstance(block, types.TextContent)
        )
        raise TencentUnavailableError(text)
    if isinstance(result.structuredContent, dict):
        return dict(result.structuredContent)
    for block in result.content:
        if isinstance(block, types.TextContent):
            parsed = json.loads(block.text)
            if isinstance(parsed, dict):
                return parsed
    raise TencentProtocolError("Tencent MCP returned no object payload")
```

Use this synchronous wrapper around the injected/default caller:

```python
def _invoke(self, tool: str, arguments: dict[str, object]) -> dict[str, object]:
    last_error: TencentGatewayError | None = None
    for attempt in range(3):
        try:
            payload = self.tool_caller(tool, arguments)
            error = str(payload.get("error", ""))
            if "400006" in error or "400007" in error:
                raise TencentAuthError("Tencent authorization failed")
            if "400008" in error:
                raise TencentRateLimitError("Tencent rate limit exceeded")
            if error:
                raise TencentUnavailableError("Tencent service unavailable")
            return payload
        except TencentAuthError:
            raise
        except TencentProtocolError:
            raise
        except TencentRateLimitError:
            last_error = TencentRateLimitError("Tencent rate limit exceeded")
        except (TimeoutError, httpx.TimeoutException):
            last_error = TencentTimeoutError("Tencent request timed out")
        except (httpx.ConnectError, httpx.NetworkError):
            last_error = TencentUnavailableError("Tencent service unavailable")
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 429:
                last_error = TencentRateLimitError("Tencent rate limit exceeded")
            elif 500 <= status < 600:
                last_error = TencentUnavailableError("Tencent service unavailable")
            else:
                raise TencentProtocolError("Tencent MCP request was rejected") from None
        if attempt < 2:
            self.sleeper((0.25, 0.5)[attempt])
    assert last_error is not None
    raise last_error
```

`self.tool_caller` is the injected callable or `lambda tool, arguments: asyncio.run(self._mcp_call(tool, arguments))`. Preserve the exception chaining only internally; API/logging code must use `error_code`, never the exception text. Parse fields and records after `_invoke`; malformed payloads raise `TencentProtocolError` without retry.

Validate `next_offset > offset` when `has_more`, `len(records) <= limit`, non-negative total/offset, required string IDs, and list-shaped fields/records.

- [ ] **Step 5: Install dependencies and run Gateway/config tests**

Run: `.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt`

Expected: exit 0 with MCP 1.x installed and no MCP 2.x package.

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_tencent_smartsheet.py tests/unit/test_config.py -v`

Expected: PASS.

- [ ] **Step 6: Commit the Gateway**

```powershell
git add requirements.txt requirements-dev.txt backend/app/config.py backend/app/services/tencent_smartsheet.py tests/unit/test_tencent_smartsheet.py
git commit -m "feat: add Tencent Smartsheet gateway"
```

### Task 3: Add source-specific schema validation and mapping

**Files:**
- Create: `backend/app/services/job_mappers.py`
- Create: `tests/unit/test_job_mappers.py`

**Interfaces:**
- Consumes: `TencentField` and `TencentRecord` from Task 2.
- Produces: `BuiltinJobSource`, `NormalizedJobCandidate`, `SkippedRecord`, `SourceSchemaChangedError`, `SourceMapper`, `BUILTIN_SOURCES`, and `MAPPERS`.

- [ ] **Step 1: Write failing mapping tests using minimal real-shape fixtures**

Create `tests/unit/test_job_mappers.py`:

```python
from backend.app.services.job_mappers import (
    MAPPERS,
    NormalizedJobCandidate,
    SkippedRecord,
    SourceSchemaChangedError,
)
from backend.app.services.tencent_smartsheet import TencentField, TencentRecord


def text(field: str, value: str) -> dict[str, object]:
    return {
        "field": field,
        "text_value": {"items": [{"text": value, "type": "text"}]},
    }


def url(field: str, value: str) -> dict[str, object]:
    return {
        "field": field,
        "url_value": {"items": [{"text": "点击内推", "type": "url", "link": value}]},
    }


def options(field: str, *values: str) -> dict[str, object]:
    return {
        "field": field,
        "option_value": {"items": [{"text": value} for value in values]},
    }


def test_first_source_never_invents_a_title() -> None:
    record = TencentRecord(
        "r1",
        [text("企业名称", "北方华创"), url("内推链接", "https://example.com/jobs")],
    )
    result = MAPPERS["tencent-27-referrals"].map(record)
    assert result == SkippedRecord("missing_title")


def test_intern_source_maps_complete_record() -> None:
    record = TencentRecord(
        "r2",
        [
            text("公司名称", "阿里云"),
            text("招聘岗位", "研发、算法"),
            text("工作地点", "北京、杭州"),
            options("招聘类型", "27届暑期实习"),
            options("多选", "互联网", "AI"),
            url("投递链接", "https://campus.example.com/jobs?id=1"),
            text("内推码", "ABC123"),
            text("截止日期", "尽快投递"),
            {"field": "更新时间", "string_value": "1773763200000"},
        ],
    )
    result = MAPPERS["tencent-intern-referrals"].map(record)
    assert isinstance(result, NormalizedJobCandidate)
    assert result.company_name == "阿里云"
    assert result.title == "研发、算法"
    assert result.locations == ["北京", "杭州"]
    assert result.recruitment_types == ["27届暑期实习"]
    assert result.industries == ["互联网", "AI"]
    assert result.apply_url == "https://campus.example.com/jobs?id=1"


def test_url_with_userinfo_is_skipped() -> None:
    record = TencentRecord(
        "r3",
        [
            text("公司名称", "示例公司"),
            text("招聘岗位", "工程师"),
            url("投递链接", "https://user:password@example.com/jobs"),
        ],
    )
    assert MAPPERS["tencent-intern-referrals"].map(record) == SkippedRecord(
        "invalid_apply_url"
    )
```

Also test missing company/title/link separately, non-HTTP schemes, missing hostname, control characters, 4,097-character URLs, blank text, stable list de-duplication, invalid timestamps, and schema field type drift.

- [ ] **Step 2: Run tests and verify the mapper module is absent**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_job_mappers.py -v`

Expected: FAIL during collection with `ModuleNotFoundError`.

- [ ] **Step 3: Implement source definitions and mapper contracts**

Create `backend/app/services/job_mappers.py` with these types and fixed definitions:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Protocol
from urllib.parse import urlsplit

from backend.app.services.tencent_smartsheet import TencentField, TencentRecord


@dataclass(frozen=True)
class BuiltinJobSource:
    source_key: str
    name: str
    file_id: str
    sheet_id: str
    mapper_version: str


BUILTIN_SOURCES = (
    BuiltinJobSource(
        "tencent-27-referrals",
        "27届内推信息【重要】",
        "DZkdPVGtGb1ZvaG5R",
        "t00i2h",
        "v1",
    ),
    BuiltinJobSource(
        "tencent-intern-referrals",
        "实习内推汇总",
        "DY3pHYkNvb0ZRSHdi",
        "BB08J2",
        "v1",
    ),
)


@dataclass(frozen=True)
class NormalizedJobCandidate:
    company_name: str
    title: str
    locations: list[str]
    recruitment_types: list[str]
    industries: list[str]
    apply_url: str
    referral_code: str | None
    deadline_text: str | None
    source_updated_at: datetime | None


@dataclass(frozen=True)
class SkippedRecord:
    reason: str


class SourceSchemaChangedError(RuntimeError):
    error_code = "source_schema_changed"


class SourceMapper(Protocol):
    version: str

    def validate_schema(self, fields: list[TencentField]) -> None:
        raise NotImplementedError

    def source_updated_at(self, record: TencentRecord) -> datetime | None:
        raise NotImplementedError

    def map(self, record: TencentRecord) -> NormalizedJobCandidate | SkippedRecord:
        raise NotImplementedError
```

Implement private extractors that find a field entry by exact title, join text items without trimming internal content, preserve option order while removing exact duplicates, split location text on `、`, `，`, `,`, `；`, `;`, and parse source timestamps as UTC milliseconds. Empty or whitespace-only values become missing.

Validate URLs with `urlsplit`: scheme in `{http, https}`, hostname present, username/password absent, no character with code point below 32 or equal to 127, and length at most 4,096.

The first mapper validates `企业名称:text` and `内推链接:url`, then always returns `SkippedRecord("missing_title")`. The second validates `公司名称:text`, `招聘岗位:text`, and `投递链接:url`, then maps exact titles as follows: company=`公司名称`, title=`招聘岗位`, locations=`工作地点`, recruitment types=`招聘类型`, industries=`多选`, URL=`投递链接`, referral code=`内推码`, deadline=`截止日期`, and source time=`更新时间`. Export:

```python
MAPPERS: dict[str, SourceMapper] = {
    "tencent-27-referrals": Tencent27ReferralsMapper(),
    "tencent-intern-referrals": TencentInternReferralsMapper(),
}
```

Both mapper classes implement `source_updated_at(record)` by parsing the exact `更新时间` field. The sync service calls this method before eligibility mapping so skipped raw records retain valid source timestamps.

- [ ] **Step 4: Run mapper tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_job_mappers.py -v`

Expected: PASS.

- [ ] **Step 5: Commit the mappers**

```powershell
git add backend/app/services/job_mappers.py tests/unit/test_job_mappers.py
git commit -m "feat: map Tencent job source records"
```

### Task 4: Add job persistence, leases, and filtered reads

**Files:**
- Create: `backend/app/repositories/jobs.py`
- Create: `tests/unit/test_job_repository.py`

**Interfaces:**
- Consumes: Task 1 ORM entities and `BuiltinJobSource` / `NormalizedJobCandidate` from Task 3.
- Produces: `ensure_builtin_sources`, `get_source`, `acquire_sync_run`, `refresh_sync_lease`, `finish_sync_run`, `insert_raw_snapshot`, `upsert_posting`, `list_postings`, `get_posting`, `SyncConflictError`, `SourceNotFoundError`, `SourceDisabledError`, and `StaleSyncLeaseError`.

- [ ] **Step 1: Write failing repository tests**

Create `tests/unit/test_job_repository.py` with an in-memory SQLite engine and `Base.metadata.create_all`. Cover these exact behaviors:

```python
from datetime import timedelta

from backend.app.db.base import utc_now
from backend.app.repositories import jobs
from backend.app.services.job_mappers import BUILTIN_SOURCES, NormalizedJobCandidate


def test_builtin_source_initialization_is_idempotent(db) -> None:
    jobs.ensure_builtin_sources(db, BUILTIN_SOURCES)
    jobs.ensure_builtin_sources(db, BUILTIN_SOURCES)
    db.commit()
    assert len(jobs.list_sources(db)) == 2


def test_active_lease_rejects_a_second_run(db) -> None:
    jobs.ensure_builtin_sources(db, BUILTIN_SOURCES)
    db.commit()
    source = jobs.get_source(db, "tencent-intern-referrals")
    assert source is not None
    now = utc_now()
    jobs.acquire_sync_run(db, source.id, now=now)
    db.commit()
    with pytest.raises(jobs.SyncConflictError):
        jobs.acquire_sync_run(db, source.id, now=now + timedelta(seconds=1))


def test_same_payload_is_one_snapshot(db) -> None:
    source = seeded_source(db)
    first, created_first = jobs.insert_raw_snapshot(
        db,
        source_id=source.id,
        external_record_id="r1",
        raw_fields=[{"field": "公司名称", "text_value": {"items": []}}],
        payload_hash="a" * 64,
        source_updated_at=None,
        observed_at=utc_now(),
    )
    second, created_second = jobs.insert_raw_snapshot(
        db,
        source_id=source.id,
        external_record_id="r1",
        raw_fields=[{"field": "公司名称", "text_value": {"items": []}}],
        payload_hash="a" * 64,
        source_updated_at=None,
        observed_at=utc_now(),
    )
    assert first.id == second.id
    assert (created_first, created_second) == (True, False)
```

Also test expired-run takeover marks the old run `FAILED` with `sync_lease_expired`, lease refresh requires the matching run ID, finishing success updates `last_successful_sync_at`, posting upsert changes `raw_record_id` without deleting history, a mapper-version change reprocesses an unchanged raw snapshot, missing upstream rows remain, stable `updated_at DESC, id DESC` ordering, company wildcard escaping, and exact recruitment-type matching on SQLite JSON arrays.

- [ ] **Step 2: Run tests and verify the repository is absent**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_job_repository.py -v`

Expected: FAIL during collection because `backend.app.repositories.jobs` does not exist.

- [ ] **Step 3: Implement source and lease persistence**

In `backend/app/repositories/jobs.py`, define:

```python
LEASE_DURATION = timedelta(minutes=10)


class SyncConflictError(RuntimeError):
    error_code = "sync_conflict"


class SourceNotFoundError(LookupError):
    pass


class SourceDisabledError(RuntimeError):
    pass


class StaleSyncLeaseError(RuntimeError):
    pass


def ensure_builtin_sources(
    db: Session, definitions: Sequence[BuiltinJobSource]
) -> None:
    for definition in definitions:
        source = get_source(db, definition.source_key)
        if source is None:
            db.add(JobSource(
                source_key=definition.source_key,
                provider=JobSourceProvider.TENCENT_SMARTSHEET,
                name=definition.name,
                file_id=definition.file_id,
                sheet_id=definition.sheet_id,
                mapper_version=definition.mapper_version,
                enabled=True,
            ))
            continue
        source.name = definition.name
        source.file_id = definition.file_id
        source.sheet_id = definition.sheet_id
        source.mapper_version = definition.mapper_version
    db.flush()


def list_sources(db: Session) -> list[JobSource]:
    return list(db.scalars(select(JobSource).order_by(JobSource.source_key)))


def get_source(
    db: Session, source_key: str, *, lock: bool = False
) -> JobSource | None:
    statement = select(JobSource).where(JobSource.source_key == source_key)
    if lock:
        statement = statement.with_for_update()
    return db.scalar(statement)


def acquire_sync_run(
    db: Session, source_id: str, *, now: datetime
) -> JobSyncRun:
    source = db.scalar(
        select(JobSource).where(JobSource.id == source_id).with_for_update()
    )
    if source is None:
        raise SourceNotFoundError(source_id)
    if not source.enabled:
        raise SourceDisabledError(source.source_key)
    if (
        source.active_sync_run_id is not None
        and source.sync_lease_expires_at is not None
        and source.sync_lease_expires_at > now
    ):
        raise SyncConflictError(source.source_key)
    if source.active_sync_run_id is not None:
        expired = db.get(JobSyncRun, source.active_sync_run_id)
        if expired is not None and expired.status is JobSyncRunStatus.RUNNING:
            expired.status = JobSyncRunStatus.FAILED
            expired.error_code = "sync_lease_expired"
            expired.finished_at = now
    run = JobSyncRun(source_id=source.id, status=JobSyncRunStatus.RUNNING, started_at=now)
    db.add(run)
    db.flush()
    source.active_sync_run_id = run.id
    source.sync_lease_expires_at = now + LEASE_DURATION
    db.flush()
    return run


def refresh_sync_lease(
    db: Session, source_id: str, run_id: str, *, now: datetime
) -> None:
    source = db.scalar(
        select(JobSource).where(JobSource.id == source_id).with_for_update()
    )
    if source is None or source.active_sync_run_id != run_id:
        raise StaleSyncLeaseError(run_id)
    source.sync_lease_expires_at = now + LEASE_DURATION
    db.flush()


def finish_sync_run(
    db: Session,
    source_id: str,
    run_id: str,
    *,
    status: JobSyncRunStatus,
    now: datetime,
    error_code: str | None,
) -> JobSyncRun:
    source = db.scalar(
        select(JobSource).where(JobSource.id == source_id).with_for_update()
    )
    if source is None or source.active_sync_run_id != run_id:
        raise StaleSyncLeaseError(run_id)
    run = db.get(JobSyncRun, run_id)
    if run is None:
        raise StaleSyncLeaseError(run_id)
    run.status = status
    run.error_code = error_code
    run.finished_at = now
    source.active_sync_run_id = None
    source.sync_lease_expires_at = None
    if status is JobSyncRunStatus.SUCCEEDED:
        source.last_successful_sync_at = now
    db.flush()
    return run
```

`acquire_sync_run` must select the source `FOR UPDATE`, reject a non-expired active lease, mark an expired active run failed, create a new run, and set `active_sync_run_id` plus `now + LEASE_DURATION` in the same transaction. `finish_sync_run` verifies lease ownership, clears both lease columns, sets `finished_at`, and updates `last_successful_sync_at` only for `SUCCEEDED`.

- [ ] **Step 4: Implement immutable snapshots, posting upsert, and queries**

Add these exact interfaces:

```python
def insert_raw_snapshot(
    db: Session,
    *,
    source_id: str,
    external_record_id: str,
    raw_fields: list[dict[str, Any]],
    payload_hash: str,
    source_updated_at: datetime | None,
    observed_at: datetime,
) -> tuple[RawJobRecord, bool]:
    existing = db.scalar(select(RawJobRecord).where(
        RawJobRecord.source_id == source_id,
        RawJobRecord.external_record_id == external_record_id,
        RawJobRecord.payload_hash == payload_hash,
    ))
    if existing is not None:
        return existing, False
    record = RawJobRecord(
        source_id=source_id,
        external_record_id=external_record_id,
        payload_hash=payload_hash,
        raw_fields=raw_fields,
        source_updated_at=source_updated_at,
        observed_at=observed_at,
    )
    db.add(record)
    db.flush()
    return record, True


def upsert_posting(
    db: Session,
    *,
    source: JobSource,
    raw_record: RawJobRecord,
    candidate: NormalizedJobCandidate,
) -> tuple[JobPosting, Literal["created", "updated", "unchanged"]]:
    posting = db.scalar(select(JobPosting).where(
        JobPosting.source_id == source.id,
        JobPosting.external_record_id == raw_record.external_record_id,
    ))
    if (
        posting is not None
        and posting.raw_record_id == raw_record.id
        and posting.mapper_version == source.mapper_version
    ):
        return posting, "unchanged"
    values = {
        "raw_record_id": raw_record.id,
        "company_name": candidate.company_name,
        "title": candidate.title,
        "locations": candidate.locations,
        "recruitment_types": candidate.recruitment_types,
        "industries": candidate.industries,
        "apply_url": candidate.apply_url,
        "referral_code": candidate.referral_code,
        "deadline_text": candidate.deadline_text,
        "source_updated_at": candidate.source_updated_at,
        "mapper_version": source.mapper_version,
    }
    if posting is None:
        posting = JobPosting(
            source_id=source.id,
            external_record_id=raw_record.external_record_id,
            status=JobPostingStatus.PENDING_COMPLETION,
            **values,
        )
        db.add(posting)
        db.flush()
        return posting, "created"
    for name, value in values.items():
        setattr(posting, name, value)
    db.flush()
    return posting, "updated"


def list_postings(
    db: Session,
    *,
    limit: int,
    offset: int,
    source_key: str | None,
    company: str | None,
    recruitment_type: str | None,
) -> tuple[int, list[tuple[JobPosting, JobSource]]]:
    filters: list[Any] = [
        JobPosting.status == JobPostingStatus.PENDING_COMPLETION
    ]
    if source_key:
        filters.append(JobSource.source_key == source_key)
    if company:
        escaped = company.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        filters.append(JobPosting.company_name.like(f"%{escaped}%", escape="\\"))
    if recruitment_type:
        if db.get_bind().dialect.name == "mysql":
            filters.append(
                func.json_contains(
                    JobPosting.recruitment_types, json.dumps(recruitment_type)
                ) == 1
            )
        else:
            labels = func.json_each(JobPosting.recruitment_types).table_valued(
                "key", "value"
            ).alias("recruitment_labels")
            filters.append(exists(
                select(1).select_from(labels).where(labels.c.value == recruitment_type)
            ))
    total = db.scalar(
        select(func.count()).select_from(JobPosting).join(JobSource).where(*filters)
    ) or 0
    statement = (
        select(JobPosting, JobSource)
        .join(JobSource)
        .where(*filters)
        .order_by(JobPosting.updated_at.desc(), JobPosting.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return int(total), [(posting, source) for posting, source in db.execute(statement)]


def get_posting(db: Session, job_id: str) -> tuple[JobPosting, JobSource] | None:
    row = db.execute(
        select(JobPosting, JobSource)
        .join(JobSource)
        .where(
            JobPosting.id == job_id,
            JobPosting.status == JobPostingStatus.PENDING_COMPLETION,
        )
    ).one_or_none()
    return (row[0], row[1]) if row is not None else None
```

Use a pre-insert lookup plus the database unique constraint for raw idempotency; the source lease prevents same-source writer races. `upsert_posting` returns `unchanged` when `raw_record_id` already matches. Escape `\\`, `%`, and `_` in company filters. Use MySQL `JSON_CONTAINS` for complete recruitment labels and SQLite `json_each` in unit tests; never use substring matching against serialized JSON.

- [ ] **Step 5: Run repository tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_job_repository.py -v`

Expected: PASS.

- [ ] **Step 6: Commit persistence**

```powershell
git add backend/app/repositories/jobs.py tests/unit/test_job_repository.py
git commit -m "feat: persist job sync runs and postings"
```

### Task 5: Orchestrate page-by-page synchronization

**Files:**
- Create: `backend/app/services/job_sync.py`
- Create: `tests/unit/test_job_sync_service.py`

**Interfaces:**
- Consumes: Task 2 `SmartsheetGateway`, Task 3 `BUILTIN_SOURCES` / `MAPPERS`, Task 4 repository functions, existing `AuditEvent`, and a SQLAlchemy `Session`.
- Produces: `JobSyncService.sync`, `SyncOutcome`, `JobSyncFailedError`, and stable `run_id/error_code` failure state.

- [ ] **Step 1: Write failing service tests with a paged fake Gateway**

Create a reusable offset-keyed `FakeGateway`; using offsets rather than consuming a queue lets the same fake prove an immediate idempotent rerun:

```python
class FakeGateway:
    def __init__(self, *, fields, pages, failure_at_offset=None):
        self.fields = fields
        self.pages = pages
        self.failure_at_offset = failure_at_offset
        self.calls: list[int] = []

    def list_fields(self, _file_id: str, _sheet_id: str):
        return self.fields

    def list_records(self, _file_id: str, _sheet_id: str, *, offset: int, limit: int):
        assert limit == 100
        self.calls.append(offset)
        if offset == self.failure_at_offset:
            raise TencentTimeoutError("constant test failure")
        return self.pages[offset]
```

Define `intern_fields()` with `公司名称:text`, `招聘岗位:text`, and `投递链接:url`; define `complete_record(record_id)` with those fields and an HTTPS link. Add tests for:

```python
def test_sync_commits_each_page_and_is_idempotent(db, complete_record) -> None:
    gateway = FakeGateway(
        fields=intern_fields(),
        pages={
            0: TencentRecordPage([complete_record("r1")], 2, True, 1),
            1: TencentRecordPage([complete_record("r2")], 2, False, 0),
        },
    )
    service = JobSyncService(
        gateway,
        now=lambda: datetime(2026, 7, 15, tzinfo=timezone.utc),
        correlation_id_factory=lambda: "c1",
    )
    first = service.sync(db, source_key="tencent-intern-referrals", actor_user_id="admin")
    second = service.sync(db, source_key="tencent-intern-referrals", actor_user_id="admin")
    assert first.status is JobSyncRunStatus.SUCCEEDED
    assert first.records_read == 2
    assert first.raw_snapshots_created == 2
    assert second.raw_snapshots_created == 0
    assert second.postings_created == 0


def test_second_page_failure_preserves_first_page(db, complete_record) -> None:
    gateway = FakeGateway(
        fields=intern_fields(),
        pages={0: TencentRecordPage([complete_record("r1")], 2, True, 1)},
        failure_at_offset=1,
    )
    service = JobSyncService(
        gateway,
        now=lambda: datetime(2026, 7, 15, tzinfo=timezone.utc),
        correlation_id_factory=lambda: "c2",
    )
    with pytest.raises(JobSyncFailedError) as caught:
        service.sync(db, source_key="tencent-intern-referrals", actor_user_id="admin")
    assert caught.value.status is JobSyncRunStatus.PARTIAL
    assert jobs.list_postings(
        db,
        limit=20,
        offset=0,
        source_key=None,
        company=None,
        recruitment_type=None,
    )[0] == 1
```

Also test schema failure before page 1 becomes FAILED, source-one records create raw snapshots but zero postings, malformed individual records increment `records_skipped_incomplete`, changed content creates one new raw snapshot and updates one posting, a source disappearing upstream does not delete its posting, page/record caps fail with `tencent_protocol_error`, and started/finished audit payloads contain only safe counters and IDs.

- [ ] **Step 2: Run tests and verify the service is absent**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_job_sync_service.py -v`

Expected: FAIL during collection with `ModuleNotFoundError`.

- [ ] **Step 3: Implement canonical hashing and outcome types**

Create `backend/app/services/job_sync.py` with:

```python
@dataclass(frozen=True)
class SyncOutcome:
    run_id: str
    source_key: str
    status: JobSyncRunStatus
    pages_read: int
    records_read: int
    raw_snapshots_created: int
    postings_created: int
    postings_updated: int
    records_skipped_incomplete: int
    started_at: datetime
    finished_at: datetime


class JobSyncFailedError(RuntimeError):
    def __init__(self, run_id: str, status: JobSyncRunStatus, error_code: str):
        super().__init__(error_code)
        self.run_id = run_id
        self.status = status
        self.error_code = error_code


def canonical_payload_hash(field_values: list[dict[str, Any]]) -> str:
    encoded = json.dumps(
        field_values,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
```

- [ ] **Step 4: Implement `JobSyncService.sync`**

Use this transaction order:

```python
class JobSyncService:
    def __init__(
        self,
        gateway: SmartsheetGateway,
        *,
        now: Callable[[], datetime] = utc_now,
        correlation_id_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        self.gateway = gateway
        self.now = now
        self.correlation_id_factory = correlation_id_factory

    def sync(
        self, db: Session, *, source_key: str, actor_user_id: str
    ) -> SyncOutcome:
        if source_key not in MAPPERS:
            raise jobs.SourceNotFoundError(source_key)
        jobs.ensure_builtin_sources(db, BUILTIN_SOURCES)
        source = jobs.get_source(db, source_key)
        if source is None:
            db.rollback()
            raise jobs.SourceNotFoundError(source_key)
        run = jobs.acquire_sync_run(db, source.id, now=self.now())
        correlation_id = self.correlation_id_factory()
        db.add(AuditEvent(
            actor_user_id=actor_user_id,
            event_type="job_sync.started",
            entity_type="job_sync_run",
            entity_id=run.id,
            correlation_id=correlation_id,
            redacted_payload={"source_key": source.source_key, "run_id": run.id},
        ))
        db.commit()
        mapper = MAPPERS[source_key]
        try:
            mapper.validate_schema(
                self.gateway.list_fields(source.file_id, source.sheet_id)
            )
            offset = 0
            expected_total: int | None = None
            while True:
                if run.pages_read >= MAX_PAGES:
                    raise TencentProtocolError("Tencent page limit exceeded")
                page = self.gateway.list_records(
                    source.file_id,
                    source.sheet_id,
                    offset=offset,
                    limit=PAGE_SIZE,
                )
                if expected_total is None:
                    expected_total = page.total
                elif page.total != expected_total:
                    raise TencentProtocolError("Tencent total changed during sync")
                if run.records_read + len(page.records) > MAX_RECORDS:
                    raise TencentProtocolError("Tencent record limit exceeded")
                for record in page.records:
                    raw, created = jobs.insert_raw_snapshot(
                        db,
                        source_id=source.id,
                        external_record_id=record.record_id,
                        raw_fields=record.field_values,
                        payload_hash=canonical_payload_hash(record.field_values),
                        source_updated_at=mapper.source_updated_at(record),
                        observed_at=self.now(),
                    )
                    if created:
                        run.raw_snapshots_created += 1
                    mapped = mapper.map(record)
                    if isinstance(mapped, SkippedRecord):
                        run.records_skipped_incomplete += 1
                        continue
                    _posting, action = jobs.upsert_posting(
                        db, source=source, raw_record=raw, candidate=mapped
                    )
                    if action == "created":
                        run.postings_created += 1
                    elif action == "updated":
                        run.postings_updated += 1
                run.pages_read += 1
                run.records_read += len(page.records)
                jobs.refresh_sync_lease(db, source.id, run.id, now=self.now())
                db.commit()
                if not page.has_more:
                    if run.records_read != expected_total:
                        raise TencentProtocolError("Tencent total did not match records read")
                    break
                offset = page.next_offset

            finished = jobs.finish_sync_run(
                db,
                source.id,
                run.id,
                status=JobSyncRunStatus.SUCCEEDED,
                now=self.now(),
                error_code=None,
            )
            db.add(AuditEvent(
                actor_user_id=actor_user_id,
                event_type="job_sync.finished",
                entity_type="job_sync_run",
                entity_id=run.id,
                correlation_id=correlation_id,
                redacted_payload={
                    "source_key": source.source_key,
                    "run_id": run.id,
                    "status": "succeeded",
                    "pages_read": run.pages_read,
                    "records_read": run.records_read,
                    "raw_snapshots_created": run.raw_snapshots_created,
                    "postings_created": run.postings_created,
                    "postings_updated": run.postings_updated,
                    "records_skipped_incomplete": run.records_skipped_incomplete,
                },
            ))
            db.commit()
            assert finished.finished_at is not None
            return SyncOutcome(
                run_id=finished.id,
                source_key=source.source_key,
                status=finished.status,
                pages_read=finished.pages_read,
                records_read=finished.records_read,
                raw_snapshots_created=finished.raw_snapshots_created,
                postings_created=finished.postings_created,
                postings_updated=finished.postings_updated,
                records_skipped_incomplete=finished.records_skipped_incomplete,
                started_at=finished.started_at,
                finished_at=finished.finished_at,
            )
        except (TencentGatewayError, SourceSchemaChangedError) as exc:
            self._finish_failure(
                db,
                source=source,
                run_id=run.id,
                actor_user_id=actor_user_id,
                correlation_id=correlation_id,
                error_code=exc.error_code,
            )
            failed = db.get(JobSyncRun, run.id)
            assert failed is not None
            raise JobSyncFailedError(run.id, failed.status, exc.error_code) from None
        except SQLAlchemyError:
            self._finish_failure(
                db,
                source=source,
                run_id=run.id,
                actor_user_id=actor_user_id,
                correlation_id=correlation_id,
                error_code="database_write_failed",
            )
            failed = db.get(JobSyncRun, run.id)
            assert failed is not None
            raise JobSyncFailedError(
                run.id, failed.status, "database_write_failed"
            ) from None

    def _finish_failure(
        self,
        db: Session,
        *,
        source: JobSource,
        run_id: str,
        actor_user_id: str,
        correlation_id: str,
        error_code: str,
    ) -> None:
        db.rollback()
        run = db.get(JobSyncRun, run_id)
        if run is None:
            raise RuntimeError("authoritative sync run disappeared")
        status = (
            JobSyncRunStatus.PARTIAL
            if run.pages_read > 0
            else JobSyncRunStatus.FAILED
        )
        jobs.finish_sync_run(
            db,
            source.id,
            run.id,
            status=status,
            now=self.now(),
            error_code=error_code,
        )
        db.add(AuditEvent(
            actor_user_id=actor_user_id,
            event_type="job_sync.finished",
            entity_type="job_sync_run",
            entity_id=run.id,
            correlation_id=correlation_id,
            redacted_payload={
                "source_key": source.source_key,
                "run_id": run.id,
                "status": status.value,
                "pages_read": run.pages_read,
                "records_read": run.records_read,
                "error_code": error_code,
            },
        ))
        db.commit()
```

Do not catch `KeyboardInterrupt` or `SystemExit`; the lease-expiry takeover path handles abrupt process death.

- [ ] **Step 5: Run sync service tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_job_sync_service.py -v`

Expected: PASS.

- [ ] **Step 6: Commit orchestration**

```powershell
git add backend/app/services/job_sync.py tests/unit/test_job_sync_service.py
git commit -m "feat: synchronize Tencent job records"
```

### Task 6: Expose authenticated sync and job query APIs

**Files:**
- Modify: `backend/app/api/dependencies.py`
- Create: `backend/app/api/routes/jobs.py`
- Modify: `backend/app/api/router.py`
- Create: `tests/contract/test_jobs_api.py`

**Interfaces:**
- Consumes: `JobSyncService.sync`, Task 4 query functions, `Settings.tencent_docs_token`, `get_current_user`, and `require_admin`.
- Produces: `POST /api/admin/job-sources/{source_key}/sync`, `GET /api/jobs`, and `GET /api/jobs/{job_id}`.

- [ ] **Step 1: Write failing API contract tests**

Create `tests/contract/test_jobs_api.py`. Build an app with the existing SQLite test session factory, insert an admin, a student, sources, and one posting, and set `app.state.job_sync_service` to a fake. Cover:

```python
def test_admin_can_sync(client, admin_headers, fake_sync_service) -> None:
    response = client.post(
        "/api/admin/job-sources/tencent-intern-referrals/sync",
        headers=admin_headers,
    )
    assert response.status_code == 200
    assert response.json()["status"] == "succeeded"
    assert fake_sync_service.calls == [
        {"source_key": "tencent-intern-referrals", "actor_user_id": fake_sync_service.admin_id}
    ]


def test_student_cannot_sync(client, student_headers) -> None:
    response = client.post(
        "/api/admin/job-sources/tencent-intern-referrals/sync",
        headers=student_headers,
    )
    assert response.status_code == 403


def test_anonymous_user_cannot_list_jobs(client) -> None:
    assert client.get("/api/jobs").status_code == 401


def test_job_detail_whitelists_fields(client, student_headers, seeded_job) -> None:
    response = client.get(f"/api/jobs/{seeded_job.id}", headers=student_headers)
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "pending_completion"
    assert "raw_fields" not in payload
    assert "payload_hash" not in payload
    assert "external_record_id" not in payload
    assert "mcp_trace" not in payload
```

Also cover 404 source/job, 409 conflict, stable 502/503/504 mappings, limit range validation, non-negative offset, source/company/recruitment filters, stable response ordering, and missing token not affecting `/api/health/ready` or `GET /api/jobs`.

- [ ] **Step 2: Run API tests and verify routes return 404**

Run: `.\.venv\Scripts\python.exe -m pytest tests/contract/test_jobs_api.py -v`

Expected: FAIL because the jobs router and dependency do not exist.

- [ ] **Step 3: Add injectable service construction**

In `backend/app/api/dependencies.py`, add:

```python
def get_job_sync_service(request: Request) -> JobSyncService:
    injected = getattr(request.app.state, "job_sync_service", None)
    if injected is not None:
        return cast(JobSyncService, injected)
    secret = request.app.state.settings.tencent_docs_token
    token = secret.get_secret_value() if secret is not None else None
    return JobSyncService(TencentSmartsheetGateway(token=token))
```

Import `JobSyncService` and `TencentSmartsheetGateway`. The Gateway creates and closes its MCP/httpx resources inside each tool call, so this dependency owns no unclosed client.

- [ ] **Step 4: Implement response models and endpoints**

Create `backend/app/api/routes/jobs.py` with these response fields:

```python
class JobSyncResponse(BaseModel):
    run_id: str
    source_key: str
    status: JobSyncRunStatus
    pages_read: int
    records_read: int
    raw_snapshots_created: int
    postings_created: int
    postings_updated: int
    records_skipped_incomplete: int
    started_at: datetime
    finished_at: datetime


class JobSummary(BaseModel):
    id: str
    company_name: str
    title: str
    locations: list[str]
    recruitment_types: list[str]
    industries: list[str]
    apply_url: str
    deadline_text: str | None
    status: JobPostingStatus
    source_key: str
    source_name: str
    updated_at: datetime


class JobListResponse(BaseModel):
    total: int
    jobs: list[JobSummary]


class JobDetail(JobSummary):
    referral_code: str | None
    source_updated_at: datetime | None
    mapper_version: str
```

Use one router with no global prefix and declare:

```python
@router.post(
    "/admin/job-sources/{source_key}/sync", response_model=JobSyncResponse
)
def sync_job_source(
    source_key: str,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(_get_db)],
    service: Annotated[JobSyncService, Depends(get_job_sync_service)],
) -> JobSyncResponse:
    outcome = service.sync(db, source_key=source_key, actor_user_id=admin.id)
    return JobSyncResponse.model_validate(outcome, from_attributes=True)
```

Map `SourceNotFoundError` to 404, `SyncConflictError` / `SourceDisabledError` to 409, and `JobSyncFailedError` by `error_code`: protocol/schema to 502, token/auth/rate-limit/unavailable/database-write failure to 503, timeout to 504. Error detail is exactly `{"error_code": error_code, "run_id": run_id}` for failures with a run and never includes `str(upstream_exception)`.

Implement `GET /jobs` with `limit: Query(20, ge=1, le=100)`, `offset: Query(0, ge=0)`, optional source/company/recruitment filters, and `get_current_user`. Implement `GET /jobs/{job_id}` after the list route so `/jobs` is not shadowed. Serialize from explicit fields only; never call `model_validate` directly on `RawJobRecord`.

- [ ] **Step 5: Register the router and run contract tests**

Add `jobs` to imports and `api_router.include_router(jobs.router)` in `backend/app/api/router.py`.

Run: `.\.venv\Scripts\python.exe -m pytest tests/contract/test_jobs_api.py tests/contract/test_health_api.py -v`

Expected: PASS.

- [ ] **Step 6: Commit the API**

```powershell
git add backend/app/api/dependencies.py backend/app/api/routes/jobs.py backend/app/api/router.py tests/contract/test_jobs_api.py
git commit -m "feat: expose job sync and query APIs"
```

### Task 7: Add MySQL, live-source, and redaction integration gates

**Files:**
- Create: `tests/integration/test_job_sync_mysql.py`
- Create: `tests/integration/test_tencent_smartsheet_live.py`
- Modify: `tests/security/test_no_sensitive_logging.py`

**Interfaces:**
- Consumes: `TEST_MYSQL_URL`, `TEST_TENCENT_DOCS_TOKEN`, all Task 1–6 interfaces, and the two fixed built-in sources.
- Produces: opt-in real dependency proof without writing to Tencent or retaining test rows.

- [ ] **Step 1: Write MySQL-specific tests**

Create `tests/integration/test_job_sync_mysql.py`, skip the module when `TEST_MYSQL_URL` is absent, and use a unique source key prefix plus `finally` cleanup. Test:

```python
def test_mysql_exact_recruitment_type_filter(mysql_session) -> None:
    posting = seed_posting(mysql_session, recruitment_types=["实习", "暑期实习"])
    total, rows = jobs.list_postings(
        mysql_session,
        limit=20,
        offset=0,
        source_key=None,
        company=None,
        recruitment_type="实习",
    )
    assert posting.id in {item.id for item, _source in rows}
    assert total >= 1


def test_mysql_active_source_lease_conflicts(mysql_session) -> None:
    source = seed_source(mysql_session)
    first = jobs.acquire_sync_run(mysql_session, source.id, now=utc_now())
    mysql_session.commit()
    with pytest.raises(jobs.SyncConflictError):
        jobs.acquire_sync_run(mysql_session, source.id, now=utc_now())
    assert first.status is JobSyncRunStatus.RUNNING
```

Also issue concurrent attempts through two independent sessions and assert exactly one obtains the `FOR UPDATE` protected lease.

- [ ] **Step 2: Write the opt-in live read-only test**

Create `tests/integration/test_tencent_smartsheet_live.py`, skip unless both `TEST_MYSQL_URL` and `TEST_TENCENT_DOCS_TOKEN` exist. Before deleting or inserting rows, parse the database URL and assert its database name ends with `_test`; fail closed otherwise. Migrate that dedicated database to head, delete prior rows for the two built-in sources in `job_postings → raw_job_records → job_sync_runs → job_sources` order, build a `TencentSmartsheetGateway` from the test token, synchronize both built-in source keys, and assert:

```python
for source_key in ("tencent-27-referrals", "tencent-intern-referrals"):
    outcome = JobSyncService(gateway).sync(
        session, source_key=source_key, actor_user_id=admin.id
    )
    assert outcome.status is JobSyncRunStatus.SUCCEEDED
    source = jobs.get_source(session, source_key)
    assert source is not None
    raw_count = session.scalar(
        select(func.count()).select_from(RawJobRecord).where(
            RawJobRecord.source_id == source.id
        )
    )
    assert raw_count == outcome.records_read

first_source = jobs.get_source(session, "tencent-27-referrals")
assert count_postings(session, first_source.id) == 0
second_source = jobs.get_source(session, "tencent-intern-referrals")
assert 0 < count_postings(session, second_source.id) <= count_raw(session, second_source.id)
```

Run the same sync again and assert `raw_snapshots_created == 0`. Delete only rows created by this test in foreign-key-safe order. The test must instantiate only the Gateway methods defined in Task 2; it must not import or invoke Tencent add/update/delete tools.

- [ ] **Step 3: Extend sensitive logging tests**

In `tests/security/test_no_sensitive_logging.py`, inject a Gateway failure whose internal message contains:

```python
secret_token = "tdoc-super-secret-token"
raw_payload = "raw-company-private-payload"
upstream_body = "upstream-debug-response"
```

Trigger the sync API, capture logs plus response text, and assert all three strings are absent while the stable error code is present.

- [ ] **Step 4: Run integration and security tests**

Run without real variables: `.\.venv\Scripts\python.exe -m pytest tests/integration/test_job_sync_mysql.py tests/integration/test_tencent_smartsheet_live.py tests/security/test_no_sensitive_logging.py -v`

Expected: new MySQL/live tests SKIP with explicit environment reasons; security tests PASS.

With the dedicated test variables loaded using the runbook, run the same command again.

Expected: all tests PASS, both Tencent sources are read successfully, and no test is skipped.

- [ ] **Step 5: Commit integration gates**

```powershell
git add tests/integration/test_job_sync_mysql.py tests/integration/test_tencent_smartsheet_live.py tests/security/test_no_sensitive_logging.py
git commit -m "test: verify real job synchronization boundaries"
```

### Task 8: Document operations and run the full release gate

**Files:**
- Modify: `docs/runbooks/platform-foundation.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: all endpoints, settings, error codes, and test commands from Tasks 1–7.
- Produces: reproducible operator instructions and final verified build.

- [ ] **Step 1: Add exact runbook instructions**

Document all of the following in `docs/runbooks/platform-foundation.md`:

```powershell
$token = [Environment]::GetEnvironmentVariable('TENCENT_DOCS_TOKEN', 'User')
if ([string]::IsNullOrWhiteSpace($token)) {
  throw 'Missing TENCENT_DOCS_TOKEN user environment variable'
}
Set-Item -Path Env:TENCENT_DOCS_TOKEN -Value $token
```

Add the two fixed source keys, an authenticated administrator `Invoke-RestMethod` example that sends the JWT only in the Authorization header, and interpretations for `SUCCEEDED`, `PARTIAL`, `FAILED`, 409, 502, 503, and 504. State explicitly that Tencent is read-only, not part of `/api/health/ready`, and a PARTIAL run is recovered by rerunning from page 0.

Add `TEST_TENCENT_DOCS_TOKEN` to the test-variable section without showing a token value. Add the new endpoint summary and design/plan links to `README.md`; do not add a frontend feature claim.

- [ ] **Step 2: Run format and static checks**

Run: `.\.venv\Scripts\python.exe -m ruff check backend src tests scripts`

Expected: `All checks passed!`

- [ ] **Step 3: Run the full Python gate against real dependencies**

Load `DB_PASSWORD` and `REDIS_PASSWORD` from user environment variables as required by `AGENTS.md`; load MinIO variables and `TEST_TENCENT_DOCS_TOKEN` without printing them. Construct `TEST_MYSQL_URL`, `TEST_REDIS_URL`, and S3 test variables using the existing runbook. Never echo a URL containing a password.

Run: `.\.venv\Scripts\python.exe -m pytest -q`

Expected: all tests PASS with zero skip when all documented test variables are configured. Record the actual passed-test count in the implementation handoff instead of hard-coding the previous `392 passed` count.

- [ ] **Step 4: Build the frontend and validate Compose configuration**

Run: `npm.cmd --prefix frontend ci`

Expected: exit 0.

Run: `npm.cmd --prefix frontend run build`

Expected: Vite production build succeeds.

Run: `docker compose -p platform-foundation config --quiet`

Expected: exit 0 without printing secret values.

Run: `docker compose -p platform-foundation up -d --build`

Expected: the migration container exits 0 at revision `20260715_0003`; MySQL, Redis, MinIO, backend, and frontend reach their documented running/healthy states.

Run: `Invoke-RestMethod http://127.0.0.1:8000/api/health/ready`

Expected: HTTP 200 with MySQL, Redis, and object store `up`; Tencent is not added to the readiness payload.

Run: `Invoke-WebRequest http://127.0.0.1:5173/ -UseBasicParsing`

Expected: HTTP 200.

- [ ] **Step 5: Run secret and boundary searches**

Run:

```powershell
rg -n "tdoc-super-secret-token|TENCENT_DOCS_TOKEN=.*[^)]|smartsheet\.(add|update|delete)|mcporter" backend tests docs README.md
```

Expected: no committed token assignment, no Tencent write-tool call, no production `mcporter` dependency; documentation may mention variable names and explain that `mcporter` is not used by the backend.

Run:

```powershell
rg -n "verified|已核验|task:submit" backend/app/api/routes/jobs.py backend/app/services/job_sync.py backend/app/services/job_mappers.py
```

Expected: no code path marks a synchronized posting verified or grants submission authority.

- [ ] **Step 6: Commit documentation and final gate changes**

```powershell
git add docs/runbooks/platform-foundation.md README.md
git commit -m "docs: document real job synchronization"
```

### Task 9: Final implementation review and handoff

**Files:**
- Review only: all files changed in Tasks 1–8

**Interfaces:**
- Consumes: the approved design and every implementation commit.
- Produces: a clean, verified branch ready for the user's chosen integration workflow.

- [ ] **Step 1: Verify working-tree scope**

Run: `git status --short`

Expected: only the pre-existing `AM AGENTS.md` and untracked `docs/platform-foundation-handover-summary.md` remain; no implementation file is uncommitted.

- [ ] **Step 2: Review the diff against the approved design**

Run: `git diff 45cda72..HEAD --stat`

Expected: changes are limited to the files named in this plan plus the plan document itself; no frontend source, GUI Agent state machine, analysis flow, or unrelated refactor appears.

- [ ] **Step 3: Re-run focused authoritative checks after review fixes**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_tencent_smartsheet.py tests/unit/test_job_mappers.py tests/unit/test_job_repository.py tests/unit/test_job_sync_service.py tests/contract/test_jobs_api.py tests/security/test_no_sensitive_logging.py -q
```

Expected: all focused tests PASS.

- [ ] **Step 4: Prepare the handoff**

Report the migration revision, endpoint paths, built-in source keys, real test results, full test count, frontend build result, and any test that could not run because a documented environment variable was unavailable. Do not report completion if a required real dependency gate did not run.
