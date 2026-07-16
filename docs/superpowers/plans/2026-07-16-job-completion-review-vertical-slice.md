# Job Completion Review Vertical Slice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将腾讯同步产生的 `pending_completion` 职位推进为可人工补全、审核、核验、拒绝和失效的权威职位，并让学生端只消费已核验职位。

**Architecture:** 保留 RawJobRecord 和来源映射作为不可变证据，在 JobPosting 上区分“最新来源候选值”和“人工确认后的规范值”。管理员通过带版本号的事务接口补全和审核；腾讯重同步不得覆盖已进入人工流程的规范字段。学生职位查询默认只返回 `verified`，管理员使用独立审核队列接口查看待处理记录。

**Tech Stack:** Python 3.13、FastAPI、Pydantic 2、SQLAlchemy 2、Alembic、MySQL 8.4、Vue 3、TypeScript、Vitest、现有 Redis/MinIO/Docker Compose 基础。

## Global Constraints

- MySQL 是职位、核验记录和审核状态的唯一权威来源。
- RawJobRecord 保持不可变，补全和审核不得覆盖原始腾讯载荷。
- 只有 `verified` 职位向学生职位中心和后续匹配模块公开。
- 扫码或邮箱直投可以核验，但必须设置 `gui_eligible=false`。
- 腾讯重同步不得覆盖 `pending_review`、`verified`、`expired` 或 `rejected` 职位的人工规范字段。
- 模型或自动补全只能生成候选值，不能直接把职位推进为 `verified`。
- API、日志和审核事件不得包含腾讯令牌、MCP trace、完整 RawJobRecord 或上游错误正文。
- 所有管理员写操作必须使用 `review_version` 实现乐观并发控制，并在同一事务写入 JobVerification。
- 本计划不实现招聘网页自动抓取、手动 JD 导入、跨来源自动合并或 GUI Agent 填写。

---

## File Structure

### 新建文件

- `alembic/versions/20260716_0004_job_completion_review.py`：扩展职位生命周期并创建不可变核验记录表。
- `backend/app/services/job_review.py`：职位补全、审核、拒绝和失效的确定性领域服务。
- `backend/app/api/job_schemas.py`：学生职位与管理员审核 API 的 Pydantic DTO。
- `tests/unit/test_job_review_service.py`：领域规则、并发版本和同步保护测试。
- `frontend/src/features/jobs/jobTypes.ts`：前端职位和审核 DTO。
- `frontend/src/features/jobs/jobsApi.ts`：职位查询和管理员审核 API。
- `frontend/src/features/jobs/JobCenter.vue`：学生已核验职位列表。
- `frontend/src/features/jobs/AdminJobReview.vue`：管理员补全和审核界面。
- `frontend/src/features/jobs/__tests__/JobCenter.spec.ts`：学生职位中心组件测试。
- `frontend/src/features/jobs/__tests__/AdminJobReview.spec.ts`：管理员审核组件测试。

### 修改文件

- `backend/app/db/models.py`：扩展 JobPostingStatus、JobPosting，并增加 JobVerification。
- `backend/app/repositories/jobs.py`：拆分学生公开查询和管理员审核查询，保护人工字段。
- `backend/app/api/routes/jobs.py`：使用统一 DTO 并增加管理员审核端点。
- `backend/app/api/routes/analysis.py`：删除与真实职位 API 冲突的遗留 `GET /jobs`。
- `backend/app/schemas.py`：删除遗留 Demo JobListResponse。
- `tests/unit/test_job_models.py`：覆盖新字段和核验实体。
- `tests/unit/test_job_repository.py`：覆盖公开可见性、审核队列和重同步保护。
- `tests/contract/test_jobs_api.py`：覆盖管理员审核和学生只读契约。
- `tests/contract/test_existing_api_contract.py`：更新 `/api/jobs` 兼容断言。
- `tests/integration/test_mysql_migration.py`：覆盖 migration 0004 往返。
- `tests/security/test_no_sensitive_logging.py`：覆盖审核请求和失败日志脱敏。
- `frontend/src/api.ts`：导出通用 `request` 函数供功能域 API 复用。
- `frontend/src/App.vue`：挂载职位中心和管理员审核入口，不继续内联职位业务代码。
- `frontend/package.json`、`frontend/package-lock.json`：增加 Vitest 和 Vue Test Utils。
- `docs/runbooks/platform-foundation.md`：增加职位审核操作、状态和并发冲突处理。

## Interfaces and Dependency Gates

| Task | Produces | Consumes | Parallel / Blocking |
| --- | --- | --- | --- |
| 1 | 数据库状态、字段、JobVerification | migration 0003 | 阻塞 Task 2–4 |
| 2 | 仓储公开查询、审核队列、同步保护 | Task 1 模型 | 与 Task 5 的前端 fixture 工作可并行 |
| 3 | JobReviewService | Task 1、Task 2 | 阻塞管理员写 API |
| 4 | 稳定 HTTP 契约 | Task 2、Task 3 | 阻塞真实前端联调；Task 5 可先用 DTO fixture |
| 5 | 学生职位中心 | Task 4 DTO | 可与 Task 3 后半段并行开发 |
| 6 | 管理员审核 UI | Task 4 DTO | 可与 Task 5 并行 |
| 7 | 真实依赖、安全和运行门禁 | Task 1–6 | 阻塞本纵向切片完成 |

---

### Task 1: Add the authoritative job review schema

**Files:**
- Create: `alembic/versions/20260716_0004_job_completion_review.py`
- Modify: `backend/app/db/models.py`
- Modify: `tests/unit/test_job_models.py`
- Modify: `tests/integration/test_mysql_migration.py`

**Interfaces:**
- Consumes: migration head `20260715_0003`; existing `JobPosting` and `User` tables.
- Produces: `JobPostingStatus.PENDING_REVIEW|VERIFIED|EXPIRED|REJECTED`; `JobPosting.review_version`; `JobVerification`.

- [ ] **Step 1: Write failing model tests for the lifecycle and verification record**

Add these assertions to `tests/unit/test_job_models.py`:

```python
from backend.app.db.models import JobPosting, JobPostingStatus, JobVerification


def test_job_posting_status_covers_review_lifecycle() -> None:
    assert {item.value for item in JobPostingStatus} == {
        "pending_completion",
        "pending_review",
        "verified",
        "expired",
        "rejected",
    }


def test_job_posting_has_review_and_source_candidate_fields() -> None:
    columns = JobPosting.__table__.columns
    assert {
        "description_text",
        "source_candidate",
        "source_changed_since_review",
        "gui_eligible",
        "review_version",
        "verified_at",
        "expired_at",
        "rejected_at",
    } <= set(columns.keys())


def test_job_verification_is_an_immutable_review_record() -> None:
    columns = JobVerification.__table__.columns
    assert {
        "job_id",
        "actor_user_id",
        "action",
        "from_status",
        "to_status",
        "review_version",
        "field_snapshot",
        "reason_code",
        "created_at",
    } <= set(columns.keys())
```

