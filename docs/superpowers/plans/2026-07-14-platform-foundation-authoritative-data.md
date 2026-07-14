# Platform Foundation and Authoritative Data Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把当前基于 JSON 用户文件和 SQLite 检查点的演示应用，升级为以 MySQL 为权威业务库、Redis 8 为运行时、加密 S3 兼容对象存储为文件层的可扩展平台基础，同时保持现有 Vue 登录、会话和分析接口可用。

**Architecture:** FastAPI 使用应用工厂和依赖注入组织认证、会话、设备、任务与健康检查；SQLAlchemy 2.0 + Alembic 管理 MySQL 权威实体，Redis 8 承担设备配对短期票据和 LangGraph checkpoint。对象在进入 S3 兼容存储前先用 AES-256-GCM 客户端加密；任何 Redis 丢失都不能改变 MySQL 中的业务状态。

**Tech Stack:** Python 3.13、FastAPI、Pydantic Settings、SQLAlchemy 2.0、Alembic、MySQL 8.4 LTS、PyMySQL、Redis 8、langgraph-checkpoint-redis 0.5、PyJWT、pwdlib Argon2、boto3、cryptography、pytest、Vue 3、Docker Compose。

## Global Constraints

- 业务权威数据只能写入 MySQL；Redis 只保存 checkpoint、短期票据、缓存、在线状态和幂等键。
- 本包不接入真实职位、简历档案或招聘网站；这些实体只能通过本包稳定接口在后续工作包添加。
- 保持现有 `POST /api/auth/register`、`POST /api/auth/login`、`GET /api/auth/me`、`/api/sessions/*`、`POST /api/analysis/run` 请求与响应字段兼容。
- 注册账户角色固定为 `student`；`admin` 只能由受控命令创建，不能通过公开注册请求提升权限。
- 密码使用 Argon2；访问令牌使用有到期时间且带 `iss`、`aud`、`sub`、`role`、`jti` 的 HS256 JWT。
- 设备凭据为一次显示的随机令牌，数据库只保存 SHA-256 摘要；撤销后必须立即失效。
- 任务最终提交只能由 `human` 发起；`executor` 没有从 `ready_for_review` 发起提交观察的权限。
- 对象存储中的业务文件必须先经过 AES-256-GCM 客户端加密；日志和审计载荷不得包含密码、令牌、Cookie、验证码、身份证号全文或完整表单值。
- 时间统一以 UTC 的 timezone-aware `datetime` 写入数据库，API 统一输出 ISO 8601。
- MySQL 使用 `utf8mb4`；所有外键、唯一约束和高频查询列必须在首个 Alembic migration 中显式建立。
- Redis checkpoint 不设置自动过期；设备配对票据固定 10 分钟到期且只能兑换一次。
- 本地开发允许 `CHECKPOINT_BACKEND=sqlite`；Docker 和生产配置必须使用 `CHECKPOINT_BACKEND=redis`。
- Windows 10/11 + Chrome 是后续本地执行器的 MVP 平台；本包的设备枚举只接受 `windows`。
- 每个任务先写失败测试，再做最小实现；每个任务结束都必须形成可独立审查的提交。

---

## 1. 最终文件结构与职责

```text
backend/app/
├── api/
│   ├── dependencies.py       # 当前用户、管理员、设备认证依赖
│   ├── router.py             # 聚合所有 API router
│   └── routes/
│       ├── analysis.py       # 现有 LangGraph 分析接口，保持契约
│       ├── auth.py           # 注册、登录、当前用户
│       ├── devices.py        # 配对票据、兑换、列表、撤销
│       ├── health.py         # live/readiness
│       └── sessions.py       # 分析会话 CRUD、状态和历史
├── db/
│   ├── base.py               # DeclarativeBase、UUID/UTC mixin
│   ├── models.py             # 首包权威实体
│   └── session.py            # Engine、SessionLocal、事务依赖
├── repositories/
│   ├── applications.py       # ApplicationTask 乐观锁更新与事件追加
│   ├── devices.py            # 设备查询和令牌摘要查询
│   ├── sessions.py           # 分析会话归属与激活
│   └── users.py              # 用户唯一查询和持久化
├── services/
│   ├── applications.py       # 确定性任务状态机与 actor 安全门
│   ├── auth.py               # Argon2、JWT、注册登录
│   ├── devices.py            # Redis 一次性票据、设备签发和撤销
│   └── storage.py            # AES-GCM + S3 兼容对象存储
├── config.py                 # 强类型环境配置
└── main.py                   # create_app()、lifespan、CORS
src/
├── checkpointing.py          # SQLite/Redis checkpointer 生命周期
└── graph.py                  # build_graph(checkpointer) 显式注入
alembic/
├── env.py
└── versions/20260714_0001_platform_foundation.py
scripts/create_admin.py
tests/
├── conftest.py
├── contract/test_existing_api_contract.py
├── integration/test_mysql_migration.py
├── integration/test_redis_checkpoint.py
└── unit/...
```

稳定的跨任务接口如下，后续任务不得自行改名：

```python
def get_settings() -> Settings: ...
def get_db() -> Iterator[Session]: ...
def get_current_user(...) -> User: ...
def require_admin(...) -> User: ...
def get_current_device(...) -> Device: ...

class AuthService:
    def register(self, db: Session, *, account: str, nickname: str, password: str) -> User: ...
    def authenticate(self, db: Session, *, account: str, password: str) -> User | None: ...
    def issue_user_token(self, user: User) -> str: ...

class DeviceService:
    def create_pairing_ticket(self, *, user_id: str) -> PairingTicket: ...
    def redeem_pairing_ticket(self, db: Session, *, code: str, name: str, public_key_pem: str) -> IssuedDevice: ...
    def revoke(self, db: Session, *, user_id: str, device_id: str) -> None: ...

class ApplicationService:
    def transition(self, db: Session, *, task_id: str, expected_version: int,
                   target: ApplicationTaskStatus, actor: TaskActor,
                   event_type: str, redacted_payload: dict[str, object]) -> ApplicationTask: ...

class EncryptedObjectStore:
    def put(self, *, key: str, plaintext: bytes, content_type: str) -> StoredObject: ...
    def get(self, *, key: str) -> bytes: ...
    def delete(self, *, key: str) -> None: ...
```

## 2. 明确不在本计划内

- 不把 `data/jobs.json` 导入 MySQL；真实职位同步属于工作包 2。
- 不创建人才档案、简历版本和投递快照表；属于工作包 3。
- 不实现 Windows 安装器、Chrome 控制、本地保险库和 GUI Agent；属于工作包 4。
- 不实现招聘网站适配器、自动点击或最终提交；任何相关按钮操作在本包都不存在。
- 不迁移本机忽略文件 `data/app_users.json`。旧演示账户需要重新注册，避免延续无盐 SHA-256 密码摘要。

## 2.1 执行前的仓库基线

当前仓库是在需求设计阶段初始化的，原始应用源码仍显示为 untracked。执行 Task 1 前先检查没有密钥或用户运行数据，然后把原始应用作为独立基线提交；否则后续任务的 diff 无法审查，最终分支也会缺少未触及的原始文件。

```powershell
rg -n "sk-[A-Za-z0-9_-]{20,}|BEGIN .*PRIVATE KEY|AKIA[0-9A-Z]{16}" .env.example README.md backend frontend src data docker-compose.yml
git add .env.example .gitignore README.md backend data/jobs.json data/sample_resume.md data/sample_resume.pdf docker-compose.yml docs/graph-overview.svg docs/image.png frontend requirements.txt src
git status --short
git commit -m "chore: import existing career assistant baseline"
```

预期：扫描结果不含真实密钥或私钥；提交中不含 `.env`、`data/app_users.json`、`checkpoints/`、`.idea/` 或 `.superpowers/`。

