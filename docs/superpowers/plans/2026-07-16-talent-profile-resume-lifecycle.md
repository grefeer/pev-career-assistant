# Talent Profile and Resume Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an encrypted, user-owned PDF/DOCX/text resume lifecycle that produces reviewable field evidence and immutable confirmed profile versions without allowing parsed or sensitive data to become trusted facts automatically.

**Architecture:** Keep document extraction, profile-domain rules, SQL persistence, HTTP DTOs, and the Vue workspace in focused modules. MySQL owns profile metadata, evidence decisions, and immutable versions; the existing AES-256-GCM object-store adapter owns resume bytes, while local-sensitive values are represented only by an allow-listed category and irreversible reference. The workflow persists upload and parse checkpoints before external work so retries can reconcile an existing encrypted object or create a new append-only import without overwriting prior evidence or confirmed versions.

**Tech Stack:** Python 3.13 target (project `.venv` for all commands), FastAPI 0.117.1, Pydantic 2.12.5, SQLAlchemy 2.0.51, Alembic 1.16.5, MySQL 8.4, `pypdf` 6.1.1, `python-docx` 1.2.0, MinIO/S3 through the existing `EncryptedObjectStore`, Vue 3.5, TypeScript 5.8, Vitest 4.1.

## Global Constraints

- All new business entities use 36-character UUID strings.
- Store all times as UTC and normalize naive MySQL datetimes to UTC at the API boundary.
- Every mutable aggregate uses an integer `version`; every mutation request carries `expected_version`, and stale writes return HTTP 409 with code `stale_profile_version`.
- `ProfileFieldEvidence`, `ProfileFieldDecision`, `ConfirmedProfileVersion`, and parse history are append-only; no retry, re-upload, or confirmation operation updates or deletes an older evidence, decision, or version row.
- Derive `user_id` only from the authenticated principal; a cross-user asset, import, evidence, or version lookup returns the same HTTP 404 as a missing resource.
- Public response DTOs use explicit field allow-lists. They never expose `object_key`, plaintext SHA-256, complete resume text, another user's ID, or storage/provider exceptions.
- Logs contain entity IDs, stable error codes, and redacted counts only; they never contain an object key, full resume, token, connection URL, raw parser exception, or local-sensitive plaintext.
- Resume bytes are encrypted by the existing Backend AES-256-GCM adapter before MinIO/S3 upload; the object key remains authenticated AAD.
- Support `.pdf`, `.docx`, `.txt`, and `.md`, with a 10 MiB plaintext upload limit. Image-only or unreadable documents become `needs_manual_entry`; OCR is outside this plan.
- Parsed candidates never become confirmed facts automatically. A user must explicitly `confirm`, `correct`, or `ignore` every candidate included in a confirmation batch.
- Cloud storage accepts local-sensitive metadata only for categories `government_id`, `family_member`, and `emergency_contact`, with references matching `lsr:v1:<64 lowercase hex characters>`; sensitive plaintext never enters MySQL, object storage, logs, or model input.
- Migration ownership is fixed: this plan alone creates `20260717_0005_profile_resume_lifecycle.py`, with revision `20260717_0005` and `down_revision = "20260716_0004"`. Alembic must remain single-head.
- Shared integration files are `backend/app/db/models.py`, `backend/app/db/__init__.py`, `backend/app/api/router.py`, `frontend/src/App.vue`, `frontend/src/api.ts`, `alembic/env.py`, and `docs/runbooks/platform-foundation.md`; acquire the shared-file integration gate before editing them and keep edits to imports, mounts, migration metadata, and operations documentation.
- Do not change `POST /api/analysis/run`, `src/resume_parser.py`, LangGraph inputs, resume drafting, approved attachments, `ApplicationSnapshot`, or any Executor behavior in this plan.
- Use `DB_PASSWORD` for the local MySQL `root` account and `REDIS_PASSWORD` for Redis; never print either value or embed it in a command committed to the repository.

---

## File Structure

### New focused files

- `backend/app/domain/profiles.py` — stable status enums, field paths, local-sensitive reference validation, JSON value types, and domain exceptions.
- `backend/app/services/profile_parser.py` — deterministic PDF/DOCX/text extraction and evidence candidate generation; no database or object-store calls.
- `backend/app/repositories/profiles.py` — ownership-scoped SQL queries, row locks, append-only inserts, and snapshot reads.
- `backend/app/services/profiles.py` — upload checkpointing, object reconciliation, import processing, evidence decisions, diff generation, and confirmed-version orchestration.
- `backend/app/api/profile_schemas.py` — explicit request/response DTO allow-lists and UTC normalization.
- `backend/app/api/routes/profiles.py` — authenticated resume, profile, evidence, local-reference, and version endpoints.
- `alembic/versions/20260717_0005_profile_resume_lifecycle.py` — six profile/resume tables, constraints, indexes, and reversible downgrade.
- `frontend/src/features/profile/profileTypes.ts` — frontend mirror of the versioned profile DTO.
- `frontend/src/features/profile/profileApi.ts` — multipart upload, parsing, evidence decisions, controlled download, and version calls.
- `frontend/src/features/profile/ProfileWorkspace.vue` — upload, parse-state, evidence diff/review, local-sensitive presence, and version history UI.
- `frontend/src/features/profile/__tests__/ProfileWorkspace.spec.ts` — user workflow, conflict reload, privacy, and recoverable-state component tests.
- `frontend/src/features/profile/__tests__/profileApi.spec.ts` — multipart/header and URL-encoding contract tests.
- `tests/unit/test_profile_domain.py` — field-path and local-reference policy tests.
- `tests/unit/test_profile_parser.py` — PDF, DOCX, text, unreadable, and candidate extraction tests.
- `tests/unit/test_profile_repository.py` — ownership filters, append-only decisions, diffs, and snapshots on SQLite.
- `tests/unit/test_profile_service.py` — external side-effect checkpoint, reconciliation, retry, and confirmation service tests.
- `tests/contract/test_profiles_api.py` — authentication, 404 ownership hiding, stable errors, DTO allow-lists, and endpoint flow.
- `tests/integration/test_profile_lifecycle_mysql.py` — real MySQL locking, concurrent confirmation, and immutable-history gate.

### Existing files changed only at integration points

- `requirements.txt` — pin `python-docx==1.2.0`.
- `backend/app/services/storage.py` — add metadata-only encrypted-object inspection used for upload reconciliation.
- `backend/app/db/models.py` and `backend/app/db/__init__.py` — declare/export the new ORM models without embedding service logic.
- `backend/app/api/dependencies.py` — expose the already-created encrypted object store through one typed dependency.
- `backend/app/api/router.py` — include `profiles.router` once.
- `frontend/src/App.vue` — add a `profile` workspace tab and mount one feature component.
- `tests/integration/test_mysql_migration.py` — change the expected head to `20260717_0005` and verify upgrade/downgrade structure.
- `tests/integration/test_object_store.py` — prove raw MinIO bytes are ciphertext and reconciliation sees only valid encryption metadata.
- `tests/security/test_no_sensitive_logging.py` — add resume/object-key/local-sensitive sentinels to the log leak gate.
- `docs/runbooks/platform-foundation.md` — add profile upload, reconciliation, deletion limitation, backup, and release-gate procedures.

### Explicitly untouched files

- `backend/app/api/routes/analysis.py`, `src/resume_parser.py`, `src/graph.py`, and `data/jobs.json` remain unchanged; workstream E owns production matching integration.
- No generated resume attachment table or file is introduced; workstream E owns `ResumeDraft`, `ApprovedResumeVersion`, and `ApplicationSnapshot`.

---

### Task 0: Freeze the shared contract and acquire integration ownership

**Files:**
- Inspect: `docs/superpowers/specs/2026-07-16-post-foundation-parallel-workstreams-design.md`
- Inspect: `docs/platform-foundation-handover-summary.md`
- Inspect: `alembic/versions/20260716_0004_job_completion_review.py`
- Inspect: shared integration files listed in Global Constraints

**Interfaces:**
- Consumes: repository baseline at Alembic `20260716_0004` and the approved workstream-A contract.
- Produces: a recorded go/no-go check that reserves revision `20260717_0005`, the `/api/resume-*`, `/api/profiles`, and `/api/profile-versions` namespaces, and this plan's shared-file merge window.

- [ ] **Step 1: Verify the repository baseline without exposing secrets**

Run:

```powershell
git status --short
git log -1 --oneline
.\.venv\Scripts\python.exe -m alembic heads
```

Expected: the status contains no unreviewed changes in this plan's target files; the single Alembic head is `20260716_0004 (head)`. Existing unrelated user changes are preserved and reported to the integrator instead of reset.

