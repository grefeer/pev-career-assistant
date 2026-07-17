# Job Feedback Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为学生提供仅面向已核验职位的幂等反馈入口，为管理员提供脱敏聚合、处置队列和不可变事件审计，同时保证反馈永远不能直接改变 `JobPosting` 状态。

**Architecture:** MySQL 保存可变 `JobFeedback` 聚合和只追加 `JobFeedbackEvent`；学生写操作先锁定 verified `JobPosting`，再按固定顺序锁定幂等事件与反馈行，以在 MySQL Repeatable Read 下实现网络重放和并发串行化。FastAPI 使用认证主体确定所有权、`Idempotency-Key` 防止重试重复、`expected_version` 防止陈旧写入，Vue 在职位详情内提供本人反馈面板并为管理员提供独立聚合队列；职位失效仍只能通过现有 `JobReviewService.expire()` 和 `JobVerification` 完成。

**Tech Stack:** Python 3.13、FastAPI、Pydantic 2、SQLAlchemy 2、Alembic、MySQL 8.4、Redis 8、Vue 3、TypeScript、Vitest、Docker Compose。

## Global Constraints

- 所有新增业务实体使用 36 字符 UUID；所有时间保存为 UTC，并在 API 边界规范化。
- MySQL 是 `JobFeedback`、`JobFeedbackEvent`、`JobPosting` 和 `JobVerification` 的唯一权威数据源；Redis 只用于固定窗口限流。
- 学生只能为 `JobPosting.status == verified` 的职位读取、创建、更新或撤回本人反馈；跨用户资源统一返回 404。
- 稳定反馈类别固定为 `closed`、`application_channel_unavailable`、`content_changed`、`incorrect_information`，前后端不得自行增加别名。
- 可变反馈使用整数 `version` 和请求中的 `expected_version`；事件只追加，不更新或删除。
- `user_id + job_id + category` 必须唯一；`actor_user_id + idempotency_key` 必须唯一；相同 key 与相同请求返回同一结果且不追加事件，相同 key 与不同请求返回 409。
- 每个学生和管理员写请求都必须携带 `Idempotency-Key`，合法格式为 16～128 个 `[A-Za-z0-9._:-]` 字符。
- 反馈服务、反馈仓储和反馈 API 不得写入 `JobPosting.status`、`review_version`、`verified_at`、`expired_at` 或 `JobVerification`。
- 管理员确认职位失效时，必须另行调用现有 `/api/admin/jobs/{job_id}/decision`，由 `JobReviewService.expire()` 使用行锁、`review_version` 和同事务 `JobVerification` 完成。
- 普通职位 DTO 不增加反馈、提交者身份或自由文本；学生反馈 DTO 只返回当前用户自己的记录；管理员 DTO 不返回 `user_id`、账号、昵称或幂等 key。
- 日志只记录 feedback ID、job ID、稳定 `error_code`、动作、类别、状态、版本和脱敏计数；不得记录自由文本、用户身份关联、幂等 key、令牌或完整请求体。
- 学生写限额为每用户每分钟 20 次，管理员处置限额为每管理员每分钟 60 次；Redis 不可用时写操作 fail closed 并返回 503。
- 反馈说明经首尾空白规范化后最多 1000 个 Unicode 字符；空字符串保存为 `NULL`。
- migration 固定为 `20260717_0007`，`down_revision` 固定为 `20260717_0006`；只有 `0006` 合并后才能创建或最终调整 `0007`，Alembic 始终保持单 head。
- 本计划不实现职位评分、公开评论、跨用户社交、模型自动处置、自动失效职位或新的职位审核状态。

---

## File Structure

### 工作流 D 独占的新文件

- `backend/app/domain/job_feedback.py`：稳定类别、状态、动作、迁移矩阵、文本和幂等 key 上限。
- `backend/app/repositories/job_feedback.py`：verified 职位锁、反馈锁、事件重放、本人列表和管理员聚合查询。
- `backend/app/services/job_feedback.py`：学生 upsert/withdraw、管理员 accept/resolve/reject、指纹和事件快照。
- `backend/app/api/job_feedback_schemas.py`：学生与管理员显式白名单 DTO。
- `backend/app/api/routes/job_feedback.py`：反馈 HTTP 端点、稳定错误映射和 Redis 限流。
- `alembic/versions/20260717_0007_job_feedback.py`：等待 `0006` 后创建两张反馈表、约束和索引。
- `tests/unit/test_job_feedback_contract.py`：共享枚举与常量契约。
- `tests/unit/test_job_feedback_models.py`：ORM 列、外键、唯一约束和索引。
- `tests/unit/test_job_feedback_repository.py`：所有权、verified 可见性、队列和聚合查询。
- `tests/unit/test_job_feedback_service.py`：状态转换、版本、幂等重放和职位隔离。
- `tests/contract/test_job_feedback_api.py`：认证、权限、DTO、错误码、限流和路由契约。
- `tests/integration/test_job_feedback_mysql.py`：真实 MySQL 并发幂等和行锁门禁。
- `tests/security/test_job_feedback_security.py`：日志、响应和公开职位 DTO 脱敏。
- `frontend/src/features/jobs/jobFeedbackTypes.ts`：反馈类型和请求联合类型。
- `frontend/src/features/jobs/jobFeedbackApi.ts`：GET、学生 mutation、管理员 queue/decision 客户端。
- `frontend/src/features/jobs/JobFeedbackPanel.vue`：学生本人反馈、更新、撤回和幂等重试 UI。
- `frontend/src/features/jobs/AdminJobFeedback.vue`：管理员聚合、明细和处置 UI。
- `frontend/src/features/jobs/__tests__/jobFeedbackApi.spec.ts`：header、路径和查询序列化测试。
- `frontend/src/features/jobs/__tests__/JobFeedbackPanel.spec.ts`：学生 UI 与 key 重用测试。
- `frontend/src/features/jobs/__tests__/AdminJobFeedback.spec.ts`：聚合、权限提示和处置测试。

### 串行合并的共享文件

- `backend/app/db/models.py:222-374`：导入反馈枚举并增加两个 ORM 实体；只在工作流 A、B 的模型修改合并后接线。
- `backend/app/db/__init__.py:1-25`：导出反馈实体与枚举；不改写其他工作流已增加的导出。
- `backend/app/api/router.py:3-12`：挂载 `job_feedback.router`；先 rebase 并保留 A、B、C 路由。
- `frontend/src/features/jobs/JobCenter.vue:1-123,253-273`：只在已选 verified 职位详情中挂载学生反馈面板。
- `frontend/src/App.vue:14-41,227-269,381-418`：增加管理员反馈工作台入口；先 rebase 并保留其他工作流入口。
- `tests/integration/test_mysql_migration.py:13-27,76-311`：把最终 Wave 1 head 和反馈表纳入 `0004 → 0005 → 0006 → 0007` 往返。
- `tests/unit/test_container_entrypoint.py:164-195`：Compose schema revision 断言更新为 `20260717_0007`。
- `docker-compose.yml:38-45`：migrate 服务 schema label 更新为 `20260717_0007`。
- `docs/runbooks/platform-foundation.md`：增加反馈状态、幂等重试、管理员处置、独立失效和验收命令。

## Interfaces and Dependency Gates

| Task | Produces | Consumes | Parallel / Blocking |
| --- | --- | --- | --- |
| 0 | `JobFeedbackCategory`、`JobFeedbackStatus`、`JobFeedbackAction` 和共享门禁证据 | 已批准并行规格、当前 `0004` 基线 | 可立即开始；不等待 A/B/C |
| 1 | `JobFeedback`、`JobFeedbackEvent` ORM | Task 0 | 领域分支可开发；共享模型合并按 A → B → D |
| 2 | 带行锁的反馈仓储和聚合结果类型 | Task 1 | 不依赖 migration 文件，可在 SQLite fixture 上开发 |
| 3 | `JobFeedbackService`、稳定异常、`FeedbackMutationResult` | Task 0–2 | 阻塞写 API |
| 4 | 学生本人反馈 API、DTO、限流 | Task 3 | 可与前端 fixture 开发并行 |
| 5 | 管理员聚合与处置 API、与 JobReview 的隔离证明 | Task 3–4 | 阻塞管理员前端联调 |
| 6 | migration `20260717_0007`、真实 MySQL 并发门禁 | Task 1–5、已合并 `20260717_0006` | 硬等待 B 的 `0006`；保持单 head |
| 7 | 学生反馈面板 | Task 4 DTO | 可在 API fixture 稳定后与 Task 5 并行 |
| 8 | 管理员聚合 UI 和共享入口接线 | Task 5、其他 Wave 1 共享入口已 rebase | 共享 `App.vue` 和 router 串行合并 |
| 9 | 安全、Compose、runbook 和全量发布证据 | Task 0–8 | 阻塞工作流 D 完成 |

---

### Task 0: Freeze the job-feedback shared contract

**Files:**
- Create: `backend/app/domain/job_feedback.py`
- Create: `tests/unit/test_job_feedback_contract.py`

**Interfaces:**
- Consumes: `JobPostingStatus.VERIFIED`; project UUID/UTC/version conventions; migration allocation `20260717_0007`.
- Produces: `JobFeedbackCategory`, `JobFeedbackStatus`, `JobFeedbackAction`, `FeedbackStudentAction`, `FeedbackAdminDecision`, `IDEMPOTENCY_KEY_PATTERN`, `FEEDBACK_NOTE_MAX_LENGTH`, and transition maps used verbatim by ORM, service, API and frontend.

- [ ] **Step 1: Record the parallel baseline without blocking non-migration work**

Run:

```powershell
git status --short
git log -1 --oneline
.\.venv\Scripts\python.exe -m alembic heads
Get-ChildItem alembic\versions\20260717_0006*.py -ErrorAction SilentlyContinue
```

Expected: the current branch and dirty files are visible; the head may still be `20260716_0004` while A/B are in parallel. If `0006` is absent, record “Task 6 migration gate closed” and continue Tasks 0–5 and 7; do not invent a temporary `down_revision`.

- [ ] **Step 2: Write the failing domain-contract test**

Create `tests/unit/test_job_feedback_contract.py`:

```python
from backend.app.domain.job_feedback import (
    FEEDBACK_NOTE_MAX_LENGTH,
    IDEMPOTENCY_KEY_PATTERN,
    JobFeedbackAction,
    JobFeedbackCategory,
    JobFeedbackStatus,
)


def test_feedback_contract_is_stable() -> None:
    assert {item.value for item in JobFeedbackCategory} == {
        "closed",
        "application_channel_unavailable",
        "content_changed",
        "incorrect_information",
    }
    assert {item.value for item in JobFeedbackStatus} == {
        "open",
        "accepted",
        "resolved",
        "rejected",
        "withdrawn",
    }
    assert {item.value for item in JobFeedbackAction} == {
        "submitted",
        "updated",
        "withdrawn",
        "accepted",
        "resolved",
        "rejected",
    }
    assert FEEDBACK_NOTE_MAX_LENGTH == 1000
    assert IDEMPOTENCY_KEY_PATTERN.fullmatch("feedback-key_1234")
    assert IDEMPOTENCY_KEY_PATTERN.fullmatch("short") is None
```

- [ ] **Step 3: Run the contract test and verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_job_feedback_contract.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'backend.app.domain.job_feedback'`.

- [ ] **Step 4: Add the exact domain contract**

Create `backend/app/domain/job_feedback.py`:

```python
from __future__ import annotations