### Task 1: 建立测试基线和强类型配置

**Files:**
- Modify: `requirements.txt`
- Create: `requirements-dev.txt`
- Create: `backend/app/config.py`
- Modify: `.env.example`
- Create: `tests/conftest.py`
- Create: `tests/unit/test_config.py`

**Interfaces:**
- Consumes: 根目录 `.env` 和进程环境变量。
- Produces: `Settings`、`get_settings() -> Settings`、`settings_override(**values)` 测试辅助函数。

- [ ] **Step 1: 写配置失败测试**

```python
# tests/unit/test_config.py
from pydantic import ValidationError
import pytest

from backend.app.config import Settings


def test_production_rejects_default_secrets() -> None:
    with pytest.raises(ValidationError, match="app_auth_secret|APP_AUTH_SECRET"):
        Settings(
            app_env="production",
            app_auth_secret="replace-with-your-own-secret",
            object_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            database_url="mysql+pymysql://app:app@mysql:3306/career_assistant",
            redis_url="redis://redis:6379/0",
            checkpoint_backend="redis",
        )


def test_test_environment_accepts_sqlite_and_memory_dependencies() -> None:
    settings = Settings(
        app_env="test",
        app_auth_secret="test-secret-with-at-least-32-characters",
        object_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        database_url="sqlite+pysqlite:///:memory:",
        redis_url="redis://localhost:6379/15",
        checkpoint_backend="sqlite",
    )
    assert settings.is_production is False
    assert settings.jwt_audience == "career-assistant-web"
```

- [ ] **Step 2: 运行测试并确认因模块不存在而失败**

Run: `python -m pytest tests/unit/test_config.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'backend.app.config'`.

- [ ] **Step 3: 增加依赖和完整配置实现**

将以下依赖追加到 `requirements.txt`：

```text
SQLAlchemy==2.0.51
alembic==1.16.5
PyMySQL==1.2.0
pydantic-settings==2.11.0
PyJWT==2.13.0
pwdlib[argon2]==0.2.1
redis==7.0.1
langgraph-checkpoint-redis==0.5.0
boto3==1.42.97
cryptography>=45.0.0,<47.0.0
```

创建 `requirements-dev.txt`：

```text
-r requirements.txt
pytest==8.4.2
fakeredis==2.36.2
httpx==0.28.1
ruff==0.15.21
mypy==1.19.1
```

创建 `backend/app/config.py`：

```python
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: Literal["development", "test", "production"] = "development"
    app_auth_secret: str
    jwt_issuer: str = "career-assistant-api"
    jwt_audience: str = "career-assistant-web"
    jwt_ttl_seconds: int = 604800
    database_url: str
    redis_url: str
    checkpoint_backend: Literal["sqlite", "redis"] = "sqlite"
    checkpoint_sqlite_path: Path = ROOT_DIR / "checkpoints" / "langgraph_checkpoints.sqlite"
    object_store_endpoint: str = "http://localhost:9000"
    object_store_region: str = "us-east-1"
    object_store_bucket: str = "career-assistant"
    object_store_access_key: str = "minioadmin"
    object_store_secret_key: str = "minioadmin"
    object_encryption_key: str
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def cors_origin_list(self) -> list[str]:
        return [value.strip() for value in self.cors_origins.split(",") if value.strip()]

    @field_validator("app_auth_secret")
    @classmethod
    def validate_auth_secret_length(cls, value: str) -> str:
        if len(value) < 32:
            raise ValueError("APP_AUTH_SECRET must contain at least 32 characters")
        return value

    @model_validator(mode="after")
    def validate_production_settings(self) -> "Settings":
        if self.is_production and self.app_auth_secret == "replace-with-your-own-secret":
            raise ValueError("APP_AUTH_SECRET must be replaced in production")
        if self.is_production and self.checkpoint_backend != "redis":
            raise ValueError("production requires CHECKPOINT_BACKEND=redis")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
```

保留现有模型变量，并把原来的单个 `APP_AUTH_SECRET` 配置段替换为：

```dotenv
APP_ENV=development
APP_AUTH_SECRET=replace-with-a-random-secret-of-at-least-32-characters
JWT_ISSUER=career-assistant-api
JWT_AUDIENCE=career-assistant-web
JWT_TTL_SECONDS=604800
DATABASE_URL=mysql+pymysql://career:career-dev-password@mysql:3306/career_assistant?charset=utf8mb4
REDIS_URL=redis://redis:6379/0
CHECKPOINT_BACKEND=redis
OBJECT_STORE_ENDPOINT=http://minio:9000
OBJECT_STORE_REGION=us-east-1
OBJECT_STORE_BUCKET=career-assistant
OBJECT_STORE_ACCESS_KEY=minioadmin
OBJECT_STORE_SECRET_KEY=minioadmin
# 运行 `python -c "import base64,secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())"` 生成
OBJECT_ENCRYPTION_KEY=replace-with-32-byte-base64-key
CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173
```

- [ ] **Step 4: 安装依赖并运行配置测试**

Run: `python -m pip install -r requirements-dev.txt && python -m pip check && python -m pytest tests/unit/test_config.py -v`

Expected: `No broken requirements found.` and `2 passed`.

- [ ] **Step 5: 提交**

```bash
git add requirements.txt requirements-dev.txt .env.example backend/app/config.py tests/conftest.py tests/unit/test_config.py
git commit -m "chore: add typed platform configuration and test baseline"
```

### Task 2: 建立 MySQL 权威模型和首个 migration

**Files:**
- Create: `backend/app/db/__init__.py`
- Create: `backend/app/db/base.py`
- Create: `backend/app/db/models.py`
- Create: `backend/app/db/session.py`
- Create: `alembic.ini`
- Create: `alembic/env.py`
- Create: `alembic/versions/20260714_0001_platform_foundation.py`
- Create: `tests/unit/test_models.py`
- Create: `tests/integration/test_mysql_migration.py`

**Interfaces:**
- Consumes: `get_settings() -> Settings`。
- Produces: `Base`、`User`、`AnalysisSession`、`Device`、`ApplicationTask`、`ApplicationEvent`、`AuditEvent`、所有枚举、`SessionLocal`、`get_db()`、`session_scope()`。

- [ ] **Step 1: 写模型约束失败测试**

```python
# tests/unit/test_models.py
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.db.base import Base
from backend.app.db.models import AnalysisSession, User, UserRole


def test_user_and_session_are_relational_and_active_session_is_derived() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        user = User(account="alice", nickname="Alice", password_hash="argon", role=UserRole.STUDENT)
        db.add(user)
        db.flush()
        first = AnalysisSession(user_id=user.id, thread_id="session-1", label="分析会话 1")
        second = AnalysisSession(user_id=user.id, thread_id="session-2", label="分析会话 2")
        db.add_all([first, second])
        db.commit()
        rows = db.scalars(select(AnalysisSession).where(AnalysisSession.user_id == user.id)).all()
    assert {row.thread_id for row in rows} == {"session-1", "session-2"}
    assert user.role is UserRole.STUDENT
```

- [ ] **Step 2: 运行模型测试并确认失败**

Run: `python -m pytest tests/unit/test_models.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'backend.app.db'`.

- [ ] **Step 3: 实现 Base、实体和会话工厂**

`backend/app/db/base.py` 使用以下完整定义：

```python
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class UUIDPrimaryKeyMixin:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )
```

`backend/app/db/models.py` 必须完整定义以下字段和约束：