- [ ] **Step 2: Prove the reserved names do not already exist**

Run:

```powershell
rg -n "20260717_0005|class Profile\b|class ResumeAsset\b|/resume-assets|/profile-versions" backend alembic tests frontend/src
```

Expected before implementation: no definitions for the reserved revision, entities, or endpoints. A hit introduced by another worktree is a merge-coordination stop, not permission to create a second contract.

- [ ] **Step 3: Confirm the exact shared contract in the implementation task record**

Record this exact block in the task/PR description; do not create a second design document:

```text
Owner: workstream A talent profile and resume lifecycle
Revision: 20260717_0005, revises 20260716_0004, one Alembic head
Aggregate: Profile.version / expected_version
Append-only: ProfileFieldEvidence, ProfileFieldDecision, ConfirmedProfileVersion
Object states: pending_upload, ready, upload_failed
Import states: pending, parsing, awaiting_confirmation, needs_manual_entry, failed
Decision actions: confirm, correct, ignore
Sensitive reference: lsr:v1:<64 lowercase hex>, category allow-list only
Shared-file merge order: models + migration, API router, App.vue, runbook
```

Expected: workstreams B and D acknowledge that their migrations revise `20260717_0005` and `20260717_0006` respectively; workstreams C and D do not edit this plan's feature files.

- [ ] **Step 4: Run the untouched baseline gates**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_encrypted_storage.py tests/contract/test_existing_api_contract.py -q
npm.cmd --prefix frontend run typecheck
```

Expected: all selected Python tests PASS and `vue-tsc` exits 0. If either fails before a plan change, capture the exact failure and resolve ownership before continuing.

No commit is created for Task 0 because it intentionally changes no repository file.

---

### Task 1: Define profile-domain rules and deterministic document extraction

**Files:**
- Create: `backend/app/domain/profiles.py`
- Create: `backend/app/services/profile_parser.py`
- Create: `tests/unit/test_profile_domain.py`
- Create: `tests/unit/test_profile_parser.py`
- Modify: `requirements.txt`

**Interfaces:**
- Consumes: raw filename and bytes only; no database, HTTP request, object key, or authenticated principal.
- Produces: `ParsedResumeDocument`, `EvidenceCandidate`, `extract_resume_document(filename: str, raw: bytes) -> ParsedResumeDocument`, `extract_evidence_candidates(text: str) -> list[EvidenceCandidate]`, stable enums, and `validate_local_sensitive_reference(category: str, reference: str) -> None`.

- [ ] **Step 1: Write failing domain-policy tests**

Create `tests/unit/test_profile_domain.py` with these contract tests:

```python
import pytest

from backend.app.domain.profiles import (
    LocalSensitiveReferenceError,
    validate_local_sensitive_reference,
)


@pytest.mark.parametrize(
    "category",
    ["government_id", "family_member", "emergency_contact"],
)
def test_local_sensitive_reference_accepts_only_metadata(category: str) -> None:
    validate_local_sensitive_reference(category, "lsr:v1:" + "a" * 64)


@pytest.mark.parametrize(
    ("category", "reference"),
    [
        ("phone", "lsr:v1:" + "a" * 64),
        ("government_id", "110101200001011234"),
        ("family_member", "lsr:v1:" + "A" * 64),
        ("emergency_contact", "lsr:v1:" + "a" * 63),
    ],
)
def test_local_sensitive_reference_rejects_unknown_or_plaintext(
    category: str, reference: str
) -> None:
    with pytest.raises(LocalSensitiveReferenceError):
        validate_local_sensitive_reference(category, reference)
```

- [ ] **Step 2: Run the policy tests and verify the red state**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_profile_domain.py -q
```

Expected: collection FAILS with `ModuleNotFoundError: No module named 'backend.app.domain.profiles'`.

- [ ] **Step 3: Add the stable domain contract**

Create `backend/app/domain/profiles.py` with the exact public contract below:

```python
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re
from typing import TypeAlias


JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class ResumeAssetStatus(StrEnum):
    PENDING_UPLOAD = "pending_upload"
    READY = "ready"
    UPLOAD_FAILED = "upload_failed"


class ResumeImportStatus(StrEnum):
    PENDING = "pending"
    PARSING = "parsing"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    NEEDS_MANUAL_ENTRY = "needs_manual_entry"
    FAILED = "failed"


class EvidenceDecisionAction(StrEnum):
    CONFIRM = "confirm"
    CORRECT = "correct"
    IGNORE = "ignore"


class EvidenceDiffAction(StrEnum):
    ADD = "add"
    REPLACE = "replace"
    UNCHANGED = "unchanged"
    CONFLICT = "conflict"


LOCAL_SENSITIVE_CATEGORIES = frozenset(
    {"government_id", "family_member", "emergency_contact"}
)
LOCAL_REFERENCE_PATTERN = re.compile(r"^lsr:v1:[0-9a-f]{64}$")
STANDARD_FIELD_PATHS = frozenset(
    {
        "basics.name",
        "basics.email",
        "basics.phone",
        "education",
        "experience",
        "projects",
        "skills",
        "awards",
        "certificates",
        "languages",
        "portfolio_links",
    }
)


class LocalSensitiveReferenceError(ValueError):
    error_code = "invalid_local_sensitive_reference"


class UnsupportedResumeTypeError(ValueError):
    error_code = "unsupported_resume_type"


@dataclass(frozen=True)
class ParsedResumeDocument:
    text: str
    needs_manual_entry: bool
    error_code: str | None


@dataclass(frozen=True)
class EvidenceCandidate:
    field_path: str
    candidate_value: JsonValue
    evidence_excerpt: str
    confidence: int


def validate_local_sensitive_reference(category: str, reference: str) -> None:
    if category not in LOCAL_SENSITIVE_CATEGORIES:
        raise LocalSensitiveReferenceError("unsupported category")
    if LOCAL_REFERENCE_PATTERN.fullmatch(reference) is None:
        raise LocalSensitiveReferenceError("invalid irreversible reference")
```

- [ ] **Step 4: Run the domain-policy tests and verify green**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_profile_domain.py -q
```

Expected: all parameterized cases PASS.

- [ ] **Step 5: Pin DOCX support and install it in the project environment**

Append exactly this dependency to `requirements.txt`, preserving one package per line:

```text
python-docx==1.2.0
```

Run:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Expected: `python-docx==1.2.0` is installed successfully; the command does not modify any global Python environment.

- [ ] **Step 6: Write failing extraction tests for every supported format and recovery class**

Create `tests/unit/test_profile_parser.py`. Generate DOCX bytes in memory and a one-page PDF with `pypdf`; assert the public behavior below:

```python
from io import BytesIO
from pathlib import Path

from docx import Document
from pypdf import PdfReader, PdfWriter

from backend.app.services.profile_parser import (
    extract_evidence_candidates,
    extract_resume_document,
)


def _docx_bytes(text: str) -> bytes:
    document = Document()
    document.add_paragraph(text)
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def test_text_and_docx_extract_reviewable_text() -> None:
    plain = extract_resume_document("resume.txt", "张三\n技能\nPython".encode())
    docx = extract_resume_document("resume.docx", _docx_bytes("张三\n技能\nPython"))
    assert plain.needs_manual_entry is False
    assert docx.needs_manual_entry is False
    assert "Python" in plain.text
    assert "Python" in docx.text


def test_text_pdf_extracts_reviewable_text_and_candidates() -> None:
    parsed = extract_resume_document(
        "sample_resume.pdf",
        Path("data/sample_resume.pdf").read_bytes(),
    )
    assert parsed.needs_manual_entry is False
    assert parsed.text
    assert extract_evidence_candidates(parsed.text)


def test_image_only_pdf_and_broken_docx_need_manual_entry() -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    buffer = BytesIO()
    writer.write(buffer)
    blank = extract_resume_document("scan.pdf", buffer.getvalue())
    broken = extract_resume_document("broken.docx", b"not-a-docx")
    assert (blank.needs_manual_entry, blank.error_code) == (
        True,
        "resume_text_unavailable",
    )
    assert (broken.needs_manual_entry, broken.error_code) == (
        True,
        "resume_parse_unreadable",
    )
    broken_pdf = extract_resume_document("broken.pdf", b"not-a-pdf")
    assert (broken_pdf.needs_manual_entry, broken_pdf.error_code) == (
        True,
        "resume_parse_unreadable",
    )