from enum import StrEnum
import re


class JobFeedbackCategory(StrEnum):
    CLOSED = "closed"
    APPLICATION_CHANNEL_UNAVAILABLE = "application_channel_unavailable"
    CONTENT_CHANGED = "content_changed"
    INCORRECT_INFORMATION = "incorrect_information"


class JobFeedbackStatus(StrEnum):
    OPEN = "open"
    ACCEPTED = "accepted"
    RESOLVED = "resolved"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


class JobFeedbackAction(StrEnum):
    SUBMITTED = "submitted"
    UPDATED = "updated"
    WITHDRAWN = "withdrawn"
    ACCEPTED = "accepted"
    RESOLVED = "resolved"
    REJECTED = "rejected"


class FeedbackStudentAction(StrEnum):
    UPSERT = "upsert"
    WITHDRAW = "withdraw"


class FeedbackAdminDecision(StrEnum):
    ACCEPT = "accept"
    RESOLVE = "resolve"
    REJECT = "reject"


FEEDBACK_NOTE_MAX_LENGTH = 1000
IDEMPOTENCY_KEY_MIN_LENGTH = 16
IDEMPOTENCY_KEY_MAX_LENGTH = 128
IDEMPOTENCY_KEY_PATTERN = re.compile(r"[A-Za-z0-9._:-]{16,128}")

STUDENT_WITHDRAW_FROM = frozenset(
    {JobFeedbackStatus.OPEN, JobFeedbackStatus.ACCEPTED}
)
ADMIN_TRANSITIONS = {
    FeedbackAdminDecision.ACCEPT: (
        frozenset({JobFeedbackStatus.OPEN}),
        JobFeedbackStatus.ACCEPTED,
        JobFeedbackAction.ACCEPTED,
    ),
    FeedbackAdminDecision.RESOLVE: (
        frozenset({JobFeedbackStatus.OPEN, JobFeedbackStatus.ACCEPTED}),
        JobFeedbackStatus.RESOLVED,
        JobFeedbackAction.RESOLVED,
    ),
    FeedbackAdminDecision.REJECT: (
        frozenset({JobFeedbackStatus.OPEN, JobFeedbackStatus.ACCEPTED}),
        JobFeedbackStatus.REJECTED,
        JobFeedbackAction.REJECTED,
    ),
}
```

- [ ] **Step 5: Run the contract test**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_job_feedback_contract.py -q`

Expected: `1 passed`.

- [ ] **Step 6: Commit the frozen contract**

```powershell
git add backend/app/domain/job_feedback.py tests/unit/test_job_feedback_contract.py
git commit -m "feat: freeze job feedback domain contract"
```

### Task 1: Add authoritative feedback ORM entities

**Files:**
- Modify: `backend/app/db/models.py:222-374`
- Modify: `backend/app/db/__init__.py:1-25`
- Create: `tests/unit/test_job_feedback_models.py`

**Interfaces:**
- Consumes: Task 0 enums; existing `User.id` and `JobPosting.id` UUID strings.
- Produces: `JobFeedback` mutable aggregate and append-only `JobFeedbackEvent`; no migration is created in this task.

- [ ] **Step 1: Write failing ORM metadata tests**

Create `tests/unit/test_job_feedback_models.py`:

```python
from backend.app.db.models import JobFeedback, JobFeedbackEvent


def _unique_columns(
    model: object,
) -> set[tuple[str, str] | tuple[str, str, str]]:
    return {
        tuple(column.name for column in constraint.columns)
        for constraint in model.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }


def test_feedback_aggregate_columns_and_unique_key() -> None:
    assert set(JobFeedback.__table__.columns.keys()) >= {
        "id", "user_id", "job_id", "category", "status", "note",
        "version", "created_at", "updated_at",
    }
    assert ("user_id", "job_id", "category") in _unique_columns(JobFeedback)


def test_feedback_event_is_append_only_idempotency_record() -> None:
    assert set(JobFeedbackEvent.__table__.columns.keys()) >= {
        "id", "feedback_id", "actor_user_id", "action", "from_status",
        "to_status", "feedback_version", "redacted_snapshot",
        "idempotency_key", "created_at",
    }
    assert ("actor_user_id", "idempotency_key") in _unique_columns(JobFeedbackEvent)
```

- [ ] **Step 2: Run the model tests and verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_job_feedback_models.py -q`

Expected: collection FAIL because `JobFeedback` and `JobFeedbackEvent` do not exist.

- [ ] **Step 3: Add imports and ORM entities**

In `backend/app/db/models.py`, import the Task 0 enums and append these entities after `JobVerification`:

```python
from backend.app.domain.job_feedback import (
    JobFeedbackAction,
    JobFeedbackCategory,
    JobFeedbackStatus,
)


