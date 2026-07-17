# Windows Executor Simulation Safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立只通过出站连接工作的 Windows 本地 Executor 骨架，复用现有设备认证、`ApplicationTask` 权威状态机和短期 task lease，在项目内模拟招聘页面上证明安全填写、人工接管和可恢复执行不会点击最终提交或歧义按钮。

**Architecture:** Backend 增加版本化 Executor DTO 和按设备隔离的任务读取、进度、结果 API；所有任务动作同时绑定 device token、task ID、设备分配关系、task version 和正确 lease scope，MySQL 继续是任务状态唯一权威源。Windows Executor 使用独立的 Playwright 可见 Chromium profile、确定性安全门和原子本地检查点；Wave 1 只消费显式 simulation fixture，Wave 2 再通过相同 DTO 提供真实非敏感 `ApplicationSnapshot`。模拟站点为单页、多页、末页、歧义动作、登录等待和回读异常提供可观测计数器，恢复测试以“最终/歧义点击为 0、字段与中间副作用无重复”为硬断言。

**Tech Stack:** Python 3.13、FastAPI 0.117.1、Pydantic 2.12.5、SQLAlchemy 2.0.51、MySQL 8.4、Redis 8 task lease、httpx 0.28.1、Playwright for Python 1.61.0、keyring 25.7.0、pytest 8.4.2、Windows Credential Locker、现有 Docker Compose 基础。

## Global Constraints

- 项目级 Wave 0 已完成；本计划的 Task 0 只是共享契约检查点，不创建新的功能波次。
- 所有业务 ID 使用 36 字符 UUID，时间使用 UTC，任务写操作使用整数 `expected_version`。
- MySQL 是 `ApplicationTask` 和 `ApplicationEvent` 的唯一权威来源；Redis、浏览器 profile 和本地检查点不得反推或重写权威任务状态。
- 复用现有 `Device`、`ApplicationTask`、`ApplicationEvent`、`ApplicationTaskStatus`、`TaskActor` 和 task lease；本计划不新增数据库实体或 Alembic migration。
- Executor 只能使用 `task:progress` 报告执行推进，使用 `task:result` 报告 HUMAN 已提交后的观察结果；系统不得签发、接受或在 OpenAPI 中出现 `task:submit`。
- 任何任务动作必须同时验证 device token、URL path task ID、`X-Task-ID`、`X-Task-Lease` scope、`device_id` 分配和 `state_version`。
- `READY_FOR_REVIEW → OBSERVING_USER_SUBMISSION` 只能由 `TaskActor.HUMAN` 执行；Executor 和 SYSTEM 不得模拟这条边。
- 单页底部动作、多页末页动作、组合动作、最终动作和无法分类的按钮一律不点击，转入 `ready_for_review`。
- 只有明确的多页非末页、存在可验证后续步骤且按钮被确定性规则判为纯中间动作时，才允许一次中间点击。
- 最终提交按钮自动点击次数必须为 0；歧义按钮点击次数必须为 0；恢复后字段重复填写次数和中间副作用重复执行次数必须为 0。
- 登录、短信、扫码、验证码、人机验证、必填字段缺失、低置信度映射、回读不一致和页面拓扑变化必须停止主动推进并交给用户。
- 缺失或低置信度字段不能阻止其他已确认字段填写；Executor 不得编造答案或随机选择值。
- 本地检查点只保存协议版本、task ID/version、步骤、页面指纹、字段 key、脱敏计数和副作用 key；不得保存密码、device token、task lease、Cookie、验证码、完整表单值、完整简历或本地敏感字段明文。
- device token 和设备私钥只保存到当前 Windows 用户的 Credential Locker；不得放入命令参数、环境变量、普通 JSON、日志或测试快照。
- Wave 1 只访问 loopback 模拟站点，不实现大疆 Moka 或其他真实招聘站点，不处理验证码，不绕过反自动化，不上传截图，不启用 LLM 页面决策。
- API 错误使用稳定 `error_code`：401 表示设备/lease 无效，404 隐藏非本设备任务，409 表示 stale version 或状态冲突，422 表示协议/领域校验失败，503 表示所需依赖不可用。
- 日志和事件只记录实体 ID、稳定错误码、页面指纹和脱敏计数，不记录 token、lease、URL 查询串、字段值、DOM、Cookie、验证码或外部错误正文。
- 共享入口 `backend/app/api/router.py` 只在独立集成步骤修改；不修改 `backend/app/db/models.py`、`backend/app/db/__init__.py`、`frontend/src/App.vue`、`frontend/src/api.ts`、`alembic/env.py` 或任何 `alembic/versions/*`。
- 所有 Python 命令使用项目根目录 `.venv\Scripts\python.exe`；Playwright 浏览器版本与 `playwright==1.61.0` 对应安装。

---

## File Structure

### 新建文件

- `requirements-executor.txt`：Windows Executor 独立、可复现的运行依赖，复用根依赖并固定 Playwright 和 keyring。
- `backend/app/api/executor_schemas.py`：Backend 的 `executor.v1` 请求/响应白名单 DTO。
- `backend/app/repositories/executor_tasks.py`：只查询分配给当前设备的任务，不包含状态写入。
- `backend/app/services/executor_tasks.py`：任务 payload provider 边界、进度/结果校验和对现有 `ApplicationService` 的编排。
- `backend/app/api/routes/executor_tasks.py`：`/api/executor/tasks`、任务详情、progress 和 result 端点。
- `executor/__init__.py`：本地 Executor 包边界和版本常量。
- `executor/protocol.py`：本地端 `executor.v1` DTO，与 Backend DTO 通过 fixture 契约测试保持一致。
- `executor/secrets.py`：Windows Credential Locker 适配器和测试可替换的 secret store protocol。
- `executor/client.py`：出站 HTTP 客户端；只重试读取，不盲目重试可能已产生状态变化的写请求。
- `executor/checkpoints.py`：无敏感值的原子 JSON 检查点和恢复校验。
- `executor/safety.py`：页面拓扑、动作风险和确定性放行规则。
- `executor/browser.py`：Playwright persistent Chromium、页面观察、字段填写回读和中间动作执行。
- `executor/engine.py`：任务读取、lease、浏览器执行、人工接管、冲突对账和恢复编排。
- `executor/cli.py`：`pair`、`run-simulation`、`resume-simulation` 三个 Windows 命令入口。
- `executor/mock_site/__init__.py`：模拟站点包。
- `executor/mock_site/app.py`：独立 FastAPI 模拟招聘站点和重置/计数端点。
- `executor/mock_site/pages/single-page.html`：单页表单与最终按钮陷阱。
- `executor/mock_site/pages/multi-step-1.html`：明确多页非末页和纯中间动作。
- `executor/mock_site/pages/multi-step-2.html`：明确多页末页和最终按钮。
- `executor/mock_site/pages/ambiguous.html`：组合文案和图标歧义按钮。
- `executor/mock_site/pages/human-gate.html`：登录/验证码式人工接管样本。
- `executor/mock_site/pages/readback-mismatch.html`：写入后重置字段的回读异常样本。
- `executor/mock_site/pages/submission-success.html`：HUMAN 提交后可确定为成功的只读结果页。
- `executor/mock_site/pages/submission-failed.html`：HUMAN 提交后可确定为失败的只读结果页。
- `executor/mock_site/pages/submission-unknown.html`：HUMAN 提交后无法确定结果的只读结果页。
- `tests/fixtures/executor/protocol_v1/task.json`：非敏感、版本化 simulation task payload。
- `tests/unit/test_executor_protocol.py`：Backend/Executor DTO、scope allowlist 和 fixture 契约。
- `tests/unit/test_executor_task_service.py`：设备归属、状态边、脱敏事件和错误分类。
- `tests/unit/test_executor_client.py`：HTTP header、读取重试、写入不重试和 secret redaction。
- `tests/unit/test_executor_checkpoints.py`：原子写入、损坏恢复、版本/指纹校验和敏感值拒绝。
- `tests/unit/test_executor_safety.py`：单页、多页、末页、最终、组合、歧义动作决策表。
- `tests/contract/test_executor_api.py`：设备认证、task/lease/scope/version 绑定、404 隔离和 OpenAPI 契约。
- `tests/contract/test_executor_mock_site.py`：模拟页面拓扑标记和计数器契约。
- `tests/integration/test_executor_simulation.py`：真实 Playwright Chromium 的填写、回读、人工接管和安全点击门禁。
- `tests/integration/test_executor_recovery.py`：断线、lease 过期、409、进程重启、pending effect 和不重复证明。
- `tests/security/test_executor_redaction.py`：API、事件、日志和本地文件不含敏感内容。
- `docs/runbooks/windows-executor-simulation.md`：Windows 安装、Credential Locker 配对、模拟运行、恢复和验收命令。

### 修改文件

- `backend/app/services/devices.py`：增加固定 scope allowlist，并拒绝签发任何未知 scope。
- `backend/app/api/router.py`：在共享入口集成步骤仅挂载 `executor_tasks.router`。
- `tests/unit/test_device_service.py`：证明未知 scope 和 `task:submit` 无法签发。
- `.gitignore`：忽略本地 Chromium profile、检查点和 Playwright 测试产物。

## Interfaces and Dependency Gates

| Task | Produces | Consumes | Parallel / Blocking |
| --- | --- | --- | --- |
| 0 | `executor.v1` fixture、DTO 名称、scope allowlist、依赖锁定 | 已批准并行规格、现有 task 状态机 | 阻塞本计划其余任务；不阻塞 A/B/D |
| 1 | 设备隔离的 task list/detail 查询 | Task 0 DTO、现有 Device/ApplicationTask | 阻塞 Task 2、3、8 |
| 2 | progress/result 写 API 和 OpenAPI 安全断言 | Task 1、现有 ApplicationService | 阻塞端到端联调 |
| 3 | Credential Locker 与 HTTP client | Task 0/1/2 HTTP 契约 | 可与 Task 4–6 并行 |
| 4 | 原子无敏感检查点 | Task 0 DTO | 可与 Task 3、5、6 并行 |
| 5 | 确定性安全门 | Task 0 page/action enum | 可与 Task 3、4、6 并行 |
| 6 | 可观测模拟站点 | Task 0 fixture | 可与 Task 3–5 并行 |
| 7 | Playwright 浏览器适配层 | Task 4–6 | 阻塞 Task 8–9 |
| 8 | Executor engine 和基本人工接管 | Task 2–7 | 阻塞恢复门禁 |
| 9 | 断线/重启/lease/stale 恢复证明 | Task 8 | 阻塞完成声明 |
| 10 | CLI、共享路由接线、运行手册和全局回归 | Task 1–9；共享入口协调窗口 | 本工作流最终门禁 |

