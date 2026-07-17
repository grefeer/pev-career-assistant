# Wave 2 证据化匹配、定制简历与投递快照 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 完成 verified job + confirmed profile → MatchReport → ResumeDraft → ApprovedResumeVersion → ApplicationSnapshot → ApplicationTask → Executor v2 的完整纵向闭环。

**Architecture:** 新建 `src/evidence_matching/` 独立 LangGraph 生产图（与 CLI demo 图隔离）；MatchService/DraftService/SnapshotService 各自负责事务边界与权限校验，图只做纯计算；新增 5 张表 + ApplicationTask 兼容迁移；Executor 新增 v2 协议保留 v1 模拟。

**Tech Stack:** Python 3.12+, FastAPI, SQLAlchemy + Alembic, LangGraph, Pydantic v2, Vue 3 + vue-router 4 + TypeScript, Vitest, pytest, MinIO/S3 AES-256-GCM, Redis

**Spec:** `docs/superpowers/specs/2026-07-17-wave2-evidence-matching-implementation-plan.md`

## Global Constraints

- MySQL 是唯一业务权威来源；Redis 丢失不得改变业务状态
- 模型输出只能生成 draft/suggestion；不能直接确认职位、修改主档案或执行浏览器副作用
- 所有新增业务实体使用 36 字符 UUID；时间保存为 UTC；可变聚合使用整数版本乐观锁
- 日志只记录实体 ID、稳定错误码和脱敏计数；API 响应不含 object key、完整简历、敏感字段
- 只有 verified job + confirmed profile 可进入匹配；只有 approved resume version 可创建快照
- `gui_eligible=false` 允许创建快照但不能创建/派发 ApplicationTask
- 敏感字段明文只能存在于 Windows 本地保险库；字段分类表统一管理白名单
- 所有创建型 POST 必须携带 `Idempotency-Key`；同一 key + 相同请求返回既有资源
- Web 生产路径不得导入 `load_jobs()` 或 `load_sample_resume()`
- executor.v1 simulation 流程保持通过；executor.v2 只处理 task_kind=application

---

## File Structure Map

```
backend/app/
├── db/
│   └── models.py                          # [MODIFY] +5 model classes, ApplicationTask cols
├── api/
│   ├── router.py                          # [MODIFY] +3 route mounts, -analysis mount
│   ├── match_schemas.py                   # [CREATE] CreateMatchRequest, MatchReportResponse, etc.
│   ├── draft_schemas.py                   # [CREATE] CreateDraftRequest, ResumeDraftResponse, etc.
│   ├── snapshot_schemas.py                # [CREATE] CreateSnapshotRequest, etc.
│   └── routes/
│       ├── analysis.py                    # [MODIFY] remove /run endpoint
│       ├── matches.py                     # [CREATE] POST/GET /api/matches
│       ├── resume_drafts.py              # [CREATE] POST/GET /api/resume-drafts + approve/reject
│       └── application_snapshots.py       # [CREATE] POST/GET /api/application-snapshots + create-task
├── services/
│   ├── job_snapshot_service.py            # [CREATE] build_verified_job_snapshot
│   ├── profile_snapshot_service.py        # [CREATE] build_confirmed_profile_snapshot
│   ├── match_service.py                   # [CREATE] MatchService.create_match
│   ├── match_scoring.py                   # [CREATE] deterministic scorer
│   ├── match_validators.py                # [CREATE] schema + evidence validation
│   ├── resume_draft_service.py            # [CREATE] create/approve/reject draft
│   ├── draft_validators.py               # [CREATE] diff op validation
│   ├── attachment_service.py              # [CREATE] PDF/DOCX generation + encryption
│   ├── application_snapshot_service.py    # [CREATE] create snapshot + create task
│   ├── snapshot_validators.py             # [CREATE] whitelist + field classification validation
│   ├── task_eligibility_service.py        # [CREATE] shared eligibility checks
│   ├── field_classification.py            # [CREATE] field path → classification table
│   ├── applications.py                    # [MODIFY] add assign_and_dispatch_task
│   ├── storage.py                         # [MODIFY] add delete method
│   └── idempotency.py                     # [CREATE] shared idempotency key helper
├── repositories/
│   ├── matches.py                         # [CREATE] MatchReport CRUD
│   ├── drafts.py                          # [CREATE] ResumeDraft CRUD
│   ├── attachments.py                     # [CREATE] ApprovedResumeAttachment CRUD
│   └── snapshots.py                       # [CREATE] ApplicationSnapshot CRUD
└── schemas/
    └── executor_schemas.py                # [MODIFY] add ExecutorTaskPayloadV2

src/
└── evidence_matching/                     # [CREATE] new production graph
    ├── __init__.py
    ├── schemas.py                         # Pydantic I/O schemas
    ├── agents.py                          # LangGraph agent nodes
    ├── graph.py                           # evidence matching graph
    └── prompts.py                         # model prompts

alembic/versions/
└── 20260718_0008_match_resume_snapshot.py # [CREATE] full migration

frontend/src/
├── main.ts                                # [MODIFY] use router
├── App.vue                                # [MODIFY] strip to AppShell + router-view
├── state/
│   ├── auth.ts                            # [CREATE] shared auth state
│   └── sessions.ts                        # [CREATE] shared session state
├── router/
│   ├── index.ts                           # [CREATE] route definitions
│   └── guards.ts                          # [CREATE] auth/admin guards
├── components/
│   └── AppShell.vue                       # [CREATE] nav + router-view
└── features/
    ├── matching/                          # [CREATE]
    │   ├── MatchingWorkspace.vue
    │   ├── ResumeDraftReview.vue
    │   ├── matchingApi.ts
    │   ├── matchingTypes.ts
    │   ├── draftApi.ts
    │   └── draftTypes.ts
    ├── snapshots/                         # [CREATE]
    │   ├── SnapshotList.vue
    │   ├── SnapshotDetail.vue
    │   ├── snapshotApi.ts
    │   └── snapshotTypes.ts
    └── devices/
        └── deviceApi.ts                   # [CREATE] read-only device list

tests/
├── unit/
│   ├── test_match_validators.py           # [CREATE]
│   ├── test_draft_validators.py           # [CREATE]
│   ├── test_snapshot_validators.py        # [CREATE]
│   ├── test_field_classification.py       # [CREATE]
│   └── test_match_scoring.py             # [CREATE]
├── integration/
│   ├── test_match_service.py             # [CREATE]
│   ├── test_resume_draft_service.py      # [CREATE]
│   ├── test_attachment_service.py        # [CREATE]
│   ├── test_application_snapshot_service.py # [CREATE]
│   └── test_executor_v2_integration.py   # [CREATE]
├── api/
│   ├── test_matches_api.py               # [CREATE]
│   ├── test_resume_drafts_api.py         # [CREATE]
│   └── test_application_snapshots_api.py # [CREATE]
└── security/
    ├── test_wave2_privacy.py             # [CREATE]
    └── test_wave2_authorization.py       # [CREATE]
```

---

## Phase 0: Contract Freeze & Readiness Gate

### Task 0.1: Validate Fixtures Exist

**Files:**
- Check: database state only — no new files

- [ ] **Step 1: Verify at least one verified JobPosting exists**

Run:
```bash
cd backend && python -c "
from app.db.session import SessionLocal
from app.db.models import JobPosting
db = SessionLocal()
count = db.query(JobPosting).filter(JobPosting.status == 'verified').count()
print(f'Verified jobs: {count}')
assert count >= 1, 'Need at least one verified job'
db.close()
"
```

- [ ] **Step 2: Verify at least one ConfirmedProfileVersion exists**

Run:
```bash
cd backend && python -c "
from app.db.session import SessionLocal
from app.db.models import ConfirmedProfileVersion
db = SessionLocal()
count = db.query(ConfirmedProfileVersion).count()
print(f'Confirmed profile versions: {count}')
assert count >= 1, 'Need at least one confirmed profile version'
db.close()
"
```

- [ ] **Step 3: If either is missing, create a fixture script**

Create `scripts/create_wave2_fixtures.py` that uses existing factories to create test data. Run it against the dev database.

### Task 0.2: Write Contract Test Skeleton

**Files:**
- Create: `tests/contracts/test_wave2_dtos.py`

- [ ] **Step 1: Write DTO serialization round-trip tests**

```python
"""Contract tests for Wave 2 DTOs — verify serialization shapes before implementation."""
import json
from datetime import datetime, timezone


class TestVerifiedJobSnapshot:
    def test_round_trip_minimal(self):
        from backend.app.services.job_snapshot_service import VerifiedJobSnapshot

        snapshot = VerifiedJobSnapshot(
            job_id="job-001",
            company_name="Test Corp",
            title="Software Engineer",
            description_text="Build things.",
            locations=["Beijing"],
            recruitment_types=["campus"],
            industries=["Tech"],
            apply_url="https://example.com/apply",
            gui_eligible=True,
            verified_at=datetime.now(timezone.utc),
            review_version=1,
            source_links=[],
        )
        data = json.loads(json.dumps(snapshot, default=str))
        assert data["job_id"] == "job-001"
        assert data["gui_eligible"] is True

    def test_gui_ineligible(self):
        from backend.app.services.job_snapshot_service import VerifiedJobSnapshot

        snapshot = VerifiedJobSnapshot(
            job_id="job-002",
            company_name="Test Corp",
            title="Referral Only",
            description_text="Email your CV.",
            locations=["Shanghai"],
            recruitment_types=["referral"],
            industries=["Finance"],
            apply_url=None,
            gui_eligible=False,
            verified_at=datetime.now(timezone.utc),
            review_version=2,
            source_links=[],
        )
        assert snapshot.gui_eligible is False
        assert snapshot.apply_url is None


class TestConfirmedProfileSnapshot:
    def test_sensitive_fields_excluded(self):
        from backend.app.services.profile_snapshot_service import ConfirmedProfileSnapshot

        snapshot = ConfirmedProfileSnapshot(
            profile_version_id="pv-001",
            profile_id="prof-001",
            version_number=1,
            facts={
                "education": [{"school": "PKU", "degree": "BS"}],
                "skills": ["Python", "Rust"],
            },
            evidence_refs={
                "education": ["ev-edu-001"],
                "skills": ["ev-skill-001"],
            },
            confirmed_at=datetime.now(timezone.utc),
        )
        # local_sensitive_references must NOT be in facts
        assert "id_number" not in snapshot.facts
        assert "family_members" not in snapshot.facts
```

- [ ] **Step 2: Run contract tests (they will fail until implementations exist)**

```bash
pytest tests/contracts/test_wave2_dtos.py -v
```

Expected: some pass (pure data shape), some fail (import errors for not-yet-created modules). Record the expected failures for tracking.

### Task 0.3: Commit Contract Freeze

```bash
git add tests/contracts/test_wave2_dtos.py scripts/create_wave2_fixtures.py
git commit -m "feat: freeze wave 2 contract test skeleton and fixtures"
```

---

## Phase 1: Migration 0008 & Domain Foundation

### Task 1.1: Create Alembic Migration 20260718_0008

**Files:**
- Create: `alembic/versions/20260718_0008_match_resume_snapshot.py`

**Interfaces:**
- Produces: 5 new tables (match_reports, resume_drafts, approved_resume_versions, approved_resume_attachments, application_snapshots) + application_tasks migration columns (task_kind, simulation_scenario, request_idempotency_key) + FK constraints + indexes

- [ ] **Step 1: Generate empty migration**

```bash
cd backend && alembic revision -m "match_resume_snapshot" --rev-id 20260718_0008
```

- [ ] **Step 2: Write the migration file**

Replace the generated `alembic/versions/20260718_0008_match_resume_snapshot.py` with content covering all 5 tables, the application_tasks ALTER, FKs, indexes, and CHECK constraints as specified in the spec sections 4.1.1–4.1.7.

The migration must:
- Set `down_revision = "20260717_0007"`
- Create match_reports, resume_drafts, approved_resume_versions, approved_resume_attachments, application_snapshots in upgrade()
- Add task_kind, simulation_scenario, request_idempotency_key to application_tasks
- Migrate existing rows: `UPDATE application_tasks SET task_kind='simulation', simulation_scenario=target_job_id, target_job_id=NULL WHERE snapshot_id IS NULL`
- Make target_job_id nullable, then add FK to job_postings.id
- Add FK snapshot_id → application_snapshots.id
- Add CHECK constraints for task_kind validity
- Add all indexes
- Reverse everything in downgrade(): restore target_job_id from simulation_scenario, drop new columns and tables

- [ ] **Step 3: Test upgrade**

```bash
cd backend && alembic upgrade 0008
```

- [ ] **Step 4: Test downgrade**

```bash
cd backend && alembic downgrade 0007
```

- [ ] **Step 5: Test full round-trip 0004→0008→0004**

```bash
cd backend && alembic downgrade 0004 && alembic upgrade 0008 && alembic downgrade 0004
```

- [ ] **Step 6: Commit**

```bash
git add alembic/versions/20260718_0008_match_resume_snapshot.py
git commit -m "feat: add migration 20260718_0008 for match, draft, snapshot tables"
```

### Task 1.2: Add ORM Models

**Files:**
- Modify: `backend/app/db/models.py`
- Modify: `backend/app/db/__init__.py` (if models are re-exported)