```python
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, Enum, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utc_now


class UserRole(StrEnum):
    STUDENT = "student"
    ADMIN = "admin"


class DevicePlatform(StrEnum):
    WINDOWS = "windows"


class DeviceStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class ApplicationTaskStatus(StrEnum):
    CREATED = "created"
    WAITING_FOR_DEVICE = "waiting_for_device"
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


class TaskActor(StrEnum):
    HUMAN = "human"
    EXECUTOR = "executor"
    SYSTEM = "system"


enum_kwargs = {
    "native_enum": False,
    "create_constraint": True,
    "validate_strings": True,
    "values_callable": lambda enum_type: [item.value for item in enum_type],
}
audit_id_type = BigInteger().with_variant(Integer, "sqlite")


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"
    account: Mapped[str] = mapped_column(String(120), unique=True, index=True, nullable=False)
    nickname: Mapped[str] = mapped_column(String(120), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(Enum(UserRole, **enum_kwargs), default=UserRole.STUDENT, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sessions: Mapped[list["AnalysisSession"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class AnalysisSession(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "analysis_sessions"
    __table_args__ = (UniqueConstraint("thread_id", name="uq_analysis_sessions_thread_id"),)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    thread_id: Mapped[str] = mapped_column(String(96), nullable=False)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True, nullable=False)
    user: Mapped[User] = relationship(back_populates="sessions")


class Device(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "devices"
    __table_args__ = (UniqueConstraint("token_hash", name="uq_devices_token_hash"),)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    platform: Mapped[DevicePlatform] = mapped_column(Enum(DevicePlatform, **enum_kwargs), nullable=False)
    status: Mapped[DeviceStatus] = mapped_column(Enum(DeviceStatus, **enum_kwargs), default=DeviceStatus.ACTIVE, index=True, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    public_key_pem: Mapped[str] = mapped_column(Text, nullable=False)
    paired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[str | None] = mapped_column(String(40))


class ApplicationTask(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "application_tasks"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    target_job_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    snapshot_id: Mapped[str | None] = mapped_column(String(36), index=True)
    device_id: Mapped[str | None] = mapped_column(ForeignKey("devices.id", ondelete="SET NULL"), index=True)
    status: Mapped[ApplicationTaskStatus] = mapped_column(
        Enum(ApplicationTaskStatus, **enum_kwargs), default=ApplicationTaskStatus.CREATED, index=True, nullable=False
    )
    state_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    current_page: Mapped[int | None] = mapped_column(Integer)
    page_count: Mapped[int | None] = mapped_column(Integer)
    last_error_code: Mapped[str | None] = mapped_column(String(80))


class ApplicationEvent(Base):
    __tablename__ = "application_events"
    __table_args__ = (Index("ix_application_events_task_created", "task_id", "created_at"),)
    id: Mapped[int] = mapped_column(audit_id_type, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("application_tasks.id", ondelete="CASCADE"), nullable=False)
    actor: Mapped[TaskActor] = mapped_column(Enum(TaskActor, **enum_kwargs), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    from_status: Mapped[str] = mapped_column(String(40), nullable=False)
    to_status: Mapped[str] = mapped_column(String(40), nullable=False)
    redacted_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_events_entity_created", "entity_type", "entity_id", "created_at"),)
    id: Mapped[int] = mapped_column(audit_id_type, primary_key=True, autoincrement=True)
    actor_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    actor_device_id: Mapped[str | None] = mapped_column(ForeignKey("devices.id", ondelete="SET NULL"), index=True)
    event_type: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    entity_type: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(80), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    redacted_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
```

`backend/app/db/session.py`：

```python
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.app.config import get_settings


settings = get_settings()
engine = create_engine(settings.database_url, pool_pre_ping=True, pool_recycle=1800)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    with SessionLocal() as db:
        yield db


@contextmanager
def session_scope() -> Iterator[Session]:
    with SessionLocal.begin() as db:
        yield db
```

- [ ] **Step 4: 配置 Alembic 并生成确定的首个 migration**

`alembic/env.py` 必须导入 `Base` 和 `backend.app.db.models`，使用 `get_settings().database_url` 覆盖 ini URL，并同时支持 offline/online migration。执行：

Run: `alembic revision --autogenerate -m "platform foundation" --rev-id 20260714_0001`

Expected: 生成 `alembic/versions/20260714_0001_platform_foundation.py`，`upgrade()` 创建 `users`、`analysis_sessions`、`devices`、`application_tasks`、`application_events`、`audit_events` 六张表，`downgrade()` 按相反依赖顺序删除它们。检查 migration 中包含命名约束 `uq_analysis_sessions_thread_id`、`uq_devices_token_hash` 和两个组合索引。

Run: `alembic upgrade head`

Expected: 空的开发 MySQL 数据库升级到 revision `20260714_0001`，退出码为 0。

- [ ] **Step 5: 运行单元测试和真实 MySQL migration 往返测试**

`tests/integration/test_mysql_migration.py`：

```python
import os
import subprocess

import pytest
from sqlalchemy import create_engine, inspect


@pytest.mark.skipif("TEST_MYSQL_URL" not in os.environ, reason="requires TEST_MYSQL_URL")
def test_mysql_migration_upgrade_and_downgrade() -> None:
    env = {**os.environ, "DATABASE_URL": os.environ["TEST_MYSQL_URL"]}
    subprocess.run(["alembic", "upgrade", "head"], check=True, env=env)
    tables = set(inspect(create_engine(env["DATABASE_URL"])).get_table_names())
    assert {"users", "analysis_sessions", "devices", "application_tasks", "application_events", "audit_events"} <= tables
    subprocess.run(["alembic", "downgrade", "base"], check=True, env=env)
```

Run: `python -m pytest tests/unit/test_models.py -v`

Expected: PASS.

Run: `$env:TEST_MYSQL_URL='mysql+pymysql://career:career-dev-password@127.0.0.1:3306/career_assistant_test?charset=utf8mb4'; python -m pytest tests/integration/test_mysql_migration.py -v`

Expected: PASS after the Compose test database from Task 9 is running.

- [ ] **Step 6: 提交**

```bash
git add backend/app/db alembic.ini alembic tests/unit/test_models.py tests/integration/test_mysql_migration.py
git commit -m "feat: add authoritative MySQL platform schema"
```

### Task 3: 用 Argon2 和 JWT 替换 JSON 文件认证

**Files:**
- Create: `backend/app/repositories/users.py`
- Create: `backend/app/services/auth.py`
- Create: `backend/app/api/dependencies.py`
- Create: `scripts/create_admin.py`
- Create: `tests/unit/test_auth_service.py`
- Delete after Task 4 passes: `src/auth_service.py`

**Interfaces:**
- Consumes: `User`、`UserRole`、`get_db()`、`Settings`。
- Produces: `AuthService`、`get_current_user()`、`require_admin()`；Task 4 的认证 router 依赖这些稳定接口。

- [ ] **Step 1: 写密码、JWT 和公开注册权限测试**

```python
# tests/unit/test_auth_service.py
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.config import Settings
from backend.app.db.base import Base
from backend.app.db.models import UserRole
from backend.app.services.auth import AuthService


def test_register_hashes_password_and_token_carries_student_role() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    settings = Settings(
        app_env="test",
        app_auth_secret="test-secret-with-at-least-32-characters",
        object_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        database_url="sqlite+pysqlite:///:memory:",
        redis_url="redis://localhost:6379/15",
        checkpoint_backend="sqlite",
    )
    service = AuthService(settings)
    with Session(engine) as db:
        user = service.register(db, account=" Alice ", nickname="Alice", password="secret12")
        db.commit()
        token = service.issue_user_token(user)
        claims = service.decode_user_token(token)
    assert user.account == "alice"
    assert user.role is UserRole.STUDENT
    assert user.password_hash != "secret12"
    assert service.verify_password("secret12", user.password_hash)
    assert claims["sub"] == user.id
    assert claims["role"] == "student"
```

- [ ] **Step 2: 运行测试并确认认证服务不存在**

Run: `python -m pytest tests/unit/test_auth_service.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'backend.app.services.auth'`.