---

### Task 0: Freeze the `executor.v1` contract and prove no submit scope exists

**Files:**
- Create: `requirements-executor.txt`
- Create: `executor/__init__.py`
- Create: `executor/protocol.py`
- Create: `tests/fixtures/executor/protocol_v1/task.json`
- Create: `tests/unit/test_executor_protocol.py`
- Modify: `backend/app/services/devices.py`
- Modify: `tests/unit/test_device_service.py`

**Interfaces:**
- Consumes: `ApplicationTaskStatus`, `TaskActor`, `DeviceService.issue_task_lease`, approved migration baseline `20260716_0004`.
- Produces: `PROTOCOL_VERSION: Literal["executor.v1"]`; `ALLOWED_TASK_LEASE_SCOPES = frozenset({"task:progress", "task:result"})`; `ExecutorTaskPayload`, `ExecutorField`, `FieldConfidence`; fixture path `tests/fixtures/executor/protocol_v1/task.json`.
- Migration gate: `alembic/versions/` and `backend/app/db/models.py` remain byte-for-byte unchanged in this workstream.

- [ ] **Step 1: Write failing scope and protocol tests**

Create `tests/unit/test_executor_protocol.py`:

```python
from pathlib import Path

import pytest

from backend.app.services.devices import ALLOWED_TASK_LEASE_SCOPES
from executor.protocol import ExecutorTaskPayload, PROTOCOL_VERSION


FIXTURE = Path("tests/fixtures/executor/protocol_v1/task.json")


def test_executor_v1_fixture_is_non_sensitive_and_parseable() -> None:
    raw = FIXTURE.read_text(encoding="utf-8")
    payload = ExecutorTaskPayload.model_validate_json(raw)
    assert payload.protocol_version == PROTOCOL_VERSION == "executor.v1"
    assert payload.target_url.host in {"127.0.0.1", "localhost"}
    assert all(field.sensitive is False for field in payload.fields)
    assert not {
        "password", "cookie", "captcha", "id_card", "resume_text", "task_lease"
    } & set(raw.lower().replace('"', "").split())


def test_task_lease_scope_allowlist_has_no_submit_capability() -> None:
    assert ALLOWED_TASK_LEASE_SCOPES == frozenset(
        {"task:progress", "task:result"}
    )
    assert "task:submit" not in ALLOWED_TASK_LEASE_SCOPES
```

Append to `tests/unit/test_device_service.py`:

```python
from backend.app.services.devices import InvalidTaskLeaseError


def test_device_service_refuses_to_issue_unknown_or_submit_scope(
    device_service, db, issued_device
) -> None:
    task = ApplicationTask(
        user_id=issued_device.device.user_id,
        target_job_id="simulation-job",
        device_id=issued_device.device.id,
    )
    db.add(task)
    db.commit()
    service = DeviceService(device_service.redis, lease_secret="x" * 32)

    with pytest.raises(InvalidTaskLeaseError, match="scope"):
        service.issue_task_lease(
            db,
            device=issued_device.device,
            task_id=task.id,
            scopes={"task:progress", "task:submit"},
        )
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_executor_protocol.py tests/unit/test_device_service.py -q
```

Expected: collection fails because `executor.protocol` and `ALLOWED_TASK_LEASE_SCOPES` do not exist.

- [ ] **Step 3: Add pinned Executor dependencies and protocol models**

Create `requirements-executor.txt`:

```text
-r requirements.txt
playwright==1.61.0
keyring==25.7.0
```

Create `executor/__init__.py`:

```python
EXECUTOR_VERSION = "0.1.0"
```

Create `executor/protocol.py`:

```python
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
```

Create `tests/fixtures/executor/protocol_v1/task.json`:

```json
{
  "protocol_version": "executor.v1",
  "task_id": "11111111-1111-4111-8111-111111111111",
  "state_version": 0,
  "target_url": "http://127.0.0.1:8765/single-page",
  "fields": [
    {
      "field_key": "full_name",
      "label": "姓名",
      "value": "Alice Example",
      "confidence": "confirmed",
      "required": true,
      "sensitive": false
    },
    {
      "field_key": "portfolio_url",
      "label": "作品链接",
      "value": null,
      "confidence": "missing",
      "required": false,
      "sensitive": false
    }
  ]
}
```

- [ ] **Step 4: Enforce the server-side lease scope allowlist**

In `backend/app/services/devices.py`, add the constant next to the lease TTL and validate before reading the task:

```python
TASK_LEASE_TTL_SECONDS = 300
ALLOWED_TASK_LEASE_SCOPES = frozenset({"task:progress", "task:result"})


def _validate_task_lease_scopes(scopes: set[str]) -> None:
    if not scopes or not scopes <= ALLOWED_TASK_LEASE_SCOPES:
        raise InvalidTaskLeaseError("task lease scope is not allowed")
```

Call it as the first line of `DeviceService.issue_task_lease`:

```python
    def issue_task_lease(
        self, db: Session, *, device: Device, task_id: str, scopes: set[str]
    ) -> str:
        _validate_task_lease_scopes(scopes)
        if not self.lease_secret:
            raise InvalidTaskLeaseError("task lease signing is not configured")
```

Export `ALLOWED_TASK_LEASE_SCOPES` in `__all__`.

- [ ] **Step 5: Install Executor dependencies and run the contract tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-executor.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe -m pytest tests/unit/test_executor_protocol.py tests/unit/test_device_service.py tests/unit/test_final_review_fixes.py -q
```

Expected: all selected tests PASS; browser install exits 0; existing `task:progress` and `task:result` lease verification still passes.

- [ ] **Step 6: Prove the no-migration gate and commit**

Run:

```powershell
git diff --name-only -- alembic backend/app/db/models.py backend/app/db/__init__.py
```

Expected: no output.

Commit:

```powershell
git add requirements-executor.txt executor/__init__.py executor/protocol.py tests/fixtures/executor/protocol_v1/task.json tests/unit/test_executor_protocol.py backend/app/services/devices.py tests/unit/test_device_service.py
git commit -m "feat: freeze executor v1 safety contract"
```

### Task 1: Add device-isolated task discovery and leased task detail

**Files:**
- Create: `backend/app/api/executor_schemas.py`
- Create: `backend/app/repositories/executor_tasks.py`
- Create: `backend/app/services/executor_tasks.py`
- Create: `backend/app/api/routes/executor_tasks.py`
- Create: `tests/unit/test_executor_task_service.py`
- Create: `tests/contract/test_executor_api.py`

**Interfaces:**
- Consumes: `PROTOCOL_VERSION == "executor.v1"`; `Device.id/user_id`; existing `ApplicationTask.status/state_version/device_id`; `require_task_progress_lease`.
- Produces: `GET /api/executor/tasks -> ExecutorTaskListResponse`; `GET /api/executor/tasks/{task_id} -> ExecutorTaskDetail`; `ExecutorPayloadProvider.payload_for(task) -> ExecutorTaskPayload`.
- Discovery exception: list returns only assigned task IDs/status/version under device authentication; payload detail is task-specific and requires `task:progress` lease plus matching `X-Task-ID`.

- [ ] **Step 1: Write failing repository/service isolation tests**

Create `tests/unit/test_executor_task_service.py` with these concrete SQLite and identity fixtures:

```python
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db.base import Base
from backend.app.db.models import Device, DevicePlatform, DeviceStatus, User


@pytest.fixture
def db() -> Session:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as value:
        yield value


def create_user_and_device(db: Session, account: str) -> tuple[User, Device]:
    user = User(account=account, nickname=account, password_hash="hash")
    db.add(user)
    db.flush()
    device = Device(
        user_id=user.id,
        name=f"{account}-windows",
        platform=DevicePlatform.WINDOWS,
        status=DeviceStatus.ACTIVE,
        token_hash=f"hash-{account}",
        public_key_pem="test-public-key",
    )
    db.add(device)
    db.commit()
    return user, device


@pytest.fixture
def alice_user(db: Session) -> User:
    return create_user_and_device(db, "alice")[0]


@pytest.fixture
def alice_device(db: Session, alice_user: User) -> Device:
    return db.query(Device).filter(Device.user_id == alice_user.id).one()


@pytest.fixture
def bob_device(db: Session) -> Device:
    return create_user_and_device(db, "bob")[1]
```

Then add:

```python
def test_list_only_returns_tasks_assigned_to_authenticated_device(
    db, alice_device, bob_device, alice_user
) -> None:
    own = ApplicationTask(
        user_id=alice_user.id,
        target_job_id="simulation-job",
        device_id=alice_device.id,
        status=ApplicationTaskStatus.DISPATCHED,
    )
    other = ApplicationTask(
        user_id=bob_device.user_id,
        target_job_id="other-job",
        device_id=bob_device.id,
        status=ApplicationTaskStatus.DISPATCHED,
    )
    db.add_all([own, other])
    db.commit()

    listed = ExecutorTaskService().list_assigned(db, device=alice_device)

    assert [item.id for item in listed] == [own.id]


def test_detail_hides_task_owned_by_another_device(db, alice_device, bob_device) -> None:
    task = ApplicationTask(
        user_id=bob_device.user_id,
        target_job_id="other-job",
        device_id=bob_device.id,
        status=ApplicationTaskStatus.DISPATCHED,
    )
    db.add(task)
    db.commit()

    with pytest.raises(ExecutorTaskNotFoundError):
        ExecutorTaskService().get_assigned(db, device=alice_device, task_id=task.id)
```

- [ ] **Step 2: Run the service tests and verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_executor_task_service.py -q
```

Expected: collection fails because `backend.app.services.executor_tasks` does not exist.

- [ ] **Step 3: Implement assigned-task queries and DTOs**

Create `backend/app/repositories/executor_tasks.py`:

```python
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.db.models import ApplicationTask, ApplicationTaskStatus


ACTIVE_EXECUTOR_STATUSES = frozenset(
    {
        ApplicationTaskStatus.DISPATCHED,
        ApplicationTaskStatus.RUNNING,
        ApplicationTaskStatus.WAITING_FOR_HUMAN,
        ApplicationTaskStatus.READY_FOR_REVIEW,
        ApplicationTaskStatus.OBSERVING_USER_SUBMISSION,
    }
)


def list_assigned(db: Session, *, device_id: str, user_id: str) -> list[ApplicationTask]:
    return list(
        db.scalars(
            select(ApplicationTask)
            .where(
                ApplicationTask.device_id == device_id,
                ApplicationTask.user_id == user_id,
                ApplicationTask.status.in_(ACTIVE_EXECUTOR_STATUSES),
            )
            .order_by(ApplicationTask.updated_at.asc(), ApplicationTask.id.asc())
        )
    )


def get_assigned(
    db: Session, *, device_id: str, user_id: str, task_id: str
) -> ApplicationTask | None:
    return db.scalar(
        select(ApplicationTask).where(
            ApplicationTask.id == task_id,
            ApplicationTask.device_id == device_id,
            ApplicationTask.user_id == user_id,
        )
    )
```