def test_candidate_extraction_covers_standard_profile_sections() -> None:
    candidates = extract_evidence_candidates(
        "张三\nzhang@example.com\n教育经历\n某大学 软件工程\n"
        "项目经历\n职业助手 LangGraph\n技能\nPython、FastAPI\n"
        "获奖\n一等奖\n证书\n英语六级\n作品链接\nhttps://example.com/me"
    )
    by_path = {item.field_path: item for item in candidates}
    assert by_path["basics.name"].candidate_value == "张三"
    assert by_path["basics.email"].candidate_value == "zhang@example.com"
    assert by_path["education"].candidate_value == ["某大学 软件工程"]
    assert by_path["projects"].candidate_value == ["职业助手 LangGraph"]
    assert by_path["skills"].candidate_value == ["Python", "FastAPI"]
    assert by_path["portfolio_links"].candidate_value == ["https://example.com/me"]
    assert all(0 <= item.confidence <= 100 for item in candidates)
```

- [ ] **Step 7: Run parser tests and verify the red state**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_profile_parser.py -q
```

Expected: collection FAILS because `backend.app.services.profile_parser` does not exist.

- [ ] **Step 8: Implement deterministic extraction and candidate generation**

Create `backend/app/services/profile_parser.py`. The implementation must use `BytesIO`, `Document`, and `PdfReader`, return `needs_manual_entry` for parser/container failures without exposing exception text, split the exact section aliases below, and emit only paths in `STANDARD_FIELD_PATHS`:

```python
SECTION_ALIASES = {
    "教育经历": "education",
    "教育背景": "education",
    "实习经历": "experience",
    "工作经历": "experience",
    "项目经历": "projects",
    "技能": "skills",
    "专业技能": "skills",
    "获奖": "awards",
    "荣誉奖项": "awards",
    "证书": "certificates",
    "语言成绩": "languages",
    "作品链接": "portfolio_links",
}


def extract_resume_document(filename: str, raw: bytes) -> ParsedResumeDocument:
    suffix = Path(filename).suffix.lower()
    if suffix not in {".txt", ".md", ".pdf", ".docx"}:
        raise UnsupportedResumeTypeError(suffix)
    try:
        if suffix in {".txt", ".md"}:
            text = raw.decode("utf-8")
        elif suffix == ".pdf":
            reader = PdfReader(BytesIO(raw))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        else:
            document = Document(BytesIO(raw))
            text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    except Exception:
        return ParsedResumeDocument("", True, "resume_parse_unreadable")
    normalized = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    if not normalized:
        return ParsedResumeDocument("", True, "resume_text_unavailable")
    return ParsedResumeDocument(normalized, False, None)
```

Add `extract_evidence_candidates` as a deterministic function returning `list[EvidenceCandidate]` that:

1. takes the first non-heading line of at most 120 characters as `basics.name` at confidence 65;
2. extracts the first RFC-like email and mainland/mobile-looking phone at confidence 90;
3. groups non-empty lines following `SECTION_ALIASES` until the next heading;
4. splits `skills` on `、`, comma, semicolon, or whitespace and de-duplicates in source order;
5. extracts `http/https` URLs into `portfolio_links` even without a heading;
6. truncates each `evidence_excerpt` to 500 characters; and
7. never recognizes an ID number, family-member field, or emergency-contact value.

- [ ] **Step 9: Run parser, domain, and Ruff gates**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_profile_domain.py tests/unit/test_profile_parser.py -q
.\.venv\Scripts\python.exe -m ruff check backend/app/domain/profiles.py backend/app/services/profile_parser.py tests/unit/test_profile_domain.py tests/unit/test_profile_parser.py
```

Expected: all tests PASS and Ruff prints `All checks passed!`.

- [ ] **Step 10: Commit Task 1**

```powershell
git add requirements.txt backend/app/domain/profiles.py backend/app/services/profile_parser.py tests/unit/test_profile_domain.py tests/unit/test_profile_parser.py
git commit -m "feat: add deterministic resume evidence parser"
```

---

### Task 2: Add the authoritative schema and single-head `0005` migration

**Files:**
- Modify: `backend/app/db/models.py`
- Modify: `backend/app/db/__init__.py`
- Create: `alembic/versions/20260717_0005_profile_resume_lifecycle.py`
- Create: `tests/unit/test_profile_repository.py`
- Modify: `tests/integration/test_mysql_migration.py`

**Interfaces:**
- Consumes: Task 1 enums and 36-character UUID/UTC conventions from `backend.app.db.base`.
- Produces: ORM classes `Profile`, `ResumeAsset`, `ResumeImport`, `ProfileFieldEvidence`, `ProfileFieldDecision`, and `ConfirmedProfileVersion`; database constraints used by the repository in Task 3; Alembic head `20260717_0005`.

- [ ] **Step 1: Acquire the shared migration/model integration gate**

Run:

```powershell
.\.venv\Scripts\python.exe -m alembic heads
git diff -- backend/app/db/models.py backend/app/db/__init__.py alembic/versions tests/integration/test_mysql_migration.py
```

Expected: one head at `20260716_0004`; no uncoordinated workstream edits in these files. Announce that the A integration window is open before editing.

- [ ] **Step 2: Write failing ORM invariants**

Start `tests/unit/test_profile_repository.py` with schema-level tests using the existing in-memory SQLite `Base.metadata.create_all` pattern:

```python
from sqlalchemy import create_engine, inspect

from backend.app.db.base import Base


def test_profile_schema_has_version_and_append_only_tables() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    inspector = inspect(engine)
    assert {
        "profiles",
        "resume_assets",
        "resume_imports",
        "profile_field_evidence",
        "profile_field_decisions",
        "confirmed_profile_versions",
    } <= set(inspector.get_table_names())
    assert {"version", "local_sensitive_references"} <= {
        column["name"] for column in inspector.get_columns("profiles")
    }
    engine.dispose()
```

- [ ] **Step 3: Run the schema test and verify red**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_profile_repository.py::test_profile_schema_has_version_and_append_only_tables -q
```

Expected: FAIL because the six tables are absent.

- [ ] **Step 4: Declare ORM models with exact columns and constraints**

Add imports for Task 1 enums to `backend/app/db/models.py`, then declare these tables. Use `enum_kwargs` and existing mixins; do not add relationships whose only purpose is eager serialization.

```python
class Profile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "profiles"
    __table_args__ = (UniqueConstraint("user_id", name="uq_profiles_user_id"),)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    local_sensitive_references: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )


class ResumeAsset(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "resume_assets"
    __table_args__ = (
        UniqueConstraint("object_key", name="uq_resume_assets_object_key"),
        Index("ix_resume_assets_profile_created", "profile_id", "created_at"),
    )
    profile_id: Mapped[str] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(120), nullable=False)
    plaintext_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    plaintext_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    encryption_version: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[ResumeAssetStatus] = mapped_column(
        Enum(ResumeAssetStatus, name="resume_asset_status", **enum_kwargs),
        nullable=False,
    )
    error_code: Mapped[str | None] = mapped_column(String(80))


class ResumeImport(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "resume_imports"
    __table_args__ = (
        Index("ix_resume_imports_profile_created", "profile_id", "created_at"),
    )
    profile_id: Mapped[str] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )
    asset_id: Mapped[str] = mapped_column(
        ForeignKey("resume_assets.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    parser_version: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[ResumeImportStatus] = mapped_column(
        Enum(ResumeImportStatus, name="resume_import_status", **enum_kwargs),
        nullable=False,
    )
    error_code: Mapped[str | None] = mapped_column(String(80))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
```

Add `ProfileFieldEvidence` with `profile_id`, `resume_import_id`, `field_path` (`String(255)`), `candidate_value` (`JSON`), `evidence_excerpt` (`Text`), `confidence` (`Integer`), `sequence` (`Integer`), and `created_at`; index `(profile_id, field_path, created_at)` and unique `(resume_import_id, sequence)`. Add `ProfileFieldDecision` with `profile_id`, `evidence_id`, `actor_user_id`, `action`, nullable `resolved_value`, and `created_at`; index `(evidence_id, created_at)`. Add `ConfirmedProfileVersion` with `profile_id`, `version_number`, `aggregate_version`, `facts_snapshot`, `evidence_refs`, `local_sensitive_references`, and `created_at`; unique `(profile_id, version_number)` and index `(profile_id, created_at)`.

Export all six models and four profile enums from `backend/app/db/__init__.py`.

- [ ] **Step 5: Create the exact `0005` migration**

Create `alembic/versions/20260717_0005_profile_resume_lifecycle.py` with:

```python
"""add talent profile and resume lifecycle

Revision ID: 20260717_0005
Revises: 20260716_0004
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260717_0005"
down_revision: Union[str, Sequence[str], None] = "20260716_0004"
branch_labels = None
depends_on = None
```