**Interfaces:**
- Produces: MatchReport, ResumeDraft, ApprovedResumeVersion, ApprovedResumeAttachment, ApplicationSnapshot SQLAlchemy models; updated ApplicationTask with task_kind, simulation_scenario, ForeignKey declarations

- [ ] **Step 1: Add MatchReport model**

At the end of `backend/app/db/models.py`, add:

```python
class MatchReport(Base):
    __tablename__ = "match_reports"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    analysis_session_id = Column(String(36), ForeignKey("analysis_sessions.id"), nullable=False)
    job_id = Column(String(36), ForeignKey("job_postings.id"), nullable=False)
    job_verification_id = Column(String(36), ForeignKey("job_verifications.id"), nullable=False)
    job_snapshot = Column(JSON, nullable=False)
    profile_version_id = Column(String(36), ForeignKey("confirmed_profile_versions.id"), nullable=False)
    request_idempotency_key = Column(String(96), nullable=False)
    request_hash = Column(String(64), nullable=False)
    score = Column(Integer, nullable=True)
    score_components = Column(JSON, nullable=True)
    scoring_rule_version = Column(String(64), nullable=False)
    strengths = Column(JSON, nullable=True)
    gaps = Column(JSON, nullable=True)
    unknowns = Column(JSON, nullable=True)
    risks = Column(JSON, nullable=True)
    application_priority = Column(String(20), nullable=True)
    recommendation = Column(JSON, nullable=True)
    model_version = Column(String(64), nullable=False)
    prompt_version = Column(String(64), nullable=False)
    output_schema_version = Column(String(64), nullable=False)
    status = Column(String(20), nullable=False, default="pending")
    error_code = Column(String(80), nullable=True)
    created_at = Column(DateTime, nullable=False, default=utcnow)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("user_id", "request_idempotency_key"),
        Index("ix_match_reports_analysis_session_id", "analysis_session_id"),
        Index("ix_match_reports_job_id", "job_id"),
        Index("ix_match_reports_profile_version_id", "profile_version_id"),
        Index("ix_match_reports_user_id_created_at", "user_id", "created_at"),
    )
```

- [ ] **Step 2: Add ResumeDraft model**

```python
class ResumeDraft(Base):
    __tablename__ = "resume_drafts"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    match_report_id = Column(String(36), ForeignKey("match_reports.id"), nullable=False)
    profile_version_id = Column(String(36), ForeignKey("confirmed_profile_versions.id"), nullable=False)
    target_job_id = Column(String(36), ForeignKey("job_postings.id"), nullable=False)
    request_idempotency_key = Column(String(96), nullable=False)
    request_hash = Column(String(64), nullable=False)
    diffs = Column(JSON, nullable=True)
    status = Column(String(20), nullable=False, default="generating")
    error_code = Column(String(80), nullable=True)
    state_version = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=utcnow)
    approved_at = Column(DateTime, nullable=True)
    rejected_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("user_id", "request_idempotency_key"),
    )
```

- [ ] **Step 3: Add ApprovedResumeVersion model**

```python
class ApprovedResumeVersion(Base):
    __tablename__ = "approved_resume_versions"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    draft_id = Column(String(36), ForeignKey("resume_drafts.id"), nullable=False)
    profile_version_id = Column(String(36), ForeignKey("confirmed_profile_versions.id"), nullable=False)
    target_job_id = Column(String(36), ForeignKey("job_postings.id"), nullable=False)
    approved_facts = Column(JSON, nullable=False)
    approved_diffs = Column(JSON, nullable=False)
    approved_at = Column(DateTime, nullable=False, default=utcnow)
    approved_by = Column(String(36), ForeignKey("users.id"), nullable=False)

    __table_args__ = (
        UniqueConstraint("draft_id"),
    )
```

- [ ] **Step 4: Add ApprovedResumeAttachment model**

```python
class ApprovedResumeAttachment(Base):
    __tablename__ = "approved_resume_attachments"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    draft_id = Column(String(36), ForeignKey("resume_drafts.id"), nullable=False)
    approved_resume_version_id = Column(String(36), ForeignKey("approved_resume_versions.id"), nullable=True)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    format = Column(String(16), nullable=False)
    object_key = Column(String(512), nullable=False, unique=True)
    content_type = Column(String(120), nullable=False)
    plaintext_size = Column(BigInteger, nullable=False)
    encryption_version = Column(String(32), nullable=False)
    status = Column(String(20), nullable=False)
    error_code = Column(String(80), nullable=True)
    created_at = Column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint("draft_id", "format"),
    )
```

- [ ] **Step 5: Add ApplicationSnapshot model**

```python
class ApplicationSnapshot(Base):
    __tablename__ = "application_snapshots"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    job_id = Column(String(36), ForeignKey("job_postings.id"), nullable=False)
    approved_resume_version_id = Column(String(36), ForeignKey("approved_resume_versions.id"), nullable=False)
    profile_version_id = Column(String(36), ForeignKey("confirmed_profile_versions.id"), nullable=False)
    job_snapshot = Column(JSON, nullable=False)
    profile_facts = Column(JSON, nullable=False)
    request_idempotency_key = Column(String(96), nullable=False)
    request_hash = Column(String(64), nullable=False)
    dynamic_answers = Column(JSON, nullable=False)
    local_sensitive_requirements = Column(JSON, nullable=False)
    attachment_ids = Column(JSON, nullable=False)
    gui_eligible = Column(Boolean, nullable=False)
    job_status_at_snapshot = Column(String(20), nullable=False)
    job_review_version_at_snapshot = Column(Integer, nullable=False)
    created_at = Column(DateTime, nullable=False, default=utcnow)
    created_by = Column(String(36), ForeignKey("users.id"), nullable=False)
    schema_version = Column(String(16), nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "request_idempotency_key"),
    )
```

- [ ] **Step 6: Update ApplicationTask model**

Find the existing `ApplicationTask` class in `models.py` and add:

```python
    task_kind = Column(String(20), nullable=False, default="simulation")
    simulation_scenario = Column(String(100), nullable=True)
    request_idempotency_key = Column(String(96), nullable=True)

    # Update ForeignKey declarations:
    # snapshot_id should already exist; add ForeignKey if missing
    # target_job_id: add ForeignKey("job_postings.id") if missing, make nullable
```

- [ ] **Step 7: Verify models import correctly**

```bash
cd backend && python -c "
from app.db.models import (
    MatchReport, ResumeDraft, ApprovedResumeVersion,
    ApprovedResumeAttachment, ApplicationSnapshot, ApplicationTask
)
print('All models import successfully')
print(f'ApplicationTask has task_kind: {hasattr(ApplicationTask, \"task_kind\")}')
"
```

- [ ] **Step 8: Commit**

```bash
git add backend/app/db/models.py
git commit -m "feat: add wave 2 ORM models (match, draft, snapshot, attachments)"
```

### Task 1.3: Implement Field Classification Table

**Files:**
- Create: `backend/app/services/field_classification.py`
- Create: `tests/unit/test_field_classification.py`

**Interfaces:**
- Produces: `ALLOWED_FIELDS: dict[str, FieldClassification]`, `classify_field(path: str) → FieldClassification`, `is_non_sensitive(path: str) → bool`, `is_local_sensitive(path: str) → bool`, `filter_non_sensitive(facts: dict) → dict`, `extract_local_sensitive_requirements(facts: dict) → list[LocalSensitiveRequirement]`

- [ ] **Step 1: Write the test**

```python
# tests/unit/test_field_classification.py
import pytest
from backend.app.services.field_classification import (
    classify_field,
    is_non_sensitive,
    is_local_sensitive,
    filter_non_sensitive,
    extract_local_sensitive_requirements,
    FieldClassification,
    UNKNOWN_FIELD,
)


class TestClassifyField:
    def test_known_non_sensitive(self):
        assert classify_field("education") == FieldClassification.NON_SENSITIVE
        assert classify_field("skills") == FieldClassification.NON_SENSITIVE
        assert classify_field("work_experience") == FieldClassification.NON_SENSITIVE

    def test_known_local_sensitive(self):
        assert classify_field("id_number") == FieldClassification.LOCAL_SENSITIVE
        assert classify_field("family_members") == FieldClassification.LOCAL_SENSITIVE
        assert classify_field("emergency_contact") == FieldClassification.LOCAL_SENSITIVE

    def test_nested_field_path(self):
        assert classify_field("education.0.school") == FieldClassification.NON_SENSITIVE
        assert classify_field("family_members.0.name") == FieldClassification.LOCAL_SENSITIVE

    def test_unknown_field(self):
        assert classify_field("unknown_field") == FieldClassification.UNKNOWN
        assert classify_field("passwords") == FieldClassification.UNKNOWN


class TestIsNonSensitive:
    def test_allows_known_fields(self):
        assert is_non_sensitive("education") is True
        assert is_non_sensitive("skills") is True

    def test_rejects_local_sensitive(self):
        assert is_non_sensitive("id_number") is False
        assert is_non_sensitive("family_members") is False

    def test_rejects_unknown(self):
        assert is_non_sensitive("random_field") is False


class TestFilterNonSensitive:
    def test_strips_local_sensitive_and_unknown(self):
        facts = {
            "education": [{"school": "PKU"}],
            "skills": ["Python"],
            "id_number": "110101199001011234",
            "family_members": [{"name": "John", "relation": "father"}],
            "unknown_field": "value",
        }
        result = filter_non_sensitive(facts)
        assert "education" in result
        assert "skills" in result
        assert "id_number" not in result
        assert "family_members" not in result
        assert "unknown_field" not in result


class TestExtractLocalSensitiveRequirements:
    def test_extracts_semantic_keys_only(self):
        facts = {
            "education": [{"school": "PKU"}],
            "id_number": "110101199001011234",
            "family_members": [{"name": "John", "relation": "father"}],
            "emergency_contact": {"name": "Jane", "phone": "13800000000"},
        }
        result = extract_local_sensitive_requirements(facts)
        keys = {r["field_key"] for r in result}
        assert "id_number" in keys
        assert "family_members" in keys
        assert "emergency_contact" in keys
        assert "education" not in keys
        # Must NOT contain plaintext values
        for r in result:
            assert "110101" not in str(r)
            assert "John" not in str(r)
            assert "13800000000" not in str(r)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/unit/test_field_classification.py -v
```

- [ ] **Step 3: Implement field_classification.py**

```python
"""Versioned field classification table — single source of truth for field allowlists."""
from enum import Enum
from typing import Any


class FieldClassification(str, Enum):
    NON_SENSITIVE = "non_sensitive"
    LOCAL_SENSITIVE = "local_sensitive"
    UNKNOWN = "unknown"


UNKNOWN_FIELD: FieldClassification = FieldClassification.UNKNOWN

# VERSION 1.0 — update when adding/removing fields; bump version in commit message
CLASSIFICATION_VERSION = "1.0"

# Top-level field paths → classification
_FIELD_TABLE: dict[str, FieldClassification] = {
    # --- Non-sensitive fields ---
    "name": FieldClassification.NON_SENSITIVE,
    "gender": FieldClassification.NON_SENSITIVE,
    "birth_date": FieldClassification.NON_SENSITIVE,
    "email": FieldClassification.NON_SENSITIVE,
    "phone": FieldClassification.NON_SENSITIVE,
    "education": FieldClassification.NON_SENSITIVE,
    "skills": FieldClassification.NON_SENSITIVE,
    "languages": FieldClassification.NON_SENSITIVE,
    "work_experience": FieldClassification.NON_SENSITIVE,
    "internship_experience": FieldClassification.NON_SENSITIVE,
    "project_experience": FieldClassification.NON_SENSITIVE,
    "awards": FieldClassification.NON_SENSITIVE,
    "certifications": FieldClassification.NON_SENSITIVE,
    "self_introduction": FieldClassification.NON_SENSITIVE,
    "career_objective": FieldClassification.NON_SENSITIVE,
    "expected_city": FieldClassification.NON_SENSITIVE,
    "expected_salary": FieldClassification.NON_SENSITIVE,
    "available_date": FieldClassification.NON_SENSITIVE,

    # --- Local-sensitive fields (semantic keys only, no plaintext in cloud) ---
    "id_number": FieldClassification.LOCAL_SENSITIVE,
    "family_members": FieldClassification.LOCAL_SENSITIVE,
    "emergency_contact": FieldClassification.LOCAL_SENSITIVE,
    "home_address": FieldClassification.LOCAL_SENSITIVE,
    "passport_number": FieldClassification.LOCAL_SENSITIVE,
    "bank_account": FieldClassification.LOCAL_SENSITIVE,
    "political_status": FieldClassification.LOCAL_SENSITIVE,
    "marital_status": FieldClassification.LOCAL_SENSITIVE,
}


def _top_level_key(path: str) -> str:
    return path.split(".")[0]


def classify_field(path: str) -> FieldClassification:
    """Classify a field path as non_sensitive, local_sensitive, or unknown."""
    return _FIELD_TABLE.get(_top_level_key(path), FieldClassification.UNKNOWN)


def is_non_sensitive(path: str) -> bool:
    return classify_field(path) == FieldClassification.NON_SENSITIVE


def is_local_sensitive(path: str) -> bool:
    return classify_field(path) == FieldClassification.LOCAL_SENSITIVE


def filter_non_sensitive(facts: dict[str, Any]) -> dict[str, Any]:
    """Return only non-sensitive fields from a facts dict."""
    return {
        k: v for k, v in facts.items()
        if classify_field(k) == FieldClassification.NON_SENSITIVE
    }


def extract_local_sensitive_requirements(facts: dict[str, Any]) -> list[dict[str, str]]:
    """Return semantic references for local-sensitive fields — NO plaintext values."""
    requirements: list[dict[str, str]] = []
    for key, classification in _FIELD_TABLE.items():
        if classification == FieldClassification.LOCAL_SENSITIVE and key in facts:
            requirements.append({
                "field_key": key,
                "category": _top_level_key(key),
                "local_reference": f"local://{key}",  # irreversible reference
            })
    return requirements
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/unit/test_field_classification.py -v
```

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/field_classification.py tests/unit/test_field_classification.py
git commit -m "feat: add field classification table v1.0"
```

### Task 1.4: Implement Validators

**Files:**
- Create: `backend/app/services/match_validators.py`
- Create: `backend/app/services/draft_validators.py`
- Create: `backend/app/services/snapshot_validators.py`
- Create: `tests/unit/test_match_validators.py`
- Create: `tests/unit/test_draft_validators.py`
- Create: `tests/unit/test_snapshot_validators.py`

**Interfaces:**
- Produces:
  - `validate_match_output(output: MatchComputationOutput, job_snapshot: VerifiedJobSnapshot, profile_snapshot: ConfirmedProfileSnapshot) → MatchComputationOutput` (raises `MatchValidationError` on failure)
  - `validate_draft_diffs(diffs: list[ResumeDiffOp], confirmed_facts: dict) → list[ResumeDiffOp]` (raises `DraftValidationError` on failure)
  - `validate_snapshot_content(content: ApplicationSnapshotContent) → ApplicationSnapshotContent` (raises `SnapshotValidationError` on failure)
  - `validate_dynamic_answers(answers: list[CloudDynamicAnswer]) → list[CloudDynamicAnswer]` (raises `SnapshotValidationError` on failure)
  - `validate_local_sensitive_requirements(reqs: list[LocalSensitiveRequirement]) → list[LocalSensitiveRequirement]` (raises `SnapshotValidationError` on failure)

- [ ] **Step 1: Write match_validators tests**

```python
# tests/unit/test_match_validators.py
import pytest
from backend.app.services.match_validators import (
    validate_match_output,
    MatchValidationError,
)