class JobFeedback(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "job_feedback"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "job_id", "category", name="uq_job_feedback_user_job_category"
        ),
        Index("ix_job_feedback_job_status_updated", "job_id", "status", "updated_at"),
        Index("ix_job_feedback_status_updated", "status", "updated_at"),
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    job_id: Mapped[str] = mapped_column(
        ForeignKey("job_postings.id", ondelete="CASCADE"), nullable=False
    )
    category: Mapped[JobFeedbackCategory] = mapped_column(
        Enum(JobFeedbackCategory, name="job_feedback_category", **enum_kwargs),
        nullable=False,
    )
    status: Mapped[JobFeedbackStatus] = mapped_column(
        Enum(JobFeedbackStatus, name="job_feedback_status", **enum_kwargs),
        default=JobFeedbackStatus.OPEN,
        nullable=False,
    )
    note: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class JobFeedbackEvent(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "job_feedback_events"
    __table_args__ = (
        UniqueConstraint(
            "actor_user_id",
            "idempotency_key",
            name="uq_job_feedback_events_actor_key",
        ),
        Index("ix_job_feedback_events_feedback_created", "feedback_id", "created_at"),
    )
    feedback_id: Mapped[str] = mapped_column(
        ForeignKey("job_feedback.id", ondelete="CASCADE"), nullable=False
    )
    actor_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    action: Mapped[JobFeedbackAction] = mapped_column(
        Enum(JobFeedbackAction, name="job_feedback_action", **enum_kwargs),
        nullable=False,
    )
    from_status: Mapped[str | None] = mapped_column(String(40))
    to_status: Mapped[str] = mapped_column(String(40), nullable=False)
    feedback_version: Mapped[int] = mapped_column(Integer, nullable=False)
    redacted_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
```

In `backend/app/db/__init__.py`, preserve existing and parallel-workstream exports and add:

```python
from .models import JobFeedback, JobFeedbackEvent
from backend.app.domain.job_feedback import (
    JobFeedbackAction,
    JobFeedbackCategory,
    JobFeedbackStatus,
)

__all__ += [
    "JobFeedback",
    "JobFeedbackAction",
    "JobFeedbackCategory",
    "JobFeedbackEvent",
    "JobFeedbackStatus",
]
```

- [ ] **Step 4: Run model and existing job-model tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_job_feedback_models.py tests/unit/test_job_models.py tests/unit/test_models.py -q
```

Expected: all selected tests PASS.

- [ ] **Step 5: Commit ORM entities without claiming migration completion**

```powershell
git add backend/app/db/models.py backend/app/db/__init__.py tests/unit/test_job_feedback_models.py
git commit -m "feat: add job feedback persistence models"
```

### Task 2: Build ownership-safe repository queries and aggregates

**Files:**
- Create: `backend/app/repositories/job_feedback.py`
- Create: `tests/unit/test_job_feedback_repository.py`

**Interfaces:**
- Consumes: `JobPostingStatus.VERIFIED`, `JobFeedback`, `JobFeedbackEvent`.
- Produces: `lock_verified_job()`, `lock_user_feedback()`, `lock_feedback()`, `lock_actor_event()`, `list_user_feedback()`, `list_admin_feedback()`, `aggregate_admin_feedback()`, `AdminFeedbackRow`, and `AdminFeedbackAggregate`.

- [ ] **Step 1: Write failing repository tests for ownership and public-state filtering**

Create an in-memory SQLite fixture in `tests/unit/test_job_feedback_repository.py` using `Base.metadata.create_all()`, two students, one admin, one verified posting and one pending posting. Add these focused assertions:

```python
def test_user_list_never_returns_another_users_feedback(seeded_db) -> None:
    rows = repository.list_user_feedback(
        seeded_db.session,
        user_id=seeded_db.student_one.id,
        job_id=seeded_db.verified_job.id,
    )
    assert [row.user_id for row in rows] == [seeded_db.student_one.id]


def test_verified_lock_hides_non_verified_posting(seeded_db) -> None:
    assert repository.lock_verified_job(
        seeded_db.session, seeded_db.pending_job.id
    ) is None


def test_admin_aggregate_counts_category_without_identity(seeded_db) -> None:
    aggregates = repository.aggregate_admin_feedback(seeded_db.session)
    assert [(row.category.value, row.total_count) for row in aggregates] == [
        ("closed", 2)
    ]
    assert not hasattr(aggregates[0], "user_id")
```

- [ ] **Step 2: Run repository tests and verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_job_feedback_repository.py -q`

Expected: collection FAIL because `backend.app.repositories.job_feedback` does not exist.

- [ ] **Step 3: Implement locked reads and user-owned reads**

Create `backend/app/repositories/job_feedback.py` with these exact signatures and current-read queries:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from backend.app.db.models import JobFeedback, JobFeedbackEvent, JobPosting
from backend.app.db.models import JobPostingStatus
from backend.app.domain.job_feedback import JobFeedbackCategory, JobFeedbackStatus


@dataclass(frozen=True)
class AdminFeedbackRow:
    feedback: JobFeedback
    company_name: str
    title: str
    job_status: JobPostingStatus
    job_review_version: int


@dataclass(frozen=True)
class AdminFeedbackAggregate:
    job_id: str
    company_name: str
    title: str
    category: JobFeedbackCategory
    open_count: int
    accepted_count: int
    total_count: int
    latest_updated_at: datetime


def lock_verified_job(db: Session, job_id: str) -> JobPosting | None:
    return db.scalar(
        select(JobPosting)
        .where(
            JobPosting.id == job_id,
            JobPosting.status == JobPostingStatus.VERIFIED,
        )
        .with_for_update()
    )


def lock_user_feedback(
    db: Session,
    *,
    user_id: str,
    job_id: str,
    category: JobFeedbackCategory,
) -> JobFeedback | None:
    return db.scalar(
        select(JobFeedback)
        .where(
            JobFeedback.user_id == user_id,
            JobFeedback.job_id == job_id,
            JobFeedback.category == category,
        )
        .with_for_update()
    )


def lock_feedback(db: Session, feedback_id: str) -> JobFeedback | None:
    return db.scalar(
        select(JobFeedback)
        .where(JobFeedback.id == feedback_id)
        .with_for_update()
    )


def lock_actor_event(
    db: Session, *, actor_user_id: str, idempotency_key: str
) -> JobFeedbackEvent | None:
    return db.scalar(
        select(JobFeedbackEvent)
        .where(
            JobFeedbackEvent.actor_user_id == actor_user_id,
            JobFeedbackEvent.idempotency_key == idempotency_key,
        )
        .with_for_update()
    )


def list_user_feedback(
    db: Session, *, user_id: str, job_id: str
) -> list[JobFeedback]:
    return list(
        db.scalars(
            select(JobFeedback)
            .where(JobFeedback.user_id == user_id, JobFeedback.job_id == job_id)
            .order_by(JobFeedback.category.asc())
        )
    )
```

- [ ] **Step 4: Implement paginated detail and grouped aggregate queries**

Append functions with these contracts. `list_admin_feedback()` filters only on server-side enum values, orders oldest active items first, and never joins `User`; `aggregate_admin_feedback()` groups by job and category and counts only `open`/`accepted` records:

```python
def list_admin_feedback(
    db: Session,
    *,
    status: JobFeedbackStatus | None,
    category: JobFeedbackCategory | None,
    limit: int,
    offset: int,
) -> tuple[int, list[AdminFeedbackRow]]:
    filters = []
    if status is None:
        filters.append(
            JobFeedback.status.in_(
                [JobFeedbackStatus.OPEN, JobFeedbackStatus.ACCEPTED]
            )
        )
    else:
        filters.append(JobFeedback.status == status)
    if category is not None:
        filters.append(JobFeedback.category == category)
    total = db.scalar(
        select(func.count()).select_from(JobFeedback).where(*filters)
    ) or 0
    rows = db.execute(
        select(JobFeedback, JobPosting)
        .join(JobPosting, JobPosting.id == JobFeedback.job_id)
        .where(*filters)
        .order_by(JobFeedback.updated_at.asc(), JobFeedback.id.asc())
        .limit(limit)
        .offset(offset)
    ).all()
    return int(total), [
        AdminFeedbackRow(
            feedback=feedback,
            company_name=posting.company_name,
            title=posting.title,
            job_status=posting.status,
            job_review_version=posting.review_version,
        )
        for feedback, posting in rows
    ]


def aggregate_admin_feedback(db: Session) -> list[AdminFeedbackAggregate]:
    rows = db.execute(
        select(
            JobFeedback.job_id,
            JobPosting.company_name,
            JobPosting.title,
            JobFeedback.category,
            func.sum(case((JobFeedback.status == JobFeedbackStatus.OPEN, 1), else_=0)),
            func.sum(
                case((JobFeedback.status == JobFeedbackStatus.ACCEPTED, 1), else_=0)
            ),
            func.count(JobFeedback.id),
            func.max(JobFeedback.updated_at),
        )
        .join(JobPosting, JobPosting.id == JobFeedback.job_id)
        .where(
            JobFeedback.status.in_(
                [JobFeedbackStatus.OPEN, JobFeedbackStatus.ACCEPTED]
            )
        )
        .group_by(
            JobFeedback.job_id,
            JobPosting.company_name,
            JobPosting.title,
            JobFeedback.category,
        )
        .order_by(func.count(JobFeedback.id).desc(), func.max(JobFeedback.updated_at).desc())
    ).all()
    return [
        AdminFeedbackAggregate(
            job_id=job_id,
            company_name=company_name,
            title=title,
            category=category,
            open_count=int(open_count or 0),
            accepted_count=int(accepted_count or 0),
            total_count=int(total_count),
            latest_updated_at=latest_updated_at,
        )
        for (
            job_id, company_name, title, category, open_count,
            accepted_count, total_count, latest_updated_at,
        ) in rows
    ]
```

- [ ] **Step 5: Run repository and Ruff checks**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_job_feedback_repository.py -q
.\.venv\Scripts\python.exe -m ruff check backend/app/repositories/job_feedback.py tests/unit/test_job_feedback_repository.py
```

Expected: repository tests PASS and Ruff prints `All checks passed!`.

- [ ] **Step 6: Commit repository behavior**

```powershell
git add backend/app/repositories/job_feedback.py tests/unit/test_job_feedback_repository.py
git commit -m "feat: add ownership-safe feedback repository"
```

### Task 3: Implement idempotent feedback state transitions

**Files:**
- Create: `backend/app/services/job_feedback.py`
- Create: `tests/unit/test_job_feedback_service.py`

**Interfaces:**
- Consumes: Task 0 transitions and Task 2 lock functions.
- Produces: `JobFeedbackService.mutate_student()` and `JobFeedbackService.decide_admin()` returning immutable `FeedbackMutationResult`; stable errors `FeedbackNotFoundError`, `FeedbackJobNotFoundError`, `StaleFeedbackError`, `InvalidFeedbackTransitionError`, and `IdempotencyKeyConflictError`.

- [ ] **Step 1: Write failing service tests for replay, versioning and transition isolation**

Create a SQLite service fixture in `tests/unit/test_job_feedback_service.py`. Exercise the service and commit exactly as routes will:

```python
def test_same_student_request_replays_without_second_event(seeded_db) -> None:
    service = JobFeedbackService(now=lambda: NOW)
    kwargs = dict(
        job_id=seeded_db.verified_job.id,
        actor_user_id=seeded_db.student.id,
        idempotency_key="student-feedback-0001",
        action=FeedbackStudentAction.UPSERT,
        category=JobFeedbackCategory.CLOSED,
        expected_version=None,
        note="官网显示已关闭",
    )
    first = service.mutate_student(seeded_db.session, **kwargs)
    seeded_db.session.commit()
    second = service.mutate_student(seeded_db.session, **kwargs)
    seeded_db.session.commit()
    assert second == first
    assert seeded_db.session.scalar(select(func.count(JobFeedbackEvent.id))) == 1


def test_same_key_with_different_body_conflicts(seeded_db) -> None:
    service = JobFeedbackService(now=lambda: NOW)
    service.mutate_student(
        seeded_db.session,
        job_id=seeded_db.verified_job.id,
        actor_user_id=seeded_db.student.id,
        idempotency_key="student-feedback-0002",
        action=FeedbackStudentAction.UPSERT,
        category=JobFeedbackCategory.CLOSED,
        expected_version=None,
        note="第一条说明",
    )
    seeded_db.session.commit()
    with pytest.raises(IdempotencyKeyConflictError):
        service.mutate_student(
            seeded_db.session,
            job_id=seeded_db.verified_job.id,
            actor_user_id=seeded_db.student.id,
            idempotency_key="student-feedback-0002",
            action=FeedbackStudentAction.UPSERT,
            category=JobFeedbackCategory.CLOSED,
            expected_version=1,
            note="不同说明",
        )


def test_feedback_decision_never_changes_job_or_adds_verification(seeded_db) -> None:
    result = _create_feedback(seeded_db)
    before = seeded_db.verified_job.review_version
    JobFeedbackService(now=lambda: NOW).decide_admin(
        seeded_db.session,
        feedback_id=result.id,
        actor_user_id=seeded_db.admin.id,
        idempotency_key="admin-feedback-0001",
        decision=FeedbackAdminDecision.RESOLVE,
        expected_version=1,
    )
    seeded_db.session.commit()
    seeded_db.session.refresh(seeded_db.verified_job)
    assert seeded_db.verified_job.status is JobPostingStatus.VERIFIED
    assert seeded_db.verified_job.review_version == before
    assert seeded_db.session.scalar(select(func.count(JobVerification.id))) == 0
```

Also add cases for non-verified job 404 semantics, another user's category row, stale versions, update resetting an accepted row to `open`, withdraw from `open|accepted`, reopen from terminal states, and all three admin decisions.

- [ ] **Step 2: Run service tests and verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_job_feedback_service.py -q`

Expected: collection FAIL because `JobFeedbackService` does not exist.

- [ ] **Step 3: Implement deterministic fingerprints and replayable redacted results**

Create `backend/app/services/job_feedback.py` with these result and helper contracts. The event snapshot contains a SHA-256 request fingerprint and the mutation response only; it never duplicates note, user ID or idempotency key:

```python
from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json

from sqlalchemy.orm import Session

from backend.app.db.base import utc_now
from backend.app.db.models import JobFeedback, JobFeedbackEvent
from backend.app.domain.job_feedback import (
    ADMIN_TRANSITIONS,
    FEEDBACK_NOTE_MAX_LENGTH,
    STUDENT_WITHDRAW_FROM,
    FeedbackAdminDecision,
    FeedbackStudentAction,
    JobFeedbackAction,
    JobFeedbackCategory,
    JobFeedbackStatus,
)
from backend.app.repositories import job_feedback as repository


class FeedbackNotFoundError(LookupError):
    pass


class FeedbackJobNotFoundError(LookupError):
    pass


class StaleFeedbackError(RuntimeError):
    pass


class InvalidFeedbackTransitionError(ValueError):
    pass


class InvalidFeedbackNoteError(ValueError):
    pass


class IdempotencyKeyConflictError(RuntimeError):
    pass


@dataclass(frozen=True)
class FeedbackMutationResult:
    id: str
    job_id: str
    category: JobFeedbackCategory
    status: JobFeedbackStatus
    version: int
    updated_at: datetime


def _normalise_note(note: str | None) -> str | None:
    value = note.strip() if note is not None else ""
    if len(value) > FEEDBACK_NOTE_MAX_LENGTH:
        raise InvalidFeedbackNoteError
    return value or None


def _fingerprint(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _result(feedback: JobFeedback, updated_at: datetime) -> FeedbackMutationResult:
    return FeedbackMutationResult(
        id=feedback.id,
        job_id=feedback.job_id,
        category=feedback.category,
        status=feedback.status,
        version=feedback.version,
        updated_at=updated_at.astimezone(timezone.utc),
    )


def _snapshot(
    fingerprint: str, result: FeedbackMutationResult
) -> dict[str, object]:
    response = asdict(result)
    response["category"] = result.category.value
    response["status"] = result.status.value
    response["updated_at"] = result.updated_at.isoformat()
    return {"request_fingerprint": fingerprint, "response": response}


def _replay(
    event: JobFeedbackEvent, fingerprint: str
) -> FeedbackMutationResult:
    if event.redacted_snapshot.get("request_fingerprint") != fingerprint:
        raise IdempotencyKeyConflictError
    response = event.redacted_snapshot["response"]
    if not isinstance(response, dict):
        raise RuntimeError("invalid feedback event response snapshot")
    return FeedbackMutationResult(
        id=str(response["id"]),
        job_id=str(response["job_id"]),
        category=JobFeedbackCategory(str(response["category"])),
        status=JobFeedbackStatus(str(response["status"])),
        version=int(response["version"]),
        updated_at=datetime.fromisoformat(str(response["updated_at"])),
    )
```

- [ ] **Step 4: Implement student mutation with job → event → feedback lock order**

Add `JobFeedbackService` and `mutate_student()`. Acquire the verified posting lock before any event read so MySQL Repeatable Read sees a committed concurrent request through current reads:

```python
class JobFeedbackService:
    def __init__(self, *, now: Callable[[], datetime] = utc_now) -> None:
        self._now = now

    def mutate_student(
        self,
        db: Session,
        *,
        job_id: str,
        actor_user_id: str,
        idempotency_key: str,
        action: FeedbackStudentAction,
        category: JobFeedbackCategory,
        expected_version: int | None,
        note: str | None,
    ) -> FeedbackMutationResult:
        normalized_note = _normalise_note(note)
        fingerprint = _fingerprint(
            {
                "operation": "student_mutation",
                "job_id": job_id,
                "action": action.value,
                "category": category.value,
                "expected_version": expected_version,
                "note": normalized_note,
            }
        )
        if repository.lock_verified_job(db, job_id) is None:
            raise FeedbackJobNotFoundError(job_id)
        prior_event = repository.lock_actor_event(
            db,
            actor_user_id=actor_user_id,
            idempotency_key=idempotency_key,
        )
        if prior_event is not None:
            return _replay(prior_event, fingerprint)

        feedback = repository.lock_user_feedback(
            db,
            user_id=actor_user_id,
            job_id=job_id,
            category=category,
        )
        previous_status = feedback.status if feedback is not None else None
        if action is FeedbackStudentAction.UPSERT:
            if feedback is None:
                if expected_version is not None:
                    raise StaleFeedbackError
                feedback = JobFeedback(
                    user_id=actor_user_id,
                    job_id=job_id,
                    category=category,
                    status=JobFeedbackStatus.OPEN,
                    note=normalized_note,
                    version=1,
                )
                db.add(feedback)
                event_action = JobFeedbackAction.SUBMITTED
            else:
                if feedback.version != expected_version:
                    raise StaleFeedbackError
                feedback.note = normalized_note
                feedback.status = JobFeedbackStatus.OPEN
                feedback.version += 1
                event_action = JobFeedbackAction.UPDATED
        else:
            if feedback is None:
                raise FeedbackNotFoundError
            if feedback.version != expected_version:
                raise StaleFeedbackError
            if feedback.status not in STUDENT_WITHDRAW_FROM:
                raise InvalidFeedbackTransitionError(feedback.status.value)
            feedback.status = JobFeedbackStatus.WITHDRAWN
            feedback.version += 1
            event_action = JobFeedbackAction.WITHDRAWN

        changed_at = self._now()
        feedback.updated_at = changed_at
        db.flush()
        result = _result(feedback, changed_at)
        db.add(
            JobFeedbackEvent(
                feedback_id=feedback.id,
                actor_user_id=actor_user_id,
                action=event_action,
                from_status=previous_status.value if previous_status else None,
                to_status=feedback.status.value,
                feedback_version=feedback.version,
                redacted_snapshot=_snapshot(fingerprint, result),
                idempotency_key=idempotency_key,
                created_at=changed_at,
            )
        )
        db.flush()
        return result
```

- [ ] **Step 5: Implement administrator decisions without touching JobPosting**

Add `decide_admin()`; its only locked aggregate is `JobFeedback`, and replay lookup occurs after that current read:

```python
    def decide_admin(
        self,
        db: Session,
        *,
        feedback_id: str,
        actor_user_id: str,
        idempotency_key: str,
        decision: FeedbackAdminDecision,
        expected_version: int,
    ) -> FeedbackMutationResult:
        fingerprint = _fingerprint(
            {
                "operation": "admin_decision",
                "feedback_id": feedback_id,
                "decision": decision.value,
                "expected_version": expected_version,
            }
        )
        feedback = repository.lock_feedback(db, feedback_id)
        if feedback is None:
            raise FeedbackNotFoundError(feedback_id)
        prior_event = repository.lock_actor_event(
            db,
            actor_user_id=actor_user_id,
            idempotency_key=idempotency_key,
        )
        if prior_event is not None:
            return _replay(prior_event, fingerprint)
        if feedback.version != expected_version:
            raise StaleFeedbackError
        allowed, target, event_action = ADMIN_TRANSITIONS[decision]
        if feedback.status not in allowed:
            raise InvalidFeedbackTransitionError(feedback.status.value)
        previous = feedback.status
        changed_at = self._now()
        feedback.status = target
        feedback.version += 1
        feedback.updated_at = changed_at
        result = _result(feedback, changed_at)
        db.add(
            JobFeedbackEvent(
                feedback_id=feedback.id,
                actor_user_id=actor_user_id,
                action=event_action,
                from_status=previous.value,
                to_status=target.value,
                feedback_version=feedback.version,
                redacted_snapshot=_snapshot(fingerprint, result),
                idempotency_key=idempotency_key,
                created_at=changed_at,
            )
        )
        db.flush()
        return result
```

- [ ] **Step 6: Run service, review-service and Ruff regression**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_job_feedback_service.py tests/unit/test_job_review_service.py -q
.\.venv\Scripts\python.exe -m ruff check backend/app/services/job_feedback.py tests/unit/test_job_feedback_service.py
```

Expected: all tests PASS; Ruff prints `All checks passed!`.

- [ ] **Step 7: Commit service transitions**

```powershell
git add backend/app/services/job_feedback.py tests/unit/test_job_feedback_service.py
git commit -m "feat: add idempotent job feedback transitions"
```

### Task 4: Expose student-owned feedback API with fail-closed rate limits

**Files:**
- Create: `backend/app/api/job_feedback_schemas.py`
- Create: `backend/app/api/routes/job_feedback.py`
- Create: `tests/contract/test_job_feedback_api.py`

**Interfaces:**
- Consumes: `JobFeedbackService.mutate_student()`, `repository.list_user_feedback()`, `RedisFixedWindowRateLimiter.check(action, identity, limit)`.
- Produces: `GET /api/jobs/{job_id}/feedback` and `POST /api/jobs/{job_id}/feedback`; `StudentFeedbackItem`, `StudentFeedbackListResponse`, `FeedbackMutationRequest`, and `FeedbackMutationResponse`.

- [ ] **Step 1: Write failing student API contract tests**

Create a complete TestClient fixture in `tests/contract/test_job_feedback_api.py` with in-memory SQLite, `fakeredis`, an admin, two students, one verified posting and one pending posting. Add these exact HTTP assertions:

```python
def test_student_can_create_replay_list_and_withdraw_own_feedback(client, seeded) -> None:
    headers = {
        **seeded.student_headers,
        "Idempotency-Key": "student-api-key-0001",
    }
    created = client.post(
        f"/api/jobs/{seeded.verified_job_id}/feedback",
        headers=headers,
        json={
            "action": "upsert",
            "category": "closed",
            "expected_version": None,
            "note": "官网已关闭",
        },
    )
    replayed = client.post(
        f"/api/jobs/{seeded.verified_job_id}/feedback",
        headers=headers,
        json={
            "action": "upsert",
            "category": "closed",
            "expected_version": None,
            "note": "官网已关闭",
        },
    )
    assert created.status_code == replayed.status_code == 200
    assert created.json() == replayed.json()
    assert set(created.json()) == {
        "id", "job_id", "category", "status", "version", "updated_at"
    }
    listed = client.get(
        f"/api/jobs/{seeded.verified_job_id}/feedback",
        headers=seeded.student_headers,
    )
    assert listed.json()["feedback"][0]["note"] == "官网已关闭"


def test_student_feedback_hides_non_verified_and_other_users_jobs(client, seeded) -> None:
    response = client.post(
        f"/api/jobs/{seeded.pending_job_id}/feedback",
        headers={**seeded.student_headers, "Idempotency-Key": "student-api-key-0002"},
        json={
            "action": "upsert",
            "category": "content_changed",
            "expected_version": None,
            "note": None,
        },
    )
    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "feedback_job_not_found"
```

Add cases for missing/invalid key (422), admin using student endpoint (403), stale version (409), key/body conflict (409), note over 1000 (422), injected limiter exceeded (429), injected limiter unavailable (503), and a second student receiving no first-student rows.

- [ ] **Step 2: Run the student contract subset and verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/contract/test_job_feedback_api.py -q`

Expected: FAIL with 404 because the feedback router is not mounted in the test app.

- [ ] **Step 3: Define explicit student and mutation DTOs**

Create `backend/app/api/job_feedback_schemas.py`:

```python
from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Self

from backend.app.domain.job_feedback import (
    FEEDBACK_NOTE_MAX_LENGTH,
    FeedbackStudentAction,
    JobFeedbackCategory,
    JobFeedbackStatus,
)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class StudentFeedbackItem(BaseModel):
    id: str
    job_id: str
    category: JobFeedbackCategory
    status: JobFeedbackStatus
    note: str | None
    version: int
    created_at: datetime
    updated_at: datetime

    _normalise_times = field_validator("created_at", "updated_at", mode="before")(_as_utc)


class StudentFeedbackListResponse(BaseModel):
    feedback: list[StudentFeedbackItem]


class FeedbackMutationRequest(BaseModel):
    action: FeedbackStudentAction
    category: JobFeedbackCategory
    expected_version: int | None = Field(default=None, ge=0)
    note: str | None = Field(default=None, max_length=FEEDBACK_NOTE_MAX_LENGTH)

    @model_validator(mode="after")
    def require_version_for_withdraw(self) -> Self:
        if self.action is FeedbackStudentAction.WITHDRAW and self.expected_version is None:
            raise ValueError("withdraw requires expected_version")
        if self.action is FeedbackStudentAction.WITHDRAW and self.note is not None:
            raise ValueError("withdraw does not accept note")
        return self


class FeedbackMutationResponse(BaseModel):
    id: str
    job_id: str
    category: JobFeedbackCategory
    status: JobFeedbackStatus
    version: int
    updated_at: datetime

    _normalise_updated_at = field_validator("updated_at", mode="before")(_as_utc)
```

- [ ] **Step 4: Add student routing, key validation, error mapping and rate limiting**

Create `backend/app/api/routes/job_feedback.py`. Use `detail.error_code` to match the existing frontend `ApiError` parser:

```python
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from backend.app.api.dependencies import _get_db, get_current_user
from backend.app.api.job_feedback_schemas import (
    FeedbackMutationRequest,
    FeedbackMutationResponse,
    StudentFeedbackItem,
    StudentFeedbackListResponse,
)
from backend.app.db.models import User, UserRole
from backend.app.domain.job_feedback import IDEMPOTENCY_KEY_PATTERN
from backend.app.repositories import job_feedback as repository
from backend.app.services.job_feedback import (
    FeedbackJobNotFoundError,
    FeedbackNotFoundError,
    IdempotencyKeyConflictError,
    InvalidFeedbackNoteError,
    InvalidFeedbackTransitionError,
    JobFeedbackService,
    StaleFeedbackError,
)
from backend.app.services.rate_limit import (
    RateLimitExceededError,
    RateLimitUnavailableError,
    RedisFixedWindowRateLimiter,
)


router = APIRouter(tags=["job-feedback"])
logger = logging.getLogger(__name__)


def _error(status_code: int, error_code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"error_code": error_code, "message": message},
    )


def _validated_key(raw: str | None) -> str:
    if raw is None or IDEMPOTENCY_KEY_PATTERN.fullmatch(raw) is None:
        raise _error(422, "invalid_idempotency_key", "Idempotency-Key 格式无效。")
    return raw


def _require_student(user: User) -> None:
    if user.role is not UserRole.STUDENT:
        raise _error(403, "student_role_required", "只有学生账号可以提交职位反馈。")


def _enforce_write_limit(request: Request, *, user_id: str, limit: int) -> None:
    if request.app.state.settings.app_env == "test" and not hasattr(
        request.app.state, "job_feedback_rate_limiter"
    ):
        return
    limiter = getattr(
        request.app.state,
        "job_feedback_rate_limiter",
        RedisFixedWindowRateLimiter(request.app.state.redis),
    )
    try:
        limiter.check(action="job-feedback-write", identity=user_id, limit=limit)
    except RateLimitExceededError:
        raise _error(429, "feedback_rate_limited", "反馈操作过于频繁，请稍后重试。") from None
    except RateLimitUnavailableError:
        raise _error(503, "feedback_rate_limit_unavailable", "反馈保护服务暂不可用。") from None


def _service_error(error: Exception) -> HTTPException:
    if isinstance(error, (FeedbackJobNotFoundError, FeedbackNotFoundError)):
        return _error(404, "feedback_job_not_found", "职位或反馈不存在。")
    if isinstance(error, StaleFeedbackError):
        return _error(409, "stale_job_feedback", "反馈版本已变化，请重新加载。")
    if isinstance(error, IdempotencyKeyConflictError):
        return _error(409, "idempotency_key_reused", "该 Idempotency-Key 已用于不同请求。")
    if isinstance(error, InvalidFeedbackTransitionError):
        return _error(409, "invalid_feedback_transition", "当前反馈状态不允许此操作。")
    if isinstance(error, InvalidFeedbackNoteError):
        return _error(422, "invalid_feedback_note", "反馈说明超过长度限制。")
    raise error
```

Append the two student endpoints:

```python
@router.get(
    "/jobs/{job_id}/feedback", response_model=StudentFeedbackListResponse
)
def list_my_job_feedback(
    job_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(_get_db)],
) -> StudentFeedbackListResponse:
    _require_student(current_user)
    from backend.app.repositories import jobs as jobs_repository

    if jobs_repository.get_public_posting(db, job_id) is None:
        raise _error(404, "feedback_job_not_found", "职位不存在。")
    rows = repository.list_user_feedback(db, user_id=current_user.id, job_id=job_id)
    return StudentFeedbackListResponse(
        feedback=[StudentFeedbackItem.model_validate(row, from_attributes=True) for row in rows]
    )


@router.post(
    "/jobs/{job_id}/feedback", response_model=FeedbackMutationResponse
)
def mutate_my_job_feedback(
    job_id: str,
    payload: FeedbackMutationRequest,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(_get_db)],
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key")
    ] = None,
) -> FeedbackMutationResponse:
    _require_student(current_user)
    key = _validated_key(idempotency_key)
    _enforce_write_limit(request, user_id=current_user.id, limit=20)
    try:
        result = JobFeedbackService().mutate_student(
            db,
            job_id=job_id,
            actor_user_id=current_user.id,
            idempotency_key=key,
            action=payload.action,
            category=payload.category,
            expected_version=payload.expected_version,
            note=payload.note,
        )
        db.commit()
    except Exception as error:
        db.rollback()
        mapped = _service_error(error)
        logger.info("job feedback rejected job_id=%s error_code=%s", job_id, mapped.detail["error_code"])
        raise mapped from None
    logger.info(
        "job feedback mutated feedback_id=%s job_id=%s category=%s status=%s version=%s",
        result.id,
        result.job_id,
        result.category.value,
        result.status.value,
        result.version,
    )
    return FeedbackMutationResponse.model_validate(result, from_attributes=True)
```

- [ ] **Step 5: Mount the router only inside the contract fixture and run tests**

Before the shared-router integration task, have the TestClient fixture call `app.include_router(job_feedback.router, prefix="/api")`; do not edit `backend/app/api/router.py` yet.

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/contract/test_job_feedback_api.py -q
.\.venv\Scripts\python.exe -m ruff check backend/app/api/job_feedback_schemas.py backend/app/api/routes/job_feedback.py tests/contract/test_job_feedback_api.py
```

Expected: student API tests PASS and Ruff prints `All checks passed!`.

- [ ] **Step 6: Commit the isolated student API**

```powershell
git add backend/app/api/job_feedback_schemas.py backend/app/api/routes/job_feedback.py tests/contract/test_job_feedback_api.py
git commit -m "feat: add rate-limited student feedback API"
```

### Task 5: Add administrator aggregates and decisions without review side effects

**Files:**
- Modify: `backend/app/api/job_feedback_schemas.py`
- Modify: `backend/app/api/routes/job_feedback.py`
- Modify: `tests/contract/test_job_feedback_api.py`
- Modify: `tests/unit/test_job_feedback_service.py`

**Interfaces:**
- Consumes: `repository.list_admin_feedback()`, `repository.aggregate_admin_feedback()`, `JobFeedbackService.decide_admin()` and existing `JobReviewService.expire()` only in separation tests.
- Produces: `GET /api/admin/job-feedback` and `POST /api/admin/job-feedback/{feedback_id}/decision`; no feedback endpoint calls `JobReviewService`.

- [ ] **Step 1: Add failing admin permission, aggregate and separation tests**

Append to `tests/contract/test_job_feedback_api.py`:

```python
def test_admin_queue_is_aggregated_and_does_not_expose_submitter(client, seeded) -> None:
    _seed_two_closed_feedback(client, seeded)
    response = client.get("/api/admin/job-feedback", headers=seeded.admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["aggregates"][0]["total_count"] == 2
    assert set(body["feedback"][0]).isdisjoint(
        {"user_id", "account", "nickname", "idempotency_key"}
    )


def test_student_cannot_read_or_decide_admin_feedback(client, seeded) -> None:
    queue = client.get("/api/admin/job-feedback", headers=seeded.student_headers)
    decision = client.post(
        "/api/admin/job-feedback/nonexistent/decision",
        headers={**seeded.student_headers, "Idempotency-Key": "student-admin-key-01"},
        json={"decision": "accept", "expected_version": 1},
    )
    assert queue.status_code == decision.status_code == 403


def test_feedback_decision_and_job_expiry_are_separate_transactions(client, seeded) -> None:
    feedback = _create_feedback(client, seeded)
    decided = client.post(
        f"/api/admin/job-feedback/{feedback['id']}/decision",
        headers={**seeded.admin_headers, "Idempotency-Key": "admin-decision-key-01"},
        json={"decision": "resolve", "expected_version": 1},
    )
    assert decided.status_code == 200
    with client.session_factory() as db:
        posting = db.get(JobPosting, seeded.verified_job_id)
        assert posting.status is JobPostingStatus.VERIFIED
        assert db.scalar(select(func.count(JobVerification.id))) == 0

    expired = client.post(
        f"/api/admin/jobs/{seeded.verified_job_id}/decision",
        headers=seeded.admin_headers,
        json={
            "decision": "expire",
            "expected_version": 0,
            "gui_eligible": False,
            "reason_code": "closed_on_official_site",
        },
    )
    assert expired.status_code == 200
    with client.session_factory() as db:
        assert db.scalar(select(func.count(JobVerification.id))) == 1
```

Also add admin same-key replay, different-body key conflict, stale version, invalid transition, 60/minute rate-limit injection, status/category filters and student 403 cases.

- [ ] **Step 2: Run the admin contract subset and verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/contract/test_job_feedback_api.py -q -k "admin or expiry"`

Expected: FAIL with 404 because admin feedback routes are absent.

- [ ] **Step 3: Add administrator DTOs with an explicit identity-free whitelist**

Append to `backend/app/api/job_feedback_schemas.py` and add `FeedbackAdminDecision` to its `backend.app.domain.job_feedback` import:

```python
class AdminFeedbackDetail(BaseModel):
    id: str
    job_id: str
    company_name: str
    title: str
    job_status: str
    job_review_version: int
    category: JobFeedbackCategory
    status: JobFeedbackStatus
    note: str | None
    version: int
    created_at: datetime
    updated_at: datetime

    _normalise_times = field_validator("created_at", "updated_at", mode="before")(_as_utc)


class AdminFeedbackAggregate(BaseModel):
    job_id: str
    company_name: str
    title: str
    category: JobFeedbackCategory
    open_count: int
    accepted_count: int
    total_count: int
    latest_updated_at: datetime

    _normalise_latest = field_validator("latest_updated_at", mode="before")(_as_utc)


class AdminFeedbackQueueResponse(BaseModel):
    total: int
    feedback: list[AdminFeedbackDetail]
    aggregates: list[AdminFeedbackAggregate]


class AdminFeedbackDecisionRequest(BaseModel):
    decision: FeedbackAdminDecision
    expected_version: int = Field(ge=0)
```

- [ ] **Step 4: Add admin queue and decision routes**

Append to `backend/app/api/routes/job_feedback.py` and import the new DTOs, `require_admin`, `JobFeedbackCategory` and `JobFeedbackStatus`:

```python
@router.get("/admin/job-feedback", response_model=AdminFeedbackQueueResponse)
def list_admin_job_feedback(
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(_get_db)],
    status: JobFeedbackStatus | None = None,
    category: JobFeedbackCategory | None = None,
    limit: int = 20,
    offset: int = 0,
) -> AdminFeedbackQueueResponse:
    if not 1 <= limit <= 100 or offset < 0:
        raise _error(422, "invalid_feedback_pagination", "分页参数无效。")
    total, rows = repository.list_admin_feedback(
        db, status=status, category=category, limit=limit, offset=offset
    )
    aggregates = repository.aggregate_admin_feedback(db)
    return AdminFeedbackQueueResponse(
        total=total,
        feedback=[
            AdminFeedbackDetail(
                id=row.feedback.id,
                job_id=row.feedback.job_id,
                company_name=row.company_name,
                title=row.title,
                job_status=row.job_status.value,
                job_review_version=row.job_review_version,
                category=row.feedback.category,
                status=row.feedback.status,
                note=row.feedback.note,
                version=row.feedback.version,
                created_at=row.feedback.created_at,
                updated_at=row.feedback.updated_at,
            )
            for row in rows
        ],
        aggregates=[AdminFeedbackAggregate.model_validate(row, from_attributes=True) for row in aggregates],
    )


@router.post(
    "/admin/job-feedback/{feedback_id}/decision",
    response_model=FeedbackMutationResponse,
)
def decide_admin_job_feedback(
    feedback_id: str,
    payload: AdminFeedbackDecisionRequest,
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(_get_db)],
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key")
    ] = None,
) -> FeedbackMutationResponse:
    key = _validated_key(idempotency_key)
    _enforce_write_limit(request, user_id=admin.id, limit=60)
    try:
        result = JobFeedbackService().decide_admin(
            db,
            feedback_id=feedback_id,
            actor_user_id=admin.id,
            idempotency_key=key,
            decision=payload.decision,
            expected_version=payload.expected_version,
        )
        db.commit()
    except Exception as error:
        db.rollback()
        mapped = _service_error(error)
        logger.info(
            "job feedback decision rejected feedback_id=%s error_code=%s",
            feedback_id,
            mapped.detail["error_code"],
        )
        raise mapped from None
    logger.info(
        "job feedback decided feedback_id=%s job_id=%s status=%s version=%s",
        result.id,
        result.job_id,
        result.status.value,
        result.version,
    )
    return FeedbackMutationResponse.model_validate(result, from_attributes=True)
```

Do not import `JobReviewService` in `backend/app/api/routes/job_feedback.py` or `backend/app/services/job_feedback.py`.

- [ ] **Step 5: Run API, service and review regressions**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/contract/test_job_feedback_api.py tests/unit/test_job_feedback_service.py tests/unit/test_job_review_service.py tests/contract/test_jobs_api.py -q
```

Expected: all selected tests PASS; the separation test proves feedback decision creates zero `JobVerification`, while the existing expiry endpoint creates exactly one.

- [ ] **Step 6: Commit administrator API**

```powershell
git add backend/app/api/job_feedback_schemas.py backend/app/api/routes/job_feedback.py tests/contract/test_job_feedback_api.py tests/unit/test_job_feedback_service.py
git commit -m "feat: add admin job feedback triage API"
```

### Task 6: Integrate migration 0007 and prove MySQL idempotency

**Files:**
- Create: `alembic/versions/20260717_0007_job_feedback.py`
- Create: `tests/integration/test_job_feedback_mysql.py`
- Modify: `tests/integration/test_mysql_migration.py:13-27,76-311`
- Modify: `docker-compose.yml:38-45`
- Modify: `tests/unit/test_container_entrypoint.py:164-195`

**Interfaces:**
- Consumes: an already merged `alembic/versions/20260717_0006*.py` whose revision is exactly `20260717_0006` and the Task 1 ORM schema.
- Produces: single Alembic head `20260717_0007`, MySQL tables `job_feedback` and `job_feedback_events`, verified concurrent same-key replay.

- [ ] **Step 1: Enforce the migration dependency gate before editing**

Run:

```powershell
$revision0006 = Get-ChildItem alembic\versions\20260717_0006*.py
if ($revision0006.Count -ne 1) { throw 'Expected exactly one migration 20260717_0006' }
.\.venv\Scripts\python.exe -m alembic heads
Select-String -Path $revision0006.FullName -Pattern 'revision.*20260717_0006'
```

Expected: exactly one `0006` file and one Alembic head `20260717_0006`. If not, stop Task 6 while other completed tasks remain valid; do not create a branch migration or merge revision.

- [ ] **Step 2: Write failing migration metadata and MySQL concurrency tests**

In `tests/integration/test_mysql_migration.py`, set `HEAD_REVISION = "20260717_0007"`, add both feedback tables to `BUSINESS_TABLES`, and extend the destructive migration test to assert:

```python
_run_alembic("upgrade", "20260717_0007", env=env)
assert _current_revision(engine) == "20260717_0007"
assert {"job_feedback", "job_feedback_events"} <= _tables(engine)
assert ("user_id", "job_id", "category") in _column_sets(
    inspect(engine).get_unique_constraints("job_feedback")
)
assert ("actor_user_id", "idempotency_key") in _column_sets(
    inspect(engine).get_unique_constraints("job_feedback_events")
)
_run_alembic("downgrade", "20260717_0006", env=env)
assert "job_feedback" not in _tables(engine)
assert "job_feedback_events" not in _tables(engine)
```

Create `tests/integration/test_job_feedback_mysql.py` using the existing `destructive_mysql_url` guard, migrating to head, and two independent `Session` objects. The core concurrent assertion is:

```python
def test_mysql_same_idempotency_key_serializes_to_one_event(mysql_engine, seeded_ids) -> None:
    barrier = Barrier(2)

    def submit() -> FeedbackMutationResult:
        with Session(mysql_engine, expire_on_commit=False) as db:
            barrier.wait(timeout=5)
            result = JobFeedbackService().mutate_student(
                db,
                job_id=seeded_ids.job_id,
                actor_user_id=seeded_ids.student_id,
                idempotency_key="mysql-feedback-key-0001",
                action=FeedbackStudentAction.UPSERT,
                category=JobFeedbackCategory.CLOSED,
                expected_version=None,
                note="官网显示职位关闭",
            )
            db.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(lambda _: submit(), range(2)))
    assert first == second
    with Session(mysql_engine) as db:
        assert db.scalar(select(func.count(JobFeedback.id))) == 1
        assert db.scalar(select(func.count(JobFeedbackEvent.id))) == 1
```

The fixture must create and clean its own User, JobSource, RawJobRecord and verified JobPosting rows and must leave no feedback rows after the test.

- [ ] **Step 3: Run tests and verify the missing revision failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_job_feedback_mysql.py tests/integration/test_mysql_migration.py -q -rs
```

Expected: default environment SKIPs destructive MySQL cases with the exact `ALLOW_DESTRUCTIVE_MYSQL_TESTS=1` gate; the offline/head assertion FAILS because `20260717_0007` does not exist.

- [ ] **Step 4: Create migration 0007 with exact foreign keys, checks and indexes**

Create `alembic/versions/20260717_0007_job_feedback.py`:

```python
"""add student job feedback loop

Revision ID: 20260717_0007
Revises: 20260717_0006
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260717_0007"
down_revision: Union[str, Sequence[str], None] = "20260717_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_feedback",
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("job_id", sa.String(36), nullable=False),
        sa.Column("category", sa.String(40), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "category IN ('closed','application_channel_unavailable','content_changed','incorrect_information')",
            name="job_feedback_category",
        ),
        sa.CheckConstraint(
            "status IN ('open','accepted','resolved','rejected','withdrawn')",
            name="job_feedback_status",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["job_postings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "job_id", "category", name="uq_job_feedback_user_job_category"
        ),
    )
    op.create_index("ix_job_feedback_user_id", "job_feedback", ["user_id"])
    op.create_index(
        "ix_job_feedback_job_status_updated",
        "job_feedback",
        ["job_id", "status", "updated_at"],
    )
    op.create_index(
        "ix_job_feedback_status_updated", "job_feedback", ["status", "updated_at"]
    )
    op.create_table(
        "job_feedback_events",
        sa.Column("feedback_id", sa.String(36), nullable=False),
        sa.Column("actor_user_id", sa.String(36), nullable=True),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("from_status", sa.String(40), nullable=True),
        sa.Column("to_status", sa.String(40), nullable=False),
        sa.Column("feedback_version", sa.Integer(), nullable=False),
        sa.Column("redacted_snapshot", sa.JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(36), nullable=False),
        sa.CheckConstraint(
            "action IN ('submitted','updated','withdrawn','accepted','resolved','rejected')",
            name="job_feedback_action",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["feedback_id"], ["job_feedback.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "actor_user_id", "idempotency_key", name="uq_job_feedback_events_actor_key"
        ),
    )
    op.create_index(
        "ix_job_feedback_events_actor_user_id",
        "job_feedback_events",
        ["actor_user_id"],
    )
    op.create_index(
        "ix_job_feedback_events_feedback_created",
        "job_feedback_events",
        ["feedback_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("job_feedback_events")
    op.drop_table("job_feedback")
```

- [ ] **Step 5: Update Compose revision evidence**

Change the migrate service label in `docker-compose.yml` and the matching assertion in `tests/unit/test_container_entrypoint.py` to:

```yaml
com.career-assistant.schema-revision: "20260717_0007"
```

- [ ] **Step 6: Run single-head and safe default migration tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m alembic heads
.\.venv\Scripts\python.exe -m pytest tests/integration/test_mysql_migration.py tests/integration/test_job_feedback_mysql.py tests/unit/test_container_entrypoint.py -q -rs
```

Expected: exactly one head `20260717_0007`; local-safe tests PASS; destructive cases SKIP only when explicit MySQL variables are absent.

- [ ] **Step 7: Run the real MySQL migration and concurrency gate**

Load `DB_PASSWORD` from the user environment without printing it, construct `TEST_MYSQL_URL` for `career_assistant_test`, and run:

```powershell
$env:DB_PASSWORD = [Environment]::GetEnvironmentVariable('DB_PASSWORD', 'User')
if ([string]::IsNullOrWhiteSpace($env:DB_PASSWORD)) { throw 'Missing DB_PASSWORD' }
$port = if ($env:MYSQL_HOST_PORT) { $env:MYSQL_HOST_PORT } else { '3307' }
$env:TEST_MYSQL_URL = .\.venv\Scripts\python.exe -c "import os,sys,urllib.parse; print('mysql+pymysql://root:'+urllib.parse.quote(os.environ['DB_PASSWORD'],safe='')+'@127.0.0.1:'+sys.argv[1]+'/career_assistant_test?charset=utf8mb4')" $port
$env:ALLOW_DESTRUCTIVE_MYSQL_TESTS = '1'
.\.venv\Scripts\python.exe -m pytest tests/integration/test_mysql_migration.py tests/integration/test_job_feedback_mysql.py -q
```

Expected: migration `0006 → 0007 → 0006` assertions PASS; concurrent same-key test produces one aggregate and one event. Never print `$env:TEST_MYSQL_URL`.

- [ ] **Step 8: Commit the migration gate**

```powershell
git add alembic/versions/20260717_0007_job_feedback.py tests/integration/test_job_feedback_mysql.py tests/integration/test_mysql_migration.py docker-compose.yml tests/unit/test_container_entrypoint.py
git commit -m "feat: add job feedback migration and mysql gate"
```

### Task 7: Add the student feedback panel with retry-stable keys

**Files:**
- Create: `frontend/src/features/jobs/jobFeedbackTypes.ts`
- Create: `frontend/src/features/jobs/jobFeedbackApi.ts`
- Create: `frontend/src/features/jobs/JobFeedbackPanel.vue`
- Create: `frontend/src/features/jobs/__tests__/jobFeedbackApi.spec.ts`
- Create: `frontend/src/features/jobs/__tests__/JobFeedbackPanel.spec.ts`
- Modify: `frontend/src/features/jobs/JobCenter.vue:1-123,253-273`

**Interfaces:**
- Consumes: Task 4 JSON field names and `detail.error_code` through existing `ApiError`.
- Produces: `fetchMyJobFeedback()`, `mutateJobFeedback()`, and `<JobFeedbackPanel token job-id>`; the component reuses an idempotency key after network/5xx failure and resets it only after success or payload change.

- [ ] **Step 1: Write failing API serialization tests**

Create `frontend/src/features/jobs/__tests__/jobFeedbackApi.spec.ts` with a mocked `request`:

```typescript
it("sends the exact Idempotency-Key header and snake_case payload", async () => {
  vi.mocked(request).mockResolvedValue({
    id: "feedback-1",
    job_id: "job-1",
    category: "closed",
    status: "open",
    version: 1,
    updated_at: "2026-07-17T00:00:00Z",
  });
  await mutateJobFeedback("token", "job-1", "key-123456789012", {
    action: "upsert",
    category: "closed",
    expected_version: null,
    note: "官网已关闭",
  });
  expect(request).toHaveBeenCalledWith(
    "/jobs/job-1/feedback",
    {
      method: "POST",
      headers: { "Idempotency-Key": "key-123456789012" },
      body: JSON.stringify({
        action: "upsert",
        category: "closed",
        expected_version: null,
        note: "官网已关闭",
      }),
    },
    "token",
  );
});
```

- [ ] **Step 2: Write failing panel tests for create, update, withdraw and retry**

In `JobFeedbackPanel.spec.ts`, mock `crypto.randomUUID`, `fetchMyJobFeedback` and `mutateJobFeedback`. Assert:

```typescript
it("reuses the same key when a failed mutation is retried", async () => {
  vi.stubGlobal("crypto", { randomUUID: vi.fn(() => "feedback-ui-key-0001") });
  vi.mocked(fetchMyJobFeedback).mockResolvedValue({ feedback: [] });
  vi.mocked(mutateJobFeedback)
    .mockRejectedValueOnce(new Error("network lost"))
    .mockResolvedValueOnce(MUTATION_RESULT);
  const wrapper = mount(JobFeedbackPanel, { props: { token: "token", jobId: "job-1" } });
  await flushPromises();
  await wrapper.get('[data-test="feedback-note"]').setValue("官网已关闭");
  await wrapper.get('[data-test="submit-feedback"]').trigger("click");
  await flushPromises();
  await wrapper.get('[data-test="submit-feedback"]').trigger("click");
  await flushPromises();
  expect(vi.mocked(mutateJobFeedback).mock.calls[0][2]).toBe("feedback-ui-key-0001");
  expect(vi.mocked(mutateJobFeedback).mock.calls[1][2]).toBe("feedback-ui-key-0001");
});
```

Add rendering tests for existing own rows, using the row version on update/withdraw, 1000-character counter, stale 409 reload and no cross-user identity fields.

- [ ] **Step 3: Run frontend tests and verify missing modules fail**

Run:

```powershell
npm.cmd --prefix frontend run test -- frontend/src/features/jobs/__tests__/jobFeedbackApi.spec.ts frontend/src/features/jobs/__tests__/JobFeedbackPanel.spec.ts
```

Expected: FAIL because the feedback types, API and panel do not exist.

- [ ] **Step 4: Add exact frontend types and API calls**

Create `jobFeedbackTypes.ts`:

```typescript
export type JobFeedbackCategory =
  | "closed"
  | "application_channel_unavailable"
  | "content_changed"
  | "incorrect_information";

export type JobFeedbackStatus = "open" | "accepted" | "resolved" | "rejected" | "withdrawn";
export type FeedbackStudentAction = "upsert" | "withdraw";
export type FeedbackAdminDecision = "accept" | "resolve" | "reject";

export interface StudentFeedbackItem {
  id: string;
  job_id: string;
  category: JobFeedbackCategory;
  status: JobFeedbackStatus;
  note: string | null;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface StudentFeedbackListResponse { feedback: StudentFeedbackItem[]; }

export interface FeedbackMutationPayload {
  action: FeedbackStudentAction;
  category: JobFeedbackCategory;
  expected_version: number | null;
  note: string | null;
}

export interface FeedbackMutationResponse {
  id: string;
  job_id: string;
  category: JobFeedbackCategory;
  status: JobFeedbackStatus;
  version: number;
  updated_at: string;
}
```

Create `jobFeedbackApi.ts`:

```typescript
import { request } from "../../api";
import type {
  FeedbackMutationPayload,
  FeedbackMutationResponse,
  StudentFeedbackListResponse,
} from "./jobFeedbackTypes";

export function fetchMyJobFeedback(token: string, jobId: string): Promise<StudentFeedbackListResponse> {
  return request(`/jobs/${encodeURIComponent(jobId)}/feedback`, {}, token);
}

export function mutateJobFeedback(
  token: string,
  jobId: string,
  idempotencyKey: string,
  payload: FeedbackMutationPayload,
): Promise<FeedbackMutationResponse> {
  return request(`/jobs/${encodeURIComponent(jobId)}/feedback`, {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey },
    body: JSON.stringify(payload),
  }, token);
}
```

- [ ] **Step 5: Implement the panel state machine**

Create `JobFeedbackPanel.vue` with category labels, a 1000-character textarea, own-row list, `import { ApiError } from "../../api";`, and these retry semantics in `<script setup>`:

```typescript
const props = defineProps<{ token: string; jobId: string }>();
const feedback = ref<StudentFeedbackItem[]>([]);
const category = ref<JobFeedbackCategory>("closed");
const note = ref("");
const pendingKey = ref("");
const withdrawKeys = new Map<string, string>();
const busy = ref(false);
const error = ref("");

const selected = computed(() => feedback.value.find((item) => item.category === category.value));
const keyForUpsert = () => pendingKey.value || (pendingKey.value = crypto.randomUUID());

watch(category, () => {
  note.value = selected.value?.note ?? "";
  pendingKey.value = "";
});
watch(note, () => { pendingKey.value = ""; });
watch(() => props.jobId, async () => {
  pendingKey.value = "";
  withdrawKeys.clear();
  await load();
});

async function load() {
  error.value = "";
  try {
    feedback.value = (await fetchMyJobFeedback(props.token, props.jobId)).feedback;
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : "反馈加载失败。";
  }
}

async function submit() {
  busy.value = true;
  error.value = "";
  try {
    await mutateJobFeedback(props.token, props.jobId, keyForUpsert(), {
      action: "upsert",
      category: category.value,
      expected_version: selected.value?.version ?? null,
      note: note.value.trim() || null,
    });
    pendingKey.value = "";
    await load();
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : "反馈提交失败。";
    if (
      caught instanceof ApiError
      && caught.status === 409
      && typeof caught.detail === "object"
      && caught.detail !== null
      && (caught.detail as Record<string, unknown>).error_code === "stale_job_feedback"
    ) {
      pendingKey.value = "";
      await load();
    }
  } finally {
    busy.value = false;
  }
}

async function withdraw(item: StudentFeedbackItem) {
  const key = withdrawKeys.get(item.id) ?? crypto.randomUUID();
  withdrawKeys.set(item.id, key);
  busy.value = true;
  try {
    await mutateJobFeedback(props.token, props.jobId, key, {
      action: "withdraw",
      category: item.category,
      expected_version: item.version,
      note: null,
    });
    withdrawKeys.delete(item.id);
    await load();
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : "反馈撤回失败。";
    if (caught instanceof ApiError && caught.status === 409) {
      withdrawKeys.delete(item.id);
      await load();
    }
  } finally {
    busy.value = false;
  }
}

onMounted(load);
```

The template must use `data-test="feedback-category"`, `feedback-note`, `submit-feedback`, `feedback-list`, and `withdraw-feedback-{id}`; disable writes while busy; render note only for the authenticated user's returned rows.

- [ ] **Step 6: Mount the panel inside verified job detail**

In `JobCenter.vue`, import the component and place it after the current detail metadata:

```vue
<JobFeedbackPanel
  v-if="selectedJob"
  :key="selectedJob.id"
  :token="token"
  :job-id="selectedJob.id"
/>
```

No feedback field is added to `JobSummary` or `JobDetail`.

- [ ] **Step 7: Run focused and existing job-center frontend tests**

Run:

```powershell
npm.cmd --prefix frontend run test -- frontend/src/features/jobs/__tests__/jobFeedbackApi.spec.ts frontend/src/features/jobs/__tests__/JobFeedbackPanel.spec.ts frontend/src/features/jobs/__tests__/JobCenter.spec.ts
npm.cmd --prefix frontend run typecheck
```

Expected: focused tests and existing JobCenter tests PASS; `vue-tsc` exits 0.

- [ ] **Step 8: Commit student UI**

```powershell
git add frontend/src/features/jobs/jobFeedbackTypes.ts frontend/src/features/jobs/jobFeedbackApi.ts frontend/src/features/jobs/JobFeedbackPanel.vue frontend/src/features/jobs/JobCenter.vue frontend/src/features/jobs/__tests__/jobFeedbackApi.spec.ts frontend/src/features/jobs/__tests__/JobFeedbackPanel.spec.ts
git commit -m "feat: add student job feedback panel"
```

### Task 8: Add administrator feedback UI and perform shared-entry integration

**Files:**
- Modify: `frontend/src/features/jobs/jobFeedbackTypes.ts`
- Modify: `frontend/src/features/jobs/jobFeedbackApi.ts`
- Create: `frontend/src/features/jobs/AdminJobFeedback.vue`
- Create: `frontend/src/features/jobs/__tests__/AdminJobFeedback.spec.ts`
- Modify: `backend/app/api/router.py:3-12`
- Modify: `frontend/src/App.vue:14-41,227-269,381-418`

**Interfaces:**
- Consumes: Task 5 admin DTOs and decisions; all A/B/C shared-router and `App.vue` changes already rebased.
- Produces: mounted `/api/admin/job-feedback`, admin-only `job_feedback` workspace, aggregate cards, identity-free detail table and accept/resolve/reject actions.

- [ ] **Step 1: Rebase and verify shared files before editing**

Run:

```powershell
git fetch
git rebase master
Get-Content backend/app/api/router.py
Select-String -Path frontend/src/App.vue -Pattern 'WorkspaceView|include|Admin'
.\.venv\Scripts\python.exe -m alembic heads
```

Expected: one head `20260717_0007`; existing A/B/C imports and views remain visible. Resolve only semantic conflicts, preserve all unrelated shared-entry additions, then rerun Task 4–7 focused tests.

- [ ] **Step 2: Write failing admin UI tests**

Create `AdminJobFeedback.spec.ts` and assert aggregate cards, identity-free detail, filters, persisted idempotency key on retry, stale 409 reload, and the explicit separation notice:

```typescript
it("never offers direct job expiry from the feedback queue", async () => {
  vi.mocked(fetchAdminJobFeedback).mockResolvedValue(QUEUE_RESPONSE);
  const wrapper = mount(AdminJobFeedback, { props: { token: "admin-token" } });
  await flushPromises();
  expect(wrapper.text()).toContain("如需失效职位，请在职位审核页另行操作");
  expect(wrapper.find('[data-test="expire-job-from-feedback"]').exists()).toBe(false);
  expect(wrapper.text()).not.toContain("student-account");
});
```

- [ ] **Step 3: Extend frontend admin types and API**

Add these exact interfaces to `jobFeedbackTypes.ts`:

```typescript
export interface AdminFeedbackDetail extends StudentFeedbackItem {
  company_name: string;
  title: string;
  job_status: string;
  job_review_version: number;
}

export interface AdminFeedbackAggregate {
  job_id: string;
  company_name: string;
  title: string;
  category: JobFeedbackCategory;
  open_count: number;
  accepted_count: number;
  total_count: number;
  latest_updated_at: string;
}

export interface AdminFeedbackQueueResponse {
  total: number;
  feedback: AdminFeedbackDetail[];
  aggregates: AdminFeedbackAggregate[];
}

export interface AdminFeedbackDecisionPayload {
  decision: FeedbackAdminDecision;
  expected_version: number;
}
```

Add to `jobFeedbackApi.ts`:

```typescript
export function fetchAdminJobFeedback(
  token: string,
  query: { status?: JobFeedbackStatus; category?: JobFeedbackCategory; limit: number; offset: number },
): Promise<AdminFeedbackQueueResponse> {
  const params = new URLSearchParams({ limit: String(query.limit), offset: String(query.offset) });
  if (query.status) params.set("status", query.status);
  if (query.category) params.set("category", query.category);
  return request(`/admin/job-feedback?${params.toString()}`, {}, token);
}

export function decideAdminJobFeedback(
  token: string,
  feedbackId: string,
  idempotencyKey: string,
  payload: AdminFeedbackDecisionPayload,
): Promise<FeedbackMutationResponse> {
  return request(`/admin/job-feedback/${encodeURIComponent(feedbackId)}/decision`, {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey },
    body: JSON.stringify(payload),
  }, token);
}
```

- [ ] **Step 4: Implement the administrator aggregate component**

Create `AdminJobFeedback.vue` with status/category filters, pagination, aggregate cards and decision buttons. Use one `Map<string, string>` keyed by `${feedback.id}:${decision}` so a failed retry reuses its key; clear the entry only on success or when the feedback version changes. Render `note` only inside the admin-only detail table, never render a submitter field, and include this fixed notice:

```vue
<p class="safety-note" data-test="feedback-review-separation">
  反馈处置不会改变职位状态。如需失效职位，请在职位审核页另行操作，系统会通过 JobReviewService 记录 JobVerification。