Create `backend/app/api/executor_schemas.py` with explicit response fields and the same field constraints as `executor/protocol.py`:

```python
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
```

- [ ] **Step 4: Implement the payload provider boundary and task service**

Create `backend/app/services/executor_tasks.py`:

```python
from typing import Protocol

from sqlalchemy.orm import Session

from backend.app.api.executor_schemas import ExecutorTaskPayload
from backend.app.db.models import ApplicationTask, Device
from backend.app.repositories import executor_tasks


class ExecutorTaskNotFoundError(LookupError):
    pass


class ExecutorPayloadUnavailableError(RuntimeError):
    pass


class ExecutorPayloadProvider(Protocol):
    def payload_for(self, task: ApplicationTask) -> ExecutorTaskPayload:
        raise NotImplementedError


class UnavailableExecutorPayloadProvider:
    def payload_for(self, task: ApplicationTask) -> ExecutorTaskPayload:
        raise ExecutorPayloadUnavailableError(task.id)


class SimulationExecutorPayloadProvider:
    ROUTES = {
        "simulation-single": "/single-page",
        "simulation-multi": "/multi-step/1",
        "simulation-ambiguous": "/ambiguous",
        "simulation-human": "/human-gate",
        "simulation-mismatch": "/readback-mismatch",
        "simulation-result-success": "/submission-success",
        "simulation-result-failed": "/submission-failed",
        "simulation-result-unknown": "/submission-unknown",
    }

    def __init__(self, base_url: str = "http://127.0.0.1:8765") -> None:
        self.base_url = base_url.rstrip("/")

    def payload_for(self, task: ApplicationTask) -> ExecutorTaskPayload:
        route = self.ROUTES.get(task.target_job_id)
        if route is None:
            raise ExecutorPayloadUnavailableError(task.id)
        return ExecutorTaskPayload(
            task_id=task.id,
            state_version=task.state_version,
            target_url=f"{self.base_url}{route}",
            fields=[
                {
                    "field_key": "full_name",
                    "label": "姓名",
                    "value": "Alice Example",
                    "confidence": "confirmed",
                    "required": True,
                    "sensitive": False,
                },
                {
                    "field_key": "portfolio_url",
                    "label": "作品链接",
                    "value": None,
                    "confidence": "missing",
                    "required": False,
                    "sensitive": False,
                },
            ],
        )


class ExecutorTaskService:
    def __init__(self, payload_provider: ExecutorPayloadProvider | None = None) -> None:
        self.payload_provider = payload_provider or UnavailableExecutorPayloadProvider()

    def list_assigned(self, db: Session, *, device: Device) -> list[ApplicationTask]:
        return executor_tasks.list_assigned(
            db, device_id=device.id, user_id=device.user_id
        )

    def get_assigned(
        self, db: Session, *, device: Device, task_id: str
    ) -> ApplicationTask:
        task = executor_tasks.get_assigned(
            db, device_id=device.id, user_id=device.user_id, task_id=task_id
        )
        if task is None:
            raise ExecutorTaskNotFoundError(task_id)
        return task

    def get_payload(
        self, db: Session, *, device: Device, task_id: str
    ) -> tuple[ApplicationTask, ExecutorTaskPayload]:
        task = self.get_assigned(db, device=device, task_id=task_id)
        payload = self.payload_provider.payload_for(task)
        if payload.task_id != task.id or payload.state_version != task.state_version:
            raise ExecutorPayloadUnavailableError(task.id)
        return task, payload
```

- [ ] **Step 5: Write failing API tests for device-only discovery and leased detail**

Create `tests/contract/test_executor_api.py` using the app/SQLite/fakeredis setup from `tests/contract/test_device_api.py`. Inject a provider whose `payload_for` returns the validated fixture with the seeded task ID/version, then assert:

```python
def test_executor_list_is_device_isolated_and_detail_requires_matching_lease(
    client, paired_device, seeded_task, payload_provider
) -> None:
    headers = {"X-Device-Token": paired_device.token}
    listed = client.get("/api/executor/tasks", headers=headers)
    assert listed.status_code == 200
    assert [item["task_id"] for item in listed.json()["tasks"]] == [seeded_task.id]
    assert "payload" not in listed.text

    no_lease = client.get(
        f"/api/executor/tasks/{seeded_task.id}", headers=headers
    )
    assert no_lease.status_code == 401

    lease = issue_lease(client, paired_device.token, seeded_task.id)
    detail = client.get(
        f"/api/executor/tasks/{seeded_task.id}",
        headers={
            **headers,
            "X-Task-ID": seeded_task.id,
            "X-Task-Lease": lease,
        },
    )
    assert detail.status_code == 200
    assert detail.json()["payload"]["protocol_version"] == "executor.v1"
```

- [ ] **Step 6: Implement read routes without touching the shared router yet**

Create `backend/app/api/routes/executor_tasks.py` with these exact dependency and mapper definitions. Development uses only the fixed loopback simulation mapping; test code may inject a provider; production never serves a fixture payload:

```python
def get_executor_task_service(request: Request) -> ExecutorTaskService:
    injected = getattr(request.app.state, "executor_payload_provider", None)
    if injected is not None:
        return ExecutorTaskService(injected)
    if request.app.state.settings.app_env == "development":
        return ExecutorTaskService(SimulationExecutorPayloadProvider())
    return ExecutorTaskService()


def _summary(task: ApplicationTask) -> ExecutorTaskSummary:
    return ExecutorTaskSummary(
        task_id=task.id,
        target_job_id=task.target_job_id,
        snapshot_id=task.snapshot_id,
        status=task.status,
        state_version=task.state_version,
    )
```

Then set `router = APIRouter(prefix="/executor/tasks", tags=["executor"])` and add these handlers:

```python
@router.get("", response_model=ExecutorTaskListResponse)
def list_executor_tasks(
    device: Annotated[Device, Depends(get_current_device)],
    db: Annotated[Session, Depends(_get_db)],
    service: Annotated[ExecutorTaskService, Depends(get_executor_task_service)],
) -> ExecutorTaskListResponse:
    return ExecutorTaskListResponse(
        tasks=[_summary(task) for task in service.list_assigned(db, device=device)]
    )


@router.get("/{task_id}", response_model=ExecutorTaskDetail)
def get_executor_task(
    task_id: str,
    header_task_id: Annotated[str, Header(alias="X-Task-ID")],
    device: Annotated[Device, Depends(require_task_progress_lease)],
    db: Annotated[Session, Depends(_get_db)],
    service: Annotated[ExecutorTaskService, Depends(get_executor_task_service)],
) -> ExecutorTaskDetail:
    if not hmac.compare_digest(task_id, header_task_id):
        raise HTTPException(status_code=401, detail={"error_code": "invalid_task_lease"})
    try:
        task, payload = service.get_payload(db, device=device, task_id=task_id)
    except ExecutorTaskNotFoundError:
        raise HTTPException(
            status_code=404, detail={"error_code": "executor_task_not_found"}
        ) from None
    except ExecutorPayloadUnavailableError:
        raise HTTPException(
            status_code=409, detail={"error_code": "executor_payload_unavailable"}
        ) from None
    return ExecutorTaskDetail(**_summary(task).model_dump(), payload=payload)
```

Do not mount the router in `backend/app/api/router.py` in this task. In the contract fixture only, append `app.include_router(executor_tasks.router, prefix="/api")` so the feature route can be tested without contending on the shared integration file.

- [ ] **Step 7: Run task query and API tests, then commit**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_executor_task_service.py tests/contract/test_executor_api.py -q
```

Expected: all tests PASS; cross-device detail returns 404 and detail without a valid progress lease returns 401.

Commit:

```powershell
git add backend/app/api/executor_schemas.py backend/app/repositories/executor_tasks.py backend/app/services/executor_tasks.py backend/app/api/routes/executor_tasks.py tests/unit/test_executor_task_service.py tests/contract/test_executor_api.py
git commit -m "feat: add leased executor task reads"
```

### Task 2: Add progress and post-human result endpoints with strict actor/scope binding

**Files:**
- Modify: `backend/app/api/executor_schemas.py`
- Modify: `backend/app/services/executor_tasks.py`
- Modify: `backend/app/api/routes/executor_tasks.py`
- Modify: `tests/unit/test_executor_task_service.py`
- Modify: `tests/contract/test_executor_api.py`

**Interfaces:**
- Consumes: `ApplicationService.transition`; `require_task_progress_lease`; `require_task_result_lease`; path/header task-ID equality; current `ApplicationTask.state_version`.
- Produces: `POST /api/executor/tasks/{task_id}/progress`; `POST /api/executor/tasks/{task_id}/result`; `ExecutorTaskState`; stable errors `stale_task_version`, `invalid_executor_transition`, `executor_task_not_found`.
- Progress target allowlist: `running`, `waiting_for_human`, `ready_for_review`, `failed`; result target allowlist: `submitted_success`, `submitted_failed`, `result_unknown`.

- [ ] **Step 1: Write failing service tests for allowed and forbidden transitions**

Add to `tests/unit/test_executor_task_service.py`:

```python
def test_progress_uses_executor_actor_and_appends_only_redacted_counts(
    db, alice_device, alice_user
) -> None:
    task = ApplicationTask(
        user_id=alice_user.id,
        target_job_id="simulation-job",
        device_id=alice_device.id,
        status=ApplicationTaskStatus.DISPATCHED,
    )
    db.add(task)
    db.commit()

    updated = ExecutorTaskService().report_progress(
        db,
        device=alice_device,
        task_id=task.id,
        expected_version=0,
        target=ApplicationTaskStatus.RUNNING,
        page_fingerprint="sha256:abc123",
        page_index=1,
        reason_code=None,
        field_counts={"confirmed": 1, "defaulted": 0, "missing": 1, "low": 0},
    )
    event = db.scalar(select(ApplicationEvent).where(ApplicationEvent.task_id == task.id))
    assert updated.status is ApplicationTaskStatus.RUNNING
    assert event.actor is TaskActor.EXECUTOR
    assert event.redacted_payload == {
        "page_fingerprint": "sha256:abc123",
        "page_index": 1,
        "reason_code": "",
        "confirmed_count": 1,
        "defaulted_count": 0,
        "missing_count": 1,
        "low_confidence_count": 0,
    }