- [ ] **Step 3: 实现仓储和认证服务**

`backend/app/repositories/users.py`：

```python
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.db.models import User


def normalize_account(account: str) -> str:
    return account.strip().lower()


def get_by_account(db: Session, account: str) -> User | None:
    return db.scalar(select(User).where(User.account == normalize_account(account)))


def get_by_id(db: Session, user_id: str) -> User | None:
    return db.get(User, user_id)
```

`backend/app/services/auth.py`：

```python
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from pwdlib import PasswordHash
from sqlalchemy.orm import Session

from backend.app.config import Settings
from backend.app.db.models import User, UserRole
from backend.app.repositories.users import get_by_account, normalize_account


class AccountExistsError(ValueError):
    pass


class AuthService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.password_hash = PasswordHash.recommended()

    def verify_password(self, plaintext: str, encoded: str) -> bool:
        return self.password_hash.verify(plaintext, encoded)

    def register(self, db: Session, *, account: str, nickname: str, password: str) -> User:
        normalized = normalize_account(account)
        if get_by_account(db, normalized):
            raise AccountExistsError("该账号已经存在，请直接登录。")
        user = User(
            account=normalized,
            nickname=nickname.strip(),
            password_hash=self.password_hash.hash(password),
            role=UserRole.STUDENT,
            last_login_at=datetime.now(timezone.utc),
        )
        db.add(user)
        db.flush()
        return user

    def authenticate(self, db: Session, *, account: str, password: str) -> User | None:
        user = get_by_account(db, account)
        if not user or not user.is_active or not self.verify_password(password, user.password_hash):
            return None
        user.last_login_at = datetime.now(timezone.utc)
        db.flush()
        return user

    def issue_user_token(self, user: User) -> str:
        now = datetime.now(timezone.utc)
        payload = {
            "iss": self.settings.jwt_issuer,
            "aud": self.settings.jwt_audience,
            "sub": user.id,
            "role": user.role.value,
            "jti": str(uuid.uuid4()),
            "iat": now,
            "exp": now + timedelta(seconds=self.settings.jwt_ttl_seconds),
        }
        return jwt.encode(payload, self.settings.app_auth_secret, algorithm="HS256")

    def decode_user_token(self, token: str) -> dict[str, Any]:
        return jwt.decode(
            token,
            self.settings.app_auth_secret,
            algorithms=["HS256"],
            audience=self.settings.jwt_audience,
            issuer=self.settings.jwt_issuer,
        )
```

- [ ] **Step 4: 实现用户与管理员认证依赖**

`backend/app/api/dependencies.py` 的用户依赖必须用 `HTTPBearer(auto_error=False)`，通过 `get_db()` 查询 JWT `sub` 对应的 active user；缺少 token、JWT 解码失败、用户不存在或停用都返回 401。`require_admin` 对非管理员返回 403。此步骤不改现有 `main.py`，因此当前 API 仍由旧认证处理，直到 Task 4 原子切换路由。

依赖稳定签名必须是 `get_current_user(credentials, db) -> User` 和 `require_admin(current_user) -> User`，便于 Task 4/6 直接复用。

- [ ] **Step 5: 增加受控管理员创建命令**

`scripts/create_admin.py` 接收 `--account` 和 `--nickname`，密码只通过 `getpass.getpass()` 读取；调用与公开注册相同的 Argon2 哈希后显式写入 `UserRole.ADMIN`。命令不得打印密码或 JWT。

- [ ] **Step 6: 运行认证单元测试**

Run: `python -m pytest tests/unit/test_auth_service.py -v`

Expected: PASS，且用户 JWT 不能通过管理员依赖。

- [ ] **Step 7: 提交**

```bash
git add backend/app/repositories/users.py backend/app/services/auth.py backend/app/api/dependencies.py scripts/create_admin.py tests/unit/test_auth_service.py
git commit -m "feat: migrate authentication to Argon2 JWT and MySQL"
```

### Task 4: 把分析会话归属迁移到 MySQL 并拆分现有 API

**Files:**
- Create: `backend/app/repositories/sessions.py`
- Create: `backend/app/api/routes/auth.py`
- Create: `backend/app/api/routes/health.py`
- Create: `backend/app/api/routes/sessions.py`
- Create: `backend/app/api/routes/analysis.py`
- Create: `backend/app/api/router.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/schemas.py`
- Modify: `src/session_service.py`
- Delete: `src/auth_service.py`
- Create: `tests/unit/test_session_repository.py`
- Create: `tests/contract/test_auth_api.py`
- Create: `tests/contract/test_existing_api_contract.py`

**Interfaces:**
- Consumes: `User`、`AnalysisSession`、认证依赖、`request.app.state.graph`。
- Produces: 与现有前端完全兼容的 session/analysis API；`create_app(settings: Settings | None = None) -> FastAPI`。

- [ ] **Step 1: 写会话隔离和激活顺序失败测试**

```python
def test_user_cannot_read_or_activate_another_users_session(client, alice_token, bob_session_id) -> None:
    response = client.get(
        f"/api/sessions/{bob_session_id}",
        headers={"Authorization": f"Bearer {alice_token}"},
    )
    assert response.status_code == 404


def test_activate_session_moves_it_to_active_position(client, alice_token, alice_old_session_id) -> None:
    response = client.post(
        f"/api/sessions/{alice_old_session_id}/activate",
        headers={"Authorization": f"Bearer {alice_token}"},
    )
    assert response.status_code == 200
    listing = client.get("/api/sessions", headers={"Authorization": f"Bearer {alice_token}"}).json()
    assert listing["active_thread_id"] == alice_old_session_id
```

- [ ] **Step 2: 运行会话测试并确认仍依赖 JSON 文件**

Run: `python -m pytest tests/unit/test_session_repository.py tests/contract/test_existing_api_contract.py -v`

Expected: FAIL because the new repository and `create_app` factory do not exist.

- [ ] **Step 3: 实现会话仓储**

`backend/app/repositories/sessions.py` 必须提供以下完整行为：

```python
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.db.models import AnalysisSession
from src.session_service import generate_thread_id


def list_for_user(db: Session, user_id: str) -> list[AnalysisSession]:
    statement = (
        select(AnalysisSession)
        .where(AnalysisSession.user_id == user_id)
        .order_by(AnalysisSession.activated_at.desc(), AnalysisSession.updated_at.desc())
    )
    return list(db.scalars(statement))


def get_owned(db: Session, user_id: str, thread_id: str) -> AnalysisSession | None:
    return db.scalar(
        select(AnalysisSession).where(
            AnalysisSession.user_id == user_id,
            AnalysisSession.thread_id == thread_id,
        )
    )


def create_for_user(db: Session, user_id: str) -> AnalysisSession:
    count = len(list_for_user(db, user_id))
    item = AnalysisSession(
        user_id=user_id,
        thread_id=generate_thread_id(),
        label=f"分析会话 {count + 1}",
    )
    db.add(item)
    db.flush()
    return item


def activate(db: Session, item: AnalysisSession) -> None:
    now = datetime.now(timezone.utc)
    item.activated_at = now
    item.updated_at = now
    db.flush()
```

- [ ] **Step 4: 拆分 routes 并建立应用工厂**

把 `backend/app/main.py` 缩减为 `create_app()`、CORS 和 lifespan；把现有分析逻辑逐行移动到 `api/routes/analysis.py`，唯一行为变化是通过 `get_owned(db, current_user.id, thread_id)` 验证归属，并在分析后调用 `activate(db, session)`。`api/routes/sessions.py` 使用仓储实现现有路径。此任务先创建只含兼容 `/health` 的 health router；`api/router.py` 聚合顺序固定为 health、auth、sessions、analysis，设备 router 在 Task 6 创建后再加入。