class TestValidateMatchOutput:
    def test_valid_output_passes(self, sample_job_snapshot, sample_profile_snapshot, sample_valid_output):
        result = validate_match_output(sample_valid_output, sample_job_snapshot, sample_profile_snapshot)
        assert result == sample_valid_output

    def test_missing_requirement_id_fails(self, sample_job_snapshot, sample_profile_snapshot, sample_valid_output):
        sample_valid_output["strengths"][0].pop("requirement_id")
        with pytest.raises(MatchValidationError, match="requirement_id"):
            validate_match_output(sample_valid_output, sample_job_snapshot, sample_profile_snapshot)

    def test_satisfied_without_evidence_fails(self, sample_job_snapshot, sample_profile_snapshot, sample_valid_output):
        sample_valid_output["strengths"][0]["evidence_ids"] = []
        with pytest.raises(MatchValidationError, match="evidence"):
            validate_match_output(sample_valid_output, sample_job_snapshot, sample_profile_snapshot)

    def test_unknown_with_fabricated_evidence_fails(self, sample_job_snapshot, sample_profile_snapshot, sample_valid_output):
        sample_valid_output["unknowns"][0]["evidence_ids"] = ["fake-ev-001"]
        with pytest.raises(MatchValidationError, match="unknown.*evidence"):
            validate_match_output(sample_valid_output, sample_job_snapshot, sample_profile_snapshot)

    def test_invalid_evidence_ref_fails(self, sample_job_snapshot, sample_profile_snapshot, sample_valid_output):
        sample_valid_output["strengths"][0]["evidence_ids"] = ["ev-nonexistent"]
        with pytest.raises(MatchValidationError, match="evidence.*not found"):
            validate_match_output(sample_valid_output, sample_job_snapshot, sample_profile_snapshot)

    def test_risk_without_requirement_ref_fails(self, sample_job_snapshot, sample_profile_snapshot, sample_valid_output):
        sample_valid_output["risks"][0]["requirement_ids"] = []
        with pytest.raises(MatchValidationError, match="requirement.*ref"):
            validate_match_output(sample_valid_output, sample_job_snapshot, sample_profile_snapshot)

    def test_verdict_mismatch_fails(self, sample_job_snapshot, sample_profile_snapshot, sample_valid_output):
        # An item in 'strengths' list must have verdict 'satisfied'
        sample_valid_output["strengths"][0]["verdict"] = "gap"
        with pytest.raises(MatchValidationError, match="verdict.*strengths"):
            validate_match_output(sample_valid_output, sample_job_snapshot, sample_profile_snapshot)
```

- [ ] **Step 2: Run match_validators tests (fail)**

```bash
pytest tests/unit/test_match_validators.py -v
```

- [ ] **Step 3: Implement match_validators.py**

```python
"""Validates LangGraph structured output before persisting as MatchReport."""
from typing import Any


class MatchValidationError(ValueError):
    """Raised when model output fails validation. Contains stable error_code."""

    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code


def validate_match_output(
    output: dict[str, Any],
    job_snapshot: Any,
    profile_snapshot: Any,
) -> dict[str, Any]:
    """Validate model output against job/profile snapshots. Returns output if valid, raises MatchValidationError otherwise."""

    # Collect all requirement IDs for cross-reference
    all_requirement_ids: set[str] = set()
    for item in output.get("strengths", []) + output.get("gaps", []) + output.get("unknowns", []):
        req_id = item.get("requirement_id")
        if not req_id:
            raise MatchValidationError("match_validation_missing_requirement_id", f"Item missing requirement_id: {item}")
        if req_id in all_requirement_ids:
            raise MatchValidationError("match_validation_duplicate_requirement_id", f"Duplicate requirement_id: {req_id}")
        all_requirement_ids.add(req_id)

    # Collect all valid evidence IDs from profile
    valid_evidence_ids: set[str] = set()
    for field_path, ev_ids in profile_snapshot.evidence_refs.items():
        valid_evidence_ids.update(ev_ids)

    # Validate each assessment category
    _validate_assessments(output.get("strengths", []), "satisfied", "strengths", all_requirement_ids, valid_evidence_ids)
    _validate_assessments(output.get("gaps", []), "gap", "gaps", all_requirement_ids, valid_evidence_ids)
    _validate_assessments(output.get("unknowns", []), "unknown", "unknowns", all_requirement_ids, valid_evidence_ids)

    # Validate risks reference requirement IDs
    for risk in output.get("risks", []):
        risk_req_ids = risk.get("requirement_ids", [])
        if not risk_req_ids:
            raise MatchValidationError("match_validation_risk_missing_ref", f"Risk missing requirement_ids: {risk}")
        for rid in risk_req_ids:
            if rid not in all_requirement_ids:
                raise MatchValidationError("match_validation_risk_invalid_ref", f"Risk references unknown requirement_id: {rid}")

    # Validate recommendation references requirement IDs
    rec = output.get("recommendation", {})
    rec_req_ids = rec.get("requirement_ids", [])
    for rid in rec_req_ids:
        if rid not in all_requirement_ids:
            raise MatchValidationError("match_validation_recommendation_invalid_ref", f"Recommendation references unknown requirement_id: {rid}")

    return output


def _validate_assessments(
    items: list[dict],
    expected_verdict: str,
    category: str,
    all_req_ids: set[str],
    valid_evidence_ids: set[str],
) -> None:
    for item in items:
        # Verdict must match category
        actual = item.get("verdict")
        if actual != expected_verdict:
            raise MatchValidationError(
                "match_validation_verdict_category_mismatch",
                f"Item in '{category}' has verdict '{actual}' instead of '{expected_verdict}': requirement_id={item.get('requirement_id')}"
            )

        # satisfied must have profile_field_path and evidence
        if expected_verdict == "satisfied":
            if not item.get("profile_field_path"):
                raise MatchValidationError(
                    "match_validation_satisfied_missing_profile_path",
                    f"Satisfied item missing profile_field_path: requirement_id={item.get('requirement_id')}"
                )
            ev_ids = item.get("evidence_ids", [])
            if not ev_ids:
                raise MatchValidationError(
                    "match_validation_satisfied_missing_evidence",
                    f"Satisfied item has no evidence_ids: requirement_id={item.get('requirement_id')}"
                )
            for eid in ev_ids:
                if eid not in valid_evidence_ids:
                    raise MatchValidationError(
                        "match_evidence_ref_invalid",
                        f"Evidence ID not found in profile: {eid} (requirement_id={item.get('requirement_id')})"
                    )

        # unknown must NOT have fabricated evidence
        if expected_verdict == "unknown":
            ev_ids = item.get("evidence_ids", [])
            if ev_ids:
                raise MatchValidationError(
                    "match_validation_unknown_with_evidence",
                    f"Unknown item must not carry evidence_ids: requirement_id={item.get('requirement_id')}"
                )
```

- [ ] **Step 4: Run match_validators tests (pass)**

```bash
pytest tests/unit/test_match_validators.py -v
```

- [ ] **Step 5: Write + implement draft_validators (TDD, same pattern)**

`draft_validators.py`:
- `class DraftValidationError(ValueError)` with `error_code`
- `def validate_draft_diffs(diffs: list[dict], confirmed_facts: dict, evidence_refs: dict) → list[dict]`
- Validates: op in {reorder, rephrase, summarize, omit, highlight}, section non-empty, fact_ref exists in confirmed_facts, evidence_ids all valid

`snapshot_validators.py`:
- `class SnapshotValidationError(ValueError)` with `error_code`
- `def validate_snapshot_content(job_snapshot, profile_facts, dynamic_answers, local_sensitive_requirements, attachment_ids) → None`
- `def validate_dynamic_answers(answers: list[dict]) → list[dict]` — every answer must have `classification: "non_sensitive"` and pass field_classification check
- `def validate_local_sensitive_requirements(reqs: list[dict]) → list[dict]` — every req must have `field_key`, `category`, `local_reference`; NO plaintext values; every field_key must classify as LOCAL_SENSITIVE

- [ ] **Step 6: Run all validator tests**

```bash
pytest tests/unit/test_match_validators.py tests/unit/test_draft_validators.py tests/unit/test_snapshot_validators.py -v
```

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/match_validators.py backend/app/services/draft_validators.py backend/app/services/snapshot_validators.py tests/unit/test_match_validators.py tests/unit/test_draft_validators.py tests/unit/test_snapshot_validators.py
git commit -m "feat: add wave 2 domain validators (match, draft, snapshot)"
```

### Task 1.5: Implement Repositories

**Files:**
- Create: `backend/app/repositories/matches.py`
- Create: `backend/app/repositories/drafts.py`
- Create: `backend/app/repositories/attachments.py`
- Create: `backend/app/repositories/snapshots.py`

**Interfaces:**
- Produces:
  - `MatchRepository`: `create(db, **kwargs) → MatchReport`, `get_by_id(db, match_id, user_id) → MatchReport | None`, `list_by_session(db, session_id, user_id) → list[MatchReport]`, `finalize(db, match_id, status, result_or_error) → MatchReport`, `recover_stale(db, timeout_minutes=10) → int`
  - `DraftRepository`: `create(db, **kwargs) → ResumeDraft`, `get_by_id(db, draft_id, user_id) → ResumeDraft | None`, `list_by_user(db, user_id) → list[ResumeDraft]`, `approve(db, draft_id, expected_version) → ResumeDraft`, `reject(db, draft_id, expected_version) → ResumeDraft`, `finalize(db, draft_id, status, diffs_or_error) → ResumeDraft`
  - `AttachmentRepository`: `create_pending(db, draft_id, user_id, format, object_key, content_type, plaintext_size, encryption_version) → ApprovedResumeAttachment`, `mark_ready(db, attachment_id, approved_version_id) → ApprovedResumeAttachment`, `mark_failed(db, attachment_id, error_code) → ApprovedResumeAttachment`, `get_by_draft(db, draft_id, user_id) → list[ApprovedResumeAttachment]`
  - `SnapshotRepository`: `create(db, **kwargs) → ApplicationSnapshot`, `get_by_id(db, snapshot_id, user_id) → ApplicationSnapshot | None`, `list_by_user(db, user_id) → list[ApplicationSnapshot]`

All repositories pattern-match existing `backend/app/repositories/` style. DraftRepository uses `state_version + 1` optimistic locking on approve/reject.

- [ ] **Step 1–4: Implement each repository (no model calls, simple CRUD + optimistic locking)**

Follow existing patterns from `backend/app/repositories/profiles.py` and `backend/app/repositories/applications.py`.

- [ ] **Step 5: Commit**

```bash
git add backend/app/repositories/matches.py backend/app/repositories/drafts.py backend/app/repositories/attachments.py backend/app/repositories/snapshots.py
git commit -m "feat: add wave 2 repositories (match, draft, attachment, snapshot)"
```

### Task 1.6: Phase 1 Integration Test

**Files:**
- Create: `tests/integration/test_wave2_migration_models.py`

- [ ] **Step 1: Write migration + model smoke test**