</p>
```

Decision handling must call only `decideAdminJobFeedback()`:

```typescript
async function decide(item: AdminFeedbackDetail, decision: FeedbackAdminDecision) {
  const mapKey = `${item.id}:${decision}`;
  const idempotencyKey = decisionKeys.get(mapKey) ?? crypto.randomUUID();
  decisionKeys.set(mapKey, idempotencyKey);
  busyId.value = item.id;
  try {
    await decideAdminJobFeedback(props.token, item.id, idempotencyKey, {
      decision,
      expected_version: item.version,
    });
    decisionKeys.delete(mapKey);
    await load();
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : "反馈处置失败。";
    if (caught instanceof ApiError && caught.status === 409) await load();
  } finally {
    busyId.value = "";
  }
}
```

- [ ] **Step 5: Mount backend and frontend shared entries**

In `backend/app/api/router.py`, preserve every existing import and add:

```python
from backend.app.api.routes import job_feedback

api_router.include_router(job_feedback.router)
```

In `App.vue`, import `AdminJobFeedback`, extend the union to include `"job_feedback"`, protect both admin views in `selectWorkspace()` and the role watcher, then add:

```vue
<button
  v-if="profile.role === 'admin'"
  type="button"
  data-test="job-feedback-view"
  :class="{ active: workspaceView === 'job_feedback' }"
  :aria-current="workspaceView === 'job_feedback' ? 'page' : undefined"
  @click="selectWorkspace('job_feedback')"