`upgrade()` must create tables in this order: `profiles`, `resume_assets`, `resume_imports`, `profile_field_evidence`, `profile_field_decisions`, `confirmed_profile_versions`. Match the model column lengths and constraint names exactly. Use named check constraints for statuses/actions and confidence:

```python
sa.CheckConstraint(
    "status IN ('pending_upload','ready','upload_failed')",
    name="resume_asset_status",
)
sa.CheckConstraint(
    "status IN ('pending','parsing','awaiting_confirmation','needs_manual_entry','failed')",
    name="resume_import_status",
)
sa.CheckConstraint(
    "action IN ('confirm','correct','ignore')",
    name="profile_evidence_decision_action",
)
sa.CheckConstraint(
    "confidence >= 0 AND confidence <= 100",
    name="ck_profile_field_evidence_confidence",
)
```

Use `sa.JSON()` without a database server default; application constructors always provide `{}` or a concrete snapshot. `downgrade()` drops the same six tables in reverse order so no profile table survives a downgrade to `0004`.

- [ ] **Step 6: Update migration assertions before running MySQL**

In `tests/integration/test_mysql_migration.py`, set:

```python
HEAD_REVISION = "20260717_0005"
PROFILE_TABLES = {
    "profiles",
    "resume_assets",
    "resume_imports",
    "profile_field_evidence",
    "profile_field_decisions",
    "confirmed_profile_versions",
}
BUSINESS_TABLES |= PROFILE_TABLES
```

Extend the existing destructive migration test to upgrade `0004 → 0005`, inspect all six tables/constraints/indexes, insert one complete profile lineage, downgrade to `0004`, assert `PROFILE_TABLES` is disjoint from the remaining tables, and finally execute the existing downgrade-to-base cleanup. Keep the `_test` database and `ALLOW_DESTRUCTIVE_MYSQL_TESTS=1` guards unchanged.

- [ ] **Step 7: Run model and offline migration gates**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_profile_repository.py::test_profile_schema_has_version_and_append_only_tables tests/unit/test_job_models.py -q
.\.venv\Scripts\python.exe -m alembic heads
.\.venv\Scripts\python.exe -m alembic upgrade head --sql *> $null
```

Expected: tests PASS, one head prints `20260717_0005 (head)`, and offline SQL generation exits 0.

- [ ] **Step 8: Run the guarded real-MySQL migration test**

After loading `DB_PASSWORD` from the User-scope environment without printing it, construct `TEST_MYSQL_URL` with the repository's URL-encoding command and set `ALLOW_DESTRUCTIVE_MYSQL_TESTS=1`. Then run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_mysql_migration.py::test_mysql_migration_upgrade_and_downgrade -q -rs
```

Expected: PASS against a database whose name ends in `_test`; otherwise an exact guard skip. Any non-test database rejection is a successful fail-closed result, never a reason to weaken the guard.

- [ ] **Step 9: Run Ruff and release the shared gate**

Run:

```powershell
.\.venv\Scripts\python.exe -m ruff check backend/app/db tests/unit/test_profile_repository.py tests/integration/test_mysql_migration.py alembic/versions/20260717_0005_profile_resume_lifecycle.py
```

Expected: `All checks passed!`. Announce that workstream B may now rebase and create revision `20260717_0006` with `down_revision = "20260717_0005"`.

- [ ] **Step 10: Commit Task 2**

```powershell
git add backend/app/db/models.py backend/app/db/__init__.py alembic/versions/20260717_0005_profile_resume_lifecycle.py tests/unit/test_profile_repository.py tests/integration/test_mysql_migration.py
git commit -m "feat: add authoritative profile resume schema"
```

---

### Task 3: Implement ownership-scoped persistence, encrypted upload checkpoints, and immutable confirmation

**Files:**
- Modify: `backend/app/services/storage.py`
- Create: `backend/app/repositories/profiles.py`
- Create: `backend/app/services/profiles.py`
- Modify: `tests/unit/test_profile_repository.py`
- Create: `tests/unit/test_profile_service.py`
- Modify: `tests/unit/test_encrypted_storage.py`

**Interfaces:**
- Consumes: Task 1 parser/domain types, Task 2 ORM schema, `EncryptedObjectStore.put/get`, and an authenticated `user_id` supplied by the future route.
- Produces: repository functions `ensure_profile`, `get_owned_asset`, `list_owned_assets`, `get_owned_import`, `get_owned_version`, `get_profile_for_update`, and append-only insert helpers; services `ResumeAssetService`, `ResumeImportService`, and `ProfileService` used unchanged by Task 4.

- [ ] **Step 1: Write failing encrypted-object inspection tests**

Extend `tests/unit/test_encrypted_storage.py`:

```python
def test_inspect_accepts_only_expected_encryption_metadata(
    memory_blob_store: MemoryBlobStore, encryption_key: str
) -> None:
    store = EncryptedObjectStore(memory_blob_store, encryption_key)
    store.put(key="users/u1/a", plaintext=b"secret", content_type="text/plain")
    assert store.inspect(key="users/u1/a").encryption == "v1-aes-256-gcm"
    memory_blob_store.objects["users/u1/a"].metadata = {"encryption": "plaintext"}
    with pytest.raises(ValueError, match="encrypted object metadata"):
        store.inspect(key="users/u1/a")
```

- [ ] **Step 2: Run the inspection test and verify red**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_encrypted_storage.py::test_inspect_accepts_only_expected_encryption_metadata -q
```

Expected: FAIL with `AttributeError: 'EncryptedObjectStore' object has no attribute 'inspect'`.

- [ ] **Step 3: Add metadata-only encrypted-object inspection**

In `backend/app/services/storage.py`, add a safe DTO without a key and this method:

```python
@dataclass(frozen=True)
class StoredObjectMetadata:
    content_type: str
    encryption: str


def inspect(self, *, key: str) -> StoredObjectMetadata:
    head = self._blob_store.head(key=key)
    metadata = head.get("Metadata", {})
    encryption = str(metadata.get("encryption", ""))
    if encryption != ENCRYPTION_VERSION:
        raise ValueError("invalid encrypted object metadata")
    return StoredObjectMetadata(
        content_type=str(head.get("ContentType", "application/octet-stream")),
        encryption=encryption,
    )
```

Place `inspect` on `EncryptedObjectStore`, not `S3BlobStore`; callers must never obtain `_blob_store`, bucket name, or raw metadata.

- [ ] **Step 4: Write failing repository ownership and append-only tests**

Extend `tests/unit/test_profile_repository.py` with two users and assert:

```python
def test_owned_queries_hide_cross_user_rows(profile_db: Session, seeded_profiles) -> None:
    owner, other, asset = seeded_profiles
    assert profiles.get_owned_asset(profile_db, owner.id, asset.id) is asset
    assert profiles.get_owned_asset(profile_db, other.id, asset.id) is None


def test_new_import_and_decisions_do_not_mutate_history(
    profile_db: Session, seeded_profiles
) -> None:
    owner, _other, asset = seeded_profiles
    first = profiles.create_import(profile_db, asset=asset, parser_version="profile-v1")
    profiles.append_evidence(
        profile_db,
        profile_id=asset.profile_id,
        import_id=first.id,
        candidates=(
            EvidenceCandidate("skills", ["Python"], "Python", 90),
        ),
    )
    second = profiles.create_import(profile_db, asset=asset, parser_version="profile-v1")
    profile_db.flush()
    assert first.id != second.id
    assert first.status is ResumeImportStatus.PENDING
    assert profile_db.scalar(select(func.count(ProfileFieldEvidence.id))) == 1
```

- [ ] **Step 5: Run repository tests and verify red**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_profile_repository.py -q
```

Expected: FAIL because `backend.app.repositories.profiles` and its functions do not exist.

- [ ] **Step 6: Implement the ownership-scoped repository**

Create `backend/app/repositories/profiles.py`. Every owned query joins `Profile` and filters both resource ID and `Profile.user_id`. The lock entry point must be:

```python
def get_profile_for_update(db: Session, user_id: str) -> Profile:
    profile = db.scalar(
        select(Profile)
        .where(Profile.user_id == user_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if profile is None:
        profile = Profile(user_id=user_id, version=0, local_sensitive_references={})
        db.add(profile)
        db.flush()
    return profile
```