```python
# tests/integration/test_wave2_migration_models.py
"""Verify migration 0008 tables exist and ORM models work after upgrade."""
import pytest
import uuid
from datetime import datetime, timezone


@pytest.mark.integration
class TestMatchReportCRUD:
    def test_create_and_read(self, db_session, test_user):
        from backend.app.db.models import MatchReport

        mid = str(uuid.uuid4())
        report = MatchReport(
            id=mid,
            user_id=test_user.id,
            analysis_session_id=str(uuid.uuid4()),
            job_id=str(uuid.uuid4()),
            job_verification_id=str(uuid.uuid4()),
            job_snapshot={"company_name": "Test"},
            profile_version_id=str(uuid.uuid4()),
            request_idempotency_key=f"test-{mid[:8]}",
            request_hash="abc123",
            status="pending",
            scoring_rule_version="1.0",
            model_version="test",
            prompt_version="test-v1",
            output_schema_version="1.0",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(report)
        db_session.commit()

        fetched = db_session.query(MatchReport).filter_by(id=mid).first()
        assert fetched is not None
        assert fetched.status == "pending"
        assert fetched.job_snapshot["company_name"] == "Test"


@pytest.mark.integration
class TestApplicationTaskMigration:
    def test_simulation_task_has_task_kind(self, db_session):
        from backend.app.db.models import ApplicationTask

        tasks = db_session.query(ApplicationTask).filter(
            ApplicationTask.snapshot_id.is_(None)
        ).limit(5).all()
        for t in tasks:
            assert t.task_kind == "simulation"
            assert t.target_job_id is None or t.simulation_scenario is not None
```

- [ ] **Step 2: Run integration tests**

```bash
pytest tests/integration/test_wave2_migration_models.py -v
```

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_wave2_migration_models.py
git commit -m "test: add wave 2 migration and model integration smoke tests"
```

---

## Phase 2: Evidence Matching Vertical Slice

### Task 2.1: Implement Snapshot Loaders

**Files:**
- Create: `backend/app/services/job_snapshot_service.py`
- Create: `backend/app/services/profile_snapshot_service.py`

**Interfaces:**
- Produces:
  - `build_verified_job_snapshot(db: Session, job_id: str) → VerifiedJobSnapshot` — raises 404/422
  - `build_confirmed_profile_snapshot(db: Session, profile_version_id: str, user_id: str) → ConfirmedProfileSnapshot` — raises 404

- [ ] **Step 1: Write job_snapshot_service.py**

```python
"""Builds VerifiedJobSnapshot from authoritative MySQL state."""
from datetime import datetime
from sqlalchemy.orm import Session
from backend.app.db.models import JobPosting, JobSourceLink

from backend.app.services.field_classification import CLASSIFICATION_VERSION


class VerifiedJobSnapshot:
    def __init__(
        self, *, job_id, company_name, title, description_text, locations,
        recruitment_types, industries, apply_url, gui_eligible,
        verified_at, review_version, source_links,
    ):
        self.job_id = job_id
        self.company_name = company_name
        self.title = title
        self.description_text = description_text
        self.locations = locations
        self.recruitment_types = recruitment_types
        self.industries = industries
        self.apply_url = apply_url
        self.gui_eligible = gui_eligible
        self.verified_at = verified_at
        self.review_version = review_version
        self.source_links = source_links


def build_verified_job_snapshot(db: Session, job_id: str) -> VerifiedJobSnapshot:
    job = db.query(JobPosting).filter(JobPosting.id == job_id).first()
    if job is None:
        raise ValueError("not_found")
    if job.status != "verified":
        raise ValueError("match_not_verified_job")

    source_links = [
        {"source_type": sl.source_type, "source_record_ref": sl.source_record_ref}
        for sl in db.query(JobSourceLink).filter(JobSourceLink.job_id == job_id).all()
    ]

    return VerifiedJobSnapshot(
        job_id=job.id,
        company_name=job.company_name or "",
        title=job.title or "",
        description_text=job.description_text or "",
        locations=job.locations or [],
        recruitment_types=job.recruitment_types or [],
        industries=job.industries or [],
        apply_url=job.apply_url,
        gui_eligible=bool(job.gui_eligible),
        verified_at=job.verified_at or datetime.min,
        review_version=job.review_version or 0,
        source_links=source_links,
    )
```

- [ ] **Step 2: Write profile_snapshot_service.py**

```python
"""Builds ConfirmedProfileSnapshot, filtering out local-sensitive fields."""
from datetime import datetime
from sqlalchemy.orm import Session
from backend.app.db.models import ConfirmedProfileVersion as CPV
from backend.app.services.field_classification import filter_non_sensitive


class ConfirmedProfileSnapshot:
    def __init__(
        self, *, profile_version_id, profile_id, version_number,
        facts, evidence_refs, confirmed_at,
    ):
        self.profile_version_id = profile_version_id
        self.profile_id = profile_id
        self.version_number = version_number
        self.facts = facts
        self.evidence_refs = evidence_refs
        self.confirmed_at = confirmed_at


def build_confirmed_profile_snapshot(
    db: Session, profile_version_id: str, user_id: str
) -> ConfirmedProfileSnapshot:
    cpv = db.query(CPV).filter(
        CPV.id == profile_version_id,
        CPV.profile.has(user_id=user_id),
    ).first()
    if cpv is None:
        raise ValueError("not_found")

    raw_facts = cpv.facts_snapshot or {}
    non_sensitive_facts = filter_non_sensitive(raw_facts)

    raw_evidence = cpv.evidence_refs or {}
    # evidence_refs shape: {field_path: [evidence_id, ...]}
    # Filter to non-sensitive fields only
    from backend.app.services.field_classification import is_non_sensitive
    filtered_evidence = {
        fp: ids for fp, ids in raw_evidence.items()
        if is_non_sensitive(fp)
    }

    return ConfirmedProfileSnapshot(
        profile_version_id=cpv.id,
        profile_id=cpv.profile_id,
        version_number=cpv.version_number,
        facts=non_sensitive_facts,
        evidence_refs=filtered_evidence,
        confirmed_at=cpv.aggregate_version,  # using aggregate_version as proxy for confirmed_at
    )
```

- [ ] **Step 3: Commit**

```bash
git add backend/app/services/job_snapshot_service.py backend/app/services/profile_snapshot_service.py
git commit -m "feat: add verified job and confirmed profile snapshot loaders"
```

### Task 2.2: Create Evidence Matching LangGraph Graph

**Files:**
- Create: `src/evidence_matching/__init__.py`
- Create: `src/evidence_matching/schemas.py`
- Create: `src/evidence_matching/prompts.py`
- Create: `src/evidence_matching/agents.py`
- Create: `src/evidence_matching/graph.py`

**Interfaces:**
- Produces: `EvidenceMatchingGraph` class with `arun(job_snapshot: VerifiedJobSnapshot, profile_snapshot: ConfirmedProfileSnapshot) → MatchComputationOutput`

- [ ] **Step 1: Write Pydantic schemas**

```python
# src/evidence_matching/schemas.py
from pydantic import BaseModel, Field
from typing import Literal


class RequirementAssessment(BaseModel):
    requirement_id: str = Field(description="Stable requirement ID from preprocessor")
    requirement: str = Field(description="Original job requirement text")
    job_field_path: str = Field(description="Field path in VerifiedJobSnapshot")
    profile_field_path: str | None = Field(default=None, description="Field path in ConfirmedProfileSnapshot, null for gap/unknown")
    verdict: Literal["satisfied", "gap", "unknown"]
    evidence_ids: list[str] = Field(default_factory=list)
    detail: str = Field(description="Assessment explanation")


class ReferencedRecommendation(BaseModel):
    text: str
    requirement_ids: list[str] = Field(default_factory=list)


class MatchComputationOutput(BaseModel):
    strengths: list[RequirementAssessment] = Field(default_factory=list)
    gaps: list[RequirementAssessment] = Field(default_factory=list)
    unknowns: list[RequirementAssessment] = Field(default_factory=list)
    risks: list[RequirementAssessment] = Field(default_factory=list)
    recommendation: ReferencedRecommendation


class EvidenceMatchingState(BaseModel):
    job_snapshot: dict = Field(default_factory=dict)
    profile_snapshot: dict = Field(default_factory=dict)
    job_requirements: list[dict] = Field(default_factory=list)
    assessments: list[dict] = Field(default_factory=list)
    result: MatchComputationOutput | None = None
    next_step: str = "extract_requirements"
    error: str | None = None
```

- [ ] **Step 2: Write prompts**

```python
# src/evidence_matching/prompts.py
EXTRACT_REQUIREMENTS_PROMPT = """\
You are a job requirement analyst. Extract structured requirements from the job posting.

Job Posting:
Company: {company_name}
Title: {title}
Description: {description_text}
Locations: {locations}
Industries: {industries}

Output a JSON array of requirements. Each requirement must have:
- requirement_id: a stable identifier like "req-001", "req-002"
- requirement: the original requirement text
- job_field_path: where in the job posting this comes from (e.g., "description_text", "requirements", "qualifications")

Only output the JSON array, no other text."""

MATCH_ASSESSMENT_PROMPT = """\
You are a career matching evaluator. Assess each job requirement against the candidate's profile.

Job Requirements:
{requirements_json}

Candidate Profile:
{profile_json}

Evidence References (field_path → evidence_ids):
{evidence_json}

For each requirement, output a RequirementAssessment:
- requirement_id: must match the input requirement_id
- requirement: same as input
- job_field_path: same as input
- profile_field_path: matching profile field path, or null if no match
- verdict: "satisfied", "gap", or "unknown" (use "unknown" when information is missing, never assume it's a gap)
- evidence_ids: list of evidence IDs from the profile that support this assessment (empty for unknown)
- detail: brief explanation

Then output risks (things to watch out for) and a recommendation:
- risks: each with requirement_ids list
- recommendation: text + requirement_ids list

Only output the JSON result, no other text."""
```

- [ ] **Step 3: Write agents**

```python
# src/evidence_matching/agents.py
import json
from langchain_core.messages import HumanMessage
from .schemas import RequirementAssessment, ReferencedRecommendation, MatchComputationOutput
from .prompts import EXTRACT_REQUIREMENTS_PROMPT, MATCH_ASSESSMENT_PROMPT


async def extract_requirements(state: dict, model) -> dict:
    job = state["job_snapshot"]
    prompt = EXTRACT_REQUIREMENTS_PROMPT.format(
        company_name=job.get("company_name", ""),
        title=job.get("title", ""),
        description_text=job.get("description_text", ""),
        locations=job.get("locations", []),
        industries=job.get("industries", []),
    )
    response = await model.ainvoke([HumanMessage(content=prompt)])
    try:
        requirements = json.loads(response.content)
    except json.JSONDecodeError:
        return {"error": "match_model_validation_failed", "next_step": "fail"}
    return {"job_requirements": requirements, "next_step": "assess"}


async def assess_match(state: dict, model) -> dict:
    requirements_json = json.dumps(state["job_requirements"], ensure_ascii=False)
    profile_json = json.dumps(state["profile_snapshot"]["facts"], ensure_ascii=False)
    evidence_json = json.dumps(state["profile_snapshot"].get("evidence_refs", {}), ensure_ascii=False)

    prompt = MATCH_ASSESSMENT_PROMPT.format(
        requirements_json=requirements_json,
        profile_json=profile_json,
        evidence_json=evidence_json,
    )
    response = await model.ainvoke([HumanMessage(content=prompt)])
    try:
        raw = json.loads(response.content)
    except json.JSONDecodeError:
        return {"error": "match_model_validation_failed", "next_step": "fail"}

    # Parse into structured output
    strengths = [RequirementAssessment(**a) for a in raw.get("strengths", [])]
    gaps = [RequirementAssessment(**a) for a in raw.get("gaps", [])]
    unknowns = [RequirementAssessment(**a) for a in raw.get("unknowns", [])]
    risks = [RequirementAssessment(**a) for a in raw.get("risks", [])]
    recommendation = ReferencedRecommendation(**raw.get("recommendation", {"text": "", "requirement_ids": []}))

    result = MatchComputationOutput(
        strengths=strengths,
        gaps=gaps,
        unknowns=unknowns,
        risks=risks,
        recommendation=recommendation,
    )
    return {"result": result, "next_step": "finish"}
```

- [ ] **Step 4: Write graph**

```python
# src/evidence_matching/graph.py
from langgraph.graph import StateGraph, END
from .schemas import EvidenceMatchingState
from .agents import extract_requirements, assess_match


class EvidenceMatchingGraph:
    """Production graph for evidence-based job-candidate matching.

    This graph accepts frozen snapshots and returns structured MatchComputationOutput.
    It does NOT access databases, files, or object storage.
    """

    def __init__(self, model):
        self.model = model
        self._graph = self._build()

    def _build(self):
        builder = StateGraph(EvidenceMatchingState)
        builder.add_node("extract_requirements", self._extract)
        builder.add_node("assess_match", self._assess)
        builder.add_node("fail", self._fail)
        builder.set_entry_point("extract_requirements")
        builder.add_conditional_edges("extract_requirements", self._route_after_extract)
        builder.add_conditional_edges("assess_match", self._route_after_assess)
        builder.add_edge("fail", END)
        return builder.compile()

    async def _extract(self, state: EvidenceMatchingState) -> dict:
        return await extract_requirements(state.model_dump(), self.model)

    async def _assess(self, state: EvidenceMatchingState) -> dict:
        return await assess_match(state.model_dump(), self.model)

    def _route_after_extract(self, state: EvidenceMatchingState) -> str:
        return state.next_step

    def _route_after_assess(self, state: EvidenceMatchingState) -> str:
        return state.next_step if state.next_step != "finish" else END

    async def _fail(self, state: EvidenceMatchingState) -> dict:
        return {"next_step": END}

    async def arun(self, job_snapshot: dict, profile_snapshot: dict) -> dict:
        initial = EvidenceMatchingState(
            job_snapshot=job_snapshot,
            profile_snapshot=profile_snapshot,
        )
        result = await self._graph.ainvoke(initial)
        return result