- [ ] **Step 2: Run the model tests and verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_job_models.py -q
```

Expected: FAIL because the new enum members, columns and `JobVerification` do not exist.

- [ ] **Step 3: Extend the SQLAlchemy models**

In `backend/app/db/models.py`, replace `JobPostingStatus` and add the fields/entity below:

```python
class JobPostingStatus(StrEnum):
    PENDING_COMPLETION = "pending_completion"
    PENDING_REVIEW = "pending_review"
    VERIFIED = "verified"
    EXPIRED = "expired"
    REJECTED = "rejected"


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
    description_text: Mapped[str | None] = mapped_column(Text)
    locations: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    recruitment_types: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    industries: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    apply_url: Mapped[str] = mapped_column(Text, nullable=False)
    referral_code: Mapped[str | None] = mapped_column(String(255))
    deadline_text: Mapped[str | None] = mapped_column(String(255))
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    mapper_version: Mapped[str] = mapped_column(String(40), nullable=False)
    source_candidate: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    source_changed_since_review: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    gui_eligible: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    review_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class JobVerification(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "job_verifications"
    __table_args__ = (
        Index("ix_job_verifications_job_created", "job_id", "created_at"),
    )
    job_id: Mapped[str] = mapped_column(
        ForeignKey("job_postings.id", ondelete="CASCADE"), nullable=False
    )
    actor_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    from_status: Mapped[str] = mapped_column(String(40), nullable=False)
    to_status: Mapped[str] = mapped_column(String(40), nullable=False)
    review_version: Mapped[int] = mapped_column(Integer, nullable=False)
    field_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
```

- [ ] **Step 4: Create migration 0004**

Create `alembic/versions/20260716_0004_job_completion_review.py` with an upgrade that:

```python
"""add authoritative job completion review

Revision ID: 20260716_0004
Revises: 20260715_0003
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260716_0004"
down_revision: Union[str, Sequence[str], None] = "20260715_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("job_posting_status", "job_postings", type_="check")
    op.create_check_constraint(
        "job_posting_status",
        "job_postings",
        "status IN ('pending_completion','pending_review','verified','expired','rejected')",
    )
    op.add_column("job_postings", sa.Column("description_text", sa.Text(), nullable=True))
    op.add_column(
        "job_postings",
        sa.Column(
            "source_candidate",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("(JSON_OBJECT())"),
        ),
    )
    op.add_column(
        "job_postings",
        sa.Column(
            "source_changed_since_review",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "job_postings",
        sa.Column("gui_eligible", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "job_postings",
        sa.Column("review_version", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("job_postings", sa.Column("verified_at", sa.DateTime(timezone=True)))
    op.add_column("job_postings", sa.Column("expired_at", sa.DateTime(timezone=True)))
    op.add_column("job_postings", sa.Column("rejected_at", sa.DateTime(timezone=True)))
    op.execute(
        """
        UPDATE job_postings
        SET source_candidate = JSON_OBJECT(
            'company_name', company_name,
            'title', title,
            'locations', locations,
            'recruitment_types', recruitment_types,
            'industries', industries,
            'apply_url', apply_url,
            'referral_code', referral_code,
            'deadline_text', deadline_text
        )
        """
    )
    op.alter_column("job_postings", "source_candidate", server_default=None)
    op.alter_column("job_postings", "source_changed_since_review", server_default=None)
    op.alter_column("job_postings", "gui_eligible", server_default=None)
    op.alter_column("job_postings", "review_version", server_default=None)
    op.create_table(
        "job_verifications",
        sa.Column("job_id", sa.String(36), nullable=False),
        sa.Column("actor_user_id", sa.String(36), nullable=True),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("from_status", sa.String(40), nullable=False),
        sa.Column("to_status", sa.String(40), nullable=False),
        sa.Column("review_version", sa.Integer(), nullable=False),
        sa.Column("field_snapshot", sa.JSON(), nullable=False),
        sa.Column("reason_code", sa.String(80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(36), nullable=False),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["job_id"], ["job_postings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_job_verifications_actor_user_id",
        "job_verifications",
        ["actor_user_id"],
    )
    op.create_index(
        "ix_job_verifications_job_created",
        "job_verifications",
        ["job_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("job_verifications")
    op.execute("UPDATE job_postings SET status = 'pending_completion'")
    op.drop_constraint("job_posting_status", "job_postings", type_="check")
    op.create_check_constraint(
        "job_posting_status",
        "job_postings",
        "status IN ('pending_completion')",
    )
    for name in (
        "rejected_at",
        "expired_at",
        "verified_at",
        "review_version",
        "gui_eligible",
        "source_changed_since_review",
        "source_candidate",
        "description_text",
    ):
        op.drop_column("job_postings", name)
```

Before implementing, confirm the actual MySQL check-constraint name with:

```sql
SELECT CONSTRAINT_NAME
FROM information_schema.TABLE_CONSTRAINTS
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME = 'job_postings'
  AND CONSTRAINT_TYPE = 'CHECK';
```

The expected name is `job_posting_status`; if the query returns a different name, use that exact returned name in both `drop_constraint` calls.

- [ ] **Step 5: Extend the migration integration assertion**

In `tests/integration/test_mysql_migration.py`, add `job_verifications` to the expected table set and assert:

```python
inspector = sa.inspect(connection)
job_columns = {column["name"] for column in inspector.get_columns("job_postings")}
assert {"review_version", "source_candidate", "gui_eligible"} <= job_columns
assert "job_verifications" in inspector.get_table_names()
```

- [ ] **Step 6: Run model and migration tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_job_models.py -q
.\.venv\Scripts\python.exe -m pytest tests/integration/test_mysql_migration.py -q -rs
```

Expected: model tests PASS; migration test PASS when `TEST_MYSQL_URL` is configured, otherwise one explicit skip.

- [ ] **Step 7: Commit Task 1**

```powershell
git add backend/app/db/models.py alembic/versions/20260716_0004_job_completion_review.py tests/unit/test_job_models.py tests/integration/test_mysql_migration.py
git commit -m "feat: add authoritative job review schema"
```

---

### Task 2: Protect reviewed jobs and split public/review repository reads

**Files:**
- Modify: `backend/app/repositories/jobs.py`
- Modify: `tests/unit/test_job_repository.py`
- Modify: `tests/unit/test_job_sync_service.py`

**Interfaces:**
- Consumes: Task 1 `JobPostingStatus`, `source_candidate`, `review_version`.
- Produces: `list_public_postings`, `get_public_posting`, `list_review_queue`, `get_posting_for_review`.

- [ ] **Step 1: Add failing tests for source-candidate preservation**

Add to `tests/unit/test_job_repository.py`:

```python
def test_sync_does_not_overwrite_reviewed_canonical_fields(db: Session) -> None:
    source = seeded_source(db)
    first_raw = snapshot(db, source_id=source.id, external_record_id="r1", payload_hash="a" * 64)
    posting, _ = jobs.upsert_posting(
        db, source=source, raw_record=first_raw, candidate=candidate(title="来源岗位")
    )
    posting.status = JobPostingStatus.PENDING_REVIEW
    posting.title = "人工确认岗位"
    posting.description_text = "人工补全的完整 JD"
    posting.review_version = 1
    db.flush()

    changed_raw = snapshot(db, source_id=source.id, external_record_id="r1", payload_hash="b" * 64)
    updated, action = jobs.upsert_posting(
        db, source=source, raw_record=changed_raw, candidate=candidate(title="来源新岗位")
    )

    assert action == "updated"
    assert updated.title == "人工确认岗位"
    assert updated.description_text == "人工补全的完整 JD"
    assert updated.source_candidate["title"] == "来源新岗位"
    assert updated.source_changed_since_review is True
```

Add public visibility and review queue tests:

```python
def test_public_query_only_returns_verified_jobs(db: Session) -> None:
    source = seeded_source(db)
    pending_raw = snapshot(db, source_id=source.id, external_record_id="pending", payload_hash="a" * 64)
    verified_raw = snapshot(db, source_id=source.id, external_record_id="verified", payload_hash="b" * 64)
    pending, _ = jobs.upsert_posting(db, source=source, raw_record=pending_raw, candidate=candidate())
    verified, _ = jobs.upsert_posting(db, source=source, raw_record=verified_raw, candidate=candidate())
    verified.status = JobPostingStatus.VERIFIED
    db.flush()

    total, rows = jobs.list_public_postings(
        db, limit=20, offset=0, source_key=None, company=None, recruitment_type=None
    )

    assert total == 1
    assert [posting.id for posting, _source in rows] == [verified.id]
    assert pending.id != verified.id


def test_review_queue_filters_pending_statuses(db: Session) -> None:
    source = seeded_source(db)
    raw = snapshot(db, source_id=source.id, external_record_id="review", payload_hash="c" * 64)
    posting, _ = jobs.upsert_posting(db, source=source, raw_record=raw, candidate=candidate())
    posting.status = JobPostingStatus.PENDING_REVIEW
    db.flush()

    total, rows = jobs.list_review_queue(
        db, statuses={JobPostingStatus.PENDING_REVIEW}, limit=20, offset=0
    )

    assert total == 1
    assert rows[0][0].id == posting.id
```

- [ ] **Step 2: Run the repository tests and verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_job_repository.py -q
```

Expected: FAIL because the query functions and sync-preservation behavior do not exist.

- [ ] **Step 3: Normalize every synced candidate into one JSON contract**

In `backend/app/repositories/jobs.py`, add:

```python
def candidate_payload(candidate: NormalizedJobCandidate) -> dict[str, Any]:
    return {
        "company_name": candidate.company_name,
        "title": candidate.title,
        "locations": list(candidate.locations),
        "recruitment_types": list(candidate.recruitment_types),
        "industries": list(candidate.industries),
        "apply_url": candidate.apply_url,
        "referral_code": candidate.referral_code,
        "deadline_text": candidate.deadline_text,
    }
```

Update `upsert_posting` so that every sync stores `source_candidate`, but canonical fields are only replaced while the posting remains `PENDING_COMPLETION`:

```python
payload = candidate_payload(candidate)
source_values: dict[str, Any] = {
    "raw_record_id": raw_record.id,
    "source_updated_at": candidate.source_updated_at,
    "mapper_version": source.mapper_version,
    "source_candidate": payload,
}
canonical_values: dict[str, Any] = {
    "company_name": candidate.company_name,
    "title": candidate.title,
    "locations": candidate.locations,
    "recruitment_types": candidate.recruitment_types,
    "industries": candidate.industries,
    "apply_url": candidate.apply_url,
    "referral_code": candidate.referral_code,
    "deadline_text": candidate.deadline_text,
}
if posting is None:
    posting = JobPosting(
        source_id=source.id,
        external_record_id=raw_record.external_record_id,
        status=JobPostingStatus.PENDING_COMPLETION,
        **source_values,
        **canonical_values,
    )
    db.add(posting)
    db.flush()
    return posting, "created"

for name, value in source_values.items():
    setattr(posting, name, value)
if posting.status is JobPostingStatus.PENDING_COMPLETION:
    for name, value in canonical_values.items():
        setattr(posting, name, value)
else:
    posting.source_changed_since_review = True
db.flush()
return posting, "updated"
```

- [ ] **Step 4: Split student and administrator queries**

Replace the existing `list_postings` and `get_posting` functions with:

```python
def list_public_postings(
    db: Session,
    *,
    limit: int,
    offset: int,
    source_key: str | None,
    company: str | None,
    recruitment_type: str | None,
) -> tuple[int, list[tuple[JobPosting, JobSource]]]:
    filters: list[Any] = [JobPosting.status == JobPostingStatus.VERIFIED]
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
                )
                == 1
            )
        else:
            labels = (
                func.json_each(JobPosting.recruitment_types)
                .table_valued("key", "value")
                .alias("recruitment_labels")
            )
            filters.append(
                exists(
                    select(1)
                    .select_from(labels)
                    .where(labels.c.value == recruitment_type)
                )
            )
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
    return int(total), [
        (posting, source) for posting, source in db.execute(statement)
    ]


def get_public_posting(
    db: Session, job_id: str
) -> tuple[JobPosting, JobSource] | None:
    row = db.execute(
        select(JobPosting, JobSource)
        .join(JobSource)
        .where(
            JobPosting.id == job_id,
            JobPosting.status == JobPostingStatus.VERIFIED,
        )
    ).one_or_none()
    return (row[0], row[1]) if row is not None else None
```

Add administrator reads:

```python
def list_review_queue(
    db: Session,
    *,
    statuses: set[JobPostingStatus],
    limit: int,
    offset: int,
) -> tuple[int, list[tuple[JobPosting, JobSource]]]:
    allowed = {
        JobPostingStatus.PENDING_COMPLETION,
        JobPostingStatus.PENDING_REVIEW,
        JobPostingStatus.REJECTED,
    }
    selected = statuses & allowed
    filters = [JobPosting.status.in_(selected or allowed)]
    total = db.scalar(
        select(func.count()).select_from(JobPosting).where(*filters)
    ) or 0
    statement = (
        select(JobPosting, JobSource)
        .join(JobSource)
        .where(*filters)
        .order_by(JobPosting.updated_at.asc(), JobPosting.id.asc())
        .limit(limit)
        .offset(offset)
    )
    return int(total), [
        (posting, source) for posting, source in db.execute(statement)
    ]


def get_posting_for_review(
    db: Session, job_id: str, *, lock: bool = False
) -> tuple[JobPosting, JobSource] | None:
    statement = (
        select(JobPosting, JobSource)
        .join(JobSource)
        .where(JobPosting.id == job_id)
    )
    if lock:
        statement = statement.with_for_update()
    row = db.execute(statement).one_or_none()
    return (row[0], row[1]) if row is not None else None
```

- [ ] **Step 5: Run repository and synchronization regression tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_job_repository.py tests/unit/test_job_sync_service.py -q
```

Expected: PASS, including the existing snapshot and partial-sync behavior.

- [ ] **Step 6: Commit Task 2**

```powershell
git add backend/app/repositories/jobs.py tests/unit/test_job_repository.py tests/unit/test_job_sync_service.py
git commit -m "feat: protect reviewed jobs from source sync"
```

---

### Task 3: Implement deterministic completion and review transitions

**Files:**
- Create: `backend/app/services/job_review.py`
- Create: `tests/unit/test_job_review_service.py`

**Interfaces:**
- Consumes: `jobs.get_posting_for_review(db, job_id, lock=True)` and Task 1 models.
- Produces: `JobReviewService.save_completion`, `verify`, `reject`, `expire`.

- [ ] **Step 1: Write failing service tests**

Create `tests/unit/test_job_review_service.py` with these imports and fixtures:

```python
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from backend.app.db.base import Base, utc_now
from backend.app.db.models import (
    JobPosting,
    JobPostingStatus,
    JobSource,
    JobSourceProvider,
    JobVerification,
    RawJobRecord,
    User,
    UserRole,
)
from backend.app.services.job_review import (
    IncompleteJobError,
    InvalidJobReviewTransition,
    JobCompletionInput,
    JobReviewService,
    StaleJobReviewError,
)


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture
def admin(db: Session) -> User:
    value = User(
        account="review-admin",
        nickname="Reviewer",
        password_hash="unused",
        role=UserRole.ADMIN,
    )
    db.add(value)
    db.flush()
    return value


@pytest.fixture
def pending_job(db: Session) -> JobPosting:
    source = JobSource(
        source_key="review-source",
        provider=JobSourceProvider.TENCENT_SMARTSHEET,
        name="Review Source",
        file_id="file",
        sheet_id="sheet",
        mapper_version="v1",
        enabled=True,
    )
    db.add(source)
    db.flush()
    raw = RawJobRecord(
        source_id=source.id,
        external_record_id="record-1",
        payload_hash="a" * 64,
        raw_fields=[],
        observed_at=utc_now(),
    )
    db.add(raw)
    db.flush()
    posting = JobPosting(
        source_id=source.id,
        external_record_id="record-1",
        raw_record_id=raw.id,
        status=JobPostingStatus.PENDING_COMPLETION,
        company_name="来源公司",
        title="来源岗位",
        locations=[],
        recruitment_types=[],
        industries=[],
        apply_url="https://example.com/source",
        mapper_version="v1",
        source_candidate={},
    )
    db.add(posting)
    db.flush()
    return posting


@pytest.fixture
def pending_review_job(pending_job: JobPosting, db: Session) -> JobPosting:
    pending_job.status = JobPostingStatus.PENDING_REVIEW
    pending_job.company_name = "示例科技"
    pending_job.title = "后端开发实习生"
    pending_job.description_text = "负责后端服务开发和测试。"
    pending_job.apply_url = "https://jobs.example.com/roles/1"
    pending_job.review_version = 1
    db.flush()
    return pending_job
```

Then add these behaviors:

```python
def test_save_completion_moves_job_to_pending_review(db, pending_job, admin) -> None:
    updated = JobReviewService().save_completion(
        db,
        job_id=pending_job.id,
        actor_user_id=admin.id,
        expected_version=0,
        values=JobCompletionInput(
            company_name="示例科技",
            title="后端开发实习生",
            description_text="负责后端服务开发和测试。",
            locations=["上海"],
            recruitment_types=["实习"],
            industries=["软件"],
            apply_url="https://jobs.example.com/roles/1",
            referral_code=None,
            deadline_text="2026-09-01",
        ),
    )
    assert updated.status is JobPostingStatus.PENDING_REVIEW
    assert updated.review_version == 1
    assert db.scalar(select(func.count()).select_from(JobVerification)) == 1


def test_verify_requires_complete_jd_and_valid_url(db, pending_review_job, admin) -> None:
    pending_review_job.description_text = None
    with pytest.raises(IncompleteJobError):
        JobReviewService().verify(
            db,
            job_id=pending_review_job.id,
            actor_user_id=admin.id,
            expected_version=pending_review_job.review_version,
            gui_eligible=True,
        )


def test_stale_review_version_is_rejected(db, pending_job, admin) -> None:
    with pytest.raises(StaleJobReviewError):
        JobReviewService().reject(
            db,
            job_id=pending_job.id,
            actor_user_id=admin.id,
            expected_version=9,
            reason_code="invalid_source",
        )
```

Add these exact channel and transition tests:

```python
def test_email_application_can_be_verified_but_not_gui_eligible(
    db, pending_review_job, admin
) -> None:
    pending_review_job.apply_url = "mailto:jobs@example.com"
    verified = JobReviewService().verify(
        db,
        job_id=pending_review_job.id,
        actor_user_id=admin.id,
        expected_version=pending_review_job.review_version,
        gui_eligible=False,
    )
    assert verified.status is JobPostingStatus.VERIFIED
    assert verified.gui_eligible is False


def test_expire_only_accepts_verified_job(db, pending_job, admin) -> None:
    with pytest.raises(InvalidJobReviewTransition):
        JobReviewService().expire(
            db,
            job_id=pending_job.id,
            actor_user_id=admin.id,
            expected_version=pending_job.review_version,
            reason_code="closed_on_official_site",
        )
```

- [ ] **Step 2: Run the service tests and verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_job_review_service.py -q
```

Expected: FAIL because `JobReviewService` and its input/error types do not exist.

- [ ] **Step 3: Implement input types and validation**

Create `backend/app/services/job_review.py` with:

```python
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from backend.app.db.models import JobPosting, JobPostingStatus, JobVerification
from backend.app.repositories import jobs


class JobNotFoundError(LookupError):
    pass


class StaleJobReviewError(RuntimeError):
    pass


class InvalidJobReviewTransition(ValueError):
    pass


class IncompleteJobError(ValueError):
    pass


@dataclass(frozen=True)
class JobCompletionInput:
    company_name: str
    title: str
    description_text: str
    locations: list[str]
    recruitment_types: list[str]
    industries: list[str]
    apply_url: str
    referral_code: str | None
    deadline_text: str | None


def _normalized_text(value: str) -> str:
    return value.strip()


def _valid_application_channel(value: str, *, gui_eligible: bool) -> bool:
    parsed = urlsplit(value)
    if gui_eligible:
        return parsed.scheme in {"http", "https"} and bool(parsed.hostname)
    return (
        parsed.scheme in {"http", "https"} and bool(parsed.hostname)
    ) or parsed.scheme == "mailto"
```

- [ ] **Step 4: Implement locked transitions and immutable events**

Add `JobReviewService` using one private transition helper:

```python
class JobReviewService:
    def _locked(
        self, db: Session, *, job_id: str, expected_version: int
    ) -> JobPosting:
        row = jobs.get_posting_for_review(db, job_id, lock=True)
        if row is None:
            raise JobNotFoundError(job_id)
        posting, _source = row
        if posting.review_version != expected_version:
            raise StaleJobReviewError(job_id)
        return posting

    def _record(
        self,
        db: Session,
        *,
        posting: JobPosting,
        actor_user_id: str,
        action: str,
        from_status: JobPostingStatus,
        reason_code: str | None,
    ) -> None:
        posting.review_version += 1
        db.add(
            JobVerification(
                job_id=posting.id,
                actor_user_id=actor_user_id,
                action=action,
                from_status=from_status.value,
                to_status=posting.status.value,
                review_version=posting.review_version,
                field_snapshot={
                    "company_name": posting.company_name,
                    "title": posting.title,
                    "description_text": posting.description_text,
                    "locations": posting.locations,
                    "recruitment_types": posting.recruitment_types,
                    "industries": posting.industries,
                    "apply_url": posting.apply_url,
                    "referral_code": posting.referral_code,
                    "deadline_text": posting.deadline_text,
                    "gui_eligible": posting.gui_eligible,
                },
                reason_code=reason_code,
            )
        )
        db.flush()

    def save_completion(
        self,
        db: Session,
        *,
        job_id: str,
        actor_user_id: str,
        expected_version: int,
        values: JobCompletionInput,
    ) -> JobPosting:
        posting = self._locked(db, job_id=job_id, expected_version=expected_version)
        if posting.status not in {
            JobPostingStatus.PENDING_COMPLETION,
            JobPostingStatus.PENDING_REVIEW,
            JobPostingStatus.REJECTED,
        }:
            raise InvalidJobReviewTransition(posting.status.value)
        from_status = posting.status
        normalized = asdict(values)
        for key in ("company_name", "title", "description_text", "apply_url"):
            normalized[key] = _normalized_text(normalized[key])
            if not normalized[key]:
                raise IncompleteJobError(key)
        if not _valid_application_channel(normalized["apply_url"], gui_eligible=False):
            raise IncompleteJobError("apply_url")
        for key, value in normalized.items():
            setattr(posting, key, value)
        posting.status = JobPostingStatus.PENDING_REVIEW
        posting.source_changed_since_review = False
        self._record(
            db,
            posting=posting,
            actor_user_id=actor_user_id,
            action="completion_saved",
            from_status=from_status,
            reason_code=None,
        )
        return posting

    def verify(
        self,
        db: Session,
        *,
        job_id: str,
        actor_user_id: str,
        expected_version: int,
        gui_eligible: bool,
    ) -> JobPosting:
        posting = self._locked(db, job_id=job_id, expected_version=expected_version)
        if posting.status is not JobPostingStatus.PENDING_REVIEW:
            raise InvalidJobReviewTransition(posting.status.value)
        if not all((posting.company_name.strip(), posting.title.strip(), (posting.description_text or "").strip())):
            raise IncompleteJobError("required_fields")
        if not _valid_application_channel(posting.apply_url, gui_eligible=gui_eligible):
            raise IncompleteJobError("apply_url")
        from_status = posting.status
        posting.status = JobPostingStatus.VERIFIED
        posting.gui_eligible = gui_eligible
        posting.verified_at = datetime.now(timezone.utc)
        posting.expired_at = None
        posting.rejected_at = None
        self._record(
            db,
            posting=posting,
            actor_user_id=actor_user_id,
            action="verified",
            from_status=from_status,
            reason_code=None,
        )
        return posting

    def reject(
        self,
        db: Session,
        *,
        job_id: str,
        actor_user_id: str,
        expected_version: int,
        reason_code: str,
    ) -> JobPosting:
        posting = self._locked(db, job_id=job_id, expected_version=expected_version)
        if posting.status not in {
            JobPostingStatus.PENDING_COMPLETION,
            JobPostingStatus.PENDING_REVIEW,
        }:
            raise InvalidJobReviewTransition(posting.status.value)
        if not reason_code.strip():
            raise IncompleteJobError("reason_code")
        from_status = posting.status
        posting.status = JobPostingStatus.REJECTED
        posting.gui_eligible = False
        posting.rejected_at = datetime.now(timezone.utc)
        self._record(
            db,
            posting=posting,
            actor_user_id=actor_user_id,
            action="rejected",
            from_status=from_status,
            reason_code=reason_code.strip(),
        )
        return posting

    def expire(
        self,
        db: Session,
        *,
        job_id: str,
        actor_user_id: str,
        expected_version: int,
        reason_code: str,
    ) -> JobPosting:
        posting = self._locked(db, job_id=job_id, expected_version=expected_version)
        if posting.status is not JobPostingStatus.VERIFIED:
            raise InvalidJobReviewTransition(posting.status.value)
        from_status = posting.status
        posting.status = JobPostingStatus.EXPIRED
        posting.gui_eligible = False
        posting.expired_at = datetime.now(timezone.utc)
        self._record(
            db,
            posting=posting,
            actor_user_id=actor_user_id,
            action="expired",
            from_status=from_status,
            reason_code=reason_code.strip(),
        )
        return posting
```

- [ ] **Step 5: Run service and repository tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_job_review_service.py tests/unit/test_job_repository.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit Task 3**

```powershell
git add backend/app/services/job_review.py tests/unit/test_job_review_service.py
git commit -m "feat: add deterministic job review workflow"
```

---

### Task 4: Expose public verified jobs and administrator review APIs

**Files:**
- Create: `backend/app/api/job_schemas.py`
- Modify: `backend/app/api/routes/jobs.py`
- Modify: `backend/app/api/routes/analysis.py`
- Modify: `backend/app/schemas.py`
- Modify: `tests/contract/test_jobs_api.py`
- Modify: `tests/contract/test_existing_api_contract.py`

**Interfaces:**
- Consumes: Task 2 repository reads and Task 3 JobReviewService.
- Produces: stable student and administrator HTTP contracts.

- [ ] **Step 1: Write failing API contract tests**

Add these cases to `tests/contract/test_jobs_api.py`:

```python
def test_student_list_only_returns_verified_jobs(client, seeded) -> None:
    verified = seeded["postings"][0]
    with client.session_factory() as db:
        item = db.get(JobPosting, verified.id)
        assert item is not None
        item.status = JobPostingStatus.VERIFIED
        item.description_text = "完整 JD"
        db.commit()

    response = client.get("/api/jobs", headers=seeded["student_headers"])
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["jobs"]] == [verified.id]


def test_admin_can_save_and_verify_job(client, seeded) -> None:
    posting = seeded["postings"][0]
    saved = client.patch(
        f"/api/admin/jobs/{posting.id}/completion",
        headers=seeded["admin_headers"],
        json={
            "expected_version": 0,
            "company_name": "Acme",
            "title": "后端实习生",
            "description_text": "负责服务端功能开发。",
            "locations": ["上海"],
            "recruitment_types": ["实习"],
            "industries": ["互联网"],
            "apply_url": "https://example.com/apply/1",
            "referral_code": None,
            "deadline_text": "2026-09-01",
        },
    )
    assert saved.status_code == 200
    assert saved.json()["status"] == "pending_review"

    verified = client.post(
        f"/api/admin/jobs/{posting.id}/decision",
        headers=seeded["admin_headers"],
        json={
            "expected_version": saved.json()["review_version"],
            "decision": "verify",
            "gui_eligible": True,
            "reason_code": None,
        },
    )
    assert verified.status_code == 200
    assert verified.json()["status"] == "verified"


def test_stale_admin_review_returns_409(client, seeded) -> None:
    posting = seeded["postings"][0]
    response = client.patch(
        f"/api/admin/jobs/{posting.id}/completion",
        headers=seeded["admin_headers"],
        json={
            "expected_version": 99,
            "company_name": "Acme",
            "title": "后端实习生",
            "description_text": "完整 JD",
            "locations": ["上海"],
            "recruitment_types": ["实习"],
            "industries": [],
            "apply_url": "https://example.com/apply/1",
            "referral_code": None,
            "deadline_text": None,
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"] == {"error_code": "stale_job_review"}
```

Add the exact authorization assertions:

```python
def test_student_cannot_use_job_review_endpoints(client, seeded) -> None:
    posting = seeded["postings"][0]
    queue = client.get(
        "/api/admin/jobs/review-queue",
        headers=seeded["student_headers"],
    )
    decision = client.post(
        f"/api/admin/jobs/{posting.id}/decision",
        headers=seeded["student_headers"],
        json={
            "expected_version": 0,
            "decision": "reject",
            "gui_eligible": False,
            "reason_code": "invalid_source",
        },
    )
    assert queue.status_code == 403
    assert decision.status_code == 403


def test_anonymous_user_cannot_read_verified_jobs(client) -> None:
    assert client.get("/api/jobs").status_code == 401
```

- [ ] **Step 2: Run the contract tests and verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/contract/test_jobs_api.py tests/contract/test_existing_api_contract.py -q
```

Expected: FAIL because student filtering and administrator review routes are absent.

- [ ] **Step 3: Define shared API schemas**

Create `backend/app/api/job_schemas.py` with Pydantic models for:

```python
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from backend.app.db.models import JobPostingStatus


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
    gui_eligible: bool
    source_key: str
    source_name: str
    updated_at: datetime


class JobDetail(JobSummary):
    description_text: str
    referral_code: str | None
    verified_at: datetime | None


class JobListResponse(BaseModel):
    total: int
    jobs: list[JobSummary]


class AdminJobDetail(JobSummary):
    description_text: str | None
    referral_code: str | None
    source_candidate: dict[str, object]
    source_changed_since_review: bool
    review_version: int


class AdminJobListResponse(BaseModel):
    total: int
    jobs: list[AdminJobDetail]


class JobCompletionRequest(BaseModel):
    expected_version: int = Field(ge=0)
    company_name: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=2000)
    description_text: str = Field(min_length=1, max_length=100_000)
    locations: list[str] = Field(max_length=100)
    recruitment_types: list[str] = Field(max_length=20)
    industries: list[str] = Field(max_length=50)
    apply_url: str = Field(min_length=1, max_length=4096)
    referral_code: str | None = Field(default=None, max_length=255)
    deadline_text: str | None = Field(default=None, max_length=255)


class JobDecisionRequest(BaseModel):
    expected_version: int = Field(ge=0)
    decision: Literal["verify", "reject", "expire"]
    gui_eligible: bool = False
    reason_code: str | None = Field(default=None, max_length=80)
```

Use one datetime normalization helper and apply it to every datetime field:

```python
def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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
    gui_eligible: bool
    source_key: str
    source_name: str
    updated_at: datetime

    _normalize_updated_at = field_validator("updated_at", mode="before")(_as_utc)


class JobDetail(JobSummary):
    description_text: str
    referral_code: str | None
    verified_at: datetime | None

    _normalize_verified_at = field_validator("verified_at", mode="before")(_as_utc)
```

Import `timezone` beside `datetime`. `AdminJobDetail` inherits the `updated_at` validator from `JobSummary`.

- [ ] **Step 4: Update the jobs routes**

Modify `backend/app/api/routes/jobs.py` to:

- import DTO from `backend.app.api.job_schemas`;
- make `GET /jobs` call `list_public_postings`;
- make `GET /jobs/{job_id}` call `get_public_posting`;
- add `GET /admin/jobs/review-queue` using `require_admin`;
- add `PATCH /admin/jobs/{job_id}/completion`;
- add `POST /admin/jobs/{job_id}/decision`;
- commit only after the service call succeeds;
- rollback and return stable errors for known domain failures.

Replace the route-local serializers with these shared DTO constructors:

```python
def _job_summary(posting: JobPosting, source: JobSource) -> JobSummary:
    return JobSummary(
        id=posting.id,
        company_name=posting.company_name,
        title=posting.title,
        locations=posting.locations,
        recruitment_types=posting.recruitment_types,
        industries=posting.industries,
        apply_url=posting.apply_url,
        deadline_text=posting.deadline_text,
        status=posting.status,
        gui_eligible=posting.gui_eligible,
        source_key=source.source_key,
        source_name=source.name,
        updated_at=posting.updated_at,
    )


def _public_detail(posting: JobPosting, source: JobSource) -> JobDetail:
    if posting.description_text is None:
        raise RuntimeError("verified job is missing description_text")
    return JobDetail(
        **_job_summary(posting, source).model_dump(),
        description_text=posting.description_text,
        referral_code=posting.referral_code,
        verified_at=posting.verified_at,
    )


def _admin_detail(posting: JobPosting, source: JobSource) -> AdminJobDetail:
    return AdminJobDetail(
        **_job_summary(posting, source).model_dump(),
        description_text=posting.description_text,
        referral_code=posting.referral_code,
        source_candidate=posting.source_candidate,
        source_changed_since_review=posting.source_changed_since_review,
        review_version=posting.review_version,
    )
```

Use these exact read endpoints:

```python
@router.get("/jobs", response_model=JobListResponse)
def list_jobs(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(_get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    source_key: Annotated[str | None, Query()] = None,
    company: Annotated[str | None, Query()] = None,
    recruitment_type: Annotated[str | None, Query()] = None,
) -> JobListResponse:
    del current_user
    total, rows = jobs.list_public_postings(
        db,
        limit=limit,
        offset=offset,
        source_key=source_key,
        company=company,
        recruitment_type=recruitment_type,
    )
    return JobListResponse(
        total=total,
        jobs=[_job_summary(posting, source) for posting, source in rows],
    )


@router.get("/jobs/{job_id}", response_model=JobDetail)
def get_job(
    job_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(_get_db)],
) -> JobDetail:
    del current_user
    row = jobs.get_public_posting(db, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="职位不存在。")
    return _public_detail(*row)


@router.get("/admin/jobs/review-queue", response_model=AdminJobListResponse)
def review_queue(
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(_get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    review_status: Annotated[JobPostingStatus | None, Query()] = None,
) -> AdminJobListResponse:
    del admin
    statuses = {review_status} if review_status is not None else set()
    total, rows = jobs.list_review_queue(
        db, statuses=statuses, limit=limit, offset=offset
    )
    return AdminJobListResponse(
        total=total,
        jobs=[_admin_detail(posting, source) for posting, source in rows],
    )
```

Use this exact exception mapping:

```python
def _job_review_error(error: Exception) -> HTTPException:
    if isinstance(error, JobNotFoundError):
        return HTTPException(status_code=404, detail={"error_code": "job_not_found"})
    if isinstance(error, StaleJobReviewError):
        return HTTPException(status_code=409, detail={"error_code": "stale_job_review"})
    if isinstance(error, InvalidJobReviewTransition):
        return HTTPException(status_code=409, detail={"error_code": "invalid_job_transition"})
    if isinstance(error, IncompleteJobError):
        return HTTPException(status_code=422, detail={"error_code": "incomplete_job"})
    raise error
```

For `JobDecisionRequest`, dispatch exactly:

```python
if body.decision == "verify":
    posting = service.verify(
        db,
        job_id=job_id,
        actor_user_id=admin.id,
        expected_version=body.expected_version,
        gui_eligible=body.gui_eligible,
    )
elif body.decision == "reject":
    posting = service.reject(
        db,
        job_id=job_id,
        actor_user_id=admin.id,
        expected_version=body.expected_version,
        reason_code=body.reason_code or "unspecified_rejection",
    )
else:
    posting = service.expire(
        db,
        job_id=job_id,
        actor_user_id=admin.id,
        expected_version=body.expected_version,
        reason_code=body.reason_code or "expired",
    )
db.commit()
```

Implement the completion endpoint with a transaction boundary and stable response:

```python
@router.patch("/admin/jobs/{job_id}/completion", response_model=AdminJobDetail)
def save_job_completion(
    job_id: str,
    body: JobCompletionRequest,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(_get_db)],
) -> AdminJobDetail:
    service = JobReviewService()
    try:
        posting = service.save_completion(
            db,
            job_id=job_id,
            actor_user_id=admin.id,
            expected_version=body.expected_version,
            values=JobCompletionInput(
                company_name=body.company_name,
                title=body.title,
                description_text=body.description_text,
                locations=body.locations,
                recruitment_types=body.recruitment_types,
                industries=body.industries,
                apply_url=body.apply_url,
                referral_code=body.referral_code,
                deadline_text=body.deadline_text,
            ),
        )
        row = jobs.get_posting_for_review(db, posting.id)
        assert row is not None
        response = _admin_detail(*row)
        db.commit()
        return response
    except (
        JobNotFoundError,
        StaleJobReviewError,
        InvalidJobReviewTransition,
        IncompleteJobError,
    ) as error:
        db.rollback()
        raise _job_review_error(error) from None
```

Implement the decision endpoint as:

```python
@router.post("/admin/jobs/{job_id}/decision", response_model=AdminJobDetail)
def decide_job(
    job_id: str,
    body: JobDecisionRequest,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(_get_db)],
) -> AdminJobDetail:
    service = JobReviewService()
    try:
        if body.decision == "verify":
            posting = service.verify(
                db,
                job_id=job_id,
                actor_user_id=admin.id,
                expected_version=body.expected_version,
                gui_eligible=body.gui_eligible,
            )
        elif body.decision == "reject":
            posting = service.reject(
                db,
                job_id=job_id,
                actor_user_id=admin.id,
                expected_version=body.expected_version,
                reason_code=body.reason_code or "unspecified_rejection",
            )
        else:
            posting = service.expire(
                db,
                job_id=job_id,
                actor_user_id=admin.id,
                expected_version=body.expected_version,
                reason_code=body.reason_code or "expired",
            )
        row = jobs.get_posting_for_review(db, posting.id)
        assert row is not None
        response = _admin_detail(*row)
        db.commit()
        return response
    except (
        JobNotFoundError,
        StaleJobReviewError,
        InvalidJobReviewTransition,
        IncompleteJobError,
    ) as error:
        db.rollback()
        raise _job_review_error(error) from None
```

Build `_admin_detail` before committing so response serialization never requires a second transaction.

- [ ] **Step 5: Remove the duplicate Demo jobs route**

Delete the `@router.get("/jobs")` function and the `load_jobs` import from `backend/app/api/routes/analysis.py`. Remove `JobListResponse` from `backend/app/schemas.py` and its import from the analysis route.

Keep `load_jobs()` inside the existing analysis execution path for this slice; replacing analysis inputs belongs to the later evidence-matching plan. The only change here is removing the conflicting public HTTP route.

- [ ] **Step 6: Run API and OpenAPI regression tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/contract/test_jobs_api.py tests/contract/test_existing_api_contract.py -q
```

Expected: PASS. The generated OpenAPI document must contain exactly one `GET /api/jobs` operation.

- [ ] **Step 7: Commit Task 4**

```powershell
git add backend/app/api/job_schemas.py backend/app/api/routes/jobs.py backend/app/api/routes/analysis.py backend/app/schemas.py tests/contract/test_jobs_api.py tests/contract/test_existing_api_contract.py
git commit -m "feat: expose verified job review APIs"
```

---

### Task 5: Add the student verified-job center

**Files:**
- Create: `frontend/src/features/jobs/jobTypes.ts`
- Create: `frontend/src/features/jobs/jobsApi.ts`
- Create: `frontend/src/features/jobs/JobCenter.vue`
- Create: `frontend/src/features/jobs/__tests__/JobCenter.spec.ts`
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/App.vue`
- Modify: `frontend/package.json`
- Modify: `frontend/package-lock.json`

**Interfaces:**
- Consumes: Task 4 `GET /api/jobs` and `GET /api/jobs/{job_id}`.
- Produces: reusable student job-list component with loading, empty and error states.

- [ ] **Step 1: Install the frontend test dependencies**

Run:

```powershell
npm.cmd --prefix frontend install --save-dev vitest @vue/test-utils jsdom
```

Add scripts to `frontend/package.json`:

```json
{
  "scripts": {
    "dev": "vite",
    "build": "vite build",
    "preview": "vite preview",
    "test": "vitest run"
  }
}
```

Add this test block to `frontend/vite.config.ts`:

```typescript
test: {
  environment: "jsdom",
  globals: true,
},
```

- [ ] **Step 2: Write the failing JobCenter component test**

Create `frontend/src/features/jobs/__tests__/JobCenter.spec.ts`:

```typescript
import { flushPromises, mount } from "@vue/test-utils";
import { describe, expect, it, vi } from "vitest";
import JobCenter from "../JobCenter.vue";

vi.mock("../jobsApi", () => ({
  fetchVerifiedJobs: vi.fn().mockResolvedValue({
    total: 1,
    jobs: [{
      id: "job-1",
      company_name: "示例科技",
      title: "后端实习生",
      locations: ["上海"],
      recruitment_types: ["实习"],
      industries: ["软件"],
      apply_url: "https://example.com/jobs/1",
      deadline_text: "2026-09-01",
      status: "verified",
      gui_eligible: true,
      source_key: "source",
      source_name: "来源",
      updated_at: "2026-07-16T00:00:00Z",
    }],
  }),
}));

describe("JobCenter", () => {
  it("renders verified jobs returned by the API", async () => {
    const wrapper = mount(JobCenter, { props: { token: "student-token" } });
    await flushPromises();
    expect(wrapper.text()).toContain("示例科技");
    expect(wrapper.text()).toContain("后端实习生");
    expect(wrapper.text()).toContain("可使用辅助填写");
  });
});
```

- [ ] **Step 3: Run the test and verify it fails**

Run:

```powershell
npm.cmd --prefix frontend run test -- JobCenter.spec.ts
```

Expected: FAIL because the component and API module do not exist.

- [ ] **Step 4: Define frontend types and API calls**

Replace the current generic error branch in `frontend/src/api.ts` and export both `ApiError` and `request`:

```typescript
export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly detail: unknown,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export async function request<T>(
  path: string,
  init: RequestInit = {},
  token?: string,
): Promise<T> {
  const headers = new Headers(init.headers || {});
  if (!(init.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  const response = await fetch(`${API_BASE}${path}`, { ...init, headers });
  if (!response.ok) {
    let detail: unknown = null;
    let message = `请求失败：${response.status}`;
    try {
      const data = await response.json();
      detail = data.detail ?? data.message ?? null;
      if (typeof detail === "string") {
        message = detail;
      } else if (detail && typeof detail === "object" && "error_code" in detail) {
        message = String((detail as { error_code: unknown }).error_code);
      }
    } catch {
      detail = null;
    }
    throw new ApiError(response.status, detail, message);
  }
  return (await response.json()) as T;
}
```

Existing callers continue to receive an `Error`; feature modules can additionally inspect `status` and structured `detail`.

Then create `jobTypes.ts`:

```typescript
export type JobStatus =
  | "pending_completion"
  | "pending_review"
  | "verified"
  | "expired"
  | "rejected";

export interface JobSummary {
  id: string;
  company_name: string;
  title: string;
  locations: string[];
  recruitment_types: string[];
  industries: string[];
  apply_url: string;
  deadline_text: string | null;
  status: JobStatus;
  gui_eligible: boolean;
  source_key: string;
  source_name: string;
  updated_at: string;
}

export interface JobListResponse {
  total: number;
  jobs: JobSummary[];
}
```

Create `jobsApi.ts`:

```typescript
import { request } from "../../api";
import type { JobListResponse } from "./jobTypes";

export function fetchVerifiedJobs(token: string): Promise<JobListResponse> {
  return request<JobListResponse>("/jobs?limit=100&offset=0", {}, token);
}
```

- [ ] **Step 5: Implement JobCenter.vue**

Create `JobCenter.vue` with this complete component:

```vue
<script setup lang="ts">
import { onMounted, ref } from "vue";
import { fetchVerifiedJobs } from "./jobsApi";
import type { JobSummary } from "./jobTypes";

const props = defineProps<{ token: string }>();
const jobs = ref<JobSummary[]>([]);
const loading = ref(true);
const error = ref("");

onMounted(async () => {
  try {
    jobs.value = (await fetchVerifiedJobs(props.token)).jobs;
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : "职位加载失败。";
  } finally {
    loading.value = false;
  }
});
</script>

<template>
  <section class="panel job-center">
    <header>
      <p class="eyebrow">VERIFIED JOBS</p>
      <h2>已核验职位</h2>
      <p>这里只展示完成人工核验的具体职位。</p>
    </header>
    <p v-if="loading" role="status">正在加载职位…</p>
    <p v-else-if="error" role="alert">{{ error }}</p>
    <p v-else-if="jobs.length === 0">当前没有已核验职位。</p>
    <div v-else class="job-grid">
      <article v-for="job in jobs" :key="job.id" class="job-card">
        <div>
          <p class="job-company">{{ job.company_name }}</p>
          <h3>{{ job.title }}</h3>
        </div>
        <dl>
          <div><dt>地点</dt><dd>{{ job.locations.join("、") || "未注明" }}</dd></div>
          <div><dt>类型</dt><dd>{{ job.recruitment_types.join("、") || "未注明" }}</dd></div>
          <div><dt>截止</dt><dd>{{ job.deadline_text || "未注明" }}</dd></div>
        </dl>
        <p>{{ job.gui_eligible ? "可使用辅助填写" : "仅支持人工投递" }}</p>
        <a :href="job.apply_url" target="_blank" rel="noopener noreferrer">
          打开官方投递入口
        </a>
      </article>
    </div>
  </section>
</template>
```

This slice deliberately has no GUI-task creation button; that action is introduced only after ApplicationSnapshot exists.

- [ ] **Step 6: Mount the component from App.vue**

Import the component and add the initial view state in `frontend/src/App.vue`:

```typescript
import JobCenter from "./features/jobs/JobCenter.vue";

const workspaceView = ref<"analysis" | "jobs">("analysis");
```

Add the view switcher immediately inside the authenticated workspace:

```vue
<nav class="workspace-tabs" aria-label="工作台功能">
  <button type="button" @click="workspaceView = 'analysis'">分析工作台</button>
  <button type="button" @click="workspaceView = 'jobs'">职位中心</button>
</nav>
<JobCenter v-if="workspaceView === 'jobs'" :token="token" />
```

Immediately before the existing `<header class="workspace-header">`, open:

```vue
<div v-show="workspaceView === 'analysis'">
```

Close that `div` immediately before the existing `</main>`. This preserves the current analysis markup and local state while hiding it when the job center is active.

- [ ] **Step 7: Run component tests and production build**

Run:

```powershell
npm.cmd --prefix frontend run test -- JobCenter.spec.ts
npm.cmd --prefix frontend run build
```

Expected: component test PASS and Vite production build succeeds.

- [ ] **Step 8: Commit Task 5**

```powershell
git add frontend/package.json frontend/package-lock.json frontend/vite.config.ts frontend/src/api.ts frontend/src/App.vue frontend/src/features/jobs
git commit -m "feat: add verified job center"
```

---

### Task 6: Add the administrator completion and review UI

**Files:**
- Modify: `frontend/src/features/jobs/jobTypes.ts`
- Modify: `frontend/src/features/jobs/jobsApi.ts`
- Create: `frontend/src/features/jobs/AdminJobReview.vue`
- Create: `frontend/src/features/jobs/__tests__/AdminJobReview.spec.ts`
- Modify: `frontend/src/App.vue`

**Interfaces:**
- Consumes: Task 4 admin review queue, completion and decision APIs.
- Produces: administrator-only queue with explicit save, verify and reject actions.

- [ ] **Step 1: Write the failing administrator component test**

Create `AdminJobReview.spec.ts`:

```typescript
import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../../api";
import AdminJobReview from "../AdminJobReview.vue";
import {
  decideJob,
  fetchJobReviewQueue,
  saveJobCompletion,
} from "../jobsApi";

vi.mock("../jobsApi", () => ({
  fetchJobReviewQueue: vi.fn(),
  saveJobCompletion: vi.fn(),
  decideJob: vi.fn(),
}));

const pending = {
  id: "job-1",
  company_name: "示例科技",
  title: "来源岗位",
  description_text: null,
  locations: ["上海"],
  recruitment_types: ["实习"],
  industries: ["软件"],
  apply_url: "https://example.com/jobs/1",
  referral_code: null,
  deadline_text: null,
  status: "pending_completion" as const,
  gui_eligible: false,
  source_key: "source",
  source_name: "来源",
  updated_at: "2026-07-16T00:00:00Z",
  source_candidate: { title: "来源岗位" },
  source_changed_since_review: false,
  review_version: 0,
};

describe("AdminJobReview", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchJobReviewQueue).mockResolvedValue({ total: 1, jobs: [pending] });
    vi.mocked(saveJobCompletion).mockResolvedValue({
      ...pending,
      title: "后端实习生",
      description_text: "完整 JD",
      status: "pending_review",
      review_version: 1,
    });
  });

it("saves completion before allowing verification", async () => {
  const wrapper = mount(AdminJobReview, { props: { token: "admin-token" } });
  await flushPromises();
  await wrapper.get('[data-test="title"]').setValue("后端实习生");
  await wrapper.get('[data-test="description"]').setValue("完整 JD");
  await wrapper.get('[data-test="save-completion"]').trigger("click");
  await flushPromises();
  expect(saveJobCompletion).toHaveBeenCalledOnce();
  await wrapper.get('input[value="no"]').setValue();
  expect(wrapper.get('[data-test="verify-job"]').attributes("disabled")).toBeUndefined();
});

  it("reloads instead of overwriting a stale review", async () => {
    vi.mocked(saveJobCompletion).mockRejectedValue(
      new ApiError(409, { error_code: "stale_job_review" }, "stale_job_review"),
    );
    const wrapper = mount(AdminJobReview, { props: { token: "admin-token" } });
    await flushPromises();
    await wrapper.get('[data-test="save-completion"]').trigger("click");
    await flushPromises();
    expect(fetchJobReviewQueue).toHaveBeenCalledTimes(2);
    expect(wrapper.text()).toContain("职位已被其他审核人更新，请重新检查。");
  });
});
```

- [ ] **Step 2: Run the test and verify it fails**

Run:

```powershell
npm.cmd --prefix frontend run test -- AdminJobReview.spec.ts
```

Expected: FAIL because the component and admin API functions do not exist.

- [ ] **Step 3: Add administrator DTO and API calls**

Extend `jobTypes.ts` with:

```typescript
export interface AdminJobDetail extends JobSummary {
  description_text: string | null;
  referral_code: string | null;
  source_candidate: Record<string, unknown>;
  source_changed_since_review: boolean;
  review_version: number;
}

export interface AdminJobListResponse {
  total: number;
  jobs: AdminJobDetail[];
}

export interface JobCompletionPayload {
  expected_version: number;
  company_name: string;
  title: string;
  description_text: string;
  locations: string[];
  recruitment_types: string[];
  industries: string[];
  apply_url: string;
  referral_code: string | null;
  deadline_text: string | null;
}

export interface JobDecisionPayload {
  expected_version: number;
  decision: "verify" | "reject" | "expire";
  gui_eligible: boolean;
  reason_code: string | null;
}
```

Add to `jobsApi.ts`:

```typescript
export function fetchJobReviewQueue(token: string): Promise<AdminJobListResponse> {
  return request<AdminJobListResponse>(
    "/admin/jobs/review-queue?limit=100&offset=0",
    {},
    token,
  );
}

export function saveJobCompletion(
  token: string,
  jobId: string,
  payload: JobCompletionPayload,
): Promise<AdminJobDetail> {
  return request<AdminJobDetail>(`/admin/jobs/${jobId}/completion`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  }, token);
}

export function decideJob(
  token: string,
  jobId: string,
  payload: JobDecisionPayload,
): Promise<AdminJobDetail> {
  return request<AdminJobDetail>(`/admin/jobs/${jobId}/decision`, {
    method: "POST",
    body: JSON.stringify(payload),
  }, token);
}
```

- [ ] **Step 4: Implement the administrator workflow**

Create `AdminJobReview.vue` with this complete workflow skeleton:

```vue
<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { ApiError } from "../../api";
import { decideJob, fetchJobReviewQueue, saveJobCompletion } from "./jobsApi";
import type { AdminJobDetail, JobCompletionPayload } from "./jobTypes";

const props = defineProps<{ token: string }>();
const jobs = ref<AdminJobDetail[]>([]);
const selected = ref<AdminJobDetail | null>(null);
const message = ref("");
const error = ref("");
const guiChoice = ref<"" | "yes" | "no">("");
const rejectReason = ref("");
const form = reactive<JobCompletionPayload>({
  expected_version: 0,
  company_name: "",
  title: "",
  description_text: "",
  locations: [],
  recruitment_types: [],
  industries: [],
  apply_url: "",
  referral_code: null,
  deadline_text: null,
});

const canVerify = computed(
  () => selected.value?.status === "pending_review" && guiChoice.value !== "",
);

function selectJob(job: AdminJobDetail): void {
  selected.value = job;
  Object.assign(form, {
    expected_version: job.review_version,
    company_name: job.company_name,
    title: job.title,
    description_text: job.description_text || "",
    locations: [...job.locations],
    recruitment_types: [...job.recruitment_types],
    industries: [...job.industries],
    apply_url: job.apply_url,
    referral_code: job.referral_code,
    deadline_text: job.deadline_text,
  });
  guiChoice.value = job.status === "pending_review" ? (job.gui_eligible ? "yes" : "") : "";
  rejectReason.value = "";
}

async function load(preferredId?: string): Promise<void> {
  const response = await fetchJobReviewQueue(props.token);
  jobs.value = response.jobs;
  const next = response.jobs.find((job) => job.id === preferredId) || response.jobs[0] || null;
  if (next) selectJob(next);
  else selected.value = null;
}

async function handleError(caught: unknown): Promise<void> {
  if (caught instanceof ApiError && caught.status === 409) {
    const id = selected.value?.id;
    await load(id);
    error.value = "职位已被其他审核人更新，请重新检查。";
    return;
  }
  error.value = caught instanceof Error ? caught.message : "职位审核操作失败。";
}

async function save(): Promise<void> {
  if (!selected.value) return;
  error.value = "";
  try {
    const updated = await saveJobCompletion(props.token, selected.value.id, {
      ...form,
      expected_version: selected.value.review_version,
    });
    selectJob(updated);
    jobs.value = jobs.value.map((job) => job.id === updated.id ? updated : job);
    message.value = "补全草稿已保存。";
  } catch (caught) {
    await handleError(caught);
  }
}

async function verify(): Promise<void> {
  if (!selected.value || !canVerify.value) return;
  try {
    await decideJob(props.token, selected.value.id, {
      expected_version: selected.value.review_version,
      decision: "verify",
      gui_eligible: guiChoice.value === "yes",
      reason_code: null,
    });
    message.value = "职位已核验并发布。";
    await load();
  } catch (caught) {
    await handleError(caught);
  }
}

async function reject(): Promise<void> {
  if (!selected.value || !rejectReason.value.trim()) {
    error.value = "拒绝职位前必须填写原因。";
    return;
  }
  try {
    await decideJob(props.token, selected.value.id, {
      expected_version: selected.value.review_version,
      decision: "reject",
      gui_eligible: false,
      reason_code: rejectReason.value.trim(),
    });
    message.value = "职位记录已拒绝。";
    await load();
  } catch (caught) {
    await handleError(caught);
  }
}

onMounted(() => load().catch(handleError));
</script>

<template>
  <section class="admin-job-review">
    <aside>
      <button
        v-for="job in jobs"
        :key="job.id"
        type="button"
        @click="selectJob(job)"
      >
        {{ job.company_name }} · {{ job.title }} · {{ job.status }}
      </button>
    </aside>
    <form v-if="selected" @submit.prevent="save">
      <p v-if="selected.source_changed_since_review" role="alert">
        来源数据已变化，请对照候选值重新审核。
      </p>
      <details>
        <summary>查看最新来源候选值</summary>
        <pre>{{ JSON.stringify(selected.source_candidate, null, 2) }}</pre>
      </details>
      <label>公司<input v-model="form.company_name" data-test="company" /></label>
      <label>岗位<input v-model="form.title" data-test="title" /></label>
      <label>完整 JD<textarea v-model="form.description_text" data-test="description" /></label>
      <label>投递入口<input v-model="form.apply_url" data-test="apply-url" /></label>
      <label>截止日期<input v-model="form.deadline_text" data-test="deadline" /></label>
      <fieldset>
        <legend>是否允许 GUI 辅助填写</legend>
        <label><input v-model="guiChoice" type="radio" value="yes" />允许</label>
        <label><input v-model="guiChoice" type="radio" value="no" />仅人工投递</label>
      </fieldset>
      <label>拒绝原因<input v-model="rejectReason" data-test="reject-reason" /></label>
      <button data-test="save-completion" type="submit">保存补全草稿</button>
      <button data-test="verify-job" type="button" :disabled="!canVerify" @click="verify">
        核验并发布
      </button>
      <button data-test="reject-job" type="button" @click="reject">拒绝记录</button>
    </form>
    <p v-else>审核队列为空。</p>
    <p v-if="message" role="status">{{ message }}</p>
    <p v-if="error" role="alert">{{ error }}</p>
  </section>
</template>
```

Do not render `raw_fields`, payload hashes, MCP trace data or complete upstream responses in this component.

- [ ] **Step 5: Mount the administrator page by role**

In `frontend/src/App.vue`, extend the Task 5 view state and imports:

```typescript
import AdminJobReview from "./features/jobs/AdminJobReview.vue";

const workspaceView = ref<"analysis" | "jobs" | "job_review">("analysis");
```

Add this administrator-only button beside the existing view buttons:

```vue
<button
  v-if="profile?.role === 'admin'"
  type="button"
  @click="workspaceView = 'job_review'"
>
  职位审核
</button>
```

Render the page with:

```vue
<AdminJobReview
  v-if="workspaceView === 'job_review' && profile?.role === 'admin'"
  :token="token"
/>
```

Students do not receive the tab; backend `require_admin` remains the final authorization control.

- [ ] **Step 6: Run frontend tests and build**

Run:

```powershell
npm.cmd --prefix frontend run test
npm.cmd --prefix frontend run build
```

Expected: all frontend tests PASS and production build succeeds.

- [ ] **Step 7: Commit Task 6**

```powershell
git add frontend/src/App.vue frontend/src/features/jobs
git commit -m "feat: add administrator job review workspace"
```

---

### Task 7: Add security, MySQL and operational release gates

**Files:**
- Modify: `tests/security/test_no_sensitive_logging.py`
- Modify: `tests/integration/test_job_sync_mysql.py`
- Modify: `docs/runbooks/platform-foundation.md`

**Interfaces:**
- Consumes: all previous tasks.
- Produces: release evidence for concurrency, resync protection, privacy and operations.

- [ ] **Step 1: Add a security regression test**

Create an administrator completion request containing sentinel values in normal business fields and assert captured logs do not contain the request body, source candidate, RawJobRecord, token or MCP trace. The response may echo whitelisted canonical job fields, but must never contain:

```python
forbidden = (
    "raw_fields",
    "payload_hash",
    "external_record_id",
    "mcp_trace",
    "authorization",
    "tencent-token-sentinel",
)
for value in forbidden:
    assert value not in captured_logs.lower()
```

- [ ] **Step 2: Add a real MySQL reviewed-job resync test**

In `tests/integration/test_job_sync_mysql.py`, seed a reviewed posting, simulate a changed upstream candidate, run the sync service and assert:

```python
assert posting.title == "人工确认岗位"
assert posting.source_candidate["title"] == "来源更新岗位"
assert posting.source_changed_since_review is True
assert posting.status is JobPostingStatus.VERIFIED
```

Also run two concurrent administrator updates with the same `review_version`; exactly one must commit and the other must receive `StaleJobReviewError` or HTTP 409.

- [ ] **Step 3: Document administrator operations**

Add this operational section to `docs/runbooks/platform-foundation.md`:

```markdown
## 职位补全与核验

职位状态依次使用 `pending_completion`、`pending_review`、`verified`、
`expired` 和 `rejected`。学生职位中心只读取 `verified`；待补全、待审核、
已拒绝和已失效记录只通过管理员接口访问。

管理员在前端“职位审核”页完成以下操作：

1. 对照最新来源候选值补全公司、具体岗位、完整 JD、地点、投递入口和截止日期。
2. 保存草稿，使记录进入 `pending_review`。
3. 明确选择“允许 GUI 辅助填写”或“仅人工投递”。
4. 核验并发布，或填写稳定原因后拒绝。

每次写操作携带 `review_version`。收到 HTTP 409 和
`stale_job_review` 时必须重新加载记录，不得重复提交旧内容。腾讯来源在人工审核后
发生变化时，只更新 `source_candidate` 并标记
`source_changed_since_review=true`，不会覆盖人工确认字段。

邮箱、二维码、扫码和其他人工渠道可以被核验，但必须保持
`gui_eligible=false`。官网职位关闭后由管理员执行失效操作；已失效记录不再出现在
学生职位中心。

迁移 `20260716_0004` 增加审核状态、版本和 `job_verifications`。降级会删除核验
记录并把全部职位重置为 `pending_completion`，因此只能在确认不需要保留审核历史的
开发或恢复场景执行。
```

- [ ] **Step 4: Run the focused backend release gate**

Run:

```powershell
.\.venv\Scripts\python.exe -m ruff check backend tests
.\.venv\Scripts\python.exe -m pytest tests/unit/test_job_models.py tests/unit/test_job_repository.py tests/unit/test_job_review_service.py tests/unit/test_job_sync_service.py tests/contract/test_jobs_api.py tests/contract/test_existing_api_contract.py tests/security/test_no_sensitive_logging.py -q
.\.venv\Scripts\python.exe -m pytest tests/integration/test_mysql_migration.py tests/integration/test_job_sync_mysql.py -q -rs
```

Expected: Ruff PASS; default-environment tests PASS; MySQL tests PASS when `TEST_MYSQL_URL` is configured, otherwise only documented skips.

- [ ] **Step 5: Run the complete repository gate**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q -rs
npm.cmd --prefix frontend run test
npm.cmd --prefix frontend run build
```

Expected: all configured tests PASS, opt-in skips are listed by exact missing environment variable, and frontend tests/build PASS.

- [ ] **Step 6: Verify the running Compose stack**

After loading secrets from user-scoped environment variables without printing them, run:

```powershell
docker compose -p platform-foundation up -d --build
docker compose -p platform-foundation ps -a
Invoke-RestMethod http://127.0.0.1:8000/api/health/ready
Invoke-WebRequest http://127.0.0.1:5173/ -UseBasicParsing
```

Expected: migration container exits 0 at revision `20260716_0004`; MySQL, Redis, MinIO and Backend are healthy; readiness returns HTTP 200; frontend returns HTTP 200.

- [ ] **Step 7: Commit Task 7**

```powershell
git add tests/security/test_no_sensitive_logging.py tests/integration/test_job_sync_mysql.py docs/runbooks/platform-foundation.md
git commit -m "test: gate verified job review workflow"
```

---

## Final Review Checklist

- [ ] Every public `GET /api/jobs` query excludes pending, rejected and expired jobs.
- [ ] Every administrator write uses `review_version` and a row lock.
- [ ] Every successful write creates exactly one JobVerification in the same transaction.
- [ ] Tencent resync updates `source_candidate` without overwriting reviewed canonical fields.
- [ ] RawJobRecord remains immutable and absent from HTTP responses and logs.
- [ ] Verify requires a complete JD and a valid application channel.
- [ ] GUI eligibility is false for email, QR/scanning and other human-only channels.
- [ ] OpenAPI contains only one `GET /api/jobs` operation.
- [ ] Student and administrator frontend states handle loading, empty, error and 409 conflict cases.
- [ ] Migration 0004 upgrade/downgrade, Ruff, Python tests, frontend tests/build and Compose checks have fresh evidence.

## Handoff to Parallel Work

After Task 4 freezes the verified-job DTO, the talent-profile plan may use `JobDetail.id`, `description_text`, `verified_at` and `gui_eligible` as stable inputs for fixture-based matching tests. It must not query `source_candidate` or RawJobRecord. The evidence-matching plan remains blocked until at least one verified job and one ConfirmedProfileVersion exist in MySQL.

The next independent plan should be `2026-07-16-talent-profile-resume-lifecycle.md`. Its schema migration must revise `20260716_0004`, but its service and frontend fixture work can be developed in parallel with Tasks 5–7 of this plan after Task 1 is merged.