Implement `ensure_profile` without a lock for read/create paths, plus the ownership getters declared in Interfaces. Implement append helpers that always construct new rows. `append_evidence` enumerates candidates into stable `sequence` values. `latest_decisions_by_evidence` selects the latest decision by `(created_at, id)`. `next_confirmed_version_number` executes `max(version_number) + 1` while the profile row is locked. Do not add update/delete helpers for evidence, decisions, or confirmed versions.

- [ ] **Step 7: Write failing service tests for upload recovery and immutable versions**

Create `tests/unit/test_profile_service.py` with an in-memory database and `MemoryBlobStore` fixture. Cover these exact outcomes:

```python
def test_upload_persists_pending_before_object_write_and_reconciles_after_commit_error(
    profile_db: Session, object_store: EncryptedObjectStore
) -> None:
    service = ResumeAssetService(object_store)
    asset = service.create_pending_asset(
        profile_db,
        user_id="user-1",
        filename="resume.txt",
        content_type="text/plain",
        raw=b"private resume",
    )
    profile_db.commit()
    service.write_encrypted_object(asset, b"private resume")
    reconciled = service.reconcile(profile_db, user_id="user-1", asset_id=asset.id)
    assert reconciled.status is ResumeAssetStatus.READY
    assert reconciled.error_code is None


def test_reparse_appends_import_and_preserves_confirmed_version(
    profile_db: Session,
    ready_asset_context: tuple[str, ResumeAsset],
    object_store: EncryptedObjectStore,
) -> None:
    owner_id, ready_asset = ready_asset_context
    import_service = ResumeImportService(object_store)
    profile_service = ProfileService()
    first = import_service.start(profile_db, user_id=owner_id, asset_id=ready_asset.id)
    import_service.process(profile_db, user_id=owner_id, import_id=first.id)
    profile = profile_repository.ensure_profile(profile_db, owner_id)
    evidence = profile_repository.list_import_evidence(profile_db, first.id)
    decided = profile_service.apply_decisions(
        profile_db,
        user_id=owner_id,
        expected_version=profile.version,
        decisions=tuple(
            EvidenceDecisionInput(item.id, EvidenceDecisionAction.CONFIRM)
            for item in evidence
        ),
    )
    confirmed = profile_service.create_confirmed_version(
        profile_db,
        user_id=owner_id,
        expected_version=decided.version,
        resume_import_id=first.id,
    )
    second = import_service.start(profile_db, user_id=owner_id, asset_id=ready_asset.id)
    import_service.process(profile_db, user_id=owner_id, import_id=second.id)
    profile_db.flush()
    assert second.id != first.id
    assert confirmed.facts_snapshot == {"skills": ["Python"]}
```

The fixture returns the owner ID beside `ready_asset`; do not add a redundant `user_id` column to `ResumeAsset` merely for test convenience.

- [ ] **Step 8: Implement lifecycle services with explicit transaction checkpoints**

Create `backend/app/services/profiles.py` with:

```python
MAX_RESUME_BYTES = 10 * 1024 * 1024
PARSER_VERSION = "profile-parser-v1"
ALLOWED_SUFFIX_CONTENT_TYPES = {
    ".txt": {"text/plain"},
    ".md": {"text/markdown", "text/plain"},
    ".pdf": {"application/pdf"},
    ".docx": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    },
}


@dataclass(frozen=True)
class EvidenceDecisionInput:
    evidence_id: str
    action: EvidenceDecisionAction
    corrected_value: JsonValue | None = None


class StaleProfileVersionError(RuntimeError):
    error_code = "stale_profile_version"


class OwnedProfileResourceNotFound(LookupError):
    error_code = "profile_resource_not_found"


class ProfileValidationError(ValueError):
    error_code = "invalid_profile_operation"


class ProfileDependencyUnavailable(RuntimeError):
    error_code = "profile_dependency_unavailable"


class ResumeTooLargeError(ProfileValidationError):
    error_code = "resume_too_large"


class ObjectStoreUnavailableError(ProfileDependencyUnavailable):
    error_code = "object_store_unavailable"


class ResumeAssetStateConflict(RuntimeError):
    error_code = "resume_asset_state_conflict"
```

`ResumeAssetService.create_pending_asset` validates suffix/MIME/size, ensures a profile, creates a UUID asset ID before deriving `users/{user_id}/resume-assets/{asset_id}`, stores only the key in MySQL, and returns a `pending_upload` row. The route commits that row before calling `write_encrypted_object`. `write_encrypted_object` calls `EncryptedObjectStore.put`; it never logs bytes or the key. `mark_ready` and `mark_upload_failed` are separate DB operations. `reconcile` calls `EncryptedObjectStore.inspect` and marks the persisted pending row ready only when content type and encryption version match.

`ResumeImportService.start` requires an owned `ready` asset and always appends a `pending` import. `process` sets `parsing`, decrypts the asset, calls Task 1 extraction, and either appends evidence then sets `awaiting_confirmation`, or sets `needs_manual_entry` with `resume_text_unavailable`/`resume_parse_unreadable`. A storage read failure sets `failed` with `resume_asset_read_failed`. No failure deletes the asset or changes an older import/version.

`ProfileService.apply_decisions` locks the profile, compares `expected_version`, verifies ownership of every evidence ID, requires `corrected_value` only for `correct`, appends one decision per input, increments `Profile.version` once, and returns the profile. `ProfileService.create_confirmed_version` locks and version-checks again; it requires a latest decision for every evidence in the selected `resume_import_id`, folds `confirm`/`correct` into a field-path snapshot, excludes `ignore`, copies local-sensitive metadata, appends one `ConfirmedProfileVersion`, increments `Profile.version`, and never edits an older row.

`ProfileService.update_local_sensitive_reference` validates the category/reference with Task 1, locks/version-checks, replaces only that category's metadata object with `{"reference": reference, "updated_at": now.isoformat()}`, and increments the profile version. Reject any extra request field through Pydantic in Task 4.

- [ ] **Step 9: Add diff behavior to repository/service tests**

Add tests proving the second import returns `unchanged` for equal facts, `replace` for a changed confirmed path, `add` for a new path, and `conflict` when the same import contains multiple unequal candidates for one path. Assert the latest confirmed snapshot remains byte-for-byte equal before the user creates another version.

- [ ] **Step 10: Run focused backend tests and Ruff**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_encrypted_storage.py tests/unit/test_profile_repository.py tests/unit/test_profile_service.py tests/unit/test_profile_parser.py -q
.\.venv\Scripts\python.exe -m ruff check backend/app/repositories/profiles.py backend/app/services/profiles.py backend/app/services/storage.py tests/unit/test_profile_repository.py tests/unit/test_profile_service.py
```

Expected: all selected tests PASS and Ruff prints `All checks passed!`.

- [ ] **Step 11: Commit Task 3**

```powershell
git add backend/app/services/storage.py backend/app/repositories/profiles.py backend/app/services/profiles.py tests/unit/test_profile_repository.py tests/unit/test_profile_service.py tests/unit/test_encrypted_storage.py
git commit -m "feat: add encrypted profile lifecycle services"
```

---

### Task 4: Expose authenticated profile, asset, evidence, and version APIs

**Files:**
- Create: `backend/app/api/profile_schemas.py`
- Create: `backend/app/api/routes/profiles.py`
- Modify: `backend/app/api/dependencies.py`
- Modify: `backend/app/api/router.py`
- Create: `tests/contract/test_profiles_api.py`
- Modify: `tests/contract/test_existing_api_contract.py`

**Interfaces:**
- Consumes: Task 3 services and ownership rules; `request.app.state.object_store` created by the existing lifespan.
- Produces: versioned JSON/multipart endpoints under `/api/resume-assets`, `/api/resume-imports`, `/api/profiles`, and `/api/profile-versions`; explicit DTOs consumed by Task 5.

- [ ] **Step 1: Write the failing contract fixture and upload/list privacy test**

Create `tests/contract/test_profiles_api.py` using `StaticPool`, `Base.metadata.create_all`, `AuthService`, `MemoryBlobStore`, and `create_app(blob_store=memory_blob_store, session_factory=session_factory)`. The first contract must assert:

```python
ASSET_FIELDS = {
    "id",
    "original_filename",
    "content_type",
    "plaintext_size",
    "encryption_version",
    "status",
    "error_code",
    "created_at",
    "updated_at",
}


def test_upload_lists_only_safe_metadata(client, student_headers) -> None:
    response = client.post(
        "/api/resume-assets",
        headers=student_headers,
        files={"file": ("resume.txt", b"Zhang San\nSkills\nPython", "text/plain")},
    )
    assert response.status_code == 201
    assert set(response.json()) == ASSET_FIELDS
    serialized = response.text.lower()
    assert "object_key" not in serialized
    assert "plaintext_sha256" not in serialized
    assert "zhang san" not in serialized
    listed = client.get("/api/resume-assets", headers=student_headers)
    assert listed.status_code == 200
    assert set(listed.json()["assets"][0]) == ASSET_FIELDS