def test_executor_result_is_rejected_until_human_started_observation(
    db, alice_device, alice_user
) -> None:
    task = ApplicationTask(
        user_id=alice_user.id,
        target_job_id="simulation-job",
        device_id=alice_device.id,
        status=ApplicationTaskStatus.READY_FOR_REVIEW,
    )
    db.add(task)
    db.commit()

    with pytest.raises(InvalidTransitionError):
        ExecutorTaskService().report_result(
            db,
            device=alice_device,
            task_id=task.id,
            expected_version=0,
            target=ApplicationTaskStatus.SUBMITTED_SUCCESS,
            page_fingerprint="sha256:result",
            reason_code="success_marker",
        )
```

- [ ] **Step 2: Run the focused service tests and verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_executor_task_service.py -q
```

Expected: FAIL because `report_progress` and `report_result` are not defined.

- [ ] **Step 3: Define write DTOs with extra fields forbidden**

Append to `backend/app/api/executor_schemas.py`:

```python
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
```

- [ ] **Step 4: Implement service writes through the existing actor matrix**

In `backend/app/services/executor_tasks.py`, use the existing `get_assigned` method as the assignment check and add these methods; do not update `ApplicationTask` directly:

```python
    def report_progress(
        self,
        db: Session,
        *,
        device: Device,
        task_id: str,
        expected_version: int,
        target: ApplicationTaskStatus,
        page_fingerprint: str,
        page_index: int | None,
        reason_code: str | None,
        field_counts: dict[str, int],
    ) -> ApplicationTask:
        self.get_assigned(db, device=device, task_id=task_id)
        task = ApplicationService().transition(
            db,
            task_id=task_id,
            expected_version=expected_version,
            target=target,
            actor=TaskActor.EXECUTOR,
            event_type="executor.progress",
            redacted_payload={
                "page_fingerprint": page_fingerprint,
                "page_index": page_index,
                "reason_code": reason_code or "",
                "confirmed_count": field_counts["confirmed"],
                "defaulted_count": field_counts["defaulted"],
                "missing_count": field_counts["missing"],
                "low_confidence_count": field_counts["low"],
            },
        )
        db.commit()
        return task

    def report_result(
        self,
        db: Session,
        *,
        device: Device,
        task_id: str,
        expected_version: int,
        target: ApplicationTaskStatus,
        page_fingerprint: str,
        reason_code: str,
    ) -> ApplicationTask:
        self.get_assigned(db, device=device, task_id=task_id)
        task = ApplicationService().transition(
            db,
            task_id=task_id,
            expected_version=expected_version,
            target=target,
            actor=TaskActor.EXECUTOR,
            event_type="executor.result_observed",
            redacted_payload={
                "page_fingerprint": page_fingerprint,
                "reason_code": reason_code,
            },
        )
        db.commit()
        return task
```

Import `ApplicationTaskStatus`, `TaskActor`, `ApplicationService`, `InvalidTransitionError`, `StaleTaskVersionError`, and `TaskNotFoundError` explicitly.

- [ ] **Step 5: Write failing API security tests**

Add parameterized cases to `tests/contract/test_executor_api.py` that prove: missing device token is 401; lease for task A with path task B is 401; progress-only lease is rejected by result; another device gets 404; stale version gets 409; `READY_FOR_REVIEW` result gets 409; and request bodies containing `form_values`, `cookie` or `task_lease` get 422. Add this OpenAPI invariant:

```python
def test_executor_openapi_has_no_submit_operation_or_scope(client) -> None:
    schema_text = client.get("/openapi.json").text.lower()
    assert "task:progress" not in schema_text
    assert "task:result" not in schema_text
    assert "task:submit" not in schema_text
    assert "/api/executor/tasks/{task_id}/progress" in schema_text
    assert "/api/executor/tasks/{task_id}/result" in schema_text
```

The progress/result scope strings stay in signed lease claims and server dependencies; they are not request-body or response fields.

- [ ] **Step 6: Add progress/result routes and stable error mapping**

Add a `_require_path_binding(task_id, header_task_id)` constant-time comparison and `_task_error(error)` mapper to `backend/app/api/routes/executor_tasks.py`, then add:

```python
@router.post("/{task_id}/progress", response_model=ExecutorTaskState)
def report_executor_progress(
    task_id: str,
    body: ExecutorProgressRequest,
    header_task_id: Annotated[str, Header(alias="X-Task-ID")],
    device: Annotated[Device, Depends(require_task_progress_lease)],
    db: Annotated[Session, Depends(_get_db)],
    service: Annotated[ExecutorTaskService, Depends(get_executor_task_service)],
) -> ExecutorTaskState:
    _require_path_binding(task_id, header_task_id)
    try:
        task = service.report_progress(
            db,
            device=device,
            task_id=task_id,
            expected_version=body.expected_version,
            target=ApplicationTaskStatus(body.target_status),
            page_fingerprint=body.page_fingerprint,
            page_index=body.page_index,
            reason_code=body.reason_code,
            field_counts=body.field_counts.model_dump(),
        )
    except (ExecutorTaskNotFoundError, StaleTaskVersionError, InvalidTransitionError) as error:
        raise _task_error(error) from None
    return ExecutorTaskState(task_id=task.id, status=task.status, state_version=task.state_version)


@router.post("/{task_id}/result", response_model=ExecutorTaskState)
def report_executor_result(
    task_id: str,
    body: ExecutorResultRequest,
    header_task_id: Annotated[str, Header(alias="X-Task-ID")],
    device: Annotated[Device, Depends(require_task_result_lease)],
    db: Annotated[Session, Depends(_get_db)],
    service: Annotated[ExecutorTaskService, Depends(get_executor_task_service)],
) -> ExecutorTaskState:
    _require_path_binding(task_id, header_task_id)
    try:
        task = service.report_result(
            db,
            device=device,
            task_id=task_id,
            expected_version=body.expected_version,
            target=ApplicationTaskStatus(body.target_status),
            page_fingerprint=body.page_fingerprint,
            reason_code=body.reason_code,
        )
    except (ExecutorTaskNotFoundError, StaleTaskVersionError, InvalidTransitionError) as error:
        raise _task_error(error) from None
    return ExecutorTaskState(task_id=task.id, status=task.status, state_version=task.state_version)
```

Use this stable mapper; do not return exception strings:

```python
def _task_error(error: Exception) -> HTTPException:
    if isinstance(error, ExecutorTaskNotFoundError):
        return HTTPException(
            status_code=404,
            detail={"error_code": "executor_task_not_found"},
        )
    if isinstance(error, StaleTaskVersionError):
        return HTTPException(
            status_code=409,
            detail={"error_code": "stale_task_version"},
        )
    if isinstance(error, InvalidTransitionError):
        return HTTPException(
            status_code=409,
            detail={"error_code": "invalid_executor_transition"},
        )
    raise error
```

- [ ] **Step 7: Run API/state-machine regressions and commit**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_executor_task_service.py tests/contract/test_executor_api.py tests/unit/test_application_state_machine.py tests/contract/test_device_api.py -q
```

Expected: all tests PASS; no test can move `READY_FOR_REVIEW` to observation with `TaskActor.EXECUTOR`.

Commit:

```powershell
git add backend/app/api/executor_schemas.py backend/app/services/executor_tasks.py backend/app/api/routes/executor_tasks.py tests/unit/test_executor_task_service.py tests/contract/test_executor_api.py
git commit -m "feat: enforce executor progress and result leases"
```

### Task 3: Build the outbound client and Windows credential boundary

**Files:**
- Create: `executor/secrets.py`
- Create: `executor/client.py`
- Create: `tests/unit/test_executor_client.py`

**Interfaces:**
- Consumes: Task 1/2 HTTP DTOs and headers; existing `/api/devices/pair`, `/api/devices/task-lease`, `/api/devices/heartbeat`.
- Produces: `SecretStore.get/set/delete`; `WindowsCredentialStore`; `ExecutorApiClient.heartbeat/list_tasks/issue_lease/get_task/report_progress/report_result`; `ApiUnauthorized`, `ApiConflict`, `UncertainWriteResult`.

- [ ] **Step 1: Write failing client tests for headers and retry policy**

Create `tests/unit/test_executor_client.py` with `httpx.MockTransport` and an in-memory secret store. Assert that every authenticated request has `X-Device-Token`, every task-specific request has matching `X-Task-ID` and `X-Task-Lease`, GET retries two transient transport failures, and POST raises `UncertainWriteResult` after the first timeout with exactly one recorded attempt.

Use this core assertion:

```python
def test_progress_write_is_never_retried_after_timeout(client, transport) -> None:
    transport.fail_next_write_with_timeout = True
    with pytest.raises(UncertainWriteResult):
        client.report_progress(
            task_id="11111111-1111-4111-8111-111111111111",
            lease="lease-in-memory",
            expected_version=0,
            target_status="running",
            page_fingerprint="sha256:abc123",
            page_index=1,
            field_counts={"confirmed": 1, "defaulted": 0, "missing": 0, "low": 0},
            reason_code=None,
        )
    assert transport.progress_attempts == 1
```

- [ ] **Step 2: Run the client tests and verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_executor_client.py -q
```

Expected: collection fails because `executor.client` and `executor.secrets` do not exist.

- [ ] **Step 3: Implement Windows Credential Locker access without fallback plaintext**

Create `executor/secrets.py`:

```python
from typing import Protocol

import keyring
from keyring.errors import KeyringError


SERVICE_NAME = "career-assistant-executor"


class SecretStoreUnavailableError(RuntimeError):
    pass


class SecretStore(Protocol):
    def get(self, key: str) -> str | None:
        raise NotImplementedError

    def set(self, key: str, value: str) -> None:
        raise NotImplementedError

    def delete(self, key: str) -> None:
        raise NotImplementedError


class WindowsCredentialStore:
    def get(self, key: str) -> str | None:
        try:
            return keyring.get_password(SERVICE_NAME, key)
        except KeyringError as error:
            raise SecretStoreUnavailableError("windows credential store unavailable") from error

    def set(self, key: str, value: str) -> None:
        try:
            keyring.set_password(SERVICE_NAME, key, value)
        except KeyringError as error:
            raise SecretStoreUnavailableError("windows credential store unavailable") from error

    def delete(self, key: str) -> None:
        try:
            keyring.delete_password(SERVICE_NAME, key)
        except keyring.errors.PasswordDeleteError:
            return
        except KeyringError as error:
            raise SecretStoreUnavailableError("windows credential store unavailable") from error
```