```

- [ ] **Step 5: Commit**

```bash
git add src/evidence_matching/
git commit -m "feat: add evidence matching langgraph production graph"
```

### Task 2.3: Implement Match Scoring Service

**Files:**
- Create: `backend/app/services/match_scoring.py`
- Create: `tests/unit/test_match_scoring.py`

**Interfaces:**
- Produces: `compute_score(assessments: MatchComputationOutput) → tuple[int, list[ScoreComponent], str]` (score 0-100, components, priority)

- [ ] **Step 1: Write scoring test**

```python
# tests/unit/test_match_scoring.py
from backend.app.services.match_scoring import compute_score, SCORING_RULE_VERSION


def test_perfect_match_scores_100():
    from backend.app.services.match_scoring import ScoreComponent
    output = type("obj", (), {
        "strengths": [type("a", (), {"requirement_id": "r1"})()] * 5,
        "gaps": [],
        "unknowns": [],
    })()
    score, components, priority = compute_score(output)
    assert score == 100
    assert priority == "high"
    assert sum(c.weight_basis_points for c in components) == 10000


def test_all_gaps_scores_0():
    output = type("obj", (), {
        "strengths": [],
        "gaps": [type("a", (), {"requirement_id": "r1"})()] * 5,
        "unknowns": [],
    })()
    score, components, priority = compute_score(output)
    assert score == 0
    assert priority == "not_recommended"


def test_deterministic():
    output = type("obj", (), {
        "strengths": [type("a", (), {"requirement_id": "r1"})()],
        "gaps": [type("a", (), {"requirement_id": "r2"})()],
        "unknowns": [type("a", (), {"requirement_id": "r3"})()],
    })()
    s1, _, _ = compute_score(output)
    s2, _, _ = compute_score(output)
    assert s1 == s2
```

- [ ] **Step 2: Implement scorer**

```python
# backend/app/services/match_scoring.py
from dataclasses import dataclass

SCORING_RULE_VERSION = "1.0"


@dataclass
class ScoreComponent:
    requirement_id: str
    weight_basis_points: int  # sum = 10000
    earned_basis_points: int


def compute_score(assessments) -> tuple[int, list[ScoreComponent], str]:
    """Deterministic scorer. Same input → same output."""
    total_items = len(assessments.strengths) + len(assessments.gaps) + len(assessments.unknowns)
    if total_items == 0:
        return 0, [], "not_recommended"

    weight_per_item = 10000 // total_items
    remainder = 10000 - (weight_per_item * total_items)

    components = []
    earned = 0

    for req in assessments.strengths:
        w = weight_per_item + (1 if remainder > 0 else 0)
        if remainder > 0:
            remainder -= 1
        components.append(ScoreComponent(req.requirement_id, w, w))
        earned += w

    for req in assessments.gaps:
        w = weight_per_item + (1 if remainder > 0 else 0)
        if remainder > 0:
            remainder -= 1
        components.append(ScoreComponent(req.requirement_id, w, 0))

    for req in assessments.unknowns:
        w = weight_per_item + (1 if remainder > 0 else 0)
        if remainder > 0:
            remainder -= 1
        components.append(ScoreComponent(req.requirement_id, w, 0))

    score = round((earned / 10000) * 100)

    if score >= 75:
        priority = "high"
    elif score >= 40:
        priority = "medium"
    elif score >= 15:
        priority = "low"
    else:
        priority = "not_recommended"

    return score, components, priority
```

- [ ] **Step 3: Run tests, commit**

```bash
pytest tests/unit/test_match_scoring.py -v
git add backend/app/services/match_scoring.py tests/unit/test_match_scoring.py
git commit -m "feat: add deterministic match scoring service"
```

### Task 2.4: Implement Idempotency Helper

**Files:**
- Create: `backend/app/services/idempotency.py`

- [ ] **Step 1: Implement**

```python
"""Shared idempotency key management for creation endpoints."""
import hashlib
import json
from sqlalchemy.orm import Session

IDEMPOTENCY_KEY_MAX_LENGTH = 96


def compute_request_hash(request_data: dict) -> str:
    """Stable SHA-256 hash of canonical JSON request body."""
    canonical = json.dumps(request_data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def check_idempotency(
    db: Session,
    model_class,
    user_id: str,
    idempotency_key: str,
    request_hash: str,
) -> tuple[object | None, bool]:
    """Returns (existing_record, is_duplicate).

    - No existing record → (None, False)
    - Same key + same hash → (record, True)
    - Same key + different hash → raises ValueError('idempotency_key_conflict')
    """
    existing = (
        db.query(model_class)
        .filter(
            model_class.user_id == user_id,
            model_class.request_idempotency_key == idempotency_key,
        )
        .first()
    )
    if existing is None:
        return None, False
    if existing.request_hash == request_hash:
        return existing, True
    raise ValueError("idempotency_key_conflict")
```

- [ ] **Step 2: Commit**

```bash
git add backend/app/services/idempotency.py
git commit -m "feat: add shared idempotency key helper"
```

### Task 2.5: Implement MatchService

**Files:**
- Create: `backend/app/services/match_service.py`
- Create: `tests/integration/test_match_service.py`

- [ ] **Step 1: Implement MatchService**

```python
# backend/app/services/match_service.py
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from backend.app.db.models import MatchReport, AnalysisSession
from backend.app.repositories.matches import MatchRepository
from backend.app.services.job_snapshot_service import build_verified_job_snapshot
from backend.app.services.profile_snapshot_service import build_confirmed_profile_snapshot
from backend.app.services.match_validators import validate_match_output, MatchValidationError
from backend.app.services.match_scoring import compute_score, SCORING_RULE_VERSION
from backend.app.services.idempotency import compute_request_hash, check_idempotency


MATCH_MODEL_VERSION = "1.0"
MATCH_PROMPT_VERSION = "1.0"
MATCH_OUTPUT_SCHEMA_VERSION = "1.0"
STALE_TIMEOUT_MINUTES = 10


class MatchService:
    def __init__(self, match_graph, model_version=MATCH_MODEL_VERSION):
        self.graph = match_graph
        self.model_version = model_version
        self.repo = MatchRepository()

    def create_match(
        self,
        db: Session,
        user_id: str,
        job_id: str,
        profile_version_id: str,
        idempotency_key: str,
        analysis_session_id: str | None = None,
    ) -> MatchReport:
        # 1. Resolve or create AnalysisSession
        session = self._resolve_session(db, user_id, analysis_session_id)

        # 2. Build request hash and check idempotency
        request_data = {
            "job_id": job_id,
            "profile_version_id": profile_version_id,
            "analysis_session_id": session.id,
        }
        request_hash = compute_request_hash(request_data)
        existing, is_dup = check_idempotency(db, MatchReport, user_id, idempotency_key, request_hash)
        if is_dup:
            return existing

        # 3. Load and freeze input snapshots
        try:
            job_snapshot = build_verified_job_snapshot(db, job_id)
        except ValueError as e:
            raise ValueError(str(e))  # re-raise with error_code

        try:
            profile_snapshot = build_confirmed_profile_snapshot(db, profile_version_id, user_id)
        except ValueError:
            raise ValueError("match_no_confirmed_profile")

        # 4. Create pending MatchReport
        match_id = str(uuid.uuid4())
        match_report = self.repo.create(
            db,
            id=match_id,
            user_id=user_id,
            analysis_session_id=session.id,
            job_id=job_id,
            job_verification_id="placeholder",  # TODO: load actual verification ID
            job_snapshot={
                "job_id": job_snapshot.job_id,
                "company_name": job_snapshot.company_name,
                "title": job_snapshot.title,
                "description_text": job_snapshot.description_text,
                "locations": job_snapshot.locations,
                "recruitment_types": job_snapshot.recruitment_types,
                "industries": job_snapshot.industries,
                "apply_url": job_snapshot.apply_url,
                "gui_eligible": job_snapshot.gui_eligible,
                "verified_at": str(job_snapshot.verified_at),
                "review_version": job_snapshot.review_version,
                "source_links": job_snapshot.source_links,
            },
            profile_version_id=profile_version_id,
            request_idempotency_key=idempotency_key,
            request_hash=request_hash,
            status="pending",
            scoring_rule_version=SCORING_RULE_VERSION,
            model_version=self.model_version,
            prompt_version=MATCH_PROMPT_VERSION,
            output_schema_version=MATCH_OUTPUT_SCHEMA_VERSION,
        )
        db.commit()
        report_id = match_report.id

        # 5. Call LangGraph (no DB transaction held)
        match_report = self.repo.get_by_id_raw(db, report_id)
        match_report.status = "running"
        match_report.started_at = datetime.now(timezone.utc)
        db.commit()

        try:
            raw_result = self.graph.arun_sync(
                job_snapshot=job_snapshot.__dict__,
                profile_snapshot=profile_snapshot.__dict__,
            )
        except Exception as e:
            return self.repo.finalize(db, report_id, "failed", error_code="match_execution_interrupted")

        # 6. Validate and score
        computation_output = raw_result.get("result")
        if computation_output is None:
            return self.repo.finalize(db, report_id, "failed", error_code="match_model_validation_failed")

        # Convert to dicts for validation
        output_dict = {
            "strengths": [a.model_dump() if hasattr(a, "model_dump") else a for a in computation_output.strengths],
            "gaps": [a.model_dump() if hasattr(a, "model_dump") else a for a in computation_output.gaps],
            "unknowns": [a.model_dump() if hasattr(a, "model_dump") else a for a in computation_output.unknowns],
            "risks": [a.model_dump() if hasattr(a, "model_dump") else a for a in computation_output.risks],
            "recommendation": computation_output.recommendation.model_dump() if hasattr(computation_output.recommendation, "model_dump") else computation_output.recommendation,
        }

        try:
            validated = validate_match_output(output_dict, job_snapshot, profile_snapshot)
        except MatchValidationError as e:
            return self.repo.finalize(db, report_id, "failed", error_code=e.error_code)

        # Score
        score, components, priority = compute_score(computation_output)

        return self.repo.finalize(
            db, report_id, "completed",
            score=score,
            score_components=[c.__dict__ for c in components],
            strengths=validated["strengths"],
            gaps=validated["gaps"],
            unknowns=validated["unknowns"],
            risks=validated["risks"],
            recommendation=validated["recommendation"],
            application_priority=priority,
        )

    def recover_stale(self, db: Session) -> int:
        return self.repo.recover_stale(db, timeout_minutes=STALE_TIMEOUT_MINUTES)

    def _resolve_session(self, db: Session, user_id: str, session_id: str | None) -> AnalysisSession:
        if session_id:
            session = db.query(AnalysisSession).filter(
                AnalysisSession.id == session_id,
                AnalysisSession.user_id == user_id,
            ).first()
            if session is None:
                raise ValueError("not_found")
            return session
        # Auto-create session
        sid = str(uuid.uuid4())
        session = AnalysisSession(id=sid, user_id=user_id, thread_id=sid, label="Match Session")
        db.add(session)
        db.flush()
        return session
```

- [ ] **Step 2: Implement stale recovery in MatchRepository**

Add `recover_stale` and `get_by_id_raw` (without user filter) to `backend/app/repositories/matches.py`:

```python
def get_by_id_raw(self, db: Session, match_id: str) -> MatchReport | None:
    return db.query(MatchReport).filter(MatchReport.id == match_id).first()

def finalize(self, db, match_id, status, *, score=None, score_components=None,
             strengths=None, gaps=None, unknowns=None, risks=None,
             recommendation=None, application_priority=None, error_code=None):
    report = self.get_by_id_raw(db, match_id)
    if report is None:
        raise ValueError("not_found")
    report.status = status
    report.completed_at = datetime.now(timezone.utc)
    if status == "completed":
        report.score = score
        report.score_components = score_components
        report.strengths = strengths
        report.gaps = gaps
        report.unknowns = unknowns
        report.risks = risks
        report.recommendation = recommendation
        report.application_priority = application_priority
    else:
        report.error_code = error_code
    db.commit()
    db.refresh(report)
    return report

def recover_stale(self, db: Session, timeout_minutes: int = 10) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=timeout_minutes)
    stale = (
        db.query(MatchReport)
        .filter(
            MatchReport.status.in_(["pending", "running"]),
            MatchReport.created_at < cutoff,
        )
        .all()
    )
    for r in stale:
        r.status = "failed"
        r.error_code = "match_execution_interrupted"
        r.completed_at = datetime.now(timezone.utc)
    if stale:
        db.commit()
    return len(stale)
```

- [ ] **Step 3: Commit**

```bash
git add backend/app/services/match_service.py tests/integration/test_match_service.py
git commit -m "feat: add MatchService with idempotency, validation, scoring, and stale recovery"
```

### Task 2.6: Implement Match API Route

**Files:**
- Create: `backend/app/api/match_schemas.py`
- Create: `backend/app/api/routes/matches.py`
- Modify: `backend/app/api/router.py`
- Create: `tests/api/test_matches_api.py`

- [ ] **Step 1: Write Pydantic schemas**

```python
# backend/app/api/match_schemas.py
from pydantic import BaseModel, Field
from typing import Literal