```

- [ ] **Step 2: Write failing lifecycle, ownership, and stable-error contract tests**

Add tests that:

- upload → `ready` → `POST /api/resume-imports` → `awaiting_confirmation`;
- read `GET /api/profiles` and receive evidence with `diff_action` but no full resume;
- apply decisions with `PATCH /api/profiles/evidence` and matching `expected_version`;
- create a version with `POST /api/profile-versions` and read it unchanged;
- download through `GET /api/resume-assets/{asset_id}/download` and receive the original bytes without a key;
- receive 404 for another user's asset/import/evidence/version ID;
- receive 409 `{"code": "stale_profile_version"}` for a stale decision;
- receive 422 `unsupported_resume_type` and `resume_too_large` without an object write;
- receive 503 `object_store_unavailable` without provider exception text;
- accept only `lsr:v1:<64 lowercase hex>` in `PATCH /api/profiles/local-sensitive-references` and never echo a plaintext field.

- [ ] **Step 3: Run contract tests and verify red**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/contract/test_profiles_api.py -q
```

Expected: collection FAILS because the route/schema modules and endpoints do not exist.

- [ ] **Step 4: Define strict request and response DTOs**

Create `backend/app/api/profile_schemas.py`. Set `model_config = ConfigDict(extra="forbid")` on mutation requests. Define these exact request shapes:

```python
class CreateResumeImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_id: str = Field(min_length=36, max_length=36)


class EvidenceDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence_id: str = Field(min_length=36, max_length=36)
    action: EvidenceDecisionAction
    corrected_value: JsonValue | None = None


class ApplyEvidenceDecisionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0)
    decisions: list[EvidenceDecisionRequest] = Field(min_length=1, max_length=200)


class CreateProfileVersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0)
    resume_import_id: str = Field(min_length=36, max_length=36)


class LocalSensitiveReferenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0)
    category: Literal["government_id", "family_member", "emergency_contact"]
    reference: str = Field(pattern=r"^lsr:v1:[0-9a-f]{64}$")
```

Define response DTOs that include only the `ASSET_FIELDS` contract, import metadata, evidence ID/path/candidate/excerpt/confidence/derived status/diff action, `Profile.version`, safe local-sensitive category/reference/update time, and confirmed version ID/version number/facts/evidence refs/timestamps. Normalize every datetime field through the same UTC validator pattern used in `job_schemas.py`.

- [ ] **Step 5: Add a typed object-store dependency and route error mapping**

Add to `backend/app/api/dependencies.py`:

```python
def get_object_store(request: Request) -> EncryptedObjectStore:
    return cast(EncryptedObjectStore, request.app.state.object_store)
```

Create `backend/app/api/routes/profiles.py` with `router = APIRouter(tags=["profiles"])` and a single error mapper:

```python
def profile_http_error(error: Exception) -> HTTPException:
    if isinstance(error, OwnedProfileResourceNotFound):
        return HTTPException(404, detail={"code": error.error_code, "message": "资源不存在。"})
    if isinstance(error, StaleProfileVersionError):
        return HTTPException(409, detail={"code": error.error_code, "message": "档案版本已变化，请重新加载。"})
    if isinstance(error, ResumeAssetStateConflict):
        return HTTPException(409, detail={"code": error.error_code, "message": "当前资产状态不允许该操作。"})
    if isinstance(error, (ProfileValidationError, LocalSensitiveReferenceError, UnsupportedResumeTypeError)):
        return HTTPException(422, detail={"code": error.error_code, "message": "档案操作不合法。"})
    if isinstance(error, ProfileDependencyUnavailable):
        return HTTPException(503, detail={"code": error.error_code, "message": "档案依赖暂不可用。"})
    raise error
```

Map upload size to `resume_too_large`, object write/read/inspect to `object_store_unavailable`, unreadable files to a successful import response with `needs_manual_entry`, and stale mutation to 409. Never include `str(error)` in an HTTP response or log.

- [ ] **Step 6: Implement endpoints with explicit checkpoint commits**

Implement these operations:

```text
POST   /resume-assets                                      201 safe asset metadata
GET    /resume-assets                                      200 user's assets newest first
GET    /resume-assets/{asset_id}                           200 safe metadata or 404
GET    /resume-assets/{asset_id}/download                  200 decrypted owned bytes
POST   /resume-assets/{asset_id}/reconcile                 200 ready metadata or 409
POST   /resume-imports                                     201 import and evidence metadata
GET    /resume-imports/{import_id}                         200 owned import/evidence or 404
GET    /profiles                                           200 aggregate, evidence diffs, refs, latest version
PATCH  /profiles/evidence                                  200 updated aggregate version
PATCH  /profiles/local-sensitive-references                200 metadata-only aggregate
POST   /profile-versions                                   201 immutable version
GET    /profile-versions                                   200 newest-first version summaries
GET    /profile-versions/{version_id}                      200 immutable owned version or 404
```

For upload: read at most `MAX_RESUME_BYTES + 1`; create and commit `pending_upload`; write ciphertext; mark ready and commit. If object writing fails, open/continue the DB session, set `upload_failed` plus the stable code, commit, and return 503. If the final ready commit fails after the object exists, roll back and return 503; a later reconcile checks encrypted metadata and completes the state.

For import: append and commit `pending`; process; commit its terminal state. A retry is another `POST /resume-imports`, never a reset of the old row. For every mutating endpoint, build the response before commit only when fields are already loaded, commit exactly once for the authoritative mutation, and roll back on domain/SQL errors.

Use `StreamingResponse(iter([plaintext]))` with the stored content type and a sanitized RFC 5987 `Content-Disposition`; never redirect to S3/MinIO or issue a presigned object URL.

- [ ] **Step 7: Mount the route during the shared API integration window**

In `backend/app/api/router.py`, change only imports and the one mount:

```python
from backend.app.api.routes import analysis, auth, devices, health, jobs, profiles, sessions

api_router.include_router(profiles.router)
```

Place the mount before the dynamic `/jobs/{job_id}` route only if route ordering tests show a collision; profile paths do not share the jobs prefix.

- [ ] **Step 8: Freeze the OpenAPI and existing API contract**

Extend `tests/contract/test_existing_api_contract.py` to assert the new operations occur exactly once, `/api/analysis/run` remains present during Wave 1, and no response schema contains `object_key` or `plaintext_sha256`.

- [ ] **Step 9: Run API, regression, and Ruff gates**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/contract/test_profiles_api.py tests/contract/test_existing_api_contract.py tests/contract/test_auth_api.py tests/unit/test_profile_service.py -q
.\.venv\Scripts\python.exe -m ruff check backend/app/api tests/contract/test_profiles_api.py
```

Expected: all selected tests PASS; Ruff prints `All checks passed!`; the profile OpenAPI schemas contain no internal object fields.

- [ ] **Step 10: Commit Task 4**

```powershell
git add backend/app/api/profile_schemas.py backend/app/api/routes/profiles.py backend/app/api/dependencies.py backend/app/api/router.py tests/contract/test_profiles_api.py tests/contract/test_existing_api_contract.py
git commit -m "feat: expose secure profile lifecycle api"
```

---

### Task 5: Build the profile upload, evidence review, and version-history workspace

**Files:**
- Create: `frontend/src/features/profile/profileTypes.ts`
- Create: `frontend/src/features/profile/profileApi.ts`
- Create: `frontend/src/features/profile/ProfileWorkspace.vue`
- Create: `frontend/src/features/profile/__tests__/profileApi.spec.ts`
- Create: `frontend/src/features/profile/__tests__/ProfileWorkspace.spec.ts`
- Modify: `frontend/src/App.vue`

**Interfaces:**
- Consumes: Task 4 DTOs/endpoints and the existing `request<T>` helper.
- Produces: a student-owned profile view with upload, parse retry, candidate decision, conflict reload, manual-entry guidance, safe local-sensitive presence, and immutable history; emits `dirty-change: boolean` to the shared shell.

- [ ] **Step 1: Define frontend DTOs exactly once**

Create `profileTypes.ts` mirroring Task 4 names and snake_case fields. The mutation union must prevent an invalid correction client-side:

```typescript
export type EvidenceDecisionPayload =
  | { evidence_id: string; action: "confirm"; corrected_value: null }
  | { evidence_id: string; action: "ignore"; corrected_value: null }
  | { evidence_id: string; action: "correct"; corrected_value: unknown };