- [ ] **Step 4: Implement the client with read-only retries and redacted errors**

First extend `executor/protocol.py` with the exact response types consumed by the client:

```python
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
```

Create `executor/client.py`. Store the constructor's `base_url: str` as `self.base_url`, build one `httpx.Client(base_url=self.base_url, timeout=10.0)`, obtain the device token from `SecretStore.get("device-token")` for each request, and never include response bodies in exceptions. Expose these exact method return types:

```python
def list_tasks(self) -> ExecutorTaskListResponse:
    response = self._read("GET", "/api/executor/tasks")
    return ExecutorTaskListResponse.model_validate(response.json())


def heartbeat(self, version: str) -> None:
    self._write(
        "POST",
        "/api/devices/heartbeat",
        json={"version": version},
    )


def issue_lease(self, task_id: str) -> str:
    response = self._write(
        "POST", "/api/devices/task-lease", json={"task_id": task_id}
    )
    return str(response.json()["lease"])


def get_task(self, task_id: str, lease: str) -> ExecutorTaskDetail:
    response = self._read(
        "GET",
        f"/api/executor/tasks/{task_id}",
        headers=self._task_headers(task_id, lease),
    )
    return ExecutorTaskDetail.model_validate(response.json())


def report_progress(
    self,
    *,
    task_id: str,
    lease: str,
    expected_version: int,
    target_status: str,
    page_fingerprint: str,
    page_index: int | None,
    field_counts: dict[str, int],
    reason_code: str | None,
) -> ExecutorTaskState:
    body = {
        "protocol_version": PROTOCOL_VERSION,
        "expected_version": expected_version,
        "target_status": target_status,
        "page_fingerprint": page_fingerprint,
        "page_index": page_index,
        "field_counts": field_counts,
        "reason_code": reason_code,
    }
    response = self._write(
        "POST",
        f"/api/executor/tasks/{task_id}/progress",
        headers=self._task_headers(task_id, lease),
        json=body,
    )
    return ExecutorTaskState.model_validate(response.json())


def report_result(
    self,
    *,
    task_id: str,
    lease: str,
    expected_version: int,
    target_status: str,
    page_fingerprint: str,
    reason_code: str,
) -> ExecutorTaskState:
    body = {
        "protocol_version": PROTOCOL_VERSION,
        "expected_version": expected_version,
        "target_status": target_status,
        "page_fingerprint": page_fingerprint,
        "reason_code": reason_code,
    }
    response = self._write(
        "POST",
        f"/api/executor/tasks/{task_id}/result",
        headers=self._task_headers(task_id, lease),
        json=body,
    )
    return ExecutorTaskState.model_validate(response.json())
```

The common task headers are:

```python
def _task_headers(self, task_id: str, lease: str) -> dict[str, str]:
    return {
        "X-Device-Token": self._device_token(),
        "X-Task-ID": task_id,
        "X-Task-Lease": lease,
    }
```

Implement `_read` with at most 3 total attempts for `httpx.TransportError`; implement `_write` with one attempt and translate `httpx.TransportError` to `UncertainWriteResult`. Translate 401 to `ApiUnauthorized`, 404 to `ApiTaskNotFound`, 409 to `ApiConflict(error_code)`, 422 to `ApiValidationError`, and 503 to `ApiDependencyUnavailable`. Parse successful bodies with models from `executor.protocol`; extend that module with summary/detail/progress/result response models matching Backend field names exactly.

- [ ] **Step 5: Run client tests and ensure secrets never enter messages**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_executor_client.py -q
```

Expected: all tests PASS; `repr(error)` and captured logs do not contain the fixture device token, task lease, response body, or target URL query string.

- [ ] **Step 6: Commit the outbound client**

```powershell
git add executor/protocol.py executor/secrets.py executor/client.py tests/unit/test_executor_client.py
git commit -m "feat: add secure executor outbound client"
```

### Task 4: Add atomic, non-sensitive local checkpoints

**Files:**
- Create: `executor/checkpoints.py`
- Create: `tests/unit/test_executor_checkpoints.py`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: `protocol_version`, task ID/version, SHA-256 page fingerprint and field/action keys.
- Produces: `ExecutorCheckpoint`; `CheckpointStore.load/save/delete`; `CheckpointMismatchError`; write-before-fill `pending_field_key` and write-before-click `pending_effect_key` protocols.

- [ ] **Step 1: Write failing checkpoint tests**

Create `tests/unit/test_executor_checkpoints.py`:

```python
def test_checkpoint_contains_only_keys_counts_and_fingerprint(tmp_path) -> None:
    store = CheckpointStore(tmp_path)
    checkpoint = ExecutorCheckpoint(
        protocol_version="executor.v1",
        task_id="11111111-1111-4111-8111-111111111111",
        task_state_version=1,
        step="fill_page",
        page_index=1,
        page_fingerprint="sha256:abc123",
        completed_field_keys=["full_name"],
        completed_effect_keys=[],
        pending_field_key=None,
        pending_effect_key=None,
        issue_counts={"missing": 1, "low": 0, "readback": 0},
    )
    store.save(checkpoint)
    raw = store.path_for(checkpoint.task_id).read_text(encoding="utf-8")
    assert "Alice Example" not in raw
    assert "device-token" not in raw
    assert "task_lease" not in raw
    assert store.load(checkpoint.task_id) == checkpoint


def test_pending_effect_survives_restart_and_forbids_retry(tmp_path) -> None:
    store = CheckpointStore(tmp_path)
    checkpoint = make_checkpoint(pending_effect_key="page-1:save-next")
    store.save(checkpoint)
    reloaded = CheckpointStore(tmp_path).load(checkpoint.task_id)
    assert reloaded.pending_effect_key == "page-1:save-next"


def test_pending_field_survives_restart_and_forbids_blind_refill(tmp_path) -> None:
    store = CheckpointStore(tmp_path)
    checkpoint = make_checkpoint(pending_field_key="full_name")
    store.save(checkpoint)
    reloaded = CheckpointStore(tmp_path).load(checkpoint.task_id)
    assert reloaded.pending_field_key == "full_name"
```

Also cover truncated JSON returning `CheckpointCorruptError`, protocol/task mismatch returning `CheckpointMismatchError`, and replacement leaving no `*.tmp` files.

- [ ] **Step 2: Run the checkpoint tests and verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_executor_checkpoints.py -q
```

Expected: collection fails because `executor.checkpoints` does not exist.

- [ ] **Step 3: Implement the constrained model and atomic store**

Create `executor/checkpoints.py`:

```python
from datetime import datetime, timezone
import json
from pathlib import Path
import re

from pydantic import BaseModel, ConfigDict, Field, ValidationError


SAFE_KEY = re.compile(r"^[a-z0-9][a-z0-9:_-]{0,119}$")


class CheckpointCorruptError(RuntimeError):
    pass


class CheckpointMismatchError(RuntimeError):
    pass


class ExecutorCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol_version: str
    task_id: str = Field(min_length=36, max_length=36)
    task_state_version: int = Field(ge=0)
    step: str = Field(pattern=r"^[a-z_]{1,40}$")
    page_index: int | None = Field(default=None, ge=1, le=100)
    page_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{6,64}$")
    completed_field_keys: list[str]
    completed_effect_keys: list[str]
    pending_field_key: str | None = None
    pending_effect_key: str | None
    issue_counts: dict[str, int]
    saved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CheckpointStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, task_id: str) -> Path:
        if not SAFE_KEY.fullmatch(task_id):
            raise ValueError("invalid task checkpoint key")
        return self.root / f"{task_id}.json"

    def save(self, checkpoint: ExecutorCheckpoint) -> None:
        target = self.path_for(checkpoint.task_id)
        temporary = target.with_suffix(".json.tmp")
        data = checkpoint.model_dump_json(indent=2)
        temporary.write_text(data, encoding="utf-8", newline="\n")
        temporary.replace(target)

    def load(self, task_id: str) -> ExecutorCheckpoint | None:
        target = self.path_for(task_id)
        if not target.exists():
            return None
        try:
            return ExecutorCheckpoint.model_validate_json(
                target.read_text(encoding="utf-8")
            )
        except (OSError, ValidationError, json.JSONDecodeError) as error:
            raise CheckpointCorruptError(task_id) from error

    def delete(self, task_id: str) -> None:
        self.path_for(task_id).unlink(missing_ok=True)
```

Before save, validate every field/effect/issue key with `SAFE_KEY` and allow only issue keys `missing`, `low`, `readback`, `defaulted`; reject any unexpected key rather than persisting it.

- [ ] **Step 4: Ignore local runtime artifacts**

Append to `.gitignore`:

```gitignore
executor-data/checkpoints/
executor-data/chrome-profile/
test-results/
playwright-report/
```

- [ ] **Step 5: Run checkpoint tests and commit**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_executor_checkpoints.py -q
```

Expected: all tests PASS; corrupt files fail closed and no sensitive value can be represented by the schema.

Commit:

```powershell
git add executor/checkpoints.py tests/unit/test_executor_checkpoints.py .gitignore
git commit -m "feat: add safe executor checkpoints"
```

### Task 5: Implement the deterministic topology and action safety gate

**Files:**
- Create: `executor/safety.py`
- Create: `tests/unit/test_executor_safety.py`

**Interfaces:**
- Consumes: page evidence `declared_topology`, `step_index`, `step_count`, `has_step_navigation`; action evidence `label`, `action_kind`, `is_bottom_action`.
- Produces: `PageTopology`; `ActionRisk`; `SafetyDecision(allowed, reason_code)`; `classify_topology`; `decide_action`.

- [ ] **Step 1: Write the complete failing decision table**

Create `tests/unit/test_executor_safety.py` with parameterized cases:

```python
@pytest.mark.parametrize(
    ("topology", "label", "action_kind", "allowed", "reason"),
    [
        (PageTopology.SINGLE_PAGE, "保存", "save", False, "single_page_bottom_action"),
        (PageTopology.MULTI_STEP_FINAL, "提交申请", "final", False, "final_action_forbidden"),
        (PageTopology.MULTI_STEP_INTERMEDIATE, "保存并下一步", "next", True, "safe_intermediate_action"),
        (PageTopology.MULTI_STEP_INTERMEDIATE, "保存并提交", "combined", False, "combined_action_forbidden"),
        (PageTopology.UNKNOWN, "继续", "unknown", False, "ambiguous_action_forbidden"),
        (PageTopology.MULTI_STEP_INTERMEDIATE, "完成申请", "next", False, "final_action_forbidden"),
    ],
)
def test_action_decision_table(topology, label, action_kind, allowed, reason) -> None:
    decision = decide_action(
        topology=topology,
        label=label,
        action_kind=action_kind,
        is_bottom_action=True,
        has_verified_next_step=topology is PageTopology.MULTI_STEP_INTERMEDIATE,
    )
    assert (decision.allowed, decision.reason_code) == (allowed, reason)