class CreateMatchRequest(BaseModel):
    job_id: str = Field(min_length=1)
    profile_version_id: str = Field(min_length=1)
    analysis_session_id: str | None = None


class RequirementAssessmentResponse(BaseModel):
    requirement_id: str
    requirement: str
    job_field_path: str
    profile_field_path: str | None
    verdict: Literal["satisfied", "gap", "unknown"]
    evidence_ids: list[str]
    detail: str


class ScoreComponentResponse(BaseModel):
    requirement_id: str
    weight_basis_points: int
    earned_basis_points: int


class MatchReportResponse(BaseModel):
    id: str
    analysis_session_id: str
    job_id: str
    profile_version_id: str
    status: Literal["pending", "running", "completed", "failed"]
    score: int | None
    score_components: list[ScoreComponentResponse] | None
    strengths: list[RequirementAssessmentResponse] | None
    gaps: list[RequirementAssessmentResponse] | None
    unknowns: list[RequirementAssessmentResponse] | None
    risks: list[RequirementAssessmentResponse] | None
    application_priority: str | None
    recommendation: dict | None
    error_code: str | None
    scoring_rule_version: str
    model_version: str
    prompt_version: str
    output_schema_version: str
    created_at: str
    started_at: str | None
    completed_at: str | None


class MatchReportListResponse(BaseModel):
    items: list[MatchReportResponse]
    total: int
```

- [ ] **Step 2: Write API route**

```python
# backend/app/api/routes/matches.py
from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.orm import Session

from backend.app.api.deps import get_db, get_current_user
from backend.app.api.match_schemas import (
    CreateMatchRequest, MatchReportResponse, MatchReportListResponse,
)
from backend.app.services.match_service import MatchService

router = APIRouter(tags=["matches"])


def get_match_service(request: Request) -> MatchService:
    return request.app.state.match_service


@router.post("/matches", response_model=MatchReportResponse, status_code=201)
def create_match(
    req: CreateMatchRequest,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    match_service: MatchService = Depends(get_match_service),
):
    try:
        report = match_service.create_match(
            db=db,
            user_id=user.id,
            job_id=req.job_id,
            profile_version_id=req.profile_version_id,
            idempotency_key=idempotency_key,
            analysis_session_id=req.analysis_session_id,
        )
    except ValueError as e:
        from fastapi import HTTPException
        code = str(e)
        if code == "not_found":
            raise HTTPException(404, detail={"code": "not_found"})
        if code == "match_not_verified_job":
            raise HTTPException(422, detail={"code": code})
        if code == "idempotency_key_conflict":
            raise HTTPException(409, detail={"code": code})
        raise

    return _to_response(report)


@router.get("/matches/{match_id}", response_model=MatchReportResponse)
def get_match(
    match_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    match_service: MatchService = Depends(get_match_service),
):
    report = match_service.repo.get_by_id(db, match_id, user.id)
    if report is None:
        from fastapi import HTTPException
        raise HTTPException(404, detail={"code": "not_found"})
    return _to_response(report)


@router.get("/matches", response_model=MatchReportListResponse)
def list_matches(
    analysis_session_id: str | None = None,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    match_service: MatchService = Depends(get_match_service),
):
    if analysis_session_id:
        items = match_service.repo.list_by_session(db, analysis_session_id, user.id)
    else:
        # list all for user, most recent first
        from backend.app.db.models import MatchReport as MR
        items = db.query(MR).filter(MR.user_id == user.id).order_by(MR.created_at.desc()).limit(50).all()
    return MatchReportListResponse(
        items=[_to_response(r) for r in items],
        total=len(items),
    )


def _to_response(report) -> MatchReportResponse:
    return MatchReportResponse(
        id=report.id,
        analysis_session_id=report.analysis_session_id,
        job_id=getattr(report, "job_id", ""),
        profile_version_id=getattr(report, "profile_version_id", ""),
        status=report.status,
        score=report.score,
        score_components=report.score_components,
        strengths=report.strengths,
        gaps=report.gaps,
        unknowns=report.unknowns,
        risks=report.risks,
        application_priority=report.application_priority,
        recommendation=report.recommendation,
        error_code=report.error_code,
        scoring_rule_version=report.scoring_rule_version,
        model_version=report.model_version,
        prompt_version=report.prompt_version,
        output_schema_version=report.output_schema_version,
        created_at=str(report.created_at),
        started_at=str(report.started_at) if report.started_at else None,
        completed_at=str(report.completed_at) if report.completed_at else None,
    )
```

- [ ] **Step 3: Wire into router**

In `backend/app/api/router.py`, add:

```python
from backend.app.api.routes.matches import router as matches_router
api_router.include_router(matches_router, prefix="/api")
```

- [ ] **Step 4: Register MatchService on app startup**

In `backend/app/main.py` (or wherever the app is created), add:

```python
from src.evidence_matching.graph import EvidenceMatchingGraph
from backend.app.services.match_service import MatchService

# During app startup:
model = ...  # existing LangChain chat model
match_graph = EvidenceMatchingGraph(model)
app.state.match_service = MatchService(match_graph)
```

- [ ] **Step 5: Write API tests**

```python
# tests/api/test_matches_api.py
import pytest
from httpx import AsyncClient


@pytest.mark.api
class TestMatchesAPI:
    async def test_create_match_requires_idempotency_key(self, client: AsyncClient, auth_headers):
        resp = await client.post("/api/matches", json={
            "job_id": "job-001",
            "profile_version_id": "pv-001",
        }, headers=auth_headers)
        assert resp.status_code == 422  # missing header

    async def test_create_match_non_verified_job(self, client: AsyncClient, auth_headers):
        resp = await client.post("/api/matches", json={
            "job_id": "non-existent",
            "profile_version_id": "pv-001",
        }, headers={**auth_headers, "Idempotency-Key": "test-key-001"})
        assert resp.status_code in (404, 422)

    async def test_create_match_idempotent(self, client: AsyncClient, auth_headers):
        key = "idem-test-001"
        body = {"job_id": "verified-job-001", "profile_version_id": "pv-001"}
        r1 = await client.post("/api/matches", json=body, headers={**auth_headers, "Idempotency-Key": key})
        r2 = await client.post("/api/matches", json=body, headers={**auth_headers, "Idempotency-Key": key})
        assert r1.status_code == r2.status_code
        if r1.status_code == 201:
            assert r1.json()["id"] == r2.json()["id"]

    async def test_cross_user_access_returns_404(self, client: AsyncClient, auth_headers, other_user_headers):
        # Create match as user A
        r1 = await client.post("/api/matches", json={
            "job_id": "verified-job-001",
            "profile_version_id": "pv-001",
        }, headers={**auth_headers, "Idempotency-Key": "cross-test-001"})
        if r1.status_code == 201:
            match_id = r1.json()["id"]
            r2 = await client.get(f"/api/matches/{match_id}", headers=other_user_headers)
            assert r2.status_code == 404
```

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/match_schemas.py backend/app/api/routes/matches.py backend/app/api/router.py tests/api/test_matches_api.py
git commit -m "feat: add /api/matches route with idempotency and authorization"
```

---

## Phase 3: ResumeDraft, Approval & Attachments

### Task 3.1: Implement ResumeDraftService

**Files:**
- Create: `backend/app/services/resume_draft_service.py`
- Create: `tests/integration/test_resume_draft_service.py`

**Interfaces:**
- Produces: `ResumeDraftService.create_draft(db, user_id, match_report_id, idempotency_key) → ResumeDraft`, `approve_draft(db, user_id, draft_id, expected_version, idempotency_key) → ApprovedResumeVersion`, `reject_draft(db, user_id, draft_id, expected_version) → ResumeDraft`
- Consumes: `DraftRepository`, `AttachmentRepository`, draft_validators, idempotency helper, field_classification

Key implementation rules from spec:
- `create_draft`: Load completed MatchReport → create `generating` draft in short transaction → call model (no DB tx held) → validate diff ops → finalize as `draft` or `failed`
- `approve_draft`: Lock owned draft with `expected_version` → create pending PDF/DOCX attachment rows → generate + encrypt + write objects (no DB tx) → verify encryption metadata → short transaction: create ApprovedResumeVersion, backfill attachment FKs, mark ready, update draft status → on any failure: compensate-delete written objects, mark attachments failed, draft stays `draft`
- `reject_draft`: Draft `expected_version` → set `status='rejected'`, `rejected_at=now`

Detailed TDD implementation follows the same pattern as MatchService (Task 2.5): write test → fail → implement → pass → commit. Key test cases (from spec 3.5):
- Completed MatchReport → draft created successfully
- Failed MatchReport → 422
- Invalid op type → DraftValidationError
- Non-existent fact_ref → failed
- approve: generates ApprovedResumeVersion + 2 attachments (PDF/DOCX)
- approve: object write fails → compensation deletes written object, draft stays `draft`
- approve: PDF succeeds, DOCX fails → both objects compensated, attachment rows failed
- approve: objects succeed, DB finalize fails → compensation, reconciliation recovers
- Duplicate approve → 409
- Same idempotency key → returns original, no duplicate model call
- Stale expected_version → 409
- Cross-user draft access → 404

### Task 3.2: Implement Attachment Service

**Files:**
- Create: `backend/app/services/attachment_service.py`
- Modify: `backend/app/services/storage.py` — add `delete(key: str)` method
- Create: `tests/integration/test_attachment_service.py`

**Interfaces:**
- Produces: `generate_and_store_attachments(db, user_id, draft_id, approved_facts, diffs) → list[ApprovedResumeAttachment]`, `compensate_attachments(db, attachment_ids) → None`, `download_attachment(db, attachment_id, user_id) → tuple[bytes, str, str]` (body, content_type, filename)
- Object key pattern: `resumes/{user_id}/{draft_id}/{attachment_id}.{format}` — deterministic, no client input

Key implementation:
```python
# backend/app/services/attachment_service.py
def generate_resume_pdf(approved_facts: dict, diffs: list) -> bytes:
    """Generate PDF from facts + diffs. Uses a simple template-based approach."""
    from jinja2 import Template
    # Build resume text from facts
    sections = []
    if "name" in approved_facts:
        sections.append(f"# {approved_facts['name']}")
    if "education" in approved_facts:
        sections.append("## Education")
        for edu in approved_facts["education"]:
            sections.append(f"- {edu.get('school', '')}: {edu.get('degree', '')}")
    # ... etc for skills, work_experience, projects, awards
    # Apply diffs
    # Render to PDF (use weasyprint or reportlab)
    return b""

def generate_resume_docx(approved_facts: dict, diffs: list) -> bytes:
    """Generate DOCX from facts + diffs."""
    from docx import Document
    doc = Document()
    # Build document from facts + diffs
    return b""
```

Storage delete method:
```python
# backend/app/services/storage.py (add to EncryptedObjectStore)
def delete(self, key: str) -> None:
    """Delete an encrypted object. Idempotent — no error if key doesn't exist."""
    try:
        self._blob_store.delete(key)
    except FileNotFoundError:
        pass  # already deleted
```

Test requirements (from spec 3.5):
- MinIO contains only AES-GCM ciphertext
- Attachment download: authorized by user ownership, response headers don't expose object_key
- Compensation deletes written objects after partial failure
- Reconciliation script: `scripts/reconcile_attachments.py` — finds pending/failed attachments without approved version, deletes orphan objects

### Task 3.3: Implement Draft API Routes

**Files:**
- Create: `backend/app/api/draft_schemas.py`
- Create: `backend/app/api/routes/resume_drafts.py`
- Modify: `backend/app/api/router.py`
- Create: `tests/api/test_resume_drafts_api.py`

**Interfaces:**
- `POST /api/resume-drafts` — requires `Idempotency-Key` header, body: `{"match_report_id": "..."}`
- `GET /api/resume-drafts/{draft_id}` — returns draft + diffs detail
- `GET /api/resume-drafts` — lists user's drafts
- `POST /api/resume-drafts/{draft_id}/approve` — requires `Idempotency-Key`, body: `{"expected_version": N}`
- `POST /api/resume-drafts/{draft_id}/reject` — body: `{"expected_version": N}`
- `GET /api/approved-resume-attachments/{attachment_id}/download` — returns decrypted file stream; no object_key in headers

Pydantic schemas:
```python
# backend/app/api/draft_schemas.py
class CreateDraftRequest(BaseModel):
    match_report_id: str

class ResumeDiffOpResponse(BaseModel):
    op: Literal["reorder", "rephrase", "summarize", "omit", "highlight"]
    section: str
    before: str | None
    after: str | None
    fact_ref: str
    evidence_ids: list[str]

class ResumeDraftResponse(BaseModel):
    id: str
    match_report_id: str
    job_title: str
    company_name: str
    diffs: list[ResumeDiffOpResponse] | None
    status: str
    error_code: str | None
    state_version: int
    created_at: str
    approved_at: str | None

class ApproveDraftRequest(BaseModel):
    expected_version: int

class RejectDraftRequest(BaseModel):
    expected_version: int

class AttachmentResponse(BaseModel):
    id: str
    format: str
    content_type: str
    plaintext_size: int

class ApprovedResumeVersionResponse(BaseModel):
    id: str
    draft_id: str
    approved_at: str
    attachments: list[AttachmentResponse]
```