export interface ProfileEvidence {
  id: string;
  resume_import_id: string;
  field_path: string;
  candidate_value: unknown;
  evidence_excerpt: string;
  confidence: number;
  status: "pending" | "confirmed" | "corrected" | "ignored";
  diff_action: "add" | "replace" | "unchanged" | "conflict";
}
```

Define `ResumeAssetMetadata`, `ResumeImportDetail`, `ProfileDetail`, `ConfirmedProfileVersionSummary`, and `ConfirmedProfileVersionDetail` with only Task 4 response fields; there is no `object_key`, SHA-256, user ID, or complete resume text property.

- [ ] **Step 2: Write failing API helper tests**

Create `profileApi.spec.ts` and assert upload uses `FormData` without a manually supplied `Content-Type`, resource IDs are encoded, and stale-write bodies carry `expected_version`:

```typescript
it("uploads a resume as multipart without exposing storage fields", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(JSON.stringify(asset), {
      status: 201,
      headers: { "Content-Type": "application/json" },
    }),
  );
  await uploadResumeAsset("token", new File(["resume"], "resume.txt", { type: "text/plain" }));
  const [, init] = vi.mocked(fetch).mock.calls[0];
  expect(init?.body).toBeInstanceOf(FormData);
  expect(new Headers(init?.headers).has("Content-Type")).toBe(false);
});
```

- [ ] **Step 3: Implement focused profile API calls**

Create `profileApi.ts` with exports:

```typescript
export function uploadResumeAsset(token: string, file: File): Promise<ResumeAssetMetadata> {
  const body = new FormData();
  body.append("file", file);
  return request<ResumeAssetMetadata>("/resume-assets", { method: "POST", body }, token);
}

export function startResumeImport(token: string, assetId: string): Promise<ResumeImportDetail> {
  return request<ResumeImportDetail>("/resume-imports", {
    method: "POST",
    body: JSON.stringify({ asset_id: assetId }),
  }, token);
}
```

Also export `fetchProfile`, `fetchResumeAssets`, `reconcileResumeAsset`, `applyEvidenceDecisions`, `updateLocalSensitiveReference`, `createProfileVersion`, `fetchProfileVersions`, and a `downloadResumeAsset` function that performs authenticated `fetch`, creates a temporary blob URL, clicks an `<a download>`, and revokes the URL in `finally`. It must never construct an object-store URL.

- [ ] **Step 4: Run API helper tests**

Run:

```powershell
npm.cmd --prefix frontend run test -- profileApi.spec.ts
```

Expected: all profile API helper tests PASS.

- [ ] **Step 5: Write failing component tests for the critical user workflow**

Create `ProfileWorkspace.spec.ts` with mocked profile API functions. Cover:

```typescript
it("requires a decision for every candidate before creating a version", async () => {
  const wrapper = mount(ProfileWorkspace, { props: { token: "token" } });
  await flushPromises();
  expect(wrapper.get('[data-test="create-version"]').attributes("disabled")).toBeDefined();
  await wrapper.get('[data-test="decision-confirm-evidence-1"]').trigger("click");
  expect(wrapper.get('[data-test="create-version"]').attributes("disabled")).toBeUndefined();
});


it("reloads on stale profile version instead of overwriting", async () => {
  vi.mocked(applyEvidenceDecisions).mockRejectedValue(
    new ApiError(409, { code: "stale_profile_version" }, "stale_profile_version"),
  );
  const wrapper = mount(ProfileWorkspace, { props: { token: "token" } });
  await flushPromises();
  await wrapper.get('[data-test="save-decisions"]').trigger("click");
  await flushPromises();
  expect(fetchProfile).toHaveBeenCalledTimes(2);
  expect(wrapper.text()).toContain("档案已被其他操作更新，请重新检查差异。")
});
```

Add cases for `.docx` accepted by the file input, `needs_manual_entry` instructions, `upload_failed` reconcile action, diff badges, evidence excerpt display, immutable version history, and safe sensitive-category presence. Assert rendered text never contains `object_key`, `plaintext_sha256`, or the test's sensitive plaintext sentinel.

- [ ] **Step 6: Implement the profile workspace**

Create `ProfileWorkspace.vue` using `ref`, `computed`, and explicit request busy flags. Its template must include:

- a file input accepting `.txt,.md,.pdf,.docx` and displaying the 10 MiB limit;
- asset rows with safe metadata, parse/reconcile/download actions, and recoverable error text;
- an evidence table with path, excerpt, confidence, diff badge, current decision, and correction JSON/text input;
- a batch “保存校对” action using the currently loaded `profile.version`;
- a “创建确认版本” action enabled only when every candidate of the selected import has `confirm`, `correct`, or `ignore`;
- a manual-entry panel for `needs_manual_entry` that does not claim OCR occurred;
- local-sensitive category/status/update time only, with copy stating that plaintext is edited on the Windows device;
- confirmed version history with version number and UTC timestamp.

Emit `dirty-change` whenever local decisions differ from the last server load. On 409, discard unsaved response assumptions, reload the profile, retain no automatic overwrite, and show the exact conflict message from the test. Do not render a free-text field for government ID, family member, or emergency contact.

- [ ] **Step 7: Run component tests and typecheck**

Run:

```powershell
npm.cmd --prefix frontend run test -- ProfileWorkspace.spec.ts profileApi.spec.ts
npm.cmd --prefix frontend run typecheck
```

Expected: both test files PASS and `vue-tsc` exits 0.

- [ ] **Step 8: Mount the feature during the shared App.vue integration window**

In `frontend/src/App.vue`, add only the feature import, view union, dirty guard, tab, and mount:

```typescript
import ProfileWorkspace from "./features/profile/ProfileWorkspace.vue";

type WorkspaceView = "analysis" | "jobs" | "profile" | "job_review";
const profileWorkspaceDirty = ref(false);
```

Before switching away from `profile`, use the same `window.confirm` pattern already used for dirty administrator review. Add a “我的档案” tab and:

```vue
<ProfileWorkspace
  v-if="workspaceView === 'profile'"
  :token="token"
  @dirty-change="profileWorkspaceDirty = $event"
/>
```

Reset `profileWorkspaceDirty` on successful logout. Do not move analysis logic or place profile API calls in `App.vue`.

- [ ] **Step 9: Run the complete frontend gate**

Run:

```powershell
npm.cmd --prefix frontend run test
npm.cmd --prefix frontend run typecheck
npm.cmd --prefix frontend run build
```

Expected: all Vitest tests PASS, `vue-tsc` exits 0, and Vite production build succeeds. Record the existing `npm audit` high-severity finding separately; do not claim it was fixed by this feature.

- [ ] **Step 10: Commit Task 5**

```powershell
git add frontend/src/features/profile frontend/src/App.vue
git commit -m "feat: add profile evidence review workspace"
```

---

### Task 6: Add security, real-dependency, concurrency, and operational release gates

**Files:**
- Create: `tests/integration/test_profile_lifecycle_mysql.py`
- Modify: `tests/integration/test_object_store.py`
- Modify: `tests/security/test_no_sensitive_logging.py`
- Modify: `docs/runbooks/platform-foundation.md`

**Interfaces:**
- Consumes: Tasks 1–5 complete vertical slice.
- Produces: fresh evidence that MySQL serialization, MinIO ciphertext, privacy boundaries, Compose startup, and the documented recovery flow meet workstream-A completion criteria.

- [ ] **Step 1: Write the real-MySQL concurrent confirmation test**

Create `tests/integration/test_profile_lifecycle_mysql.py` using the existing `destructive_mysql_url` fixture and independent SQLAlchemy sessions. Seed one user/profile/import/evidence, then synchronize two threads so both call `ProfileService.create_confirmed_version` with the same `expected_version`. Assert exactly one succeeds and one raises `StaleProfileVersionError`:

```python
assert sorted(outcomes) == ["stale_profile_version", "success"]
with Session(engine) as db:
    assert db.scalar(select(func.count(ConfirmedProfileVersion.id))) == 1
    assert db.scalar(select(func.count(ProfileFieldEvidence.id))) == evidence_count
    assert db.scalar(select(func.count(ProfileFieldDecision.id))) == decision_count