>
  职位反馈
</button>

<AdminJobFeedback
  v-if="workspaceView === 'job_feedback' && profile.role === 'admin'"
  :token="token"
/>
```

- [ ] **Step 6: Run backend route, frontend and type gates**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/contract/test_job_feedback_api.py tests/contract/test_jobs_api.py -q
npm.cmd --prefix frontend run test -- frontend/src/features/jobs/__tests__/AdminJobFeedback.spec.ts frontend/src/features/jobs/__tests__/JobFeedbackPanel.spec.ts frontend/src/features/jobs/__tests__/JobCenter.spec.ts
npm.cmd --prefix frontend run typecheck
npm.cmd --prefix frontend run build
```

Expected: backend contract tests PASS; frontend tests PASS; typecheck and production build exit 0.

- [ ] **Step 7: Commit shared integration after confirming preserved routes/views**

```powershell
git add backend/app/api/router.py frontend/src/App.vue frontend/src/features/jobs/jobFeedbackTypes.ts frontend/src/features/jobs/jobFeedbackApi.ts frontend/src/features/jobs/AdminJobFeedback.vue frontend/src/features/jobs/__tests__/AdminJobFeedback.spec.ts
git commit -m "feat: integrate admin job feedback workspace"
```

### Task 9: Close security, runbook and Compose release gates