`api/routes/auth.py` 保留原来的三个路径，注册成功后在同一事务创建默认分析会话。错误映射固定为：重复账户 409、错误账户或密码 401、无效/过期 token 401、非管理员访问 403；公开注册模型不包含 `role`。统一 serializer 为：

```python
def serialize_profile(user: User, sessions: list[AnalysisSession]) -> dict[str, object]:
    ordered = sorted(sessions, key=lambda item: item.activated_at, reverse=True)
    return {
        "account": user.account,
        "nickname": user.nickname,
        "role": user.role.value,
        "created_at": user.created_at.isoformat(),
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else "",
        "active_thread_id": ordered[0].thread_id if ordered else "",
        "sessions": [
            {
                "thread_id": item.thread_id,
                "label": item.label,
                "created_at": item.created_at.isoformat(),
                "updated_at": item.updated_at.isoformat(),
            }
            for item in ordered
        ],
    }
```

`create_app` 的稳定签名：

```python
def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or get_settings()
    app = FastAPI(title="Campus Recruitment Career Assistant API", version="2.0.0", lifespan=lifespan)
    app.state.settings = resolved
    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(api_router, prefix="/api")
    return app


app = create_app()
```

`backend/app/schemas.py` 的 `UserProfile` 新增 `role: Literal["student", "admin"]`，其余现有字段、默认值和中文错误文案保持兼容。

- [ ] **Step 5: 删除 JSON 认证路径并做静态扫描**

Run: `rg "app_users.json|USER_STORE_PATH|hashlib.sha256\(password" backend src`

Expected: no matches.

- [ ] **Step 6: 运行后端契约测试和前端构建**

Run: `python -m pytest tests/unit/test_session_repository.py tests/contract/test_auth_api.py tests/contract/test_existing_api_contract.py -v`

Expected: PASS.

Run: `npm --prefix frontend run build`

Expected: TypeScript and Vite build succeed.

- [ ] **Step 7: 提交**

```bash
git add backend/app src/session_service.py tests/unit/test_session_repository.py tests/contract/test_auth_api.py tests/contract/test_existing_api_contract.py
git rm src/auth_service.py
git commit -m "refactor: persist analysis session ownership in MySQL"
```

### Task 5: 建立禁止自动提交的权威任务状态机和审计事件

**Files:**
- Create: `backend/app/repositories/applications.py`
- Create: `backend/app/services/applications.py`
- Create: `tests/unit/test_application_state_machine.py`
- Create: `tests/unit/test_audit_redaction.py`

**Interfaces:**
- Consumes: `ApplicationTask`、`ApplicationEvent`、`ApplicationTaskStatus`、`TaskActor`。
- Produces: `ApplicationService.transition(...)`、`InvalidTransitionError`、`StaleTaskVersionError`、`UnsafeAuditPayloadError`。

- [ ] **Step 1: 写允许路径、禁止自动提交和乐观锁测试**

```python
def test_executor_cannot_start_final_submission(application_service, db, ready_task) -> None:
    with pytest.raises(InvalidTransitionError, match="human"):
        application_service.transition(
            db,
            task_id=ready_task.id,
            expected_version=ready_task.state_version,
            target=ApplicationTaskStatus.OBSERVING_USER_SUBMISSION,
            actor=TaskActor.EXECUTOR,
            event_type="submission_started",
            redacted_payload={},
        )


def test_human_can_start_observation_and_executor_can_report_result(application_service, db, ready_task) -> None:
    observing = application_service.transition(
        db,
        task_id=ready_task.id,
        expected_version=0,
        target=ApplicationTaskStatus.OBSERVING_USER_SUBMISSION,
        actor=TaskActor.HUMAN,
        event_type="user_clicked_submit",
        redacted_payload={"page_kind": "final_review"},
    )
    completed = application_service.transition(
        db,
        task_id=ready_task.id,
        expected_version=observing.state_version,
        target=ApplicationTaskStatus.SUBMITTED_SUCCESS,
        actor=TaskActor.EXECUTOR,
        event_type="submission_result_observed",
        redacted_payload={"result": "success"},
    )
    assert completed.status is ApplicationTaskStatus.SUBMITTED_SUCCESS
    assert completed.state_version == 2
```

- [ ] **Step 2: 运行状态机测试并确认失败**

Run: `python -m pytest tests/unit/test_application_state_machine.py -v`

Expected: FAIL because `ApplicationService` does not exist.

- [ ] **Step 3: 实现显式转换矩阵和 actor 安全门**

`backend/app/services/applications.py` 中使用固定矩阵：

```python
ALLOWED_TRANSITIONS = {
    ApplicationTaskStatus.CREATED: {ApplicationTaskStatus.WAITING_FOR_DEVICE, ApplicationTaskStatus.CANCELLED},
    ApplicationTaskStatus.WAITING_FOR_DEVICE: {ApplicationTaskStatus.DISPATCHED, ApplicationTaskStatus.CANCELLED},
    ApplicationTaskStatus.DISPATCHED: {ApplicationTaskStatus.RUNNING, ApplicationTaskStatus.WAITING_FOR_HUMAN, ApplicationTaskStatus.FAILED, ApplicationTaskStatus.CANCELLED},
    ApplicationTaskStatus.RUNNING: {ApplicationTaskStatus.WAITING_FOR_HUMAN, ApplicationTaskStatus.READY_FOR_REVIEW, ApplicationTaskStatus.FAILED, ApplicationTaskStatus.CANCELLED},
    ApplicationTaskStatus.WAITING_FOR_HUMAN: {ApplicationTaskStatus.RUNNING, ApplicationTaskStatus.READY_FOR_REVIEW, ApplicationTaskStatus.FAILED, ApplicationTaskStatus.CANCELLED},
    ApplicationTaskStatus.READY_FOR_REVIEW: {ApplicationTaskStatus.OBSERVING_USER_SUBMISSION, ApplicationTaskStatus.CANCELLED},
    ApplicationTaskStatus.OBSERVING_USER_SUBMISSION: {ApplicationTaskStatus.SUBMITTED_SUCCESS, ApplicationTaskStatus.SUBMITTED_FAILED, ApplicationTaskStatus.RESULT_UNKNOWN},
    ApplicationTaskStatus.SUBMITTED_SUCCESS: set(),
    ApplicationTaskStatus.SUBMITTED_FAILED: set(),
    ApplicationTaskStatus.RESULT_UNKNOWN: set(),
    ApplicationTaskStatus.FAILED: set(),
    ApplicationTaskStatus.CANCELLED: set(),
}
```

若目标为 `OBSERVING_USER_SUBMISSION` 且 actor 不是 `HUMAN`，必须拒绝。仓储使用单条带版本条件的更新：

```python
statement = (
    update(ApplicationTask)
    .where(ApplicationTask.id == task_id, ApplicationTask.state_version == expected_version)
    .values(status=target, state_version=expected_version + 1, updated_at=utc_now())
)
if db.execute(statement).rowcount != 1:
    raise StaleTaskVersionError(task_id)
```

状态更新与 `ApplicationEvent` 追加必须在同一数据库事务完成。

- [ ] **Step 4: 实现审计载荷拒绝规则**

在写事件前递归检查 key，以下大小写不敏感 key 直接拒绝：`password`、`token`、`cookie`、`captcha`、`id_card`、`form_values`、`resume_text`。字符串值超过 500 字符也拒绝，确保事件仅为脱敏摘要。

- [ ] **Step 5: 运行状态机与审计测试**

Run: `python -m pytest tests/unit/test_application_state_machine.py tests/unit/test_audit_redaction.py -v`

Expected: PASS，覆盖所有状态边和所有终止状态。

- [ ] **Step 6: 提交**