```

Add a second test that uploads/imports the same bytes twice and proves two import IDs and two evidence batches exist while the first `ConfirmedProfileVersion.facts_snapshot` is unchanged.

- [ ] **Step 2: Extend the MinIO gate to inspect raw ciphertext and reconciliation**

In `tests/integration/test_object_store.py`, write a unique resume object through `EncryptedObjectStore`, inspect it through `S3BlobStore.head`, and assert:

```python
assert plaintext not in raw_body
assert head["Metadata"] == {"encryption": "v1-aes-256-gcm"}
assert store.inspect(key=object_key).encryption == "v1-aes-256-gcm"
assert store.get(key=object_key) == plaintext
```

Always delete the test object in `finally`. Skip only when the exact required `TEST_S3_*` variables are absent.

- [ ] **Step 3: Add API and logging leak sentinels**

Extend `tests/security/test_no_sensitive_logging.py` with an authenticated upload/parse/decision flow containing these sentinels:

```python
forbidden = (
    "resume-body-sentinel",
    "110101200001011234",
    "users/student-id/resume-assets/",
    "plaintext_sha256",
    "object_key",
    "storage-provider-exception-sentinel",
)
for value in forbidden:
    assert value.lower() not in captured_logs.lower()
    assert value.lower() not in serialized_responses.lower()
```

The test may assert returned evidence excerpts contain the minimal ordinary source excerpt when explicitly reading the owned profile; it must still prove unrelated API responses and all logs omit the complete resume and every internal/sensitive sentinel.

- [ ] **Step 4: Document exact operations and recovery behavior**

Add a `## 档案与简历生命周期` section to `docs/runbooks/platform-foundation.md` containing:

```markdown
## 档案与简历生命周期

简历上传限制为 10 MiB，支持 PDF、DOCX、TXT 和 Markdown。Backend 先在 MySQL
创建 `pending_upload` 资产，再使用 AES-256-GCM 加密并写入 MinIO/S3，最后将资产
标记为 `ready`。API 和前端不显示对象 key；下载必须经过用户 JWT、所有权检查和
Backend 解密，不能直接访问对象存储。

若对象写入后数据库确认失败，使用资产 ID 调用受控 reconcile 接口。reconcile 只检查
对象是否存在且 metadata 中 `encryption=v1-aes-256-gcm`，不会把对象 key 或原始
provider 错误返回给用户。对象不存在时由用户重新上传，新上传创建新资产；不要手工
把数据库状态改为 `ready`。

每次解析创建新的 `ResumeImport`。`needs_manual_entry` 表示图片型、空文本或无法可靠
读取，需要用户在线补充；一期不运行 OCR。解析证据、用户决策和
`ConfirmedProfileVersion` 只追加，重复上传和重试不会覆盖历史版本。

身份证、家庭成员和紧急联系人明文只允许保存在 Windows 本地保险库。云端只保存类别、
`lsr:v1:<64 lowercase hex>` 不可逆引用和更新时间。不得通过档案 API、日志、对象存储
或模型输入传输这些明文。

备份与恢复必须同时覆盖 MySQL 档案元数据、MinIO 密文对象和对应的
`OBJECT_ENCRYPTION_KEY`。只恢复其中一部分无法完成简历下载。当前版本不提供用户删除
对象的产品流程；任何数据删除需求必须先定义 MySQL 行、不可变审计、对象和备份保留期
的一致删除策略，不能直接删除 bucket 对象。
```

Also update the migration list and release commands to name `20260717_0005`; keep future `0006/0007/0008` entries conditional on those revisions actually existing.

- [ ] **Step 5: Run focused backend and security gates**

Run:

```powershell
.\.venv\Scripts\python.exe -m ruff check backend src tests scripts
.\.venv\Scripts\python.exe -m pytest tests/unit/test_profile_domain.py tests/unit/test_profile_parser.py tests/unit/test_profile_repository.py tests/unit/test_profile_service.py tests/contract/test_profiles_api.py tests/security/test_no_sensitive_logging.py -q
```

Expected: Ruff prints `All checks passed!`; all selected default-environment tests PASS.

- [ ] **Step 6: Run guarded real MySQL and MinIO gates**

After loading secret values from User-scope environment variables without printing them, run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_mysql_migration.py tests/integration/test_profile_lifecycle_mysql.py tests/integration/test_object_store.py -q -rs
```

Expected: MySQL migration/concurrency and MinIO ciphertext tests PASS when `ALLOW_DESTRUCTIVE_MYSQL_TESTS=1`, a `_test` database URL, and all `TEST_S3_*` variables are configured. Otherwise only exact environment-variable skips are accepted and recorded as external validation incomplete.

- [ ] **Step 7: Run the complete repository gate**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q -rs
npm.cmd --prefix frontend run test
npm.cmd --prefix frontend run typecheck
npm.cmd --prefix frontend run build
git diff --check
```

Expected: all configured Python and frontend tests PASS, opt-in skips list their exact missing environment variables, the production build succeeds, and `git diff --check` emits no output.

- [ ] **Step 8: Verify Compose migration and a real encrypted profile flow**

Load the six required User-scope secrets without printing them and preserve this machine's documented port overrides. Run:

```powershell
$env:MYSQL_HOST_PORT = '3307'
$env:REDIS_HOST_PORT = '6380'
$env:MINIO_HOST_PORT = '19000'
$env:MINIO_CONSOLE_HOST_PORT = '19001'
$env:BACKEND_HOST_PORT = '18000'
$env:FRONTEND_HOST_PORT = '15173'
docker compose -p platform-foundation up -d --build
docker compose -p platform-foundation ps -a
docker compose -p platform-foundation run --rm migrate alembic current
Invoke-RestMethod http://127.0.0.1:18000/api/health/ready
Invoke-WebRequest http://127.0.0.1:15173/ -UseBasicParsing
```

Expected: migrate exits 0 at `20260717_0005`; MySQL, Redis, MinIO, and Backend are healthy; readiness and frontend return HTTP 200. Through the visible UI, upload one non-sensitive fixture resume, parse it, decide every candidate, and create one confirmed version. Inspect the MinIO object through the integration test/API rather than the console and record that raw bytes do not contain the fixture plaintext.

- [ ] **Step 9: Commit Task 6**

```powershell
git add tests/integration/test_profile_lifecycle_mysql.py tests/integration/test_object_store.py tests/security/test_no_sensitive_logging.py docs/runbooks/platform-foundation.md
git commit -m "test: gate encrypted profile resume lifecycle"
```

---

## Final Review Checklist

- [ ] Alembic has one head at `20260717_0005`, revising `20260716_0004`; workstream B is explicitly unblocked to revise `0005`.
- [ ] PDF, DOCX, TXT, and Markdown produce reviewable evidence; image-only and unreadable files retain the encrypted asset and return `needs_manual_entry` without OCR claims.
- [ ] An upload record is committed before object storage; valid encrypted-object metadata can reconcile a post-write database failure.
- [ ] Every re-upload and retry creates new asset/import/evidence history and never overwrites a confirmed version.
- [ ] Every candidate requires an explicit `confirm`, `correct`, or `ignore` decision before the selected import can create a version.
- [ ] Profile decisions use a locked row plus `expected_version`; the real MySQL concurrency test yields one success and one 409-equivalent stale result.
- [ ] Evidence, decisions, and confirmed versions have no application update/delete helpers and remain append-only in tests.
- [ ] MinIO/S3 raw bytes are AES-256-GCM ciphertext and use the object key as AAD through the unchanged storage primitive.
- [ ] Asset/profile/version DTOs never expose `object_key`, plaintext SHA-256, full resume text, storage exceptions, or another user's identity.
- [ ] Cross-user assets, imports, evidence, downloads, and profile versions return 404.
- [ ] Local-sensitive metadata accepts only the three stable categories and `lsr:v1:<64 lowercase hex>` references; no Web input can collect the corresponding plaintext.
- [ ] Frontend upload, manual-entry, diff review, stale reload, dirty navigation, and immutable history states pass Vitest/typecheck/build.
- [ ] Ruff, complete Python regression, guarded MySQL/MinIO gates, Compose migration/readiness, and `git diff --check` have fresh evidence.
- [ ] Missing external test variables and the existing high-severity `npm audit` finding are reported honestly and are not folded into a complete-release claim.
- [ ] `POST /api/analysis/run`, local demo jobs, LangGraph matching, resume drafting, approved attachments, snapshots, and Executor behavior remain unchanged for workstream E/C.

## Handoff to Wave 2 Integration

Workstream E may consume only a `ConfirmedProfileVersion` ID owned by the authenticated user, its immutable `facts_snapshot`, `evidence_refs`, and safe local-sensitive reference metadata. It must not read `ResumeAsset.object_key`, raw resume bytes, pending evidence, ignored candidates, or any local-sensitive plaintext. A Wave 2 match or draft becomes stale by input ID/version selection and creates a new artifact; it never edits this plan's confirmed version.