**Files:**
- Create: `tests/security/test_job_feedback_security.py`
- Modify: `docs/runbooks/platform-foundation.md`

**Interfaces:**
- Consumes: complete Tasks 0–8 and existing platform test guard/environment conventions.
- Produces: evidence that logs/responses do not expose note/key/identity, public job DTO remains unchanged, Compose migrated to `0007`, live/ready/frontend respond, and authenticated feedback smoke succeeds.

- [ ] **Step 1: Write failing security tests around sensitive marker capture**

Create `tests/security/test_job_feedback_security.py` with a TestClient fixture and log capture matching `tests/security/test_no_sensitive_logging.py`. Use unique markers and assert:

```python
def test_feedback_logs_and_admin_dto_redact_identity_and_idempotency(client, seeded, captured_logs) -> None:
    note_marker = "feedback-note-secret-7f3c"
    key_marker = "feedback-idempotency-7f3c"
    response = client.post(
        f"/api/jobs/{seeded.job_id}/feedback",
        headers={**seeded.student_headers, "Idempotency-Key": key_marker},
        json={
            "action": "upsert",
            "category": "incorrect_information",
            "expected_version": None,
            "note": note_marker,
        },
    )
    assert response.status_code == 200
    logs = captured_logs.getvalue()
    assert note_marker not in logs
    assert key_marker not in logs
    admin = client.get("/api/admin/job-feedback", headers=seeded.admin_headers)
    encoded = admin.text
    assert seeded.student_id not in encoded
    assert seeded.student_account not in encoded
    assert key_marker not in encoded


def test_public_job_dto_never_gains_feedback_fields(client, seeded) -> None:
    response = client.get(f"/api/jobs/{seeded.job_id}", headers=seeded.student_headers)
    assert response.status_code == 200
    assert set(response.json()).isdisjoint(
        {"feedback", "feedback_count", "user_id", "note", "idempotency_key"}
    )
```