```

Add these cases to the same parameter table:

```python
        (PageTopology.UNKNOWN, "", "unknown", False, "ambiguous_action_forbidden"),
        (PageTopology.UNKNOWN, "⚡", "unknown", False, "ambiguous_action_forbidden"),
        (PageTopology.MULTI_STEP_FINAL, "submit", "final", False, "final_action_forbidden"),
        (PageTopology.MULTI_STEP_FINAL, "confirm application", "final", False, "final_action_forbidden"),
        (PageTopology.MULTI_STEP_FINAL, "finish", "final", False, "final_action_forbidden"),
        (PageTopology.MULTI_STEP_INTERMEDIATE, "save and submit", "combined", False, "combined_action_forbidden"),
```

Add separate `classify_topology` assertions that `has_step_navigation=False` and `step_index=3, step_count=2` both return `PageTopology.UNKNOWN`.

- [ ] **Step 2: Run the safety tests and verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_executor_safety.py -q
```

Expected: collection fails because `executor.safety` does not exist.

- [ ] **Step 3: Implement fail-closed topology classification**

Create `executor/safety.py` with these enums and classifier:

```python
from dataclasses import dataclass
from enum import StrEnum


class PageTopology(StrEnum):
    SINGLE_PAGE = "single_page"
    MULTI_STEP_INTERMEDIATE = "multi_step_intermediate"
    MULTI_STEP_FINAL = "multi_step_final"
    UNKNOWN = "unknown"


class ActionRisk(StrEnum):
    SAFE_INTERMEDIATE = "safe_intermediate"
    FINAL = "final"
    COMBINED = "combined"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class SafetyDecision:
    allowed: bool
    risk: ActionRisk
    reason_code: str


def classify_topology(
    *, declared_topology: str | None, step_index: int | None,
    step_count: int | None, has_step_navigation: bool
) -> PageTopology:
    if declared_topology == "single" and step_index is None and step_count is None:
        return PageTopology.SINGLE_PAGE
    if (
        declared_topology == "multi"
        and has_step_navigation
        and step_index is not None
        and step_count is not None
        and 1 <= step_index <= step_count
    ):
        if step_index < step_count:
            return PageTopology.MULTI_STEP_INTERMEDIATE
        return PageTopology.MULTI_STEP_FINAL
    return PageTopology.UNKNOWN
```

- [ ] **Step 4: Implement the permanent deny rules before the allow rule**

Normalize labels with `casefold()` and whitespace removal. Check final tokens (`提交`, `投递`, `完成申请`, `submit`, `confirmapplication`, `finish`) and combined tokens (`保存并提交`, `saveandsubmit`) before considering topology. `decide_action` returns allowed only when all of these are true: topology is `MULTI_STEP_INTERMEDIATE`, `action_kind == "next"`, `has_verified_next_step`, non-empty label, and no forbidden token. Any unknown input returns `AMBIGUOUS`/`ambiguous_action_forbidden`.

Use exact final return:

```python
    return SafetyDecision(
        allowed=True,
        risk=ActionRisk.SAFE_INTERMEDIATE,
        reason_code="safe_intermediate_action",
    )
```

- [ ] **Step 5: Run the safety tests and commit**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_executor_safety.py -q
```

Expected: all cases PASS, including 0 allowed final/combined/ambiguous cases.

Commit:

```powershell
git add executor/safety.py tests/unit/test_executor_safety.py
git commit -m "feat: add fail-closed executor safety gate"
```

### Task 6: Build the instrumented project-local recruitment simulation site

**Files:**
- Create: `executor/mock_site/__init__.py`
- Create: `executor/mock_site/app.py`
- Create: `executor/mock_site/pages/single-page.html`
- Create: `executor/mock_site/pages/multi-step-1.html`
- Create: `executor/mock_site/pages/multi-step-2.html`
- Create: `executor/mock_site/pages/ambiguous.html`
- Create: `executor/mock_site/pages/human-gate.html`
- Create: `executor/mock_site/pages/readback-mismatch.html`
- Create: `tests/contract/test_executor_mock_site.py`

**Interfaces:**
- Consumes: safety evidence attributes `data-topology`, `data-step-index`, `data-step-count`, `data-step-nav`, `data-action-kind`, `data-field-key`.
- Produces: loopback routes `/single-page`, `/multi-step/1`, `/multi-step/2`, `/ambiguous`, `/human-gate`, `/readback-mismatch`, `/submission-success`, `/submission-failed`, `/submission-unknown`, `/telemetry`, `/reset`; counters `field_events`, `intermediate_clicks`, `final_clicks`, `ambiguous_clicks`.

- [ ] **Step 1: Write failing simulation-site contract tests**

Create `tests/contract/test_executor_mock_site.py`:

```python
def test_simulation_routes_expose_explicit_topology_and_action_evidence(client) -> None:
    single = client.get("/single-page")
    first = client.get("/multi-step/1")
    final = client.get("/multi-step/2")
    ambiguous = client.get("/ambiguous")
    assert 'data-topology="single"' in single.text
    assert 'data-topology="multi"' in first.text
    assert 'data-step-index="1"' in first.text
    assert 'data-action-kind="next"' in first.text
    assert 'data-step-index="2"' in final.text
    assert 'data-action-kind="final"' in final.text
    assert 'data-action-kind="combined"' in ambiguous.text
    assert 'data-submission-result="success"' in client.get("/submission-success").text
    assert 'data-submission-result="failed"' in client.get("/submission-failed").text
    assert 'data-submission-result="unknown"' in client.get("/submission-unknown").text


def test_reset_clears_all_click_and_field_counters(client) -> None:
    assert client.post("/reset").json() == {"status": "reset"}
    assert client.get("/telemetry").json() == {
        "field_events": {},
        "intermediate_clicks": 0,
        "final_clicks": 0,
        "ambiguous_clicks": 0,
    }
```

- [ ] **Step 2: Run the site tests and verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/contract/test_executor_mock_site.py -q
```

Expected: collection fails because `executor.mock_site.app` does not exist.

- [ ] **Step 3: Implement the telemetry server**

Create `executor/mock_site/app.py` with an in-memory `Telemetry` dataclass guarded by `threading.Lock`, `HTMLResponse` routes that read files relative to `Path(__file__).parent / "pages"`, and JSON endpoints. Add `POST /event` accepting only:

```python
class BrowserEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["field", "intermediate", "final", "ambiguous"]
    key: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,79}$")
```

For `field`, require `key` and increment `field_events[key]`; for each click kind increment only its named counter. The endpoint never accepts or stores a field value.

- [ ] **Step 4: Create all page fixtures with explicit traps**

Each page includes `<meta charset="utf-8">`, a form field `data-field-key="full_name"`, and JavaScript that posts only event kind/key. The single page final button uses:

```html
<main data-topology="single">
  <label>姓名 <input data-field-key="full_name" name="full_name"></label>
  <button type="button" data-action-kind="final" id="final-submit">提交申请</button>
</main>
```

The first multi-step page declares `data-topology="multi" data-step-index="1" data-step-count="2" data-step-nav="true"`, and its `data-action-kind="next"` button posts `intermediate` then navigates to `/multi-step/2`. The second declares step 2/2 with a `final` button. The ambiguous page contains both `保存并提交` with `data-action-kind="combined"` and an icon-only `data-action-kind="unknown"` button, each wired to `ambiguous`. The human gate declares `data-human-required="login"` and provides only a user-operated `#human-continue` control. The mismatch page resets `full_name` to an empty string in its `input` handler after posting the field event. The three submission result pages contain no controls and use exactly one marker: `<main data-submission-result="success">`, `<main data-submission-result="failed">`, or `<main data-submission-result="unknown">`.