```bash
git add backend/app/repositories/applications.py backend/app/services/applications.py tests/unit/test_application_state_machine.py tests/unit/test_audit_redaction.py
git commit -m "feat: add authoritative application safety state machine"
```

### Task 6: 实现 Redis 一次性设备配对、认证和撤销

**Files:**
- Create: `backend/app/repositories/devices.py`
- Create: `backend/app/services/devices.py`
- Create: `backend/app/api/routes/devices.py`
- Modify: `backend/app/api/dependencies.py`
- Modify: `backend/app/api/router.py`
- Modify: `backend/app/main.py`
- Create: `tests/unit/test_device_service.py`
- Create: `tests/contract/test_device_api.py`

**Interfaces:**
- Consumes: Redis client、`Device`、`AuditEvent`、当前用户依赖。
- Produces: `PairingTicket`、`IssuedDevice`、`DeviceService`、`get_current_device()` 和 `/api/devices` API。

- [ ] **Step 1: 写一次性、过期和撤销测试**

```python
def test_pairing_ticket_can_only_be_redeemed_once(device_service, db, user) -> None:
    ticket = device_service.create_pairing_ticket(user_id=user.id)
    issued = device_service.redeem_pairing_ticket(
        db,
        code=ticket.code,
        name="Alice Windows",
        public_key_pem=VALID_TEST_PUBLIC_KEY,
    )
    assert issued.device.platform is DevicePlatform.WINDOWS
    assert len(issued.plaintext_token) >= 43
    with pytest.raises(InvalidPairingTicketError):
        device_service.redeem_pairing_ticket(
            db, code=ticket.code, name="Replay", public_key_pem=VALID_TEST_PUBLIC_KEY
        )


def test_revoked_device_token_no_longer_authenticates(device_service, db, issued_device) -> None:
    assert device_service.authenticate(db, issued_device.plaintext_token).id == issued_device.device.id
    device_service.revoke(db, user_id=issued_device.device.user_id, device_id=issued_device.device.id)
    assert device_service.authenticate(db, issued_device.plaintext_token) is None


def test_heartbeat_sets_short_online_ttl(device_service, db, issued_device) -> None:
    device_service.heartbeat(db, issued_device.plaintext_token, version="0.1.0")
    key = f"device-online:{issued_device.device.id}"
    assert device_service.redis.get(key) == b"1"
    assert 1 <= device_service.redis.ttl(key) <= 90
```

- [ ] **Step 2: 运行测试并确认设备服务不存在**

Run: `python -m pytest tests/unit/test_device_service.py -v`

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: 实现票据和设备凭据**

固定 Redis key 为 `pairing-ticket:{sha256(code)}`，value 为只含 `user_id` 和 `created_at` 的 JSON，TTL 600 秒。兑换必须通过 Redis `GETDEL` 原子读取删除。设备 token 用 `secrets.token_urlsafe(32)` 生成，数据库只保存：

```python
def token_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
```

每次创建票据、成功配对和撤销设备都追加 `AuditEvent`，载荷只能含设备平台、版本和事件结果，不含 code、token 或 public key。

`heartbeat` 对通过认证的设备写入 `device-online:{device_id}=1`，TTL 固定 90 秒，并更新 MySQL 的 `last_seen_at` 和 `version`。设备列表的 `online` 只由该 Redis key 是否存在计算；Redis 丢失只会显示离线，不得修改设备状态。

- [ ] **Step 4: 实现设备 API 和认证依赖**

固定 API：

```text
POST   /api/devices/pairing-tickets   Bearer user -> {code, expires_at}
POST   /api/devices/pair              public one-time code -> {device, device_token}
GET    /api/devices                   Bearer user -> {devices: [...]}
DELETE /api/devices/{device_id}       Bearer owner -> 204
GET    /api/devices/me                X-Device-Token -> current device summary
POST   /api/devices/heartbeat         X-Device-Token -> {status: "online", expires_in: 90}
```

`device_token` 只在配对响应出现一次。列表模型不得包含 `token_hash` 或 `public_key_pem`。错误码固定：无效/过期/已用票据 400、无效设备令牌 401、非设备所有者 404。

`main.py` lifespan 创建 `redis.Redis.from_url(settings.redis_url)` 并保存到 `app.state.redis`，退出时关闭；测试通过 dependency override 注入 `fakeredis`。`api/router.py` 在 analysis 之后加入 devices router，不改变已有路径。

- [ ] **Step 5: 运行设备单元与契约测试**

Run: `python -m pytest tests/unit/test_device_service.py tests/contract/test_device_api.py -v`

Expected: PASS，并用 `fakeredis` 验证 `GETDEL`、600 秒配对 TTL 和 90 秒在线 TTL；测试依赖不得连接开发 Redis。

- [ ] **Step 6: 提交**

```bash
git add backend/app/repositories/devices.py backend/app/services/devices.py backend/app/api/routes/devices.py backend/app/api/dependencies.py backend/app/api/router.py backend/app/main.py tests/unit/test_device_service.py tests/contract/test_device_api.py
git commit -m "feat: add one-time Windows device pairing and revocation"
```

### Task 7: 实现客户端加密的 S3 兼容对象存储

**Files:**
- Create: `backend/app/services/storage.py`
- Create: `tests/unit/test_encrypted_storage.py`
- Create: `tests/integration/test_object_store.py`

**Interfaces:**
- Consumes: S3 client、`OBJECT_ENCRYPTION_KEY`。
- Produces: `BlobStore` protocol、`S3BlobStore`、`EncryptedObjectStore`、`StoredObject`。

- [ ] **Step 1: 写密文不可见和认证失败测试**

```python
def test_round_trip_encrypts_before_blob_store(memory_blob_store, encryption_key) -> None:
    store = EncryptedObjectStore(memory_blob_store, encryption_key)
    result = store.put(key="users/u1/resume.pdf", plaintext=b"private resume", content_type="application/pdf")
    raw = memory_blob_store.objects[result.key]
    assert b"private resume" not in raw.body
    assert store.get(key=result.key) == b"private resume"


def test_ciphertext_tampering_is_rejected(memory_blob_store, encryption_key) -> None:
    store = EncryptedObjectStore(memory_blob_store, encryption_key)
    store.put(key="users/u1/a", plaintext=b"secret", content_type="text/plain")
    memory_blob_store.objects["users/u1/a"].body = b"corrupted"
    with pytest.raises(InvalidTag):
        store.get(key="users/u1/a")
```

- [ ] **Step 2: 运行测试并确认存储服务不存在**

Run: `python -m pytest tests/unit/test_encrypted_storage.py -v`

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: 实现 AES-256-GCM 包装层**

`EncryptedObjectStore.put` 必须：base64 解码并验证 32-byte key；生成 12-byte nonce；以对象 key 的 UTF-8 bytes 作为 AAD；保存 `nonce + ciphertext`；metadata 只含 `encryption=v1-aes-256-gcm` 和原始 content type。`get` 读取 nonce 前 12 bytes 并使用相同 AAD 解密。`S3BlobStore` 只实现 `put_bytes/get_bytes/delete/head/ensure_bucket`，不得记录 body 或凭据。

稳定返回类型：

```python
@dataclass(frozen=True)
class StoredObject:
    key: str
    content_type: str
    plaintext_size: int
    encryption: str = "v1-aes-256-gcm"
```

- [ ] **Step 4: 增加 MinIO 集成测试**

`tests/integration/test_object_store.py` 使用环境变量 `TEST_S3_ENDPOINT` 控制 skip；创建唯一前缀对象，断言 S3 原始 body 不含明文、服务层可解密，最后在 `finally` 删除对象。

Run: `$env:TEST_S3_ENDPOINT='http://127.0.0.1:9000'; python -m pytest tests/integration/test_object_store.py -v`

Expected: PASS when Task 9 Compose is running.