Also send a 409 key-conflict and assert the response contains only `error_code` and the generic message, not either request body.

- [ ] **Step 2: Run the security tests and correct every leak**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/security/test_job_feedback_security.py tests/security/test_no_sensitive_logging.py -q
```

Expected: all security tests PASS. If a marker appears, replace the offending log/DTO field with ID, stable code or count and rerun this exact command.

- [ ] **Step 3: Document state, retry and separate-expiry operations**

Add a “职位反馈闭环” section to `docs/runbooks/platform-foundation.md` containing:

```markdown
### 职位反馈闭环

- 学生只可对 `verified` 职位提交四类反馈；写请求必须携带 16～128 字符的 `Idempotency-Key`。
- 网络超时重试必须复用原 key 和原请求体；修改请求体前必须生成新 key。
- 409 `stale_job_feedback`：重新读取本人反馈或管理员队列后再操作。
- 409 `idempotency_key_reused`：原 key 已绑定其他请求，确认用户意图后使用新 key。
- 管理员 accept/resolve/reject 只处置反馈，不改变职位。
- 确认职位失效时，在职位审核页另行调用既有失效操作；该操作必须增加 `review_version` 并追加一条 `JobVerification`。
- 日志排障只使用 feedback ID、job ID 和 `error_code`，不得复制说明、账号、token 或 Idempotency-Key。
```

Document the focused pytest, frontend and MySQL commands from this plan and the Compose smoke below; never include real credentials or full database URLs.

- [ ] **Step 4: Run complete local regression and static checks**

Run:

```powershell
.\.venv\Scripts\python.exe -m ruff check backend src tests scripts
.\.venv\Scripts\python.exe -m pytest -q -rs
npm.cmd --prefix frontend run test
npm.cmd --prefix frontend run typecheck
npm.cmd --prefix frontend run build
git diff --check
```

Expected: Ruff `All checks passed!`; Python and Vitest have zero failures; only explicitly gated external tests skip; typecheck/build exit 0; `git diff --check` emits no output.

- [ ] **Step 5: Rebuild Compose and verify the authoritative head**

Load the six required user-scope secrets without printing them, preserve this workstation's ports, then run:

```powershell
$required = @('DB_PASSWORD','REDIS_PASSWORD','MINIO_ROOT_USER','MINIO_ROOT_PASSWORD','APP_AUTH_SECRET','OBJECT_ENCRYPTION_KEY')
foreach ($name in $required) {
  $value = [Environment]::GetEnvironmentVariable($name, 'User')
  if ([string]::IsNullOrWhiteSpace($value)) { throw "Missing $name" }
  Set-Item -Path "Env:$name" -Value $value
}
$env:MYSQL_HOST_PORT='3307'
$env:REDIS_HOST_PORT='6380'
$env:MINIO_HOST_PORT='19000'
$env:MINIO_CONSOLE_HOST_PORT='19001'
$env:BACKEND_HOST_PORT='18000'
$env:FRONTEND_HOST_PORT='15173'
docker compose -p platform-foundation up -d --build
docker compose -p platform-foundation ps -a
docker compose -p platform-foundation run --rm migrate alembic current
Invoke-RestMethod http://127.0.0.1:18000/api/health/ready
Invoke-WebRequest http://127.0.0.1:15173/ -UseBasicParsing
```

Expected: migrate exits 0 at `20260717_0007`; MySQL, Redis, MinIO and backend are healthy; readiness is HTTP 200 with all three dependencies `up`; frontend is HTTP 200. Do not run `docker compose down -v`.

- [ ] **Step 6: Run authenticated feedback smoke against one real verified job**

Read student and admin bearer tokens without echoing them, then create, observe in the admin aggregate, and withdraw a smoke feedback:

```powershell
$studentSecure = Read-Host 'Student bearer token' -AsSecureString
$adminSecure = Read-Host 'Admin bearer token' -AsSecureString
$studentToken = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($studentSecure))
$adminToken = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($adminSecure))
$studentHeaders = @{ Authorization = "Bearer $studentToken" }
$adminHeaders = @{ Authorization = "Bearer $adminToken" }
$jobs = Invoke-RestMethod http://127.0.0.1:18000/api/jobs -Headers $studentHeaders
if ($jobs.total -lt 1) { throw 'A real manually verified job is required for feedback smoke' }
$jobId = $jobs.jobs[0].id
$key = "compose-feedback-$([guid]::NewGuid().ToString('N'))"
$writeHeaders = @{ Authorization = "Bearer $studentToken"; 'Idempotency-Key' = $key }
$body = @{ action='upsert'; category='incorrect_information'; expected_version=$null; note='Compose feedback smoke' } | ConvertTo-Json
$created = Invoke-RestMethod "http://127.0.0.1:18000/api/jobs/$jobId/feedback" -Method Post -Headers $writeHeaders -ContentType 'application/json' -Body $body
$queue = Invoke-RestMethod http://127.0.0.1:18000/api/admin/job-feedback -Headers $adminHeaders
if (-not ($queue.feedback | Where-Object id -eq $created.id)) { throw 'Feedback missing from admin queue' }
$withdrawHeaders = @{ Authorization = "Bearer $studentToken"; 'Idempotency-Key' = "compose-withdraw-$([guid]::NewGuid().ToString('N'))" }
$withdraw = @{ action='withdraw'; category='incorrect_information'; expected_version=$created.version; note=$null } | ConvertTo-Json
$result = Invoke-RestMethod "http://127.0.0.1:18000/api/jobs/$jobId/feedback" -Method Post -Headers $withdrawHeaders -ContentType 'application/json' -Body $withdraw
if ($result.status -ne 'withdrawn') { throw 'Feedback smoke cleanup failed' }
$studentToken=$null; $adminToken=$null; $studentSecure.Dispose(); $adminSecure.Dispose()
```

Expected: the verified job accepts feedback, the identity-free admin queue contains it, and the student can withdraw it. If no real manually verified job exists, record this external acceptance as incomplete; do not seed a fake public job into the development database and do not claim the Compose business smoke passed.

- [ ] **Step 7: Commit security and operations documentation**

```powershell
git add tests/security/test_job_feedback_security.py docs/runbooks/platform-foundation.md
git commit -m "test: close job feedback release gates"
```

## Final Completion Checklist

- [ ] `git log --oneline` shows a focused commit for each task boundary; no unrelated user changes were staged.
- [ ] `.\.venv\Scripts\python.exe -m alembic heads` prints only `20260717_0007`.
- [ ] Same actor + same key + same body returns the same mutation response and leaves exactly one event under real MySQL concurrency.
- [ ] Same actor + same key + different body returns 409 `idempotency_key_reused`.
- [ ] Cross-user reads and non-verified job feedback return 404; students receive 403 from administrator endpoints.
- [ ] Feedback mutation and decision leave `JobPosting.status` and `review_version` unchanged and add zero `JobVerification` rows.
- [ ] The separate existing expiry endpoint changes the verified job through `JobReviewService` and adds exactly one `JobVerification`.
- [ ] Student and administrator rate limits return 429 at 20/minute and 60/minute respectively; Redis failure returns 503.
- [ ] Public job DTOs contain no feedback data; admin feedback DTOs contain no submitter identity or idempotency key.
- [ ] Logs contain no feedback note, account, user/feedback association, idempotency key, token or raw request body.
- [ ] Frontend tests, `vue-tsc`, production build, Python regression, Ruff, MySQL gates and Compose health checks all pass with fresh output.
- [ ] Missing real Tencent token or verified job is reported as an external acceptance gap, never folded into a full release-pass claim.