Commit after all three sub-tasks pass:
```bash
git add backend/app/services/resume_draft_service.py backend/app/services/attachment_service.py backend/app/services/storage.py backend/app/api/draft_schemas.py backend/app/api/routes/resume_drafts.py backend/app/api/router.py tests/
git commit -m "feat: add resume draft service, attachment generation, and draft API routes"
```

---

## Phase 4: ApplicationSnapshot & Executor Integration

### Task 4.1: Implement TaskEligibilityService

**Files:**
- Create: `backend/app/services/task_eligibility_service.py`
- Create: `tests/unit/test_task_eligibility_service.py`

**Interfaces:**
- Produces: `check_task_eligibility(db, user_id, snapshot_id) → tuple[bool, str | None]` (can_create, reason_code)
- Shared by: `ApplicationSnapshotService.create_application_task`, `assign_and_dispatch_task`, eligibility query API

```python
# backend/app/services/task_eligibility_service.py
from sqlalchemy.orm import Session
from backend.app.db.models import ApplicationSnapshot, JobPosting, ApprovedResumeVersion, ApprovedResumeAttachment

def check_task_eligibility(db: Session, user_id: str, snapshot_id: str) -> tuple[bool, str | None]:
    """Returns (can_create_task, reason_code). Shared across create/dispatch/query."""
    snapshot = db.query(ApplicationSnapshot).filter(
        ApplicationSnapshot.id == snapshot_id,
        ApplicationSnapshot.user_id == user_id,
    ).first()
    if snapshot is None:
        return False, "not_found"

    # gui_eligible check (snapshot value)
    if not snapshot.gui_eligible:
        return False, "snapshot_gui_not_eligible"

    # Current job state check
    job = db.query(JobPosting).filter(JobPosting.id == snapshot.job_id).first()
    if job is None or job.status != "verified":
        return False, "snapshot_job_expired"
    if not job.gui_eligible:
        return False, "snapshot_gui_not_eligible"

    # Review version match
    if job.review_version != snapshot.job_review_version_at_snapshot:
        return False, "snapshot_version_stale"

    # Approved resume version available
    arv = db.query(ApprovedResumeVersion).filter(
        ApprovedResumeVersion.id == snapshot.approved_resume_version_id,
    ).first()
    if arv is None:
        return False, "snapshot_version_stale"

    # All attachments ready
    attachments = db.query(ApprovedResumeAttachment).filter(
        ApprovedResumeAttachment.approved_resume_version_id == arv.id,
    ).all()
    if any(a.status != "ready" for a in attachments):
        return False, "snapshot_version_stale"

    return True, None
```

### Task 4.2: Implement ApplicationSnapshotService

**Files:**
- Create: `backend/app/services/application_snapshot_service.py`
- Create: `tests/integration/test_application_snapshot_service.py`

**Interfaces:**
- `create_snapshot(db, user_id, job_id, approved_resume_version_id, dynamic_answers, local_sensitive_requirements, idempotency_key) → ApplicationSnapshot`
- `create_application_task(db, user_id, snapshot_id, idempotency_key, device_id=None) → ApplicationTask`

Implementation follows the dual-transaction pattern: create pending/idempotent → validate in short tx → return.

Key validations from spec 4.1:
- `dynamic_answers`: every item must have `classification: "non_sensitive"`, pass `field_classification`
- `local_sensitive_requirements`: every item's `field_key` must classify as `LOCAL_SENSITIVE`, no plaintext values
- API DTO strips `object_key`, sensitive fields, internal storage refs
- Snapshot records `gui_eligible`, `job_status`, `review_version` at creation time
- Same idempotency key → returns existing snapshot

### Task 4.3: Integrate with ApplicationService State Machine

**Files:**
- Modify: `backend/app/services/applications.py`
- Modify: `backend/app/repositories/applications.py`

Add `assign_and_dispatch_task(db, user_id, task_id, device_id, expected_version) → ApplicationTask`:
1. Load task, verify snapshot belongs to user
2. Re-check eligibility via `TaskEligibilityService`
3. Validate device: active, not expired, owned by user
4. Transition: `CREATED → SYSTEM actor → WAITING_FOR_DEVICE` (verify `state_version`, write `ApplicationEvent`)
5. Bind device, transition: `WAITING_FOR_DEVICE → SYSTEM actor → DISPATCHED` (write `ApplicationEvent`)
6. Each step uses `state_version`; no CREATED→DISPATCHED skip

### Task 4.4: Implement Snapshot API Routes

**Files:**
- Create: `backend/app/api/snapshot_schemas.py`
- Create: `backend/app/api/routes/application_snapshots.py`
- Modify: `backend/app/api/router.py`
- Create: `tests/api/test_application_snapshots_api.py`

Endpoints:
- `POST /api/application-snapshots` — `Idempotency-Key` required
- `GET /api/application-snapshots/{snapshot_id}`
- `GET /api/application-snapshots`
- `POST /api/application-snapshots/{snapshot_id}/create-task` — `Idempotency-Key` required, body: `{"device_id": "..."}` (optional)
- `GET /api/application-snapshots/{snapshot_id}/task-eligibility` — returns `{"can_create_task": bool, "reason_code": str | null}`

### Task 4.5: Executor v2 Protocol

**Files:**
- Modify: `backend/app/schemas/executor_schemas.py`
- Create: `backend/app/services/executor_v2_provider.py`
- Modify: `backend/app/api/routes/executor_tasks.py`
- Create: `tests/integration/test_executor_v2_integration.py`

Key changes:
1. Add `ExecutorTaskPayloadV2` to schemas:
```python
class ExecutorTaskPayloadV2(BaseModel):
    protocol_version: Literal["executor.v2"] = "executor.v2"
    task_id: str
    state_version: int
    snapshot_id: str
    target_url: str
    non_sensitive_fields: dict  # only fields from field_classification NON_SENSITIVE
    local_sensitive_requirements: list[dict]  # semantic refs only
    attachment_ids: list[str]
```

2. Create `SnapshotExecutorPayloadProvider` that builds v2 payloads for `task_kind=application`

3. Modify `ExecutorTaskService` to select provider by `task_kind`:
   - `simulation` → existing v1 provider (unchanged)
   - `application` → new v2 provider

4. Add attachment download endpoint with device+task+snapshot+attachment+lease validation

5. v2 payload must NOT contain: object keys, full profile snapshot, passwords, cookies, captcha, local sensitive plaintext

### Task 4.6: Commit Phase 4

```bash
git add backend/app/services/ backend/app/api/ backend/app/schemas/ backend/app/repositories/ tests/
git commit -m "feat: add application snapshot service, task eligibility, and executor v2 protocol"
```

---

## Phase 5: Frontend Router & Three Pages

### Task 5.1: Extract Auth/Session State & Add vue-router

**Files:**
- Create: `frontend/src/state/auth.ts`
- Create: `frontend/src/state/sessions.ts`
- Create: `frontend/src/router/index.ts`
- Create: `frontend/src/router/guards.ts`
- Create: `frontend/src/components/AppShell.vue`
- Modify: `frontend/src/main.ts`
- Modify: `frontend/src/App.vue`

- [ ] **Step 1: Install vue-router**

```bash
cd frontend && npm install vue-router@4
```

- [ ] **Step 2: Create auth state**

```typescript
// frontend/src/state/auth.ts
import { ref, computed } from 'vue'
import { login as apiLogin, register as apiRegister } from '../api'

interface User {
  id: string
  nickname: string
  role: 'student' | 'admin'
}

const user = ref<User | null>(null)
const token = ref<string | null>(localStorage.getItem('token'))
const loading = ref(true)

export function useAuth() {
  const isAuthenticated = computed(() => !!token.value && !!user.value)
  const isAdmin = computed(() => user.value?.role === 'admin')

  async function bootstrap() {
    const t = localStorage.getItem('token')
    if (t) {
      token.value = t
      try {
        const resp = await fetch('/api/auth/me', { headers: { Authorization: `Bearer ${t}` } })
        if (resp.ok) {
          user.value = await resp.json()
        } else {
          token.value = null
          localStorage.removeItem('token')
        }
      } catch {
        token.value = null
      }
    }
    loading.value = false
  }

  async function login(nickname: string, password: string) {
    const resp = await apiLogin(nickname, password)
    token.value = resp.token
    user.value = { id: resp.user_id, nickname: resp.nickname, role: resp.role }
    localStorage.setItem('token', resp.token)
  }

  async function register(nickname: string, password: string) {
    const resp = await apiRegister(nickname, password)
    token.value = resp.token
    user.value = { id: resp.user_id, nickname: resp.nickname, role: resp.role }
    localStorage.setItem('token', resp.token)
  }

  function logout() {
    token.value = null
    user.value = null
    localStorage.removeItem('token')
  }

  return { user, token, loading, isAuthenticated, isAdmin, bootstrap, login, register, logout }
}
```

- [ ] **Step 3: Create session state**

```typescript
// frontend/src/state/sessions.ts
import { ref } from 'vue'
import { fetchSessions, activateSession } from '../api'

export function useSessions() {
  const sessions = ref<any[]>([])
  const currentSessionId = ref<string | null>(null)

  async function load() {
    const resp = await fetchSessions()
    sessions.value = resp.sessions
  }

  async function select(id: string) {
    await activateSession(id)
    currentSessionId.value = id
  }

  return { sessions, currentSessionId, load, select }
}
```

- [ ] **Step 4: Create router**

```typescript
// frontend/src/router/index.ts
import { createRouter, createWebHistory } from 'vue-router'
import { useAuth } from '../state/auth'

const routes = [
  { path: '/', redirect: '/matching' },
  {
    path: '/matching',
    name: 'matching',
    component: () => import('../features/matching/MatchingWorkspace.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/resume-drafts/:draftId',
    name: 'resume-draft',
    component: () => import('../features/matching/ResumeDraftReview.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/jobs',
    name: 'jobs',
    component: () => import('../features/jobs/JobCenter.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/jobs/submissions',
    name: 'job-submissions',
    component: () => import('../features/job-submissions/JobSubmissions.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/profile',
    name: 'profile',
    component: () => import('../features/profile/ProfileWorkspace.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/snapshots',
    name: 'snapshots',
    component: () => import('../features/snapshots/SnapshotList.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/snapshots/:id',
    name: 'snapshot-detail',
    component: () => import('../features/snapshots/SnapshotDetail.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/devices',
    name: 'devices',
    component: () => import('../features/devices/DevicePlaceholder.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/admin/jobs',
    name: 'admin-jobs',
    component: () => import('../features/jobs/AdminJobReview.vue'),
    meta: { requiresAuth: true, requiresAdmin: true },
  },
  {
    path: '/admin/submissions',
    name: 'admin-submissions',
    component: () => import('../features/job-submissions/AdminJobSubmissions.vue'),
    meta: { requiresAuth: true, requiresAdmin: true },
  },
  {
    path: '/admin/feedbacks',
    name: 'admin-feedbacks',
    component: () => import('../features/jobs/AdminJobFeedback.vue'),
    meta: { requiresAuth: true, requiresAdmin: true },
  },
  { path: '/analysis', redirect: '/matching' },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
})

export default router
```

- [ ] **Step 5: Create guards**

```typescript
// frontend/src/router/guards.ts
import { router } from './index'
import { useAuth } from '../state/auth'

router.beforeEach(async (to, _from, next) => {
  const auth = useAuth()

  // Wait for bootstrap on first navigation
  if (auth.loading.value) {
    await new Promise<void>((resolve) => {
      const unwatch = setInterval(() => {
        if (!auth.loading.value) {
          clearInterval(unwatch)
          resolve()
        }
      }, 50)
    })
  }

  if (to.meta.requiresAuth && !auth.isAuthenticated.value) {
    next('/login')
    return
  }

  if (to.meta.requiresAdmin && !auth.isAdmin.value) {
    next('/')
    return
  }

  next()
})
```

- [ ] **Step 6: Create AppShell**

```vue
<!-- frontend/src/components/AppShell.vue -->
<template>
  <div class="app-shell">
    <nav class="shell-nav">
      <router-link to="/matching">Match</router-link>
      <router-link to="/jobs">Jobs</router-link>
      <router-link to="/profile">Profile</router-link>
      <router-link to="/snapshots">Snapshots</router-link>
      <router-link to="/devices">Devices</router-link>
      <template v-if="isAdmin">
        <router-link to="/admin/jobs">Admin Jobs</router-link>
        <router-link to="/admin/submissions">Admin Submissions</router-link>
        <router-link to="/admin/feedbacks">Admin Feedback</router-link>
      </template>
      <span class="spacer" />
      <span v-if="user">{{ user.nickname }} ({{ user.role }})</span>
      <button @click="logout">Logout</button>
    </nav>
    <main class="shell-main">
      <router-view />
    </main>
  </div>
</template>

<script setup lang="ts">
import { useAuth } from '../state/auth'
const { user, isAdmin, logout } = useAuth()
</script>
```

- [ ] **Step 7: Update main.ts**

```typescript
// frontend/src/main.ts
import { createApp } from 'vue'
import App from './App.vue'
import router from './router'
import './router/guards'
import { useAuth } from './state/auth'
import './styles.css'

async function main() {
  const app = createApp(App)
  app.use(router)

  const auth = useAuth()
  await auth.bootstrap()

  app.mount('#app')
}

main()
```