- [ ] **Step 5: 运行存储测试并提交**

Run: `python -m pytest tests/unit/test_encrypted_storage.py -v`

Expected: PASS.

```bash
git add backend/app/services/storage.py tests/unit/test_encrypted_storage.py tests/integration/test_object_store.py
git commit -m "feat: add client-encrypted object storage"
```

### Task 8: 把 LangGraph checkpoint 抽象为 SQLite 开发 / Redis 8 生产

**Files:**
- Create: `src/checkpointing.py`
- Modify: `src/graph.py`
- Modify: `src/main.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/api/routes/analysis.py`
- Create: `tests/unit/test_checkpointing.py`
- Create: `tests/integration/test_redis_checkpoint.py`

**Interfaces:**
- Consumes: `Settings.checkpoint_backend`、Redis 8、LangGraph BaseCheckpointSaver。
- Produces: `checkpointer_context(settings)` context manager、`build_graph(checkpointer)`。

- [ ] **Step 1: 写显式注入和后端选择测试**

```python
def test_build_graph_uses_injected_checkpointer() -> None:
    saver = InMemorySaver()
    graph = build_graph(checkpointer=saver)
    assert graph.checkpointer is saver


def test_sqlite_checkpoint_context_closes_connection(tmp_path, test_settings) -> None:
    settings = test_settings.model_copy(
        update={"checkpoint_backend": "sqlite", "checkpoint_sqlite_path": tmp_path / "cp.sqlite"}
    )
    with checkpointer_context(settings) as saver:
        saver.get_tuple({"configurable": {"thread_id": "t1"}})
    assert settings.checkpoint_sqlite_path.exists()
```

- [ ] **Step 2: 运行测试并确认旧代码固定使用 SQLite**

Run: `python -m pytest tests/unit/test_checkpointing.py -v`

Expected: FAIL because `build_graph` has no `checkpointer` parameter and `src.checkpointing` does not exist.

- [ ] **Step 3: 实现有生命周期的 checkpointer context**

`src/checkpointing.py`：

```python
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.redis import RedisSaver
from langgraph.checkpoint.sqlite import SqliteSaver

from backend.app.config import Settings


@contextmanager
def checkpointer_context(settings: Settings) -> Iterator[BaseCheckpointSaver]:
    if settings.checkpoint_backend == "redis":
        with RedisSaver.from_conn_string(settings.redis_url) as saver:
            saver.setup()
            yield saver
        return

    settings.checkpoint_sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(settings.checkpoint_sqlite_path, check_same_thread=False)
    try:
        yield SqliteSaver(connection)
    finally:
        connection.close()
```

修改 `src/graph.py`：删除 `build_sqlite_checkpointer()` 和 sqlite 全局缓存，稳定签名改为：

```python
def build_graph(checkpointer: BaseCheckpointSaver):
    graph = StateGraph(InternshipAgentState)
    # 保留原有所有 node 和 edge 定义
    return graph.compile(checkpointer=checkpointer)
```

- [ ] **Step 4: 在 FastAPI lifespan 中持有 graph**

lifespan 必须进入 `checkpointer_context(app.state.settings)`，设置 `app.state.graph` 后再 yield；退出时释放 Redis/SQLite 资源。路由通过 `Request` 读取 graph，禁止重新建立全局 cache。

`src/main.py` 增加缺失的 `from typing import Any`，并把原来的 `app = build_graph()` 后续流程包进：

```python
settings = get_settings()
with checkpointer_context(settings) as saver:
    app = build_graph(checkpointer=saver)
    run_cli_action(app, args)
```

把 `main()` 中解析参数之后的原有分支原样抽成 `run_cli_action(app, args) -> None`，确保每个早退都发生在 context 内。输出从固定的 `checkpoint_db` 改为 `checkpoint_backend`，Redis 模式不得声称使用 SQLite 文件。

- [ ] **Step 5: 写 Redis 8 真集成测试**

`tests/integration/test_redis_checkpoint.py` 用唯一 `thread_id` 执行一个最小 `StateGraph` 两次，第二个 graph 实例必须能读到第一次的 state。测试前断言 `JSON.SET` 和 `FT._LIST` 可用，以便普通 Redis 7 给出明确失败原因。

Run: `$env:TEST_REDIS_URL='redis://127.0.0.1:6379/15'; python -m pytest tests/integration/test_redis_checkpoint.py -v`

Expected: PASS on Redis 8 and no checkpoint TTL.

- [ ] **Step 6: 运行单元测试并提交**

Run: `python -m pytest tests/unit/test_checkpointing.py -v`

Expected: PASS.

```bash
git add src/checkpointing.py src/graph.py src/main.py backend/app/main.py backend/app/api/routes/analysis.py tests/unit/test_checkpointing.py tests/integration/test_redis_checkpoint.py
git commit -m "feat: use Redis 8 for production LangGraph checkpoints"
```

### Task 9: 完成依赖就绪检查和 Docker Compose 开发环境

**Files:**
- Modify: `backend/app/api/routes/health.py`
- Modify: `backend/app/main.py`
- Modify: `docker-compose.yml`
- Modify: `backend/Dockerfile`
- Create: `docker/mysql/init-test-db.sql`
- Create: `tests/contract/test_health_api.py`

**Interfaces:**
- Consumes: MySQL、Redis 8、S3BlobStore。
- Produces: `/api/health/live`、`/api/health/ready`；保留 `/api/health` 兼容路径。

- [ ] **Step 1: 写 live/readiness 行为测试**

```python
def test_live_does_not_depend_on_external_services(client) -> None:
    response = client.get("/api/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_reports_each_dependency_without_secrets(client_with_failed_dependencies) -> None:
    response = client_with_failed_dependencies.get("/api/health/ready")
    assert response.status_code == 503
    assert response.json()["detail"] == {
        "status": "not_ready",
        "dependencies": {"mysql": "down", "redis": "down", "object_store": "down"},
    }
    assert "password" not in response.text.lower()
```

- [ ] **Step 2: 运行测试并确认 readiness 不存在**

Run: `python -m pytest tests/contract/test_health_api.py -v`

Expected: FAIL with 404 for `/api/health/live` and `/api/health/ready`.

- [ ] **Step 3: 实现健康检查**

`live` 永远只反映进程存活。FastAPI lifespan 在接受流量前调用一次 `S3BlobStore.ensure_bucket()`；`ready` 只读执行 MySQL `SELECT 1`、Redis `PING`、对象 bucket `head_bucket`。任何失败都返回 503 和固定的 `up/down` 状态，不返回异常消息、host、用户名或密钥。`GET /api/health` 继续返回 `{"status": "ok"}`。

- [ ] **Step 4: 扩展 Compose**

`docker-compose.yml` 必须包含：