- [ ] **Step 5: Run site contract tests and a manual loopback smoke check**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/contract/test_executor_mock_site.py -q
$server = Start-Process -FilePath ".\.venv\Scripts\python.exe" -ArgumentList "-m","uvicorn","executor.mock_site.app:app","--host","127.0.0.1","--port","8765" -WindowStyle Hidden -PassThru
try {
  (Invoke-WebRequest http://127.0.0.1:8765/single-page -UseBasicParsing).StatusCode
  Invoke-RestMethod http://127.0.0.1:8765/telemetry
} finally {
  Stop-Process -Id $server.Id
}
```

Expected: tests PASS; page returns 200; telemetry contains all four zeroed counters.

- [ ] **Step 6: Commit the simulation site**

```powershell
git add executor/mock_site tests/contract/test_executor_mock_site.py
git commit -m "test: add instrumented executor simulation site"
```

### Task 7: Add the visible persistent Chromium observer and filler

**Files:**
- Create: `executor/browser.py`
- Create: `tests/integration/test_executor_simulation.py`

**Interfaces:**
- Consumes: Task 0 `ExecutorField`; Task 5 `classify_topology/decide_action`; Task 6 DOM evidence.
- Produces: `BrowserSession.open/observe/observe_submission_result/fill_confirmed/click_safe_intermediate/close`; `PageObservation`; `FillReport`; SHA-256 fingerprint excluding field values and URL query strings.

- [ ] **Step 1: Write failing real-browser tests for filling and safety**

Start the mock app on a free loopback port in a pytest fixture using `uvicorn.Server` in a daemon thread. Add tests that open a persistent context with `headless=True` and assert:

```python
def test_single_page_fills_confirmed_fields_but_never_clicks_bottom_action(
    browser, mock_site_url, payload_fields, telemetry
) -> None:
    browser.open(f"{mock_site_url}/single-page")
    observation = browser.observe()
    report = browser.fill_confirmed(payload_fields)
    decision = browser.action_decision(observation)
    assert report.confirmed_keys == ["full_name"]
    assert decision.allowed is False
    assert decision.reason_code == "single_page_bottom_action"
    assert telemetry()["final_clicks"] == 0


def test_ambiguous_and_final_buttons_are_never_clicked(
    browser, mock_site_url, telemetry
) -> None:
    for path in ("/ambiguous", "/multi-step/2"):
        browser.open(f"{mock_site_url}{path}")
        observation = browser.observe()
        assert browser.action_decision(observation).allowed is False
    current = telemetry()
    assert current["ambiguous_clicks"] == 0
    assert current["final_clicks"] == 0
```

Also assert missing/low fields are returned in issue lists after confirmed fields are filled, and mismatch returns `readback_mismatch` after exactly two fill attempts (initial plus one readback retry).

- [ ] **Step 2: Run the browser tests and verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_executor_simulation.py -q
```

Expected: collection fails because `executor.browser` does not exist.

- [ ] **Step 3: Implement persistent Chromium lifecycle and observation**

Create `executor/browser.py` using `playwright.sync_api.sync_playwright`. Windows runtime defaults are `headless=False`, `channel="chrome"` and a caller-provided dedicated `user_data_dir`, so it never opens the user's ordinary Chrome profile. Tests explicitly pass `headless=True, channel=None` and use Playwright's pinned bundled Chromium. `observe()` reads only structural attributes and returns:

```python
@dataclass(frozen=True)
class PageObservation:
    topology: PageTopology
    page_index: int | None
    page_count: int | None
    fingerprint: str
    human_required: str | None
    action_label: str
    action_kind: str
    has_verified_next_step: bool
```

Build `fingerprint` from JSON containing `urlsplit(page.url).path`, topology value, page index/count, sorted `data-field-key` names and action kind; hash with SHA-256 and prefix `sha256:`. Do not include hostname, query string, DOM text, input value or Cookie.

- [ ] **Step 4: Implement idempotent fill and exact readback**

For each confirmed non-sensitive field with a non-null value, locate `[data-field-key="<key>"]`. If its normalized current value already equals the payload value, report it as confirmed without calling `fill`. Otherwise call a required `before_write(field_key)` callback, which the engine uses to persist `pending_field_key`, then fill, read back, retry once only within the same uninterrupted attempt, and report `readback_mismatch` if still unequal. On successful readback call `after_verified(field_key)`, which moves the key to `completed_field_keys` and clears `pending_field_key`. Missing and low-confidence fields are never filled. Implement selector escaping with `json.dumps(field.field_key)` passed to `page.locator` so field keys never become executable script.

Return only keys and counts:

```python
@dataclass(frozen=True)
class FillReport:
    confirmed_keys: list[str]
    missing_keys: list[str]
    low_confidence_keys: list[str]
    readback_mismatch_keys: list[str]
    defaulted_keys: list[str]
```

- [ ] **Step 5: Implement the only clickable method behind `SafetyDecision`**

`click_safe_intermediate(observation)` must recompute the live decision immediately before click and raise `UnsafeActionError(reason_code)` unless `allowed` is true. Locate exactly one `[data-action-kind="next"]`; multiple or zero matches fail closed. After click, wait for a changed structural fingerprint. A timeout raises `IntermediateActionUncertainError` and the caller must not retry.

No `click_final`, generic `click`, label-based click or keyboard shortcut method is exposed by `BrowserSession`.

Implement `observe_submission_result()` as a read-only lookup of exactly one `[data-submission-result]`. Return `submitted_success`, `submitted_failed`, or `result_unknown`; absent, duplicated, or unknown markers return `result_unknown`. This method never clicks, reloads or submits.

- [ ] **Step 6: Run simulation tests in headed and headless modes**

Run the automated suite:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_executor_simulation.py -q
```

Expected: all tests PASS; telemetry shows `final_clicks == 0` and `ambiguous_clicks == 0`.

Then run one visible smoke test on Windows:

```powershell
$env:EXECUTOR_HEADED_SMOKE = '1'
.\.venv\Scripts\python.exe -m pytest tests/integration/test_executor_simulation.py -q -k headed_smoke
Remove-Item Env:EXECUTOR_HEADED_SMOKE
```

Expected: one visible dedicated Chromium window opens the loopback page, fills `full_name`, remains open at review, and the test passes without clicking the final button.

- [ ] **Step 7: Commit the browser adapter**

```powershell
git add executor/browser.py tests/integration/test_executor_simulation.py
git commit -m "feat: add safe playwright executor browser"
```

### Task 8: Orchestrate simulation tasks and explicit human handoff

**Files:**
- Create: `executor/engine.py`
- Modify: `executor/protocol.py`
- Modify: `tests/integration/test_executor_simulation.py`

**Interfaces:**
- Consumes: `ExecutorApiClient`, `BrowserSession`, `CheckpointStore`, Task 0 fixture, Task 2 progress/result API.
- Produces: `ExecutorEngine.run(task_id, simulation_payload) -> RunOutcome`; outcomes `ready_for_review`, `waiting_for_human`, `stopped_unauthorized`, `stopped_conflict`, `failed_safe`, `result_observed`; reason codes used by API events.

- [ ] **Step 1: Write failing engine tests for single/multi/human/mismatch flows**

Use a fake API client that records calls and a real `BrowserSession` against the mock site. Assert:

```python
def test_single_page_ends_in_review_without_final_click(engine, payload, telemetry) -> None:
    outcome = engine.run(payload.task_id, payload)
    assert outcome.kind == "ready_for_review"
    assert engine.client.progress_targets == ["running", "ready_for_review"]
    assert telemetry()["final_clicks"] == 0


def test_login_gate_waits_for_explicit_user_resume(engine, payload_for, telemetry) -> None:
    outcome = engine.run(payload_for("/human-gate").task_id, payload_for("/human-gate"))
    assert outcome.kind == "waiting_for_human"
    assert outcome.reason_code == "login_required"
    assert engine.client.progress_targets == ["running", "waiting_for_human"]
    assert telemetry()["final_clicks"] == 0
```

Add multi-step expected progress `running → ready_for_review`, mismatch expected review reason `readback_mismatch`, and missing/low fields expected counts while confirmed fields still fill.

Add one test per post-HUMAN result marker. Seed the fake authoritative task as `observing_user_submission`, open the corresponding result page, and assert exactly one `report_result` call with these mappings and no progress call:

```python
@pytest.mark.parametrize(
    ("path", "target"),
    [
        ("/submission-success", "submitted_success"),
        ("/submission-failed", "submitted_failed"),
        ("/submission-unknown", "result_unknown"),
    ],
)
def test_observation_mode_only_reports_post_human_result(
    engine_for_path, path, target, telemetry
) -> None:
    engine = engine_for_path(path, initial_status="observing_user_submission")
    outcome = engine.run()
    assert outcome.kind == "result_observed"
    assert outcome.reason_code == target
    assert engine.client.result_targets == [target]
    assert engine.client.progress_targets == []
    assert telemetry()["final_clicks"] == 0
```

- [ ] **Step 2: Run the engine tests and verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_executor_simulation.py -q -k engine
```

Expected: collection fails because `executor.engine` does not exist.

- [ ] **Step 3: Implement the orchestration order and state-version updates**

Create `executor/engine.py`. The exact order is: heartbeat with `EXECUTOR_VERSION`; issue progress/result lease in memory; fetch leased detail; compare detail protocol/task/version to the simulation payload. If authoritative status is `observing_user_submission`, open the page, call only `observe_submission_result()`, and send one result request; never enter fill or action code. Otherwise transition `dispatched → running`; open page; observe; validate checkpoint; fill; save checkpoint; stop for human/readback/safety or prepare a safe intermediate effect; report only status transitions. After every successful server response, replace the local `task_state_version` with the returned value.

Define:

```python
@dataclass(frozen=True)
class RunOutcome:
    kind: Literal[
        "ready_for_review",
        "waiting_for_human",
        "stopped_unauthorized",
        "stopped_conflict",
        "failed_safe",
        "result_observed",
    ]
    reason_code: str


class ExecutorEngine:
    def __init__(self, client, browser, checkpoints) -> None:
        self.client = client
        self.browser = browser
        self.checkpoints = checkpoints
```

Do not catch `KeyboardInterrupt` or `SystemExit`; the checkpoint already written before any allowed intermediate click is the recovery record.

- [ ] **Step 4: Implement field completion and review summaries without values**

Convert `FillReport` to field counts only. Save completed field keys after successful readback. If `readback_mismatch_keys` is non-empty, report `ready_for_review` with reason `readback_mismatch`. If `human_required` is set, report `waiting_for_human` with one of `login_required`, `verification_required`, or `human_takeover_required`. Missing/low fields remain in counts and do not short-circuit confirmed filling.

- [ ] **Step 5: Implement write-uncertainty and conflict handling**

On `ApiUnauthorized`, close active automation and return `stopped_unauthorized`; do not click or request another lease inside the same run. On `ApiConflict`, perform one read-only task refresh; if authoritative status/version equals the intended completed transition, accept it, otherwise return `stopped_conflict`. On `UncertainWriteResult`, perform the same read-only reconciliation and never repeat the POST automatically. No exception message is logged; log only task ID and stable local reason code.

- [ ] **Step 6: Run engine and existing state-machine tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_executor_simulation.py tests/unit/test_application_state_machine.py -q
```

Expected: all tests PASS; the engine produces no transition from `ready_for_review` to `observing_user_submission`.

- [ ] **Step 7: Commit the engine**

```powershell
git add executor/engine.py executor/protocol.py tests/integration/test_executor_simulation.py
git commit -m "feat: orchestrate safe executor simulation"
```

### Task 9: Prove disconnect, expiry, stale-version and process-restart recovery

**Files:**
- Create: `tests/integration/test_executor_recovery.py`
- Modify: `executor/engine.py`
- Modify: `executor/checkpoints.py`
- Modify: `tests/integration/test_executor_simulation.py`

**Interfaces:**
- Consumes: `pending_effect_key` write-before-click protocol; client 401/409/timeout errors; persistent Chromium profile and mock telemetry.
- Produces: recovery invariants for no repeated field fills, no repeated intermediate click, no final/ambiguous click and fail-closed page fingerprint changes.

- [ ] **Step 1: Write failing process-restart tests with fault injection**

Create `tests/integration/test_executor_recovery.py`. Add an engine test hook enum available only through constructor injection:

```python
class FaultPoint(StrEnum):
    AFTER_FIELD_WRITE_BEFORE_CHECKPOINT = "after_field_write_before_checkpoint"
    AFTER_INTERMEDIATE_CLICK_BEFORE_CONFIRM = "after_intermediate_click_before_confirm"
```

The first run raises `InjectedCrash` at the selected point; construct a new `ExecutorEngine`, `BrowserSession` and `CheckpointStore` with the same profile/checkpoint directories for the second run. Assert final telemetry:

```python
assert telemetry["field_events"]["full_name"] == 1
assert telemetry["intermediate_clicks"] == 1
assert telemetry["final_clicks"] == 0
assert telemetry["ambiguous_clicks"] == 0
```

The first intermediate click is expected; the duplicate count after recovery is zero.

- [ ] **Step 2: Write failing network, lease and fingerprint recovery tests**

Add these named tests with exact assertions:

```python
def test_read_transport_failure_succeeds_on_third_attempt(recovery_engine) -> None:
    recovery_engine.client.read_failures_remaining = 2
    assert recovery_engine.run().kind == "ready_for_review"
    assert recovery_engine.client.read_attempts == 3


def test_progress_timeout_reconciles_without_second_post(recovery_engine) -> None:
    recovery_engine.client.timeout_after_progress_commit = True
    assert recovery_engine.run().kind == "ready_for_review"
    assert recovery_engine.client.progress_attempts == 2
    assert recovery_engine.client.progress_attempts_by_target["running"] == 1
    assert recovery_engine.client.progress_attempts_by_target["ready_for_review"] == 1


def test_expired_lease_stops_before_more_browser_actions(recovery_engine) -> None:
    recovery_engine.client.expire_lease_on_detail = True
    assert recovery_engine.run().kind == "stopped_unauthorized"
    assert recovery_engine.browser.action_count == 0


def test_stale_task_version_stops_without_write_retry(recovery_engine) -> None:
    recovery_engine.client.conflict_on_progress = True
    assert recovery_engine.run().kind == "stopped_conflict"
    assert recovery_engine.client.progress_attempts == 1


def test_changed_fingerprint_enters_review_without_click(recovery_engine) -> None:
    recovery_engine.checkpoints.save(checkpoint_for_fingerprint("sha256:old123"))
    recovery_engine.browser.forced_fingerprint = "sha256:new456"
    outcome = recovery_engine.run()
    assert (outcome.kind, outcome.reason_code) == (
        "ready_for_review",
        "page_topology_changed",
    )
    assert recovery_engine.browser.click_count == 0
```

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_executor_recovery.py -q
```

Expected: failures identify missing fault hooks and pending-effect recovery behavior.

- [ ] **Step 3: Write the pending marker before any intermediate action**

Immediately before `click_safe_intermediate`, save a checkpoint whose `pending_effect_key` is `page-<index>:safe-next`. Only after a changed page fingerprint is observed may the engine move that key to `completed_effect_keys` and clear `pending_effect_key`. If a restart loads a non-null pending key, the engine must not retry the action; it reports `ready_for_review` with reason `intermediate_result_uncertain`.

- [ ] **Step 4: Reconcile fields from live page state instead of blindly refilling**

If a restart loads `pending_field_key`, inspect the live field before any navigation or write. An equal value is marked complete without another `fill()` call. If the browser/profile no longer has that value, do not refill the uncertain field; clear no marker and enter review with `field_write_uncertain`. For every other field key absent from `completed_field_keys`, `BrowserSession.fill_confirmed` first compares the live value with the payload before deciding to fill. If the fingerprint differs from the saved fingerprint before any verified navigation, do not resume; report `page_topology_changed`.

- [ ] **Step 5: Make 401, 409 and write timeout fail closed**

401 closes the automation session and requires a new user-initiated run to obtain permission. 409 and uncertain write perform exactly one task-detail GET; they never retry a click or state-changing POST. If the refreshed task is terminal or cancelled, delete the checkpoint and stop. If it is `ready_for_review`, leave the browser for user inspection. Any other mismatch stops with a stable reason.

- [ ] **Step 6: Run all recovery and simulation gates**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_executor_simulation.py tests/integration/test_executor_recovery.py -q
```

Expected: all tests PASS with `field_events[full_name] == 1`, `intermediate_clicks == 1`, `final_clicks == 0`, and `ambiguous_clicks == 0` in restart scenarios.

- [ ] **Step 7: Commit recovery behavior**

```powershell
git add executor/engine.py executor/checkpoints.py tests/integration/test_executor_simulation.py tests/integration/test_executor_recovery.py
git commit -m "test: prove executor recovery is idempotent"
```

### Task 10: Add CLI, security gates, shared route integration and operator runbook

**Files:**
- Create: `executor/cli.py`
- Create: `tests/security/test_executor_redaction.py`
- Create: `docs/runbooks/windows-executor-simulation.md`
- Modify: `backend/app/api/router.py`
- Modify: `tests/contract/test_executor_api.py`

**Interfaces:**
- Consumes: all Tasks 0–9; shared-router coordination window; fixture `executor.v1`.
- Produces: `python -m executor.cli pair`; `run-simulation`; `resume-simulation`; mounted `/api/executor/tasks`; repeatable Windows acceptance commands.

- [ ] **Step 1: Write failing CLI and redaction tests**

In `tests/security/test_executor_redaction.py`, run progress/result failures with sentinel password, token, lease, Cookie, captcha, resume and form value strings. Assert none appear in `caplog.text`, API response text, `ApplicationEvent.redacted_payload`, checkpoint files or CLI stderr. In `tests/unit/test_executor_client.py`, assert `--help` exposes no `--device-token`, `--task-lease`, `--password` or generic URL argument.

- [ ] **Step 2: Implement pairing and simulation-only CLI commands**

Create `executor/cli.py` with `argparse` subcommands:

- `pair --base-url --device-name`: read the one-time pairing code with `getpass.getpass`, generate an RSA-3072 device key, store private PEM and returned device token in `WindowsCredentialStore`, and never print either secret.
- `run-simulation --base-url --task-id --fixture --data-dir`: require `base_url` host to be `127.0.0.1` or `localhost`; load the checked-in fixture, replace only its `task_id` with the CLI task ID, use a visible persistent Chromium profile, and run once.
- `resume-simulation` takes the same non-secret arguments and requires an existing checkpoint.

Reject non-loopback fixture targets and non-loopback base URLs with `simulation_requires_loopback`. Pairing may use configured HTTPS production base URL, but simulation cannot.

- [ ] **Step 3: Mount the feature router in the coordinated shared-entry window**

After confirming A/B/D are not editing `backend/app/api/router.py`, change its import and final include only:

```python
from backend.app.api.routes import (
    analysis,
    auth,
    devices,
    executor_tasks,
    health,
    jobs,
    sessions,
)


api_router.include_router(executor_tasks.router)
```

Remove the test-only `app.include_router(executor_tasks.router, prefix="/api")` from `tests/contract/test_executor_api.py`; all contract tests must now use the production router.

- [ ] **Step 4: Write the Windows simulation runbook with secret-safe commands**

Create `docs/runbooks/windows-executor-simulation.md` covering: project `.venv`; dependency/browser installation; mock server startup; Web-created pairing ticket; hidden pairing-code prompt; Credential Locker verification using `python -m keyring diagnose` without reading secrets; simulation task seeding only in a dedicated test database; visible run/resume; checkpoint/profile locations; lease expiry and 409 recovery; revocation; and telemetry assertions. State explicitly that Wave 1 has no real-site adapter, no task submit permission and no cloud persistence of local sensitive values.

- [ ] **Step 5: Run focused contract, security, simulation and no-migration gates**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_executor_protocol.py tests/unit/test_executor_task_service.py tests/unit/test_executor_client.py tests/unit/test_executor_checkpoints.py tests/unit/test_executor_safety.py tests/contract/test_executor_api.py tests/contract/test_executor_mock_site.py tests/integration/test_executor_simulation.py tests/integration/test_executor_recovery.py tests/security/test_executor_redaction.py -q
git diff --name-only -- alembic backend/app/db/models.py backend/app/db/__init__.py
```

Expected: all Executor tests PASS; the second command prints nothing; OpenAPI contains progress/result paths and no submit path or scope.

- [ ] **Step 6: Run existing backend regression and static checks**

Run:

```powershell
.\.venv\Scripts\python.exe -m ruff check backend executor tests scripts
.\.venv\Scripts\python.exe -m pytest tests/unit/test_device_service.py tests/unit/test_application_state_machine.py tests/contract/test_device_api.py tests/integration/test_application_state_machine_mysql.py -q
```

Expected: Ruff prints `All checks passed!`; all selected existing tests PASS. The established pre-change focused baseline is `243 passed`; any count increase must come only from new tests.

- [ ] **Step 7: Run complete project gates**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q -rs
npm.cmd --prefix frontend run test
npm.cmd --prefix frontend run typecheck
npm.cmd --prefix frontend run build
git diff --check
```

Expected: Python tests pass with only documented external/opt-in skips; frontend Vitest, `vue-tsc` and Vite build pass unchanged; `git diff --check` has no output.

- [ ] **Step 8: Run the visible end-to-end simulation acceptance**

Start the project-local mock site and backend, seed one `DISPATCHED` simulation task assigned to the paired device in the dedicated test database, then run each fixture path through the CLI. Query `/telemetry` after each reset. Acceptance is exact:

```text
single_page: final_clicks=0
multi_step: intermediate_clicks=1, final_clicks=0
multi_step_restart: duplicate_field_writes=0, duplicate_intermediate_clicks=0
ambiguous: ambiguous_clicks=0
human_gate: status=waiting_for_human
readback_mismatch: status=ready_for_review
post_human_success: task_result=submitted_success, final_clicks=0
post_human_failed: task_result=submitted_failed, final_clicks=0
post_human_unknown: task_result=result_unknown, final_clicks=0
expired_lease: browser_actions_after_401=0
stale_version: state_changing_retries=0
```

Do not mark this plan complete if any counter differs.

- [ ] **Step 9: Commit integration and documentation**

```powershell
git add executor/cli.py backend/app/api/router.py tests/contract/test_executor_api.py tests/security/test_executor_redaction.py docs/runbooks/windows-executor-simulation.md
git commit -m "feat: complete windows executor simulation safety slice"
```

## Completion Evidence

Record the following in the implementation handoff with the exact commit SHA and command output counts:

- Executor-focused pytest result and Playwright Chromium version.
- Existing device/state-machine regression result.
- Ruff, full Python, frontend test/typecheck/build and `git diff --check` results.
- `git diff --name-only -- alembic backend/app/db/models.py backend/app/db/__init__.py` empty output.
- OpenAPI proof that only task read/progress/result operations exist and no submit capability exists.
- Simulation telemetry for single page, multi-step, final page, ambiguous buttons, login wait, mismatch, disconnect, expiry, stale version and restart.
- Credential Locker backend name and successful pairing/heartbeat without printing the token.
- Any opt-in MySQL/Redis/Compose gate not executed must be listed by exact environment variable name and must not be described as passed.