- [ ] **Step 8: Strip App.vue**

```vue
<!-- frontend/src/App.vue -->
<template>
  <AppShell />
</template>

<script setup lang="ts">
import AppShell from './components/AppShell.vue'
</script>
```

- [ ] **Step 9: Mechanically migrate 6 existing views**

Move each existing v-if view's template+script to its own `.vue` file:
- `features/jobs/JobCenter.vue` from the `jobs` v-if block
- `features/job-submissions/JobSubmissions.vue` from `job_submissions`
- `features/profile/ProfileWorkspace.vue` from `profile`
- `features/jobs/AdminJobReview.vue` from `job_review`
- `features/job-submissions/AdminJobSubmissions.vue` from `admin_job_submissions`
- `features/jobs/AdminJobFeedback.vue` from `admin_feedbacks`

Add `onBeforeRouteLeave` dirty-change guards to each page that had workspace-switch confirmations.

- [ ] **Step 10: Verify no regression**

```bash
cd frontend && npm run dev
# Manually verify: all 6 original views render, nav works, auth guards redirect
```

- [ ] **Step 11: Commit**

```bash
git add frontend/
git commit -m "feat: add vue-router, auth/session state, AppShell, migrate 6 existing views"
```

### Task 5.2: Build Matching Workspace Page

**Files:**
- Create: `frontend/src/features/matching/MatchingWorkspace.vue`
- Create: `frontend/src/features/matching/matchingApi.ts`
- Create: `frontend/src/features/matching/matchingTypes.ts`

- [ ] **Step 1: Write API client**

```typescript
// frontend/src/features/matching/matchingApi.ts
const BASE = '/api/matches'

function idempotencyKey(): string {
  return `match-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
}

export async function createMatch(
  jobId: string,
  profileVersionId: string,
  sessionId?: string,
): Promise<MatchReportResponse> {
  const key = idempotencyKey()
  const resp = await fetch(BASE, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Idempotency-Key': key,
      Authorization: `Bearer ${localStorage.getItem('token')}`,
    },
    body: JSON.stringify({
      job_id: jobId,
      profile_version_id: profileVersionId,
      analysis_session_id: sessionId || undefined,
    }),
  })
  if (!resp.ok) throw await resp.json()
  return resp.json()
}

export async function getMatch(matchId: string): Promise<MatchReportResponse> {
  const resp = await fetch(`${BASE}/${matchId}`, {
    headers: { Authorization: `Bearer ${localStorage.getItem('token')}` },
  })
  if (!resp.ok) throw await resp.json()
  return resp.json()
}
```

- [ ] **Step 2: Write types**

```typescript
// frontend/src/features/matching/matchingTypes.ts
export interface RequirementAssessment {
  requirement_id: string
  requirement: string
  job_field_path: string
  profile_field_path: string | null
  verdict: 'satisfied' | 'gap' | 'unknown'
  evidence_ids: string[]
  detail: string
}

export interface MatchReportResponse {
  id: string
  status: 'pending' | 'running' | 'completed' | 'failed'
  score: number | null
  score_components: Array<{ requirement_id: string; weight_basis_points: number; earned_basis_points: number }> | null
  strengths: RequirementAssessment[] | null
  gaps: RequirementAssessment[] | null
  unknowns: RequirementAssessment[] | null
  risks: RequirementAssessment[] | null
  application_priority: string | null
  recommendation: { text: string; requirement_ids: string[] } | null
  error_code: string | null
  created_at: string
  completed_at: string | null
}
```

- [ ] **Step 3: Write MatchingWorkspace.vue**

Component with:
- Verified jobs dropdown (fetch from `GET /api/jobs/?status=verified`)
- Confirmed profile versions dropdown (fetch from `GET /api/profile-versions`)
- "Start Match" button → `POST /api/matches` with generated `Idempotency-Key`
- Result display: score bar, priority badge, strengths/gaps/unknowns expandable cards
- Each assessment shows evidence_ids inline
- "Generate Custom Resume" button → calls `POST /api/resume-drafts`, navigates to `/resume-drafts/:draftId`

- [ ] **Step 4: Commit**

```bash
git add frontend/src/features/matching/
git commit -m "feat: add matching workspace page with match creation and result display"
```

### Task 5.3: Build ResumeDraft Review Page

**Files:**
- Create: `frontend/src/features/matching/ResumeDraftReview.vue`
- Create: `frontend/src/features/matching/draftApi.ts`
- Create: `frontend/src/features/matching/draftTypes.ts`

- [ ] **Step 1: Write API client and types**

`draftApi.ts`: `createDraft(matchReportId)`, `getDraft(draftId)`, `approveDraft(draftId, expectedVersion)`, `rejectDraft(draftId, expectedVersion)`

`draftTypes.ts`: `ResumeDiffOp`, `ResumeDraftResponse`, `ApprovedResumeVersionResponse`

- [ ] **Step 2: Write ResumeDraftReview.vue**

Component with:
- Side-by-side diff view (original facts left, modified resume right)
- Each diff op rendered with color coding: green (highlight/add), yellow (rephrase), blue (reorder), red (omit)
- Each diff shows `fact_ref` and `evidence_ids`
- "Approve" button with `expected_version` from draft response → 409 handling with reload
- "Reject" button with `expected_version`
- After approval: show `ApprovedResumeVersion` + attachment download links
- After approval: show "Create Application Snapshot" form (collect dynamic answers classified as `non_sensitive` + local sensitive semantic references)

- [ ] **Step 3: Commit**

```bash
git add frontend/src/features/matching/ResumeDraftReview.vue frontend/src/features/matching/draftApi.ts frontend/src/features/matching/draftTypes.ts
git commit -m "feat: add resume draft review page with approve/reject and snapshot creation"
```

### Task 5.4: Build Snapshot Pages

**Files:**
- Create: `frontend/src/features/snapshots/SnapshotList.vue`
- Create: `frontend/src/features/snapshots/SnapshotDetail.vue`
- Create: `frontend/src/features/snapshots/snapshotApi.ts`
- Create: `frontend/src/features/snapshots/snapshotTypes.ts`
- Create: `frontend/src/features/devices/deviceApi.ts`
- Create: `frontend/src/features/devices/DevicePlaceholder.vue`

- [ ] **Step 1: Write snapshot API**

`snapshotApi.ts`: `createSnapshot(...)`, `getSnapshot(id)`, `listSnapshots()`, `createTask(snapshotId, deviceId?)`, `getTaskEligibility(snapshotId)`

`deviceApi.ts`: `listActiveDevices()` — read-only fetch for device selector

- [ ] **Step 2: Write SnapshotList.vue**

Component with:
- Table of user's snapshots: company, title, created_at, gui_eligible badge
- Click row → navigate to `/snapshots/:id`

- [ ] **Step 3: Write SnapshotDetail.vue**

Component with:
- Snapshot content summary (non-sensitive fields only, expandable)
- Approved resume version info + attachment download links
- Device selector (fetches active devices)
- Conditional button:
  - `gui_eligible=true` → "Create Delivery Task" button (with device selection)
  - `gui_eligible=false` → "Manual delivery only" notice
- Task eligibility status display (poll `GET .../task-eligibility`)
- Idempotency key generation for task creation

- [ ] **Step 4: Verify frontend build**

```bash
cd frontend && npx vue-tsc --noEmit && npm run build
```

- [ ] **Step 5: Commit**

```bash
git add frontend/src/features/snapshots/ frontend/src/features/devices/
git commit -m "feat: add snapshot list/detail pages and device selector"
```

---

## Phase 6: Cutover & Release Gates

### Task 6.1: Remove Old Analysis Endpoint

**Files:**
- Modify: `backend/app/api/routes/analysis.py`
- Modify: `backend/app/api/router.py`

- [ ] **Step 1: Remove `/run` endpoint**

In `backend/app/api/routes/analysis.py`, delete the `POST /api/analysis/run` function. Keep session list, activate, status, and history endpoints.

- [ ] **Step 2: Remove analysis router only if no endpoints remain; otherwise keep trimmed router**

In `backend/app/api/router.py`, remove only the `/run` route registration if analysis router still serves sessions.

- [ ] **Step 3: Verify no web path imports load_jobs**

```bash
cd backend && grep -r "load_jobs\|load_sample_resume" app/ --include="*.py"
```
Expected: no results in `app/` directory. CLI `src/main.py` references are allowed.

- [ ] **Step 4: Verify frontend doesn't call runAnalysis**

```bash
cd frontend && grep -r "runAnalysis\|/api/analysis/run" src/ --include="*.ts" --include="*.vue"
```
Expected: no results (removed in Phase 5 when old analysis form was deleted).

### Task 6.2: Run Global Test Gates

- [ ] **Step 1: Backend full regression**

```bash
cd backend && pytest -x --timeout=60 -v
```

- [ ] **Step 2: Ruff lint**

```bash
cd backend && ruff check .
```

- [ ] **Step 3: Migration round-trip**

```bash
cd backend && alembic downgrade 0004 && alembic upgrade 0008 && alembic downgrade 0004 && alembic upgrade 0008
```

- [ ] **Step 4: Frontend type check + build**

```bash
cd frontend && npx vue-tsc --noEmit && npm run build && npx vitest run
```

- [ ] **Step 5: Docker Compose smoke test**

```bash
docker compose down -v && docker compose up -d --build
# Wait for healthy
curl -f http://localhost:8000/api/health/live
curl -f http://localhost:8000/api/health/ready
# Smoke test: login → create match → create draft → approve → create snapshot
```

### Task 6.3: Security & Privacy Gates

Run each check manually or via script (`tests/security/test_wave2_privacy.py`):

- [ ] Cross-user access: student A cannot read student B's match/draft/snapshot → 404
- [ ] Admin guard: student cannot access `/admin/*` → redirect
- [ ] API responses: no `object_key`, no full resume text, no sensitive fields
- [ ] ApplicationSnapshot: no local sensitive plaintext in profile_facts or dynamic_answers
- [ ] Unclassified field → API rejects with 422
- [ ] Local-sensitive field submitted as non_sensitive → API rejects
- [ ] Log inspection: match/draft/snapshot logs contain only entity IDs + error codes
- [ ] `task:submit` scope: grep confirms no such scope exists in codebase
- [ ] Model output never directly changes profile confirmation state or job verification state
- [ ] Attachment download: authorized by user, response headers don't expose object_key
- [ ] Executor v2 attachment download: validates device token + task binding + lease + snapshot→attachment chain

### Task 6.4: End-to-End E2E Verification

Using fixtures, run the full chain:

```bash
# Script: scripts/e2e_wave2_closure.py
```

1. Load verified JobPosting + ConfirmedProfileVersion from fixtures
2. `POST /api/matches` → status=completed, all assessments have evidence refs
3. `POST /api/resume-drafts` → status=draft, each diff refs confirmed facts
4. `POST /api/resume-drafts/{id}/approve` → ApprovedResumeVersion + 2 encrypted attachments
5. `POST /api/application-snapshots` → immutable snapshot, no sensitive fields
6. `POST /api/application-snapshots/{id}/create-task` → ApplicationTask CREATED, task_kind=application
7. ApplicationService: CREATED→WAITING_FOR_DEVICE→DISPATCHED → ApplicationEvents written
8. Executor GET task → executor.v2 payload, non-sensitive fields only
9. Executor attachment download → authorized, no object_key in response

Additional E2E checks:
- [ ] Redis flush → MySQL authoritative data unaffected (match/draft/snapshot intact)
- [ ] Modify job status after snapshot → existing snapshot unchanged, new task creation returns `snapshot_job_expired`
- [ ] gui_eligible=false job → snapshot created, task creation returns `snapshot_gui_not_eligible`
- [ ] Model outputs fabricated evidence IDs → match_report status = failed
- [ ] Model timeout/crash → pending/running report recoverable via stale recovery
- [ ] PDF write succeeds but DOCX fails → compensation deletes PDF object, draft stays draft
- [ ] executor.v1 simulation pipeline still passes (no JobPosting FK requirement)
- [ ] `grep -r "load_jobs\|load_sample_resume" backend/app/` returns empty
- [ ] `grep -r "runAnalysis" frontend/src/` returns empty

### Task 6.5: Cleanup

- [ ] `data/jobs.json`: move to `src/cli/data/jobs.json` if still needed for CLI demo; update `src/main.py` import
- [ ] Verify `docker-compose.yml` has no `data/jobs.json` volume mount
- [ ] Update `docs/runbooks/platform-foundation.md` with new API endpoints and migration step
- [ ] Search-and-destroy: any remaining Web-path references to `load_jobs`, `load_sample_resume`, `runAnalysis`

### Task 6.6: Final Commit

```bash
git add -A
git commit -m "feat: wave 2 closure — remove old analysis, pass all gates, e2e verified"
```

---

## Verification Checklist (Pre-Merge)

- [ ] All pytest + Ruff pass
- [ ] All Vitest + vue-tsc + production build pass
- [ ] Migration 0004→0008→0004 round-trip passes
- [ ] Docker Compose smoke test passes
- [ ] Full E2E chain (match→draft→approve→snapshot→task→executor v2) passes
- [ ] Privacy/security gates pass
- [ ] executor.v1 simulation regression passes
- [ ] No `load_jobs`/`load_sample_resume`/`runAnalysis` in Web paths
- [ ] All idempotency-key retry scenarios pass
- [ ] Compensation/reconciliation for partial attachment failures verified