```yaml
services:
  mysql:
    image: mysql:8.4
    environment:
      MYSQL_DATABASE: career_assistant
      MYSQL_USER: career
      MYSQL_PASSWORD: career-dev-password
      MYSQL_ROOT_PASSWORD: root-dev-password
    command: ["--character-set-server=utf8mb4", "--collation-server=utf8mb4_0900_ai_ci"]
    ports: ["3306:3306"]
    volumes:
      - mysql-data:/var/lib/mysql
      - ./docker/mysql/init-test-db.sql:/docker-entrypoint-initdb.d/10-test-db.sql:ro
    healthcheck:
      test: ["CMD-SHELL", "mysqladmin ping -h localhost -uroot -p$$MYSQL_ROOT_PASSWORD --silent"]
      interval: 5s
      timeout: 5s
      retries: 20

  redis:
    image: redis:8.0-alpine
    command: ["redis-server", "--appendonly", "yes"]
    ports: ["6379:6379"]
    volumes: ["redis-data:/data"]
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 20

  minio:
    image: minio/minio:RELEASE.2025-04-22T22-12-26Z
    command: server /data --console-address :9001
    environment:
      MINIO_ROOT_USER: minioadmin
      MINIO_ROOT_PASSWORD: minioadmin
    ports: ["9000:9000", "9001:9001"]
    volumes: ["minio-data:/data"]
    healthcheck:
      test: ["CMD", "mc", "ready", "local"]
      interval: 5s
      timeout: 5s
      retries: 20

  migrate:
    build:
      context: .
      dockerfile: backend/Dockerfile
    command: ["alembic", "upgrade", "head"]
    depends_on:
      mysql:
        condition: service_healthy
    env_file: .env

  backend:
    build:
      context: .
      dockerfile: backend/Dockerfile
    command: ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
    depends_on:
      migrate:
        condition: service_completed_successfully
      redis:
        condition: service_healthy
      minio:
        condition: service_healthy
    env_file: .env
    ports: ["8000:8000"]

  frontend:
    build:
      context: .
      dockerfile: frontend/Dockerfile
    depends_on:
      backend:
        condition: service_started
    ports: ["5173:80"]

volumes:
  mysql-data:
  redis-data:
  minio-data:
```

`docker/mysql/init-test-db.sql` 只创建 `career_assistant_test` 并授权给 `career`，不插入用户或个人数据。

- [ ] **Step 5: 启动并验证依赖、migration 和 readiness**

Run: `docker compose config`

Expected: exit 0 with all six services and three named volumes.

Run: `docker compose up -d --build mysql redis minio migrate backend frontend`

Expected: `mysql`, `redis`, `minio`, `backend`, `frontend` healthy/running and `migrate` exited 0.

Run: `Invoke-RestMethod http://127.0.0.1:8000/api/health/ready | ConvertTo-Json -Depth 4`

Expected: `status` is `ready`; dependencies `mysql`, `redis`, `object_store` are all `up`.

- [ ] **Step 6: 运行完整集成测试并提交**

Run: `$env:TEST_MYSQL_URL='mysql+pymysql://career:career-dev-password@127.0.0.1:3306/career_assistant_test?charset=utf8mb4'; $env:TEST_REDIS_URL='redis://127.0.0.1:6379/15'; $env:TEST_S3_ENDPOINT='http://127.0.0.1:9000'; python -m pytest tests/integration -v`

Expected: PASS.

```bash
git add backend/app/api/routes/health.py backend/app/main.py backend/Dockerfile docker-compose.yml docker/mysql/init-test-db.sql tests/contract/test_health_api.py
git commit -m "chore: add MySQL Redis 8 and encrypted object store environment"
```

### Task 10: 前端兼容、文档、全量质量门和迁移收尾

**Files:**
- Modify: `frontend/src/types.ts`
- Modify: `README.md`
- Modify: `.gitignore`
- Create: `docs/runbooks/platform-foundation.md`
- Create: `tests/security/test_no_sensitive_logging.py`

**Interfaces:**
- Consumes: 本包全部 API 和 Compose 服务。
- Produces: 可复现的开发运行手册、兼容前端构建、最终质量门。

- [ ] **Step 1: 更新前端类型并验证现有界面不需要重写**

把 `frontend/src/types.ts` 的 `UserProfile` 增加：

```typescript
role: "student" | "admin";
```

不得修改现有 token localStorage key、API path 或 session 字段。管理员界面不在本包增加。

Run: `npm --prefix frontend run build`

Expected: PASS.

- [ ] **Step 2: 写敏感日志失败测试**

测试对注册失败、登录失败、无效设备 token、对象存储失败和状态转换失败分别捕获日志，断言日志不含提交的 password、完整 token、pairing code、对象明文及配置 secret。

Run: `python -m pytest tests/security/test_no_sensitive_logging.py -v`

Expected before log filters are wired: FAIL if any secret is present; otherwise PASS.

- [ ] **Step 3: 完成运行手册和 README**

`docs/runbooks/platform-foundation.md` 必须逐条给出：环境变量生成、Compose 启停、migration upgrade/downgrade、管理员创建、设备撤销、数据库/Redis/对象存储备份边界、Redis 丢失恢复、密钥轮换前置检查、健康检查、测试命令。明确写出：MySQL 是权威库；删除 Redis 不得自动重跑或改变任何投递任务；旧 `data/app_users.json` 不迁移。

README 技术栈把 SQLite 改为“MySQL 权威数据 + Redis 8 LangGraph checkpoint（SQLite 仅开发）+ 客户端加密对象存储”，并链接总体设计和本计划。

`.gitignore` 增加 `.superpowers/`、本地 MinIO 数据、测试覆盖率和所有运行日志，保留已经提交的 `docs/superpowers` 文档。

- [ ] **Step 4: 运行格式、静态检查、所有测试和前端构建**

Run: `python -m ruff check backend src tests scripts`

Expected: `All checks passed!`

Run: `python -m pytest -v`

Expected: all unit/contract/security tests pass；仅在未设置集成环境变量时显示明确 skip。

Run: `npm --prefix frontend run build`

Expected: PASS.

Run: `rg "app_users.json|USER_STORE_PATH|replace-with-your-own-secret|password_hash.*sha256|postgres" backend src frontend docker-compose.yml README.md`

Expected: no matches；运行手册中对旧文件的迁移说明可用单独限定搜索核对。

- [ ] **Step 5: 做生产配置负向验证**

Run: `$env:APP_ENV='production'; $env:APP_AUTH_SECRET='replace-with-your-own-secret'; $env:CHECKPOINT_BACKEND='sqlite'; python -c "from backend.app.config import Settings; Settings()"`

Expected: non-zero exit and validation error; process must not start with默认 secret或 SQLite production checkpoint.

- [ ] **Step 6: 提交**

```bash
git add frontend/src/types.ts README.md .gitignore docs/runbooks/platform-foundation.md tests/security/test_no_sensitive_logging.py
git commit -m "docs: finalize platform foundation runbook and quality gates"
```

## 3. 完成判定

只有同时满足以下条件，工作包 1 才算完成：

- 新注册、登录、`/auth/me` 和分析会话全程使用 MySQL，代码中没有 JSON 用户存储读写。
- 现有 Vue 应用无需修改 API path 即可完成注册、登录、创建/切换会话和运行分析。
- MySQL migration 可从空库 upgrade 到 head，并能 downgrade 到 base。
- Redis 8 checkpoint 能跨 graph 实例恢复；删除 Redis 不改变 MySQL 中 `ApplicationTask.status`。
- 设备配对码 10 分钟到期且一次性，设备 token 只显示一次，撤销立即生效。
- 状态机单元测试证明 executor 无权从 review 状态发起最终提交。
- 对象存储原始 body 不含简历明文，篡改密文会认证失败。
- `/api/health/live` 不依赖外部服务；`/api/health/ready` 能分别上报 MySQL、Redis 和对象存储状态且不泄露连接信息。
- 后端全量测试、静态检查、前端 build、真实 MySQL/Redis 8/MinIO 集成测试全部通过。
- 每个任务形成一个独立提交，提交中不包含 `.env`、数据库卷、Redis 数据、对象存储数据或用户个人数据。

## 4. 实施参考

- FastAPI OAuth2/JWT 与 Argon2：`https://fastapi.tiangolo.com/tutorial/security/oauth2-jwt/`
- SQLAlchemy MySQL + PyMySQL：`https://docs.sqlalchemy.org/en/20/core/engines.html#mysql`
- LangGraph Redis Saver：`https://github.com/redis-developer/langgraph-redis`
- Redis 8 内置 JSON/Search：`https://redis.io/docs/latest/develop/whats-new/8-0/`
- 总体产品设计：`docs/superpowers/specs/2026-07-14-campus-recruitment-career-assistant-design.md`
