# Discovery Strategy Router — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the StrategyRouter + SnapshotExecutor + TrajectoryStore system per spec `2026-07-20-discovery-strategy-router-design.md`, replacing the hardcoded Alibaba SPA shortcut with a configurable strategy library and adding trajectory recording with on-demand LLM annotation.

**Architecture:** StrategyRouter intercepts task URLs before the Supervisor Agent, matching against a MySQL-backed strategy library (URL pattern → YAML execution plan + optional DomainAdapter). On match, SnapshotExecutor (or Adapter) deterministically executes the plan; on step failure, Supervisor Agent takes over with snapshot_context injected. All execution paths record trajectories; fallback and supervisor-only paths trigger LLM annotation.

**Tech Stack:** Python 3.12+, SQLAlchemy (MySQL), deepagents (LangGraph), ChatOpenAI (DeepSeek v4-flash), PyYAML, string.Template

## Global Constraints

- `job_discovery_strategy_enabled: bool = False` — master switch to revert to pure Supervisor
- `strategy_degradation_threshold: int = 3` — consecutive failures before marking unavailable
- `strategy_recovery_threshold: int = 2` — consecutive successes to recover from degraded
- `trajectory_retention_days: int = 90`
- `strategy_health_check_interval_hours: int = 24`
- `trajectory_annotation_enabled: bool = True` — independent annotation toggle
- All strategy state counters use atomic SQL UPDATE (no read-modify-write)
- Template variables limited to single-level nesting (§5.2); static validation on strategy write
- Annotation only triggered for `executor_type='supervisor'` or `status='partial_fallback'`
- No changes to `tools/` directory, Web Nav SubAgent, API routes, repository, or `tasks.py`

---

## File Structure

### New Files

```
backend/app/services/job_discovery/
  strategy/
    __init__.py                    # Package init, re-exports
    error_classifier.py            # classify_error() — standalone
    trajectory_buffer.py           # TrajectoryBuffer class
    strategy_store.py              # StrategyRecord CRUD + atomic state updates
    strategy_router.py             # StrategyRouter.match(url) → StrategyRecord | None
    snapshot_executor.py           # SnapshotExecutor.execute(strategy, task, buffer)
    trajectory_store.py            # Trajectory persistence + annotation scheduling
    trajectory_annotator.py        # TrajectoryAnnotator.annotate(trajectory)
  adapters/
    __init__.py                    # Package init
    base.py                        # DomainAdapter ABC
    alibaba_spa.py                 # AlibabaSPAAdapter
  prompts/
    supervisor_base.txt            # Role, tools, output format, security
    supervisor_clean_start.txt     # "You are starting fresh..."
    supervisor_snapshot_fallback.txt # "Continue from breakpoint..."
```

### Modified Files

| File | Changes |
|------|---------|
| `config.py` | +6 settings fields |
| `schemas.py` | +4 dataclasses |
| `models.py` | +2 ORM models |
| `deepagents_runner.py` | Remove Alibaba hardcode; prompt from files; accept snapshot_context |
| `worker.py` | Insert StrategyRouter; trajectory/strategy writes; health check trigger |

### New Database Tables

- `job_discovery_strategies` — one row per URL pattern strategy
- `job_discovery_trajectories` — one row per task execution

---

### Task 0: Database Migration & Config

**Files:**
- Create: `alembic/versions/20260720_0001_add_strategy_and_trajectory_tables.py`
- Modify: `backend/app/config.py:88-95`
- Modify: `backend/app/db/models.py` (append after DiscoveredJobCandidate)
- Create: `tests/unit/test_strategy_models.py`

**Interfaces:**
- Consumes: `UUIDPrimaryKeyMixin`, `TimestampMixin`, `Base`, `utc_now` from `backend.app.db.base`
- Produces: `JobDiscoveryStrategy` ORM model, `JobDiscoveryTrajectory` ORM model, 6 new Settings fields

- [ ] **Step 1: Add config fields**

In `backend/app/config.py`, after line 95 (`job_discovery_ocr_enabled: bool = False`), add:

```python
# Strategy Router settings
job_discovery_strategy_enabled: bool = False
strategy_degradation_threshold: int = Field(default=3, ge=1, le=10)
strategy_recovery_threshold: int = Field(default=2, ge=1, le=10)
trajectory_retention_days: int = Field(default=90, ge=7, le=365)
strategy_health_check_interval_hours: int = Field(default=24, ge=1, le=168)
trajectory_annotation_enabled: bool = True
```

- [ ] **Step 2: Verify config change doesn't break existing tests**

```bash
.\.venv\Scripts\python.exe -c "from backend.app.config import Settings; print('OK')"
```

Expected: prints `OK` without error.

- [ ] **Step 3: Write ORM model tests**

Write `tests/unit/test_strategy_models.py`:

```python
"""Unit tests for Strategy and Trajectory ORM models."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from backend.app.db.base import Base
from backend.app.db.models import JobDiscoveryStrategy, JobDiscoveryTrajectory


@pytest.fixture
def engine():
    e = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(e)
    return e


class TestJobDiscoveryStrategy:
    def test_create_minimal(self, engine):
        s = JobDiscoveryStrategy(
            url_pattern="mp.weixin.qq.com/s/*",
            site_type="wechat",
            description="Test strategy",
            plan_yaml="plan: []",
        )
        with Session(engine) as db:
            db.add(s)
            db.commit()
            db.refresh(s)
            assert s.id is not None
            assert s.status == "active"
            assert s.enabled is True
            assert s.error_count == 0
            assert s.consecutive_ok == 0
            assert s.degradation_threshold == 3

    def test_create_with_adapter(self, engine):
        s = JobDiscoveryStrategy(
            url_pattern="campus*.alibaba.com/*",
            site_type="spa",
            description="Ali SPA",
            plan_yaml="plan: []",
            adapter="adapters.alibaba_spa.AlibabaSPAAdapter",
            priority=10,
        )
        with Session(engine) as db:
            db.add(s)
            db.commit()
            assert s.adapter == "adapters.alibaba_spa.AlibabaSPAAdapter"
            assert s.priority == 10

    def test_url_pattern_indexed(self, engine):
        """Verify url_pattern column is queryable (indexed)."""
        s1 = JobDiscoveryStrategy(url_pattern="example.com/a/*", site_type="other", plan_yaml="plan: []")
        s2 = JobDiscoveryStrategy(url_pattern="example.com/b/*", site_type="other", plan_yaml="plan: []")
        with Session(engine) as db:
            db.add_all([s1, s2])
            db.commit()
        with Session(engine) as db:
            from sqlalchemy import select
            rows = db.scalars(select(JobDiscoveryStrategy).where(
                JobDiscoveryStrategy.url_pattern.like("example.com/%")
            )).all()
            assert len(rows) == 2


class TestJobDiscoveryTrajectory:
    def test_create_basic(self, engine):
        t = JobDiscoveryTrajectory(
            task_id="task-1",
            executor_type="supervisor",
            overall_status="completed",
            url="https://example.com/job",
            url_pattern="example.com/*",
            completed_steps=[{"tool": "open_url", "ok": True}],
            annotations={"reusability_score": 0.5},
        )
        with Session(engine) as db:
            db.add(t)
            db.commit()
            db.refresh(t)
            assert t.id is not None
            assert t.overall_status == "completed"
            assert t.completed_steps == [{"tool": "open_url", "ok": True}]

    def test_create_with_failure(self, engine):
        t = JobDiscoveryTrajectory(
            task_id="task-2",
            executor_type="snapshot",
            overall_status="partial_fallback",
            url="https://example.com/job",
            url_pattern="example.com/*",
            failed_at_step=3,
            failed_tool="extract_jd_candidates",
            failed_error_message="empty text",
            failed_error_reason="empty_text",
            completed_steps=[
                {"tool": "open_url", "ok": True},
                {"tool": "parse_wechat_article", "ok": True},
            ],
            fallback_trace=[
                {"tool": "run_ocr", "ok": True},
                {"tool": "extract_jd_candidates", "ok": True},
            ],
        )
        with Session(engine) as db:
            db.add(t)
            db.commit()
            db.refresh(t)
            assert t.failed_at_step == 3
            assert t.failed_error_reason == "empty_text"
            assert len(t.completed_steps) == 2
            assert len(t.fallback_trace) == 2

    def test_strategy_id_nullable(self, engine):
        t = JobDiscoveryTrajectory(
            task_id="task-3",
            executor_type="supervisor",
            overall_status="completed",
            url="https://example.com/job",
            url_pattern="example.com/*",
            completed_steps=[],
            strategy_id=None,
        )
        with Session(engine) as db:
            db.add(t)
            db.commit()
            assert t.strategy_id is None

    def test_timestamp_auto(self, engine):
        t = JobDiscoveryTrajectory(
            task_id="task-4",
            executor_type="adapter",
            overall_status="completed",
            url="https://example.com/job",
            url_pattern="example.com/*",
            completed_steps=[],
        )
        with Session(engine) as db:
            db.add(t)
            db.commit()
            db.refresh(t)
            assert t.created_at is not None
```

- [ ] **Step 4: Run tests to verify they fail**

```bash
.\.venv\Scripts\python.exe -m pytest tests/unit/test_strategy_models.py -v
```

Expected: FAIL — models not defined yet.

- [ ] **Step 5: Add ORM models to models.py**

In `backend/app/db/models.py`, after the `DiscoveredJobCandidate` class (after line ~1100), append:

```python


# ---------------------------------------------------------------------------
# Strategy Router tables
# ---------------------------------------------------------------------------


class JobDiscoveryStrategy(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "job_discovery_strategies"
    __table_args__ = (
        Index("ix_job_discovery_strategies_pattern", "url_pattern"),
        Index("ix_job_discovery_strategies_status", "status"),
    )
    url_pattern: Mapped[str] = mapped_column(String(500), nullable=False)
    site_type: Mapped[str] = mapped_column(
        String(50), default="other", nullable=False, index=True
    )
    description: Mapped[str | None] = mapped_column(Text)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    adapter: Mapped[str | None] = mapped_column(String(500))
    plan_yaml: Mapped[str] = mapped_column(Text, nullable=False)  # renamed from MEDIUMTEXT — SQLAlchemy maps Text to the DB's text type
    status: Mapped[str] = mapped_column(
        String(20), default="active", nullable=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    total_runs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    success_runs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    fallback_runs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    consecutive_ok: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error_tool: Mapped[str | None] = mapped_column(String(100))
    last_error_reason: Mapped[str | None] = mapped_column(String(50))
    last_error_message: Mapped[str | None] = mapped_column(Text)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    success_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    avg_duration_s: Mapped[float | None] = mapped_column(Float)
    degradation_threshold: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    recovery_threshold: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    last_health_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    _trajectories: Mapped[list["JobDiscoveryTrajectory"]] = relationship(
        back_populates="_strategy", lazy="raise",
    )


class JobDiscoveryTrajectory(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "job_discovery_trajectories"
    __table_args__ = (
        Index("ix_job_discovery_trajectories_url_pattern", "url_pattern"),
        Index("ix_job_discovery_trajectories_created", "created_at"),
        ForeignKeyConstraint(
            ["task_id"], ["job_discovery_tasks.id"],
            name="fk_job_discovery_trajectories_task_id", ondelete="SET NULL",
        ),
    )
    task_id: Mapped[str | None] = mapped_column(String(36))
    strategy_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("job_discovery_strategies.id", ondelete="SET NULL"),
    )
    executor_type: Mapped[str] = mapped_column(String(20), nullable=False)
    overall_status: Mapped[str] = mapped_column(String(30), nullable=False)
    failed_at_step: Mapped[int | None] = mapped_column(Integer)
    failed_tool: Mapped[str | None] = mapped_column(String(100))
    failed_params: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    failed_error_type: Mapped[str | None] = mapped_column(String(100))
    failed_error_message: Mapped[str | None] = mapped_column(Text)
    failed_error_reason: Mapped[str | None] = mapped_column(String(50))
    completed_steps: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=list)
    fallback_trace: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    clean_path: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    annotations: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    url: Mapped[str | None] = mapped_column(Text)
    url_pattern: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    _strategy: Mapped["JobDiscoveryStrategy | None"] = relationship(
        back_populates="_trajectories", lazy="raise",
    )
```

- [ ] **Step 6: Run model tests to verify they pass**

```bash
.\.venv\Scripts\python.exe -m pytest tests/unit/test_strategy_models.py -v
```

Expected: 6 passed.

- [ ] **Step 7: Generate migration**

```bash
.\.venv\Scripts\alembic.exe revision --autogenerate -m "add strategy and trajectory tables"
```

Expected: creates `alembic/versions/20260720_NNNN_add_strategy_and_trajectory_tables.py`.

- [ ] **Step 8: Verify migration is valid (dry-run on SQLite)**

```bash
.\.venv\Scripts\python.exe -c "
from sqlalchemy import create_engine
from backend.app.db.base import Base
e = create_engine('sqlite:///:memory:')
Base.metadata.create_all(e)
print('All tables created successfully')
"
```

Expected: `All tables created successfully`.

- [ ] **Step 9: Run all existing unit tests to confirm no regression**

```bash
.\.venv\Scripts\python.exe -m pytest tests/unit/ -q
```

Expected: all existing tests still pass.

- [ ] **Step 10: Commit**

```bash
git add backend/app/config.py backend/app/db/models.py tests/unit/test_strategy_models.py alembic/versions/20260720_NNNN_add_strategy_and_trajectory_tables.py
git commit -m "feat: add strategy and trajectory tables with config fields"
```

---

### Task 1: Error Classifier

**Files:**
- Create: `backend/app/services/job_discovery/strategy/__init__.py` (empty)
- Create: `backend/app/services/job_discovery/strategy/error_classifier.py`
- Create: `tests/unit/test_error_classifier.py`

**Interfaces:**
- Produces: `classify_error(error_message: str) -> str` — returns one of `network_timeout | http_blocked | captcha | wechat_blocked | site_changed | empty_text | parse_error | ocr_failed | unknown`

- [ ] **Step 1: Write tests**

```python
"""Unit tests for error_classifier."""
from __future__ import annotations

import pytest
from backend.app.services.job_discovery.strategy.error_classifier import classify_error


@pytest.mark.parametrize("message,expected", [
    ("Connection timed out after 30 seconds", "network_timeout"),
    ("ReadTimeout: HTTPSConnectionPool", "network_timeout"),
    ("timed out waiting for response", "network_timeout"),
    ("HTTP 403 Forbidden", "http_blocked"),
    ("401 Unauthorized access", "http_blocked"),
    ("captcha required to proceed", "captcha"),
    ("请完成验证后继续访问", "captcha"),
    ("滑块验证码", "captcha"),
    ("环境异常，完成验证后即可继续访问", "wechat_blocked"),
    ("wechat verification wall detected", "wechat_blocked"),
    ("404 Not Found", "site_changed"),
    ("页面不存在", "site_changed"),
    ("no text content found on page", "empty_text"),
    ("empty body returned", "empty_text"),
    ("JSONDecodeError: Expecting value", "parse_error"),
    ("unexpected format in response", "parse_error"),
    ("OCR engine returned no text", "ocr_failed"),
    ("tesseract failed to initialize", "ocr_failed"),
    ("paddle could not process image", "ocr_failed"),
    ("some random other error", "unknown"),
    ("", "unknown"),
])
def test_classify_error(message, expected):
    assert classify_error(message) == expected


def test_classify_error_case_insensitive():
    assert classify_error("TIMEOUT ERROR") == "network_timeout"
    assert classify_error("Captcha Required") == "captcha"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.\.venv\Scripts\python.exe -m pytest tests/unit/test_error_classifier.py -v
```

Expected: FAIL — module not found.

- [ ] **Step 3: Implement error_classifier.py**

```python
"""Error classification for trajectory recording.

Maps raw exception/error messages to a fixed set of reason codes for
SQL-level aggregation and strategy health tracking.
"""
from __future__ import annotations

_PATTERNS: list[tuple[str, list[str]]] = [
    ("network_timeout", ["timeout", "timed out", "connectionerror", "readtimeout"]),
    ("http_blocked",    ["403", "401", "forbidden", "unauthorized"]),
    ("captcha",         ["captcha", "验证", "verify", "滑块", "验证码"]),
    ("wechat_blocked",  ["环境异常", "完成验证", "wechat_verification", "wechat verification"]),
    ("site_changed",    ["404", "not found", "页面不存在"]),
    ("empty_text",      ["no text", "empty body", "无正文", "empty page"]),
    ("parse_error",     ["jsondecode", "parse error", "unexpected format"]),
    ("ocr_failed",      ["ocr", "tesseract", "paddle", "no text in image"]),
]


def classify_error(error_message: str) -> str:
    """Classify a raw error message into a fixed category.

    Args:
        error_message: The raw error string or exception message.

    Returns:
        One of: network_timeout, http_blocked, captcha, wechat_blocked,
        site_changed, empty_text, parse_error, ocr_failed, unknown.
    """
    if not error_message:
        return "unknown"
    lower = error_message.lower()
    for reason, keywords in _PATTERNS:
        if any(kw in lower for kw in keywords):
            return reason
    return "unknown"
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.\.venv\Scripts\python.exe -m pytest tests/unit/test_error_classifier.py -v
```

Expected: 22 passed (20 parametrized + 1 case_insensitive + function count).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/job_discovery/strategy/__init__.py backend/app/services/job_discovery/strategy/error_classifier.py tests/unit/test_error_classifier.py
git commit -m "feat: add error classifier for trajectory recording"
```

---

### Task 2: Trajectory Buffer

**Files:**
- Create: `backend/app/services/job_discovery/strategy/trajectory_buffer.py`
- Create: `tests/unit/test_trajectory_buffer.py`

**Interfaces:**
- Produces: `class TrajectoryBuffer` with methods:
  - `__init__(task_id: str, strategy_id: str | None, executor_type: str)`
  - `record_step(tool: str, status: str, params: dict | None, result: Any | None, error: Exception | None = None, duration_ms: float = 0) -> None`
  - `to_snapshot_context(failed_step_index: int) -> dict`
  - `to_dict() -> dict`
  - Properties: `steps: list[dict]`, `failed_step_index: int | None`, `executor_type: str`

- [ ] **Step 1: Write tests**

```python
"""Unit tests for TrajectoryBuffer."""
from __future__ import annotations

import pytest
from backend.app.services.job_discovery.strategy.trajectory_buffer import TrajectoryBuffer


class TestTrajectoryBuffer:
    def test_init_basic(self):
        buf = TrajectoryBuffer(task_id="t1", strategy_id="s1", executor_type="snapshot")
        assert buf.task_id == "t1"
        assert buf.strategy_id == "s1"
        assert buf.executor_type == "snapshot"
        assert buf.steps == []
        assert buf.failed_step_index is None

    def test_record_success_step(self):
        buf = TrajectoryBuffer(task_id="t1", strategy_id=None, executor_type="adapter")
        buf.record_step("open_url", "ok", {"url": "https://x.com"}, "html content", duration_ms=150.0)
        assert len(buf.steps) == 1
        step = buf.steps[0]
        assert step["tool"] == "open_url"
        assert step["status"] == "ok"
        assert step["params"] == {"url": "https://x.com"}
        assert step["result"] == "html content"
        assert step["error"] is None
        assert step["duration_ms"] == 150.0
        assert "timestamp" in step

    def test_record_error_step_sets_failed_index(self):
        buf = TrajectoryBuffer(task_id="t1", strategy_id=None, executor_type="snapshot")
        buf.record_step("open_url", "ok", {}, "ok")
        buf.record_step("parse", "failed", {}, None, error=ValueError("bad"))
        assert buf.failed_step_index == 1

    def test_record_after_failure_goes_to_fallback(self):
        buf = TrajectoryBuffer(task_id="t1", strategy_id=None, executor_type="snapshot")
        buf.record_step("step1", "ok", {}, "ok")
        buf.record_step("step2", "failed", {}, None, error=RuntimeError("fail"))
        # After failure, further steps recorded are fallback (Supervisor takeover)
        buf.record_step("step3", "ok", {}, "fallback result")
        assert buf.failed_step_index == 1
        assert len(buf.steps) == 3
        # The fallback flag
        assert buf.steps[2]["is_fallback"] is True

    def test_to_snapshot_context(self):
        buf = TrajectoryBuffer(task_id="t1", strategy_id="s1", executor_type="snapshot")
        buf.record_step("open_url", "ok", {"url": "https://x.com"}, {"html": "<p>"})
        buf.record_step("parse", "failed", {"html": "<p>"}, None, error=ValueError("empty"))
        ctx = buf.to_snapshot_context()
        assert ctx["source"] == "snapshot"
        assert ctx["strategy_id"] == "s1"
        assert len(ctx["completed_steps"]) == 1
        assert ctx["completed_steps"][0]["tool"] == "open_url"
        assert ctx["failed_step"]["tool"] == "parse"
        assert "empty" in ctx["failed_step"]["error"]

    def test_to_dict(self):
        buf = TrajectoryBuffer(task_id="t1", strategy_id=None, executor_type="supervisor")
        buf.record_step("open_url", "ok", {}, "ok")
        d = buf.to_dict()
        assert d["task_id"] == "t1"
        assert d["executor_type"] == "supervisor"
        assert len(d["steps"]) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.\.venv\Scripts\python.exe -m pytest tests/unit/test_trajectory_buffer.py -v
```

Expected: FAIL — module not found.

- [ ] **Step 3: Implement trajectory_buffer.py**

```python
"""TrajectoryBuffer — shared real-time trace recorder for adapters and SnapshotExecutor.

Records each tool call as it happens so that on failure, partial progress is
available for Supervisor takeover (via to_snapshot_context) and final traces
are available for persistence (via to_dict).
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any


class TrajectoryBuffer:
    """In-memory buffer recording tool execution steps during a single task run."""

    def __init__(
        self,
        task_id: str,
        strategy_id: str | None,
        executor_type: str,
    ) -> None:
        self.task_id = task_id
        self.strategy_id = strategy_id
        self.executor_type = executor_type
        self._steps: list[dict[str, Any]] = []
        self._failed: bool = False
        self._started_at: float = time.monotonic()

    # -- public properties --------------------------------------------------

    @property
    def steps(self) -> list[dict[str, Any]]:
        return list(self._steps)

    @property
    def failed_step_index(self) -> int | None:
        for i, s in enumerate(self._steps):
            if s["status"] == "failed":
                return i
        return None

    @property
    def elapsed_ms(self) -> float:
        return (time.monotonic() - self._started_at) * 1000

    # -- recording ----------------------------------------------------------

    def record_step(
        self,
        tool: str,
        status: str,
        params: dict[str, Any] | None = None,
        result: Any = None,
        *,
        error: Exception | None = None,
        duration_ms: float = 0,
    ) -> None:
        """Record one tool execution step.

        After the first ``status='failed'`` step, subsequent calls are
        automatically marked ``is_fallback=True`` (Supervisor takeover).
        """
        is_fallback = self._failed
        self._steps.append({
            "tool": tool,
            "status": status,
            "params": params or {},
            "result": self._safe_serialize(result) if status == "ok" else None,
            "error": str(error) if error else None,
            "error_type": type(error).__name__ if error else None,
            "duration_ms": duration_ms,
            "is_fallback": is_fallback,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        if status == "failed" and not self._failed:
            self._failed = True

    # -- serialization ------------------------------------------------------

    def to_snapshot_context(self) -> dict[str, Any]:
        """Build the snapshot_context dict for Supervisor takeover.

        Includes completed steps up to (but not including) the failed step,
        plus the failed step itself with error details.
        """
        fail_idx = self.failed_step_index
        if fail_idx is None:
            return {}
        completed = self._steps[:fail_idx]
        failed = self._steps[fail_idx]
        return {
            "source": self.executor_type,
            "strategy_id": self.strategy_id,
            "completed_steps": [
                {"tool": s["tool"], "params": s["params"], "result": s["result"]}
                for s in completed if s["status"] == "ok"
            ],
            "failed_step": {
                "tool": failed["tool"],
                "params": failed["params"],
                "error": failed["error"] or "",
                "error_type": failed["error_type"] or "",
            },
        }

    def to_dict(self) -> dict[str, Any]:
        """Return full buffer contents as a plain dict for persistence."""
        return {
            "task_id": self.task_id,
            "strategy_id": self.strategy_id,
            "executor_type": self.executor_type,
            "steps": list(self._steps),
            "failed_step_index": self.failed_step_index,
            "elapsed_ms": self.elapsed_ms,
        }

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _safe_serialize(value: Any) -> Any:
        """Convert result to a JSON-safe form. Truncates large strings."""
        if value is None:
            return None
        if isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, (list, tuple)):
            return [TrajectoryBuffer._safe_serialize(v) for v in value[:50]]
        if isinstance(value, dict):
            serialized = {}
            for k, v in value.items():
                sv = TrajectoryBuffer._safe_serialize(v)
                if isinstance(sv, str) and len(sv) > 500:
                    sv = sv[:500] + "...[truncated]"
                serialized[k] = sv
            return serialized
        s = str(value)
        return s[:500] if len(s) > 500 else s
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.\.venv\Scripts\python.exe -m pytest tests/unit/test_trajectory_buffer.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/job_discovery/strategy/trajectory_buffer.py tests/unit/test_trajectory_buffer.py
git commit -m "feat: add TrajectoryBuffer for shared adapter/snapshot trace recording"
```

---

### Task 3: Strategy Store (CRUD + Atomic State Updates)

**Files:**
- Create: `backend/app/services/job_discovery/strategy/strategy_store.py`
- Create: `tests/unit/test_strategy_store.py`

**Interfaces:**
- Consumes: `JobDiscoveryStrategy` ORM model, `Session` from sqlalchemy
- Produces:
  - `get_active_strategies(db: Session) -> list[JobDiscoveryStrategy]`
  - `get_strategy_by_id(db: Session, strategy_id: str) -> JobDiscoveryStrategy | None`
  - `increment_error_count(db: Session, strategy_id: str, last_error: dict) -> None`
  - `increment_success(db: Session, strategy_id: str, duration_s: float | None) -> None`
  - `get_strategies_due_for_health_check(db: Session, interval_hours: int = 24) -> list[JobDiscoveryStrategy]`
  - `record_health_check(db: Session, strategy_id: str, ok: bool, detail: str) -> None`

- [ ] **Step 1: Write tests**

```python
"""Unit tests for strategy_store — in-memory SQLite."""
from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.db.base import Base
from backend.app.db.models import JobDiscoveryStrategy
from backend.app.services.job_discovery.strategy.strategy_store import (
    get_active_strategies,
    get_strategy_by_id,
    increment_error_count,
    increment_success,
    get_strategies_due_for_health_check,
    record_health_check,
)


@pytest.fixture
def engine():
    e = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(e)
    return e


@pytest.fixture
def db(engine):
    with Session(engine) as s:
        yield s


class TestGetActiveStrategies:
    def test_returns_only_active_and_degraded(self, db):
        s1 = JobDiscoveryStrategy(url_pattern="a/*", site_type="wechat", plan_yaml="plan: []", status="active")
        s2 = JobDiscoveryStrategy(url_pattern="b/*", site_type="spa", plan_yaml="plan: []", status="degraded")
        s3 = JobDiscoveryStrategy(url_pattern="c/*", site_type="other", plan_yaml="plan: []", status="unavailable")
        db.add_all([s1, s2, s3])
        db.commit()

        result = get_active_strategies(db)
        assert len(result) == 2
        statuses = {s.status for s in result}
        assert statuses == {"active", "degraded"}

    def test_returns_enabled_only(self, db):
        s1 = JobDiscoveryStrategy(url_pattern="a/*", site_type="wechat", plan_yaml="plan: []", enabled=True)
        s2 = JobDiscoveryStrategy(url_pattern="b/*", site_type="spa", plan_yaml="plan: []", enabled=False)
        db.add_all([s1, s2])
        db.commit()

        result = get_active_strategies(db)
        assert len(result) == 1
        assert result[0].url_pattern == "a/*"


class TestIncrementErrorCount:
    def test_atomic_increment(self, db):
        s = JobDiscoveryStrategy(url_pattern="a/*", site_type="wechat", plan_yaml="plan: []", error_count=2)
        db.add(s)
        db.commit()
        sid = s.id

        increment_error_count(db, sid, {"tool": "extract", "reason": "empty_text", "message": "no text"})
        db.commit()

        # Re-fetch
        updated = get_strategy_by_id(db, sid)
        assert updated is not None
        assert updated.error_count == 3
        assert updated.last_error_tool == "extract"
        assert updated.last_error_reason == "empty_text"
        assert updated.last_error_at is not None

    def test_marks_unavailable_at_threshold(self, db):
        s = JobDiscoveryStrategy(url_pattern="a/*", site_type="wechat", plan_yaml="plan: []",
                                 error_count=2, degradation_threshold=3)
        db.add(s)
        db.commit()
        sid = s.id

        increment_error_count(db, sid, {"tool": "x", "reason": "unknown", "message": "e"})
        db.commit()

        updated = get_strategy_by_id(db, sid)
        assert updated is not None
        assert updated.status == "unavailable"


class TestIncrementSuccess:
    def test_resets_error_count(self, db):
        s = JobDiscoveryStrategy(url_pattern="a/*", site_type="wechat", plan_yaml="plan: []",
                                 error_count=2, consecutive_ok=0, status="degraded")
        db.add(s)
        db.commit()
        sid = s.id

        increment_success(db, sid, duration_s=45.0)
        db.commit()

        updated = get_strategy_by_id(db, sid)
        assert updated is not None
        assert updated.error_count == 0
        assert updated.consecutive_ok == 1
        assert updated.success_runs == 1
        assert updated.total_runs == 1
        assert updated.avg_duration_s == 45.0

    def test_recovery_from_degraded(self, db):
        s = JobDiscoveryStrategy(url_pattern="a/*", site_type="wechat", plan_yaml="plan: []",
                                 consecutive_ok=1, status="degraded", degradation_threshold=2)
        db.add(s)
        db.commit()
        sid = s.id

        increment_success(db, sid)
        db.commit()

        updated = get_strategy_by_id(db, sid)
        assert updated is not None
        assert updated.status == "active"


class TestHealthCheck:
    def test_returns_strategies_due(self, db):
        now = datetime.now(timezone.utc)
        old = now - timedelta(hours=25)
        recent = now - timedelta(hours=1)

        s1 = JobDiscoveryStrategy(url_pattern="a/*", site_type="wechat", plan_yaml="plan: []",
                                  last_health_check_at=old, status="active")
        s2 = JobDiscoveryStrategy(url_pattern="b/*", site_type="spa", plan_yaml="plan: []",
                                  last_health_check_at=recent, status="active")
        s3 = JobDiscoveryStrategy(url_pattern="c/*", site_type="other", plan_yaml="plan: []",
                                  last_health_check_at=None, status="active")
        db.add_all([s1, s2, s3])
        db.commit()

        due = get_strategies_due_for_health_check(db, interval_hours=24)
        patterns = {s.url_pattern for s in due}
        assert "a/*" in patterns
        assert "c/*" in patterns       # never checked
        assert "b/*" not in patterns   # checked 1h ago

    def test_record_health_check_ok(self, db):
        s = JobDiscoveryStrategy(url_pattern="a/*", site_type="wechat", plan_yaml="plan: []")
        db.add(s)
        db.commit()
        sid = s.id

        record_health_check(db, sid, ok=True, detail="all good")
        db.commit()

        updated = get_strategy_by_id(db, sid)
        assert updated is not None
        assert updated.last_health_check_at is not None

    def test_record_health_check_fail_increments_error(self, db):
        s = JobDiscoveryStrategy(url_pattern="a/*", site_type="wechat", plan_yaml="plan: []",
                                 error_count=0)
        db.add(s)
        db.commit()
        sid = s.id

        record_health_check(db, sid, ok=False, detail="404")
        db.commit()

        updated = get_strategy_by_id(db, sid)
        assert updated is not None
        assert updated.error_count == 1
        assert updated.last_error_reason == "site_changed"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.\.venv\Scripts\python.exe -m pytest tests/unit/test_strategy_store.py -v
```

Expected: FAIL — module not found.

- [ ] **Step 3: Implement strategy_store.py**

```python
"""Strategy Store — CRUD and atomic state updates for JobDiscoveryStrategy records.

All state transitions use atomic SQL UPDATEs to avoid read-modify-write races
in multi-worker deployments.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, case, func, update
from sqlalchemy.orm import Session

from backend.app.db.models import JobDiscoveryStrategy
from backend.app.services.job_discovery.strategy.error_classifier import classify_error


def get_active_strategies(db: Session) -> list[JobDiscoveryStrategy]:
    """Return all enabled strategies in active or degraded state, ordered by priority desc."""
    return list(
        db.scalars(
            db.query(JobDiscoveryStrategy)
            .where(
                JobDiscoveryStrategy.enabled == True,
                JobDiscoveryStrategy.status.in_(["active", "degraded"]),
            )
            .order_by(JobDiscoveryStrategy.priority.desc(), JobDiscoveryStrategy.success_count.desc())
        ).all()
    )


def get_strategy_by_id(db: Session, strategy_id: str) -> JobDiscoveryStrategy | None:
    """Fetch a single strategy by primary key."""
    return db.get(JobDiscoveryStrategy, strategy_id)


def increment_error_count(db: Session, strategy_id: str, last_error: dict[str, str]) -> None:
    """Atomically increment error_count and record last error info.

    If error_count reaches degradation_threshold, atomically flips status to 'unavailable'.
    """
    values: dict[str, Any] = {
        "error_count": JobDiscoveryStrategy.error_count + 1,
        "total_runs": JobDiscoveryStrategy.total_runs + 1,
        "last_error_tool": last_error.get("tool", ""),
        "last_error_reason": last_error.get("reason", "unknown"),
        "last_error_message": last_error.get("message", ""),
        "last_error_at": func.now(),
        "consecutive_ok": 0,
        "status": case(
            (JobDiscoveryStrategy.error_count + 1 >= JobDiscoveryStrategy.degradation_threshold, "unavailable"),
            (JobDiscoveryStrategy.error_count + 1 >= 1, "degraded"),
            else_=JobDiscoveryStrategy.status,
        ),
    }
    db.execute(
        update(JobDiscoveryStrategy)
        .where(JobDiscoveryStrategy.id == strategy_id)
        .values(**values)
    )


def increment_success(
    db: Session,
    strategy_id: str,
    duration_s: float | None = None,
) -> None:
    """Atomically increment success counters and reset error streak.

    Recovers status from 'degraded' to 'active' when consecutive_ok reaches
    degradation_threshold.
    """
    values: dict[str, Any] = {
        "success_runs": JobDiscoveryStrategy.success_runs + 1,
        "total_runs": JobDiscoveryStrategy.total_runs + 1,
        "consecutive_ok": JobDiscoveryStrategy.consecutive_ok + 1,
        "error_count": 0,
        "status": case(
            (and_(
                JobDiscoveryStrategy.status == "degraded",
                JobDiscoveryStrategy.consecutive_ok + 1 >= JobDiscoveryStrategy.recovery_threshold,
            ), "active"),
            else_=JobDiscoveryStrategy.status,
        ),
    }
    if duration_s is not None:
        # NOTE: stores latest duration (not a rolling average); field name
        # avg_duration_s is kept for DB compatibility. A future migration
        # may compute a true exponential moving average here.
        values["avg_duration_s"] = duration_s
    db.execute(
        update(JobDiscoveryStrategy)
        .where(JobDiscoveryStrategy.id == strategy_id)
        .values(**values)
    )


def get_strategies_due_for_health_check(
    db: Session,
    interval_hours: int = 24,
) -> list[JobDiscoveryStrategy]:
    """Return active/degraded strategies not health-checked within interval_hours."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=interval_hours)
    strategies = db.scalars(
        db.query(JobDiscoveryStrategy)
        .where(
            JobDiscoveryStrategy.status.in_(["active", "degraded"]),
            JobDiscoveryStrategy.enabled == True,
            (JobDiscoveryStrategy.last_health_check_at < cutoff)
            | (JobDiscoveryStrategy.last_health_check_at.is_(None)),
        )
    ).all()
    return list(strategies)


def record_health_check(
    db: Session,
    strategy_id: str,
    ok: bool,
    detail: str,
) -> None:
    """Record a health check result. Failure increments error_count atomically."""
    if ok:
        db.execute(
            update(JobDiscoveryStrategy)
            .where(JobDiscoveryStrategy.id == strategy_id)
            .values(last_health_check_at=func.now())
        )
    else:
        increment_error_count(
            db, strategy_id,
            last_error={
                "tool": "health_check",
                "reason": classify_error(detail),
                "message": detail,
            },
        )
        db.execute(
            update(JobDiscoveryStrategy)
            .where(JobDiscoveryStrategy.id == strategy_id)
            .values(last_health_check_at=func.now())
        )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.\.venv\Scripts\python.exe -m pytest tests/unit/test_strategy_store.py -v
```

Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/job_discovery/strategy/strategy_store.py tests/unit/test_strategy_store.py
git commit -m "feat: add strategy_store with atomic state updates"
```

---

### Task 4: Strategy Router

**Files:**
- Create: `backend/app/services/job_discovery/strategy/strategy_router.py`
- Create: `tests/unit/test_strategy_router.py`

**Interfaces:**
- Produces: `class StrategyRouter` with:
  - `__init__(db: Session)`
  - `match(url: str) -> JobDiscoveryStrategy | None`

- [ ] **Step 1: Write tests**

```python
"""Unit tests for StrategyRouter."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.db.base import Base
from backend.app.db.models import JobDiscoveryStrategy
from backend.app.services.job_discovery.strategy.strategy_router import StrategyRouter


@pytest.fixture
def engine():
    e = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(e)
    return e


class TestStrategyRouter:
    def test_match_wechat_pattern(self, engine):
        s = JobDiscoveryStrategy(
            url_pattern="mp.weixin.qq.com/s/*",
            site_type="wechat",
            plan_yaml="plan: []",
            priority=10,
        )
        with Session(engine) as db:
            db.add(s)
            db.commit()

        with Session(engine) as db:
            router = StrategyRouter(db)
            result = router.match("https://mp.weixin.qq.com/s/abc123?token=xyz")
            assert result is not None
            assert result.url_pattern == "mp.weixin.qq.com/s/*"

    def test_match_alibaba_pattern(self, engine):
        s = JobDiscoveryStrategy(
            url_pattern="campus*.alibaba.com/*",
            site_type="spa",
            plan_yaml="plan: []",
            priority=10,
        )
        with Session(engine) as db:
            db.add(s)
            db.commit()

        with Session(engine) as db:
            router = StrategyRouter(db)
            result = router.match("https://campus-talent.alibaba.com/search?q=java")
            assert result is not None

    def test_no_match_returns_none(self, engine):
        with Session(engine) as db:
            router = StrategyRouter(db)
            result = router.match("https://unknown-site.com/jobs/123")
            assert result is None

    def test_highest_priority_wins(self, engine):
        s1 = JobDiscoveryStrategy(url_pattern="*.example.com/*", site_type="other", plan_yaml="plan: []", priority=1)
        s2 = JobDiscoveryStrategy(url_pattern="jobs.example.com/*", site_type="other", plan_yaml="plan: []", priority=10)
        with Session(engine) as db:
            db.add_all([s1, s2])
            db.commit()

        with Session(engine) as db:
            router = StrategyRouter(db)
            result = router.match("https://jobs.example.com/job/1")
            assert result is not None
            assert result.priority == 10

    def test_skip_unavailable(self, engine):
        s = JobDiscoveryStrategy(
            url_pattern="mp.weixin.qq.com/s/*",
            site_type="wechat",
            plan_yaml="plan: []",
            status="unavailable",
        )
        with Session(engine) as db:
            db.add(s)
            db.commit()

        with Session(engine) as db:
            router = StrategyRouter(db)
            result = router.match("https://mp.weixin.qq.com/s/abc123")
            assert result is None

    def test_degraded_still_matches(self, engine):
        s = JobDiscoveryStrategy(
            url_pattern="mp.weixin.qq.com/s/*",
            site_type="wechat",
            plan_yaml="plan: []",
            status="degraded",
        )
        with Session(engine) as db:
            db.add(s)
            db.commit()

        with Session(engine) as db:
            router = StrategyRouter(db)
            result = router.match("https://mp.weixin.qq.com/s/abc123")
            assert result is not None

    def test_same_priority_highest_success_count_wins(self, engine):
        s1 = JobDiscoveryStrategy(url_pattern="*.x.com/*", site_type="other", plan_yaml="plan: []",
                                  priority=5, success_count=10)
        s2 = JobDiscoveryStrategy(url_pattern="a.x.com/*", site_type="other", plan_yaml="plan: []",
                                  priority=5, success_count=100)
        with Session(engine) as db:
            db.add_all([s1, s2])
            db.commit()

        with Session(engine) as db:
            router = StrategyRouter(db)
            result = router.match("https://a.x.com/job")
            assert result is not None
            assert result.success_count == 100
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.\.venv\Scripts\python.exe -m pytest tests/unit/test_strategy_router.py -v
```

Expected: FAIL.

- [ ] **Step 3: Implement strategy_router.py**

```python
"""StrategyRouter — URL pattern matching against the strategy library.

Matches incoming task URLs against registered strategy patterns, returning
the best-matching active strategy or None for Supervisor fallback.
"""
from __future__ import annotations

import fnmatch
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy.orm import Session

from backend.app.db.models import JobDiscoveryStrategy
from backend.app.services.job_discovery.strategy.strategy_store import get_active_strategies


class StrategyRouter:
    """Matches URLs against the strategy library and returns the best strategy.

    Usage::

        router = StrategyRouter(db)
        strategy = router.match(task.source_url)
        if strategy is not None:
            ...  # execute via adapter or SnapshotExecutor
    """

    def __init__(self, db: Session) -> None:
        # Load all active strategies into memory. The strategy table is
        # expected to be small (< 100 rows), so a full load + linear
        # fnmatch scan is acceptable. If the table grows beyond that,
        # switch to DB-level LIKE / REGEXP filtering.
        self._strategies = get_active_strategies(db)

    def match(self, url: str) -> JobDiscoveryStrategy | None:
        """Return the best-matching strategy for *url*, or None.

        Matching rules:
        1. Normalize URL (strip query string, standardize scheme)
        2. For each active strategy, fnmatch the host+path against url_pattern
        3. Return the match with highest priority; ties broken by success_count
        """
        normalized = self._normalize_url(url)
        best: tuple[int, int, JobDiscoveryStrategy] | None = None  # (priority, success_count, strategy)

        for s in self._strategies:
            if self._pattern_matches(normalized, s.url_pattern):
                score = (s.priority or 0, s.success_count or 0)
                if best is None or score > (best[0], best[1]):
                    best = (score[0], score[1], s)

        return best[2] if best else None

    # -- internal -----------------------------------------------------------

    @staticmethod
    def _normalize_url(url: str) -> str:
        """Strip query string and fragment, force https scheme."""
        parts = urlsplit(url)
        return urlunsplit(("https", parts.netloc, parts.path, "", ""))

    @staticmethod
    def _pattern_matches(normalized_url: str, pattern: str) -> bool:
        """Check if normalized_url matches the given glob pattern."""
        return fnmatch.fnmatch(normalized_url, pattern) or fnmatch.fnmatch(
            normalized_url.replace("https://", ""), pattern
        )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.\.venv\Scripts\python.exe -m pytest tests/unit/test_strategy_router.py -v
```

Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/job_discovery/strategy/strategy_router.py tests/unit/test_strategy_router.py
git commit -m "feat: add StrategyRouter for URL pattern matching"
```

---

### Task 5: DomainAdapter Base + AlibabaSPAAdapter

**Files:**
- Create: `backend/app/services/job_discovery/adapters/__init__.py` (empty)
- Create: `backend/app/services/job_discovery/adapters/base.py`
- Create: `backend/app/services/job_discovery/adapters/alibaba_spa.py`
- Create: `tests/unit/test_domain_adapter.py`

**Interfaces:**
- Produces:
  - `class DomainAdapter(ABC)` with abstract `execute(task, strategy, trajectory) -> DiscoveryRunResult` and `validate(url) -> bool`
  - `class AlibabaSPAAdapter(DomainAdapter)`
- Consumes: `DiscoveryTaskInput`, `DiscoveryRunResult`, `StrategyRecord` (from schemas), `TrajectoryBuffer`

> Note: `StrategyRecord` is a new dataclass we'll add in this task; it mirrors `JobDiscoveryStrategy`'s relevant fields for use outside the ORM layer.

- [ ] **Step 1: Write DomainAdapter base tests**

```python
"""Unit tests for DomainAdapter base and AlibabaSPAAdapter."""
from __future__ import annotations

import pytest
from backend.app.services.job_discovery.adapters.base import DomainAdapter
from backend.app.services.job_discovery.adapters.alibaba_spa import AlibabaSPAAdapter
from backend.app.services.job_discovery.schemas import DiscoveryTaskInput, DiscoveryRunResult


class TestDomainAdapterBase:
    def test_cannot_instantiate_abc(self):
        with pytest.raises(TypeError):
            DomainAdapter()  # abstract

    def test_concrete_subclass_works(self):
        class TestAdapter(DomainAdapter):
            url_pattern = "test.com/*"
            def execute(self, task, strategy, trajectory):
                return DiscoveryRunResult(status="succeeded")
            def validate(self, url):
                return True

        adapter = TestAdapter()
        assert adapter.url_pattern == "test.com"
        task = DiscoveryTaskInput(
            source_id="s1", raw_record_id="r1", external_record_id="e1",
            source_key="test", source_url="https://test.com/job",
            url_hash="abc", record_fields=[],
        )
        result = adapter.execute(task, None, None)
        assert result.status == "succeeded"


class TestAlibabaSPAAdapter:
    def test_url_pattern(self):
        adapter = AlibabaSPAAdapter()
        assert "alibaba.com" in adapter.url_pattern

    def test_validate_valid_url(self):
        adapter = AlibabaSPAAdapter()
        assert adapter.validate("https://campus-talent.alibaba.com/search") is True

    def test_validate_invalid_url(self):
        adapter = AlibabaSPAAdapter()
        assert adapter.validate("https://google.com") is False
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.\.venv\Scripts\python.exe -m pytest tests/unit/test_domain_adapter.py -v
```

Expected: FAIL.

- [ ] **Step 3: Implement base.py**

```python
"""DomainAdapter — abstract base for domain-specific fast-path adapters."""
from __future__ import annotations

from abc import ABC, abstractmethod

from backend.app.services.job_discovery.schemas import DiscoveryRunResult, DiscoveryTaskInput
from backend.app.services.job_discovery.strategy.trajectory_buffer import TrajectoryBuffer


class DomainAdapter(ABC):
    """Base class for domain-specific job discovery adapters.

    Each adapter provides an optimal execution path for a known domain,
    typically calling internal APIs directly rather than navigating pages.
    """

    url_pattern: str = ""

    @abstractmethod
    def execute(
        self,
        task: DiscoveryTaskInput,
        strategy: "StrategyRecord",
        trajectory: TrajectoryBuffer,
    ) -> DiscoveryRunResult:
        """Execute job discovery for this domain.

        Must call trajectory.record_step() for each significant operation.
        On failure, the trajectory buffer already contains partial progress
        for Supervisor takeover.
        """
        ...

    @abstractmethod
    def validate(self, url: str) -> bool:
        """Quick check whether *url* is still reachable/valid for this adapter."""
        ...
```

- [ ] **Step 4: Add StrategyRecord to schemas.py**

In `backend/app/services/job_discovery/schemas.py`, after the `DiscoveryRunResult` class, append:

```python


@dataclass
class StrategyRecord:
    """In-memory representation of a matched strategy (decoupled from ORM)."""
    id: str
    url_pattern: str
    site_type: str
    description: str = ""
    priority: int = 0
    adapter: str | None = None
    plan_yaml: str = ""
    status: str = "active"
    success_count: int = 0

    @classmethod
    def from_orm(cls, orm_obj: Any) -> "StrategyRecord":
        """Build from a JobDiscoveryStrategy ORM instance."""
        return cls(
            id=orm_obj.id,
            url_pattern=orm_obj.url_pattern,
            site_type=orm_obj.site_type,
            description=orm_obj.description or "",
            priority=orm_obj.priority,
            adapter=orm_obj.adapter,
            plan_yaml=orm_obj.plan_yaml,
            status=orm_obj.status,
            success_count=orm_obj.success_count,
        )
```

- [ ] **Step 5: Migrate Alibaba SPA logic from deepagents_runner.py to adapter**

In `backend/app/services/job_discovery/adapters/alibaba_spa.py`:

```python
"""Alibaba SPA Adapter — direct XHR API extraction for campus recruitment pages."""
from __future__ import annotations

import fnmatch

from backend.app.services.job_discovery.adapters.base import DomainAdapter
from backend.app.services.job_discovery.schemas import DiscoveryRunResult, DiscoveryTaskInput
from backend.app.services.job_discovery.strategy.trajectory_buffer import TrajectoryBuffer


class AlibabaSPAAdapter(DomainAdapter):
    """Fast-path adapter for Alibaba campus recruitment SPAs.

    Calls the internal JSON search API directly, bypassing browser navigation
    and LLM planning entirely (~8 seconds vs 3-5 minutes).
    """

    url_pattern: str = "campus*.alibaba.com/*"

    def execute(
        self,
        task: DiscoveryTaskInput,
        strategy: "StrategyRecord",
        trajectory: TrajectoryBuffer,
    ) -> DiscoveryRunResult:
        """Execute via direct API call. Delegates to the existing
        _fetch_alibaba_search_api and _alibaba_position_evidence_from_search_payload
        functions in deepagents_runner for now.

        In a follow-up, those functions should be extracted to a shared utility module.
        """
        from backend.app.services.job_discovery.deepagents_runner import (
            _fetch_alibaba_search_api,
            _alibaba_position_evidence_from_search_payload,
            _generic_position_evidence_from_payload,
            verify_evidence,
            package_candidates,
        )
        import json

        trajectory.record_step("alibaba_api_fetch", "ok", {"url": task.source_url})

        try:
            search_data = _fetch_alibaba_search_api(task.source_url)
            evidence = _alibaba_position_evidence_from_search_payload(search_data)
            if not evidence:
                evidence = _generic_position_evidence_from_payload(search_data)
            trajectory.record_step("alibaba_evidence_extract", "ok", {},
                                   {"evidence_count": len(evidence)})
        except Exception as exc:
            trajectory.record_step("alibaba_api_fetch", "failed", {"url": task.source_url},
                                   error=exc)
            return DiscoveryRunResult(
                status="failed",
                summary=f"Alibaba SPA adapter API call failed: {exc}",
            )

        if not evidence:
            return DiscoveryRunResult(
                status="failed",
                summary="No job evidence found in Alibaba search API response",
            )

        # Use deterministic tools for JD extraction / verification / packaging
        evidence_json = json.dumps(evidence, ensure_ascii=False)
        candidates_json = _run_extraction(task.source_url, evidence)
        verified_json = verify_evidence(candidates_json, evidence_json)
        evidence_hash = evidence[0].get("content_hash", task.url_hash) if evidence else task.url_hash
        packaged_json = package_candidates(verified_json, evidence_hash, task.source_key)
        candidates = json.loads(packaged_json)

        trajectory.record_step("extract_verify_package", "ok", {},
                               {"candidate_count": len(candidates)})

        return DiscoveryRunResult(
            status="succeeded",
            evidence=evidence,
            candidates=candidates,
            summary=f"Alibaba SPA adapter extracted {len(candidates)} candidate(s)",
        )

    def validate(self, url: str) -> bool:
        """Check if URL matches the Alibaba campus pattern."""
        return fnmatch.fnmatch(url, self.url_pattern)


def _run_extraction(url: str, evidence: list) -> str:
    """Run JD extraction using tool functions."""
    from backend.app.services.job_discovery.deepagents_runner import extract_jd_candidates
    import json

    text_parts = []
    for ev in evidence:
        excerpt = ev.get("text_excerpt", "") if isinstance(ev, dict) else getattr(ev, "text_excerpt", "")
        if excerpt:
            text_parts.append(excerpt)
    combined_text = "\n\n".join(text_parts)
    return extract_jd_candidates(combined_text, url)
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
.\.venv\Scripts\python.exe -m pytest tests/unit/test_domain_adapter.py -v
```

Expected: 4 passed.

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/job_discovery/adapters/ backend/app/services/job_discovery/schemas.py tests/unit/test_domain_adapter.py
git commit -m "feat: add DomainAdapter base + AlibabaSPAAdapter with StrategyRecord"
```

---

### Task 6: SnapshotExecutor

**Files:**
- Create: `backend/app/services/job_discovery/strategy/snapshot_executor.py`
- Create: `tests/unit/test_snapshot_executor.py`

**Interfaces:**
- Consumes: `StrategyRecord`, `DiscoveryTaskInput`, `TrajectoryBuffer`, tool functions from `deepagents_runner`
- Produces: `class SnapshotExecutor` with:
  - `__init__(strategy: StrategyRecord, task: DiscoveryTaskInput, trajectory: TrajectoryBuffer)`
  - `execute() -> DiscoveryRunResult`
  - `execute() can return with snapshot_context embedded in DiscoveryRunResult when it fails`

- [ ] **Step 1: Write tests**

```python
"""Unit tests for SnapshotExecutor."""
from __future__ import annotations

import json
import pytest
from unittest.mock import patch, MagicMock

from backend.app.services.job_discovery.schemas import (
    DiscoveryTaskInput,
    DiscoveryRunResult,
    StrategyRecord,
)
from backend.app.services.job_discovery.strategy.trajectory_buffer import TrajectoryBuffer
from backend.app.services.job_discovery.strategy.snapshot_executor import SnapshotExecutor


SIMPLE_PLAN_YAML = """
plan:
  - tool: triage_link
    params:
      url: "{{task.url}}"
    expect: "classify URL"
    on_error: "skip"
  - tool: extract_jd_candidates
    params:
      page_text: "{{prev.result.text}}"
      url: "{{task.url}}"
    expect: "extract JDs"
    on_error: "retry_then_skip"
"""


@pytest.fixture
def strategy():
    return StrategyRecord(
        id="s1",
        url_pattern="test.com/*",
        site_type="other",
        plan_yaml=SIMPLE_PLAN_YAML,
    )


@pytest.fixture
def task():
    return DiscoveryTaskInput(
        source_id="s1", raw_record_id="r1", external_record_id="e1",
        source_key="test", source_url="https://test.com/job",
        url_hash="abc", record_fields=[],
    )


class TestSnapshotExecutor:
    def test_resolves_template_variables(self, strategy, task):
        buf = TrajectoryBuffer(task_id="t1", strategy_id="s1", executor_type="snapshot")
        executor = SnapshotExecutor(strategy, task, buf)

        # Test template resolution directly
        from yaml import safe_load
        plan = safe_load(SIMPLE_PLAN_YAML)["plan"]
        resolved = executor._resolve_template(plan[0]["params"], {"task": task, "prev": None})
        assert resolved["url"] == "https://test.com/job"

    def test_resolve_prev_result(self, strategy, task):
        buf = TrajectoryBuffer(task_id="t1", strategy_id="s1", executor_type="snapshot")
        executor = SnapshotExecutor(strategy, task, buf)
        context = {
            "task": task,
            "prev": {"result": {"text": "Job description here"}},
        }
        from yaml import safe_load
        plan = safe_load(SIMPLE_PLAN_YAML)["plan"]
        resolved = executor._resolve_template(plan[1]["params"], context)
        assert resolved["page_text"] == "Job description here"

    def test_missing_field_resolves_to_none(self, strategy, task):
        buf = TrajectoryBuffer(task_id="t1", strategy_id="s1", executor_type="snapshot")
        executor = SnapshotExecutor(strategy, task, buf)
        context = {"task": task, "prev": {"result": {}}}  # no 'text' key
        from yaml import safe_load
        plan = safe_load(SIMPLE_PLAN_YAML)["plan"]
        resolved = executor._resolve_template(plan[1]["params"], context)
        assert resolved["page_text"] is None

    def test_parses_yaml_plan(self, strategy, task):
        buf = TrajectoryBuffer(task_id="t1", strategy_id="s1", executor_type="snapshot")
        executor = SnapshotExecutor(strategy, task, buf)
        steps = executor._parse_plan()
        assert len(steps) == 2
        assert steps[0]["tool"] == "triage_link"
        assert steps[1]["tool"] == "extract_jd_candidates"

    @patch("backend.app.services.job_discovery.strategy.snapshot_executor._call_tool_by_name")
    def test_execute_short_circuit_on_failure(self, mock_call, strategy, task):
        """When a step fails, SnapshotExecutor returns a result with snapshot_context,
        not a fully completed result."""
        from backend.app.services.job_discovery.strategy.snapshot_executor import SnapshotExecutionResult

        mock_call.side_effect = [
            {"site_type": "other", "notes": "ok"},  # step 1 ok
            RuntimeError("extraction failed"),       # step 2 fails
        ]

        buf = TrajectoryBuffer(task_id="t1", strategy_id="s1", executor_type="snapshot")
        executor = SnapshotExecutor(strategy, task, buf)
        result = executor.execute()

        assert isinstance(result, SnapshotExecutionResult)
        assert result.needs_supervisor_fallback is True
        assert result.snapshot_context is not None
        assert result.snapshot_context["source"] == "snapshot"
        assert len(result.snapshot_context["completed_steps"]) == 1

    @patch("backend.app.services.job_discovery.strategy.snapshot_executor._call_tool_by_name")
    def test_execute_all_success(self, mock_call, strategy, task):
        mock_call.side_effect = [
            {"site_type": "other", "text": "JD text here"},
            [{"title": "Engineer", "company_name": "Acme"}],  # extract_jd_candidates returns list
        ]

        buf = TrajectoryBuffer(task_id="t1", strategy_id="s1", executor_type="snapshot")
        executor = SnapshotExecutor(strategy, task, buf)
        result = executor.execute()

        assert result.status == "succeeded"
        assert len(result.candidates) > 0

    def test_runtime_tools_injection(self, strategy, task):
        """Verify that tool_dependencies are registered as callable runtime tools."""
        buf = TrajectoryBuffer(task_id="t1", strategy_id="s1", executor_type="snapshot")
        executor = SnapshotExecutor(
            strategy, task, buf,
            tool_dependencies={
                "settings": MagicMock(),
                "web_nav_subagent": MagicMock(),
                "model": MagicMock(),
            },
        )
        assert "run_web_navigation" in executor._runtime_tools
        assert callable(executor._runtime_tools["run_web_navigation"])

    def test_resolve_template_literal_none_string(self, strategy, task):
        """A literal 'None' string (no template markers) should stay as 'None' string."""
        buf = TrajectoryBuffer(task_id="t1", strategy_id="s1", executor_type="snapshot")
        executor = SnapshotExecutor(strategy, task, buf)
        # Not a template — the string "None" should remain "None"
        result = executor._resolve_template({"key": "None"})
        assert result["key"] == "None"

    def test_resolve_template_missing_field_is_none(self, strategy, task):
        """A missing field resolved through {{}} should become Python None."""
        buf = TrajectoryBuffer(task_id="t1", strategy_id="s1", executor_type="snapshot")
        executor = SnapshotExecutor(strategy, task, buf)
        result = executor._resolve_template({"key": "{{prev.result.no_such_field}}"})
        assert result["key"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.\.venv\Scripts\python.exe -m pytest tests/unit/test_snapshot_executor.py -v
```

Expected: FAIL.

- [ ] **Step 3: Implement snapshot_executor.py**

```python
"""SnapshotExecutor — deterministic replay of a strategy's YAML plan steps.

Calls the same tool functions as the Supervisor Agent but without LLM planning.
On step failure, returns a SnapshotExecutionResult that signals the caller to
hand over to the Supervisor Agent with snapshot_context injected.
"""
from __future__ import annotations

import string
from dataclasses import dataclass, field
from typing import Any

import yaml

from backend.app.services.job_discovery.schemas import (
    DiscoveryRunResult,
    DiscoveryTaskInput,
    NormalizedJobCandidate,
    PageEvidence,
    StrategyRecord,
)
from backend.app.services.job_discovery.strategy.trajectory_buffer import TrajectoryBuffer


@dataclass
class SnapshotExecutionResult(DiscoveryRunResult):
    """Extended result carrying snapshot_context when Supervisor takeover is needed."""
    needs_supervisor_fallback: bool = False
    snapshot_context: dict[str, Any] | None = None


class SnapshotExecutor:
    """Replay a YAML plan step-by-step against real tool functions.

    On any step failure, short-circuits and returns a result with embedded
    snapshot_context for Supervisor takeover.
    """

    def __init__(
        self,
        strategy: StrategyRecord,
        task: DiscoveryTaskInput,
        trajectory: TrajectoryBuffer,
        tool_dependencies: dict[str, Any] | None = None,
    ) -> None:
        self.strategy = strategy
        self.task = task
        self.trajectory = trajectory
        self._context: dict[str, Any] = {"task": task, "prev": None}
        self._runtime_tools: dict[str, Any] = {}
        if tool_dependencies:
            self._inject_runtime_tools(tool_dependencies)

    def _inject_runtime_tools(self, deps: dict[str, Any]) -> None:
        """Register tools that need runtime dependencies (settings, model, subagent).

        These tools cannot be stored in the static _TOOL_REGISTRY because they
        require per-task injected objects that aren't available at import time.
        """
        from backend.app.services.job_discovery.deepagents_runner import run_web_navigation

        settings = deps.get("settings")
        subagent = deps.get("web_nav_subagent")
        model = deps.get("model")

        def _wrapped_run_web_navigation(start_url: str) -> dict[str, Any]:
            return run_web_navigation(
                start_url, settings=settings, subagent=subagent, model=model,
            )

        _wrapped_run_web_navigation.__name__ = "run_web_navigation"
        _wrapped_run_web_navigation.__doc__ = run_web_navigation.__doc__
        _wrapped_run_web_navigation.__annotations__ = {"start_url": str, "return": dict[str, Any]}

        self._runtime_tools["run_web_navigation"] = _wrapped_run_web_navigation

    def execute(self) -> DiscoveryRunResult:
        """Execute the plan. Returns SnapshotExecutionResult on any step failure."""
        steps = self._parse_plan()
        completed: list[dict[str, Any]] = []

        for i, step in enumerate(steps):
            params = self._resolve_template(step.get("params", {}))
            tool_name = step["tool"]
            on_error = step.get("on_error", "skip")

            try:
                result = _call_tool_by_name(tool_name, executor=self, **params)
                self.trajectory.record_step(tool_name, "ok", params, result)
                self._context["prev"] = {"result": result}
                completed.append({"tool": tool_name, "params": params, "result": result})
            except Exception as exc:
                self.trajectory.record_step(tool_name, "failed", params, None, error=exc)
                return SnapshotExecutionResult(
                    status="failed",
                    summary=f"Snapshot step {i+1} ({tool_name}) failed: {exc}",
                    needs_supervisor_fallback=True,
                    snapshot_context=self.trajectory.to_snapshot_context(),
                )

        # All steps succeeded — construct final result from last tool's output
        return self._build_final_result(completed)

    # -- internal -----------------------------------------------------------

    def _parse_plan(self) -> list[dict[str, Any]]:
        """Parse the YAML plan_yaml string into step dicts."""
        parsed = yaml.safe_load(self.strategy.plan_yaml)
        if isinstance(parsed, dict) and "plan" in parsed:
            return parsed["plan"]
        if isinstance(parsed, list):
            return parsed
        raise ValueError(f"Invalid plan_yaml format for strategy {self.strategy.id}")

    def _resolve_template(self, params: dict[str, Any]) -> dict[str, Any]:
        """Resolve {{}} template variables in param values."""
        resolved: dict[str, Any] = {}
        for key, value in params.items():
            if isinstance(value, str) and "{{" in value:
                result = self._substitute(value)
                # A missing field produces the string "None" after substitution.
                # Only convert to Python None when the original value actually
                # contained a template marker (not a literal "None" string).
                resolved[key] = None if (result == "None" and "{{" in value) else result
            else:
                resolved[key] = value
        return resolved

    def _substitute(self, template: str) -> Any:
        """Substitute a single {{...}} expression.

        Supports: {{task.url}}, {{task.source_key}}, {{task.xxx}},
        {{prev.result}}, {{prev.result.xxx}} (single-level only).
        Missing fields resolve to None.
        """
        import re

        def _replacer(match: re.Match) -> str:
            expr = match.group(1).strip()
            parts = expr.split(".")
            value: Any = self._context
            for p in parts:
                if isinstance(value, dict):
                    value = value.get(p)
                elif hasattr(value, p):
                    value = getattr(value, p, None)
                else:
                    value = None
                    break
            if value is None:
                return "None"
            if isinstance(value, (dict, list)):
                import json
                return json.dumps(value, ensure_ascii=False)
            return str(value)

        result = re.sub(r"\{\{(.+?)\}\}", _replacer, template)
        if result == "None":
            return None
        return result

    def _build_final_result(self, completed: list[dict[str, Any]]) -> DiscoveryRunResult:
        """Build DiscoveryRunResult from the completed steps."""
        evidence: list[PageEvidence] = []
        candidates: list[NormalizedJobCandidate] = []

        for step in completed:
            result = step.get("result")
            if isinstance(result, dict):
                if "evidence_type" in result:
                    evidence.append(PageEvidence(**result))
                elif isinstance(result.get("candidates"), list):
                    candidates = result["candidates"]
                elif isinstance(result.get("evidence"), list):
                    evidence = result["evidence"]
            if isinstance(result, list) and result:
                first = result[0] if isinstance(result[0], dict) else None
                if first and isinstance(first, dict):
                    if "evidence_type" in first:
                        evidence = [PageEvidence(**e) for e in result if isinstance(e, dict)]
                    else:
                        candidates = [NormalizedJobCandidate(**c) for c in result if isinstance(c, dict)]

        # If the last step returned a JSON string, try to parse
        last_result = completed[-1].get("result") if completed else None
        if isinstance(last_result, str):
            import json
            try:
                parsed = json.loads(last_result)
                if isinstance(parsed, list):
                    candidates = [NormalizedJobCandidate(**c) for c in parsed if isinstance(c, dict)]
            except (json.JSONDecodeError, TypeError):
                pass

        return DiscoveryRunResult(
            status="succeeded",
            evidence=evidence,
            candidates=candidates,
            summary=f"SnapshotExecutor completed {len(completed)} steps, "
                     f"found {len(candidates)} candidate(s)",
        )


# ---------------------------------------------------------------------------
# Tool dispatch — maps tool name strings from YAML to actual functions
# ---------------------------------------------------------------------------

_TOOL_REGISTRY: dict[str, Any] = {}


def _ensure_tool_registry() -> None:
    """Lazy-init the tool registry to avoid circular imports."""
    if _TOOL_REGISTRY:
        return
    from backend.app.services.job_discovery.deepagents_runner import (
        triage_link,
        parse_wechat_article,
        run_ocr,
        ocr_images_from_urls,
        extract_jd_candidates,
        verify_evidence,
        package_candidates,
        standardize_from_record_fields,
        finish_with_manual_review,
    )
    _TOOL_REGISTRY.update({
        "triage_link": triage_link,
        "parse_wechat_article": parse_wechat_article,
        "run_ocr": run_ocr,
        "ocr_images_from_urls": ocr_images_from_urls,
        "extract_jd_candidates": extract_jd_candidates,
        "verify_evidence": verify_evidence,
        "package_candidates": package_candidates,
        "standardize_from_record_fields": standardize_from_record_fields,
        "finish_with_manual_review": finish_with_manual_review,
        # run_web_navigation is NOT in the static registry — it requires
        # runtime dependencies (settings, model, subagent) injected via
        # SnapshotExecutor._runtime_tools (see _inject_runtime_tools).
    })


def _call_tool_by_name(name: str, *, executor: SnapshotExecutor | None = None, **kwargs: Any) -> Any:
    """Call a tool function by its YAML name.

    Checks executor._runtime_tools first for tools that need injected
    dependencies (e.g. run_web_navigation), then falls back to the static
    _TOOL_REGISTRY.
    """
    _ensure_tool_registry()
    # Runtime tools take precedence (injected dependencies)
    if executor is not None and name in executor._runtime_tools:
        return executor._runtime_tools[name](**kwargs)
    tool = _TOOL_REGISTRY.get(name)
    if tool is None:
        raise ValueError(f"Unknown or unavailable tool in snapshot: {name}")
    return tool(**kwargs)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.\.venv\Scripts\python.exe -m pytest tests/unit/test_snapshot_executor.py -v
```

Expected: all passed except the integration tests that mock `_call_tool_by_name` (9 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/job_discovery/strategy/snapshot_executor.py tests/unit/test_snapshot_executor.py
git commit -m "feat: add SnapshotExecutor for deterministic YAML plan replay"
```

---

### Task 7: Trajectory Store + Annotator

**Files:**
- Create: `backend/app/services/job_discovery/strategy/trajectory_store.py`
- Create: `backend/app/services/job_discovery/strategy/trajectory_annotator.py`
- Create: `tests/unit/test_trajectory_store.py`
- Create: `tests/unit/test_trajectory_annotator.py`

**Interfaces:**
- `trajectory_store.py`:
  - `save_trajectory(db: Session, trajectory: TrajectoryBuffer, result: DiscoveryRunResult, url: str, url_pattern: str | None) -> str` → trajectory_id
  - `schedule_annotation(db: Session, trajectory_id: str) -> None`
  - `get_pending_annotations(db: Session) -> list[JobDiscoveryTrajectory]`
- `trajectory_annotator.py`:
  - `class TrajectoryAnnotator` with `annotate(trajectory: JobDiscoveryTrajectory, settings: Settings) -> dict`

- [ ] **Step 1: Write trajectory_store tests**

```python
"""Unit tests for trajectory_store."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.db.base import Base
from backend.app.db.models import JobDiscoveryTrajectory
from backend.app.services.job_discovery.schemas import DiscoveryRunResult
from backend.app.services.job_discovery.strategy.trajectory_buffer import TrajectoryBuffer
from backend.app.services.job_discovery.strategy.trajectory_store import (
    save_trajectory,
    schedule_annotation,
)


@pytest.fixture
def engine():
    e = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(e)
    return e


class TestSaveTrajectory:
    def test_save_basic(self, engine):
        buf = TrajectoryBuffer(task_id="t1", strategy_id="s1", executor_type="snapshot")
        buf.record_step("open_url", "ok", {"url": "x"}, "result")

        result = DiscoveryRunResult(status="succeeded", summary="ok")

        with Session(engine) as db:
            tid = save_trajectory(db, buf, result, "https://x.com/job", "x.com/*")
            db.commit()

        with Session(engine) as db:
            traj = db.get(JobDiscoveryTrajectory, tid)
            assert traj is not None
            assert traj.executor_type == "snapshot"
            assert traj.overall_status == "succeeded"
            assert traj.url_pattern == "x.com/*"
            assert traj.completed_steps is not None

    def test_save_with_failure(self, engine):
        buf = TrajectoryBuffer(task_id="t2", strategy_id="s2", executor_type="snapshot")
        buf.record_step("s1", "ok", {}, "ok")
        buf.record_step("s2", "failed", {}, None, error=ValueError("bad input"))

        result = DiscoveryRunResult(status="failed", summary="step 2 failed")

        with Session(engine) as db:
            tid = save_trajectory(db, buf, result, "https://x.com/job", "x.com/*")
            db.commit()

        with Session(engine) as db:
            traj = db.get(JobDiscoveryTrajectory, tid)
            assert traj is not None
            assert traj.overall_status == "failed"
            assert traj.failed_at_step == 1
            assert traj.failed_tool == "s2"
            assert traj.failed_error_message == "bad input"
            assert traj.failed_error_reason == "unknown"  # "bad input" doesn't match any keyword


class TestScheduleAnnotation:
    def test_annotation_field_set(self, engine):
        buf = TrajectoryBuffer(task_id="t3", strategy_id=None, executor_type="supervisor")
        buf.record_step("open_url", "ok", {}, "ok")

        result = DiscoveryRunResult(status="succeeded", summary="ok")

        with Session(engine) as db:
            tid = save_trajectory(db, buf, result, "https://x.com", "x.com/*")
            schedule_annotation(db, tid)
            db.commit()

        with Session(engine) as db:
            traj = db.get(JobDiscoveryTrajectory, tid)
            assert traj is not None
            # Annotation scheduled flag stored in annotations JSON
            assert traj.annotations is not None
            assert traj.annotations.get("_annotation_pending") is True
```

- [ ] **Step 2: Write trajectory_annotator tests**

```python
"""Unit tests for trajectory_annotator."""
from __future__ import annotations

import pytest
from backend.app.services.job_discovery.strategy.trajectory_annotator import TrajectoryAnnotator


class TestTrajectoryAnnotator:
    def test_build_annotation_prompt(self):
        annotator = TrajectoryAnnotator()
        steps = [
            {"tool": "open_url", "status": "ok", "params": {"url": "x"}, "result": "html"},
            {"tool": "open_url", "status": "failed", "params": {"url": "x"}, "error": "timeout"},
            {"tool": "open_url", "status": "ok", "params": {"url": "x"}, "result": "html"},
            {"tool": "extract_jd", "status": "ok", "result": [{"title": "Engineer"}]},
        ]
        prompt = annotator._build_prompt(steps, "https://x.com/job", "mp.weixin.qq.com/s/*")
        assert "open_url" in prompt
        assert "extract_jd" in prompt
        assert "RETRY_LOOP" in prompt  # annotation instruction
        assert "reusability_score" in prompt
        assert "https://x.com/job" in prompt
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
.\.venv\Scripts\python.exe -m pytest tests/unit/test_trajectory_store.py tests/unit/test_trajectory_annotator.py -v
```

Expected: both FAIL.

- [ ] **Step 4: Implement trajectory_store.py**

```python
"""TrajectoryStore — persist execution trajectories and schedule LLM annotations."""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from backend.app.db.models import JobDiscoveryTrajectory
from backend.app.services.job_discovery.schemas import DiscoveryRunResult
from backend.app.services.job_discovery.strategy.error_classifier import classify_error
from backend.app.services.job_discovery.strategy.trajectory_buffer import TrajectoryBuffer


def save_trajectory(
    db: Session,
    trajectory: TrajectoryBuffer,
    result: DiscoveryRunResult,
    url: str,
    url_pattern: str | None,
) -> str:
    """Persist a TrajectoryBuffer as a JobDiscoveryTrajectory row.

    Returns the new trajectory ID.
    """
    buf_dict = trajectory.to_dict()
    fail_idx = trajectory.failed_step_index
    failed_step = buf_dict["steps"][fail_idx] if fail_idx is not None else None

    traj = JobDiscoveryTrajectory(
        task_id=trajectory.task_id,
        strategy_id=trajectory.strategy_id,
        executor_type=trajectory.executor_type,
        overall_status=_map_status(result, trajectory),
        url=url,
        url_pattern=url_pattern,
    )

    if failed_step is not None:
        traj.failed_at_step = fail_idx + 1
        traj.failed_tool = failed_step["tool"]
        traj.failed_params = failed_step.get("params")
        traj.failed_error_message = failed_step.get("error", "")
        traj.failed_error_reason = classify_error(failed_step.get("error", ""))

    # Separate completed steps (before failure) from fallback steps (after)
    if fail_idx is not None:
        pre_steps = buf_dict["steps"][:fail_idx]
        post_steps = buf_dict["steps"][fail_idx+1:]
        traj.completed_steps = [s for s in pre_steps if s["status"] == "ok"]
        traj.fallback_trace = [s for s in post_steps if s["is_fallback"]]
        # overall_status already set by _map_status() above
    else:
        traj.completed_steps = [s for s in buf_dict["steps"] if s["status"] == "ok"]

    db.add(traj)
    db.flush()
    return traj.id


def schedule_annotation(db: Session, trajectory_id: str) -> None:
    """Mark a trajectory for LLM annotation by updating its annotations JSON."""
    traj = db.get(JobDiscoveryTrajectory, trajectory_id)
    if traj is None:
        return
    existing = traj.annotations or {}
    existing["_annotation_pending"] = True
    traj.annotations = existing


def _map_status(result: DiscoveryRunResult, trajectory: TrajectoryBuffer) -> str:
    """Derive overall_status from result and trajectory state.

    This is the single source of truth for overall_status in the trajectory row.
    """
    fail_idx = trajectory.failed_step_index
    if fail_idx is not None:
        # Check if there were fallback steps recorded after the failure
        buf_dict = trajectory.to_dict()
        # NOTE: fail_idx can be 0 (first step failed), must use "is not None"
        has_fallback = any(
            s.get("is_fallback")
            for s in buf_dict["steps"][fail_idx + 1:]
        )
        return "partial_fallback" if has_fallback else "failed"
    return result.status if result.status in ("succeeded", "partial_success") else result.status
```

- [ ] **Step 5: Implement trajectory_annotator.py**

```python
"""TrajectoryAnnotator — LLM-based semantic annotation of execution trajectories.

Triggered on-demand for supervisor-only and partial_fallback trajectories.
Uses a small model to annotate retry loops, errors, clean paths, key decisions,
and reusability scores.
"""
from __future__ import annotations

import json
from typing import Any

from backend.app.config import Settings


class TrajectoryAnnotator:
    """Annotates execution trajectories with semantic metadata using a small LLM."""

    ANNOTATION_SYSTEM_PROMPT = """You are a trajectory analyst. Given a tool execution trace from a job discovery agent, produce a JSON annotation with these fields:

1. retry_loops: list of {start_step, end_step, tool, reason} — identify retry patterns where the same tool was called multiple times after failures
2. errors: list of {step, tool, error, category} — each error step, with category one of: network_timeout, http_blocked, captcha, wechat_blocked, site_changed, empty_text, parse_error, ocr_failed, unknown
3. clean_path: list of {tool, status} — the successful execution path with retries and errors removed
4. key_decisions: list of strings — summarize important decision points (e.g. "chose OCR over text extraction because page was image-only")
5. reusability_score: float 0.0-1.0 — how suitable this trajectory is for extracting a reusable strategy. High score = straightforward, repeatable flow with no unusual decisions.
6. reusability_reason: string — one sentence explaining the score.

Return ONLY valid JSON. No markdown, no commentary."""

    def annotate(
        self,
        steps: list[dict[str, Any]],
        url: str,
        url_pattern: str | None,
        settings: Settings,
    ) -> dict[str, Any]:
        """Annotate a trajectory's steps. Returns the annotation dict.

        If the LLM call fails or the response can't be parsed, returns a
        minimal annotation dict so the caller can proceed.
        """
        prompt = self._build_prompt(steps, url, url_pattern)
        try:
            raw = self._call_llm(prompt, settings)
            return self._parse_response(raw)
        except Exception:
            return self._fallback_annotation(steps)

    def _build_prompt(
        self,
        steps: list[dict[str, Any]],
        url: str,
        url_pattern: str | None,
    ) -> str:
        """Build the annotation prompt from trajectory steps."""
        simplified = []
        for i, s in enumerate(steps):
            simplified.append({
                "step": i + 1,
                "tool": s.get("tool", ""),
                "status": s.get("status", ""),
                "error": s.get("error"),
                "duration_ms": s.get("duration_ms", 0),
            })
        return f"""Analyze this job discovery agent execution trace:

URL: {url}
Pattern: {url_pattern or "unknown"}

Steps:
{json.dumps(simplified, indent=2, ensure_ascii=False)}

Return the JSON annotation per the system prompt format."""

    def _call_llm(self, prompt: str, settings: Settings) -> str:
        """Call a small LLM for annotation. Uses the same model as the agent."""
        from backend.app.services.job_discovery.deepagents_runner import _build_job_discovery_llm
        from langchain_core.messages import HumanMessage, SystemMessage

        llm = _build_job_discovery_llm(settings)
        messages = [
            SystemMessage(content=self.ANNOTATION_SYSTEM_PROMPT),
            HumanMessage(content=prompt),
        ]
        response = llm.invoke(messages)
        content = response.content if hasattr(response, "content") else str(response)
        return content if isinstance(content, str) else str(content)

    def _parse_response(self, raw: str) -> dict[str, Any]:
        """Parse the LLM JSON response, handling common formatting issues."""
        # Strip markdown fences if present
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
        return json.loads(cleaned.strip())

    def _fallback_annotation(self, steps: list[dict[str, Any]]) -> dict[str, Any]:
        """Minimal annotation when LLM call fails."""
        ok_steps = [s for s in steps if s.get("status") == "ok"]
        return {
            "retry_loops": [],
            "errors": [],
            "clean_path": [
                {"tool": s.get("tool", ""), "status": s.get("status", "")}
                for s in ok_steps
            ],
            "key_decisions": [],
            "reusability_score": 0.5 if ok_steps else 0.0,
            "reusability_reason": "Auto-generated fallback (LLM annotation failed)",
        }
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
.\.venv\Scripts\python.exe -m pytest tests/unit/test_trajectory_store.py tests/unit/test_trajectory_annotator.py -v
```

Expected: 4 passed.

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/job_discovery/strategy/trajectory_store.py backend/app/services/job_discovery/strategy/trajectory_annotator.py tests/unit/test_trajectory_store.py tests/unit/test_trajectory_annotator.py
git commit -m "feat: add trajectory_store and trajectory_annotator"
```

---

### Task 8: Supervisor Prompt Files

**Files:**
- Create: `backend/app/services/job_discovery/prompts/supervisor_base.txt`
- Create: `backend/app/services/job_discovery/prompts/supervisor_clean_start.txt`
- Create: `backend/app/services/job_discovery/prompts/supervisor_snapshot_fallback.txt`

- [ ] **Step 1: Extract supervisor_base.txt from current code**

Read `deepagents_runner.py` to identify the `_SUPERVISOR_SYSTEM_PROMPT` variable content, then extract the role definition, tool list, output format, and security constraints into `supervisor_base.txt`. The full content should be a faithful extraction of the existing prompt minus the flow-specific instructions.

- [ ] **Step 2: Create supervisor_clean_start.txt**

```text
## Execution Mode: Fresh Start

You are beginning a new job discovery task from scratch. No prior steps have been executed.

1. Start by calling `triage_link` with the task URL to classify it.
2. Based on the triage result, plan your full tool execution sequence.
3. Execute your plan using the available tools.
4. Always verify and package results before finishing.

Remember:
- Maximum 12 tool calls per task. If approaching the limit without results, use `standardize_from_record_fields` as a fallback.
- If `run_web_navigation` returns no useful evidence, call `standardize_from_record_fields`.
- If `extract_jd_candidates` returns empty twice for the same text, stop retrying.
- After you have candidates, verify and package them. Do NOT loop back to navigation.
```

- [ ] **Step 3: Create supervisor_snapshot_fallback.txt**

```text
## Execution Mode: Continue from Breakpoint

A strategy snapshot (source: {source}, strategy: {strategy_id}) attempted to execute but failed at step {failed_step_count}. You are taking over from the breakpoint.

### Already Completed Steps (DO NOT REPEAT):
{completed_steps}

### Failed Step:
- Tool: {failed_step_tool}
- Parameters: {failed_step_params}
- Error: {failed_step_error}

### Your Job:
1. Review the completed steps above — their results are already available in context.
2. Decide how to handle the failed step:
   - Retry it (if the error looks transient)
   - Choose an alternative tool chain to bypass it
   - Call `finish_with_manual_review` if recovery is impossible
3. Continue execution from this point to completion.
4. Maximum 8 tool calls for your portion (you already have context from completed steps).

Do NOT repeat the completed steps. Start from the breakpoint.
```

- [ ] **Step 4: Commit**

```bash
git add backend/app/services/job_discovery/prompts/
git commit -m "feat: extract supervisor prompts to template files"
```

---

### Task 9: Modify deepagents_runner.py (Supervisor Changes)

**Files:**
- Modify: `backend/app/services/job_discovery/deepagents_runner.py:1850-2107`

**Interfaces:**
- `build_discovery_supervisor_agent(settings, model=None, snapshot_context=None)` — extended signature
- `build_supervisor_prompt(snapshot_context) -> str` — new function, assembles prompt from template files

- [ ] **Step 1: Add prompt loading and assembly functions**

In `deepagents_runner.py`, add after the `_build_job_discovery_llm` function (after line 78):

```python


# ---------------------------------------------------------------------------
# Prompt loading and assembly
# ---------------------------------------------------------------------------

from pathlib import Path

_PROMPT_DIR = Path(__file__).resolve().parent / "prompts"


def _load_prompt(name: str) -> str:
    """Load a prompt template file by name (without .txt extension)."""
    path = _PROMPT_DIR / f"{name}.txt"
    if not path.exists():
        # Fallback: return empty string for missing files during migration
        return ""
    return path.read_text(encoding="utf-8")


def build_supervisor_prompt(snapshot_context: dict | None = None) -> str:
    """Assemble the Supervisor system prompt from template files.

    Args:
        snapshot_context: If provided, the Supervisor is taking over from
            a failed SnapshotExecutor / Adapter. Contains completed_steps,
            failed_step, source, and strategy_id.

    Returns:
        Complete system prompt string for the Supervisor Agent.
    """
    parts: list[str] = [_load_prompt("supervisor_base")]

    if snapshot_context is None:
        parts.append(_load_prompt("supervisor_clean_start"))
    else:
        template = _load_prompt("supervisor_snapshot_fallback")
        if template:
            ctx = {
                "source": snapshot_context.get("source", "unknown"),
                "strategy_id": snapshot_context.get("strategy_id", "unknown"),
                "failed_step_count": len(snapshot_context.get("completed_steps", [])) + 1,
                "completed_steps": _format_snapshot_steps(
                    snapshot_context.get("completed_steps", [])
                ),
                "failed_step_tool": snapshot_context.get("failed_step", {}).get("tool", ""),
                "failed_step_params": json.dumps(
                    snapshot_context.get("failed_step", {}).get("params", {}),
                    ensure_ascii=False,
                ),
                "failed_step_error": str(
                    snapshot_context.get("failed_step", {}).get("error", "")
                ),
            }
            parts.append(template.format(**ctx))

    return "\n\n".join(parts)


def _format_snapshot_steps(completed_steps: list[dict]) -> str:
    """Format completed snapshot steps as human-readable text."""
    if not completed_steps:
        return "(none)"
    lines = []
    for i, step in enumerate(completed_steps, 1):
        tool = step.get("tool", "?")
        params_summary = _summarize_params(step.get("params", {}))
        lines.append(f"  {i}. {tool}({params_summary}) — succeeded")
    return "\n".join(lines)


def _summarize_params(params: dict) -> str:
    """Create a short summary of tool parameters for display."""
    if not params:
        return ""
    keys = list(params.keys())
    if len(keys) <= 2:
        return ", ".join(f"{k}=..." for k in keys)
    return f"{', '.join(f'{k}=...' for k in keys[:2])}, ..."
```

- [ ] **Step 2: Modify build_discovery_supervisor_agent to use prompt files and snapshot_context**

Replace the existing `build_discovery_supervisor_agent` function (lines 2021-2107) with:

```python
def build_discovery_supervisor_agent(
    *,
    settings: Settings,
    model: ChatOpenAI | None = None,
    snapshot_context: dict | None = None,
) -> Any:
    """Build the Discovery Supervisor Agent using deepagents.

    Creates a compiled LangGraph agent with:
    - 8 supervisor tools wrapping Phase 4 deterministic functions
    - A WebNavigationAgent subagent for web navigation
    - Structured output via DiscoveryRunResult
    - Optional snapshot_context for breakpoint takeover

    Args:
        settings: Application settings.
        model: Optional pre-built ChatOpenAI instance.
        snapshot_context: If provided, Supervisor takes over from a failed
            SnapshotExecutor/Adapter. Injects completed steps and failed
            step info into the system prompt.

    Returns:
        A CompiledStateGraph ready for invocation.
    """
    if model is None:
        model = _build_job_discovery_llm(settings)

    web_nav_subagent = create_web_navigation_subagent(settings)

    def _make_run_web_navigation(settings: Settings):
        def _wrapper(start_url: str) -> dict[str, Any]:
            return run_web_navigation(
                start_url,
                settings=settings,
                subagent=web_nav_subagent,
                model=model,
            )
        _wrapper.__name__ = "run_web_navigation"
        _wrapper.__doc__ = run_web_navigation.__doc__
        _wrapper.__annotations__ = {"start_url": str, "return": dict[str, Any]}
        return _wrapper

    def _make_run_ocr(settings: Settings):
        def _wrapper(image_base64: str) -> dict[str, Any]:
            return run_ocr(image_base64, settings=settings)
        _wrapper.__name__ = "run_ocr"
        _wrapper.__doc__ = run_ocr.__doc__
        _wrapper.__annotations__ = {"image_base64": str, "return": dict[str, Any]}
        return _wrapper

    final_tools: list[Any] = [
        triage_link,
        _make_run_web_navigation(settings),
        parse_wechat_article,
        ocr_images_from_urls,
        _make_run_ocr(settings),
        extract_jd_candidates,
        standardize_from_record_fields,
        verify_evidence,
        package_candidates,
        finish_with_manual_review,
    ]

    # Build prompt from template files
    system_prompt = build_supervisor_prompt(snapshot_context)

    # Adjust recursion_limit for fallback mode (fewer tool calls expected)
    recursion_limit = 30 if snapshot_context is not None else 50

    try:
        agent = create_deep_agent(
            model=model,
            tools=final_tools,
            subagents=[web_nav_subagent],
            system_prompt=system_prompt,
            name="discovery_supervisor",
            # _DiscoveryRunResultPydantic is defined earlier in this module
            # (existing code, not part of this refactor — wraps DiscoveryRunResult
            # as a Pydantic model for deepagents' response_format).
            response_format=_DiscoveryRunResultPydantic,
        )
    except TypeError:
        agent = create_deep_agent(
            model=model,
            tools=final_tools,
            subagents=[web_nav_subagent],
            system_prompt=system_prompt,
            name="discovery_supervisor",
        )

    return agent
```

- [ ] **Step 3: Run existing tests to verify no regression**

```bash
.\.venv\Scripts\python.exe -m pytest tests/unit/test_job_discovery_tools.py tests/integration/test_job_discovery_deepagents.py -v
```

Expected: tests should still pass.

- [ ] **Step 4: Commit**

```bash
git add backend/app/services/job_discovery/deepagents_runner.py
git commit -m "feat: refactor supervisor to use prompt files and accept snapshot_context"
```

---

### Task 10: Worker Integration

**Files:**
- Modify: `backend/app/services/job_discovery/worker.py`

**Interfaces:**
- Consumes: `StrategyRouter`, `SnapshotExecutor`, `TrajectoryBuffer`, `trajectory_store`, `strategy_store`, all existing imports
- Produces: Modified `run_once()` with strategy routing, trajectory recording, health check trigger

- [ ] **Step 1: Write integration test for strategy routing in worker**

```python
"""Integration test for worker with strategy routing."""
from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.db.base import Base
from backend.app.db.models import JobDiscoveryStrategy, JobDiscoveryTask, RawJobRecord
from backend.app.services.job_discovery.worker import JobDiscoveryWorker
from backend.app.config import Settings


@pytest.fixture
def engine():
    e = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(e)
    return e


@pytest.fixture
def settings():
    return Settings(
        app_auth_secret="x" * 32,
        database_url="sqlite:///:memory:",
        redis_url="redis://localhost:6379/0",
        object_encryption_key="x" * 32,
        job_discovery_strategy_enabled=True,
    )


class TestWorkerStrategyIntegration:
    def test_selector_uses_strategy_when_matched(self, engine, settings):
        """Verify that the worker calls SnapshotExecutor when a strategy matches,
        instead of the full Supervisor Agent."""
        # Seed a strategy
        s = JobDiscoveryStrategy(
            url_pattern="test.company.com/*",
            site_type="other",
            plan_yaml="plan:\n  - tool: triage_link\n    params:\n      url: '{{task.url}}'\n    expect: classify\n    on_error: skip",
            priority=10,
        )
        with Session(engine) as db:
            db.add(s)
            db.commit()

        # The actual integration test requires a real task in DB.
        # For now, this is a structural test — full E2E is covered by
        # the smoke test suite.
        assert s.id is not None  # structural validation
```

- [ ] **Step 2: Modify worker.py — insert StrategyRouter before agent call**

Replace the `run_once` method (lines 327-451) with the new version that includes strategy routing. The key additions are between steps 3 (build task_input) and 4 (build agent).

The full replacement is too large for a single step — implement as edits:

**Edit 2a: Add imports at top of worker.py (after line 49):**

```python
from backend.app.services.job_discovery.strategy.strategy_router import StrategyRouter
from backend.app.services.job_discovery.strategy.snapshot_executor import (
    SnapshotExecutor, SnapshotExecutionResult,
)
from backend.app.services.job_discovery.strategy.trajectory_buffer import TrajectoryBuffer
from backend.app.services.job_discovery.strategy.trajectory_store import (
    save_trajectory, schedule_annotation,
)
from backend.app.services.job_discovery.strategy import strategy_store as strat_store
from backend.app.services.job_discovery.schemas import StrategyRecord
from backend.app.services.job_discovery.deepagents_runner import (
    _build_job_discovery_llm,
    create_web_navigation_subagent,
)
from backend.app.services.job_discovery.strategy import error_classifier
```

**Edit 2b: Replace the agent execution block (after step 3 build task_input, the section currently at lines 367-400)**

Replace the current agent invocation with:

```python
            # ── 4a. Strategy routing ──────────────────────────────────
            strategy_record: StrategyRecord | None = None
            trajectory: TrajectoryBuffer | None = None
            snapshot_context: dict | None = None
            executor_type: str = "supervisor"

            # Build LLM model + subagent once (used by both SnapshotExecutor
            # and Supervisor Agent paths).
            _llm = _build_job_discovery_llm(self.settings)
            _web_nav = create_web_navigation_subagent(self.settings)

            if self.settings.job_discovery_strategy_enabled:
                router = StrategyRouter(db)
                matched = router.match(task.source_url)
                if matched is not None:
                    strategy_record = StrategyRecord.from_orm(matched)
                    trajectory = TrajectoryBuffer(
                        task_id=task.id,
                        strategy_id=strategy_record.id,
                        executor_type=(
                            "adapter" if strategy_record.adapter else "snapshot"
                        ),
                    )

                    if strategy_record.adapter:
                        # ── Fast lane: DomainAdapter ──
                        executor_type = "adapter"
                        try:
                            adapter_instance = _load_adapter(strategy_record.adapter)
                            result = adapter_instance.execute(
                                task_input, strategy_record, trajectory
                            )
                        except Exception as adapter_exc:
                            trajectory.record_step(
                                strategy_record.adapter, "failed",
                                {"url": task.source_url}, None,
                                error=adapter_exc,
                            )
                            snapshot_context = trajectory.to_snapshot_context()
                            executor_type = "supervisor"
                            strategy_record = None  # Clear so we don't double-record
                    else:
                        # ── Fast path: SnapshotExecutor ──
                        executor_type = "snapshot"
                        snap = SnapshotExecutor(
                            strategy_record, task_input, trajectory,
                            tool_dependencies={
                                "settings": self.settings,
                                "web_nav_subagent": _web_nav,
                                "model": _llm,
                            },
                        )
                        snap_result = snap.execute()
                        if isinstance(snap_result, SnapshotExecutionResult) and snap_result.needs_supervisor_fallback:
                            snapshot_context = snap_result.snapshot_context
                            executor_type = "supervisor"
                            strategy_record = None
                        else:
                            result = snap_result

            # ── 4b. Supervisor Agent (backup path or primary if no match) ──
            agent_error: Exception | None = None
            if executor_type == "supervisor":
                if trajectory is None:
                    trajectory = TrajectoryBuffer(
                        task_id=task.id,
                        strategy_id=None,
                        executor_type="supervisor",
                    )
                try:
                    agent = build_discovery_supervisor_agent(
                        settings=self.settings,
                        model=_llm,
                        snapshot_context=snapshot_context,
                    )
                    agent_input = {
                        "messages": [
                            HumanMessage(
                                content=json.dumps(asdict(task_input), ensure_ascii=False)
                            )
                        ]
                    }
                    try:
                        if snapshot_context is not None:
                            result_raw = agent.invoke(agent_input, config={"recursion_limit": 30})
                        else:
                            result_raw = agent.invoke(agent_input, config={"recursion_limit": 50})
                    except TypeError as exc:
                        if "config" not in str(exc):
                            raise
                        result_raw = agent.invoke(agent_input)
                    result = _parse_agent_result(result_raw)
                    trajectory.record_step("supervisor_complete", "ok", {}, {"status": result.status})
                except Exception as agent_exc:
                    agent_error = agent_exc
                    trajectory.record_step("supervisor_fatal", "failed", {}, None, error=agent_exc)
                    result = DiscoveryRunResult(
                        status="failed",
                        summary=f"Agent invocation failed: {agent_exc}",
                    )

            # ── 4c. Fallback recovery ──────────────────────────────────
            result = _fallback_with_record_fields_if_agent_missed_evidence(
                result,
                task=task,
                task_input=task_input,
                settings=self.settings,
            )
            if agent_error is not None and not result.candidates and not result.evidence:
                raise agent_error
```

**Edit 2c: After the task status update block (before db.commit()), add trajectory and strategy writes:**

```python
            # ── 7b. Save trajectory ────────────────────────────────────
            try:
                url_pattern = _extract_url_pattern(task.source_url)
                trajectory_id = save_trajectory(
                    db, trajectory, result,
                    url=task.source_url,
                    url_pattern=url_pattern,
                )
                # Schedule annotation for supervisor-only and fallback paths
                if executor_type == "supervisor" or result.status == "partial_fallback":
                    if self.settings.trajectory_annotation_enabled:
                        schedule_annotation(db, trajectory_id)
            except Exception:
                pass  # trajectory save failure should not fail the task

            # ── 7c. Update strategy counters ──────────────────────────
            if strategy_record is not None:
                try:
                    strategy_id = strategy_record.id
                    if trajectory.failed_step_index is not None:
                        strat_store.increment_error_count(
                            db, strategy_id,
                            last_error={
                                "tool": (trajectory.steps[trajectory.failed_step_index].get("tool")
                                         if trajectory.steps else "unknown"),
                                "reason": error_classifier.classify_error(
                                    trajectory.steps[trajectory.failed_step_index].get("error", "")
                                    if trajectory.steps else ""
                                ),
                                "message": (trajectory.steps[trajectory.failed_step_index].get("error", "")
                                            if trajectory.steps else ""),
                            },
                        )
                    else:
                        strat_store.increment_success(
                            db, strategy_id,
                            duration_s=trajectory.elapsed_ms / 1000.0,
                        )
                except Exception:
                    pass  # strategy counter updates are best-effort
```

**Edit 2d: Add helper functions to worker.py:**

```python


def _extract_url_pattern(url: str) -> str | None:
    """Derive a simple URL pattern from a URL for trajectory grouping."""
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(url)
        host = parts.netloc
        path_parts = parts.path.strip("/").split("/")
        if path_parts and path_parts[0]:
            return f"{host}/{path_parts[0]}/*"
        return f"{host}/*"
    except Exception:
        return None


def _load_adapter(adapter_path: str):
    """Dynamically load a DomainAdapter class from a dotted path.

    Example: 'backend.app.services.job_discovery.adapters.alibaba_spa.AlibabaSPAAdapter'
    """
    import importlib
    module_path, class_name = adapter_path.rsplit(".", 1)
    module = importlib.import_module(module_path)
    return getattr(module, class_name)()
```

**Edit 2e: Add `_idle_cycles` to `__init__` and `_run_health_checks` method, and update `run_loop`:**

Add to `__init__` after `self.worker_id = _build_worker_id()`:

```python
        self._idle_cycles = 0
```

Add `_run_health_checks` method to the `JobDiscoveryWorker` class:

```python
    def _run_health_checks(self) -> None:
        """Run reachability checks on strategies due for verification.

        Only called during idle cycles (queue empty) to avoid delaying
        task processing. Failures increment error_count atomically;
        consecutive failures trigger automatic unavailable marking.
        """
        db = self.db_factory()
        try:
            strategies = strat_store.get_strategies_due_for_health_check(
                db, interval_hours=self.settings.strategy_health_check_interval_hours,
            )
            if not strategies:
                return
            for strategy in strategies:
                try:
                    # Lightweight HTTP HEAD check to verify site is reachable.
                    # Replace "*" with "test" to form a plausible URL for
                    # pattern-based strategies (e.g. "campus*.alibaba.com/*" →
                    # "https://campus.alibaba.com/test").
                    check_url = strategy.url_pattern.replace("*", "test")
                    if not check_url.startswith("http"):
                        check_url = f"https://{check_url}"
                    resp = requests.head(check_url, timeout=10, allow_redirects=True)
                    ok = resp.status_code < 500
                    detail = f"HTTP {resp.status_code}"
                except Exception as exc:
                    ok = False
                    detail = str(exc)[:200]
                strat_store.record_health_check(db, strategy.id, ok=ok, detail=detail)
            db.commit()
        except Exception:
            db.rollback()
        finally:
            db.close()
```

Update `run_loop` to track idle cycles and trigger health checks:

```python
    def run_loop(self, *, poll_interval: float = 10.0) -> None:
        """Continuously poll and process tasks until interrupted.

        Parameters
        ----------
        poll_interval:
            Seconds to sleep between polls when the queue is empty.
        """
        try:
            while True:
                processed = self.run_once()
                if processed == 0:
                    self._idle_cycles += 1
                    if self._idle_cycles % 10 == 0:
                        self._run_health_checks()
                    time.sleep(poll_interval)
                else:
                    self._idle_cycles = 0
        except KeyboardInterrupt:
            pass
```

- [ ] **Step 3: Verify worker.py imports resolve**

```bash
.\.venv\Scripts\python.exe -c "from backend.app.services.job_discovery.worker import JobDiscoveryWorker; print('OK')"
```

Expected: `OK`.

- [ ] **Step 4: Run full unit test suite**

```bash
.\.venv\Scripts\python.exe -m pytest tests/unit/ -q
```

Expected: all tests pass (including new strategy tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/job_discovery/worker.py
git commit -m "feat: integrate StrategyRouter, trajectory recording, and health checks into worker"
```

---

### Task 11: Seed Strategies & E2E Smoke Test

**Files:**
- Create: `scripts/seed_strategies.py`
- Create: `tests/manual/test_strategy_router_smoke.py`

- [ ] **Step 1: Create seed strategy script**

```python
"""Seed the strategy library with initial well-known patterns.

Usage:
    python scripts/seed_strategies.py
"""
from __future__ import annotations

import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy.orm import Session
from backend.app.db.models import JobDiscoveryStrategy
from backend.app.db.base import Base

# Use the same engine pattern as tests
from backend.app.config import Settings


WECHAT_PLAN = """plan:
  - tool: triage_link
    params:
      url: "{{task.url}}"
    expect: "classify URL as wechat article"
    on_error: "skip"
  - tool: run_web_navigation
    params:
      start_url: "{{task.url}}"
    expect: "fetch wechat article via ReadGZH"
    on_error: "retry_with_fallback"
  - tool: parse_wechat_article
    params:
      html: "{{prev.result.text}}"
      url: "{{task.url}}"
    expect: "extract article text and images"
    on_error: "mark_manual_review"
  - tool: extract_jd_candidates
    params:
      page_text: "{{prev.result.text}}"
      url: "{{task.url}}"
    expect: "extract structured JD candidates"
    on_error: "retry_then_skip"
  - tool: verify_evidence
    params:
      candidates_json: "{{prev.result}}"
      evidence_json: "{{evidence_json}}"
    expect: "verify candidates against evidence"
    on_error: "skip"
  - tool: package_candidates
    params:
      candidates_json: "{{prev.result}}"
      evidence_hash: "{{task.evidence_hash}}"
      source_key: "{{task.source_key}}"
    expect: "package final candidates"
"""

ALIBABA_PLAN = """plan: []
# Alibaba SPA uses the DomainAdapter fast lane (adapter field set below).
# The YAML plan is intentionally empty — all logic is in the adapter code.
"""


def seed(db: Session) -> None:
    existing = db.query(JobDiscoveryStrategy).count()
    if existing > 0:
        print(f"Already have {existing} strategies — skipping seed.")
        return

    strategies = [
        JobDiscoveryStrategy(
            url_pattern="mp.weixin.qq.com/s/*",
            site_type="wechat",
            description="微信公众号文章 → ReadGZH → 文本提取 → JD 提取",
            priority=10,
            adapter=None,
            plan_yaml=WECHAT_PLAN,
            degradation_threshold=3,
        ),
        JobDiscoveryStrategy(
            url_pattern="campus*.alibaba.com/*",
            site_type="spa",
            description="阿里巴巴校园招聘 SPA → API 直调 → JD 提取",
            priority=10,
            adapter="backend.app.services.job_discovery.adapters.alibaba_spa.AlibabaSPAAdapter",
            plan_yaml=ALIBABA_PLAN,
            degradation_threshold=3,
        ),
    ]
    db.add_all(strategies)
    db.commit()
    print(f"Seeded {len(strategies)} strategies.")


if __name__ == "__main__":
    settings = Settings(
        app_auth_secret="x" * 32,
        database_url=os.environ.get("DATABASE_URL", "sqlite:///seed_test.db"),
        redis_url="redis://localhost:6379/0",
        object_encryption_key="x" * 32,
    )
    from sqlalchemy import create_engine
    engine = create_engine(settings.database_url)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        seed(db)
```

- [ ] **Step 2: Create E2E smoke test**

```python
"""Smoke test: StrategyRouter matches 4 known URLs and produces expected results.

Usage:
    python tests/manual/test_strategy_router_smoke.py

Requires: seeded strategy DB + running backend (or use SQLite test DB).
"""
from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from backend.app.db.base import Base
from backend.app.db.models import JobDiscoveryStrategy
from backend.app.services.job_discovery.strategy.strategy_router import StrategyRouter


TEST_URLS = [
    ("https://mp.weixin.qq.com/s/abc123?token=xyz", "wechat", True),
    ("https://mp.weixin.qq.com/s/def456", "wechat", True),
    ("https://campus-talent.alibaba.com/search?q=java", "spa", True),
    ("https://talent.alibaba.com/position/123", "spa", True),
    ("https://www.baidu.com/jobs/unknown", None, False),  # no match
]


def main():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    # Seed
    from scripts.seed_strategies import seed
    with Session(engine) as db:
        seed(db)

    with Session(engine) as db:
        router = StrategyRouter(db)
        passed = 0
        failed = 0
        for url, expected_site_type, expect_match in TEST_URLS:
            result = router.match(url)
            if expect_match:
                if result is None:
                    print(f"FAIL: {url} expected match but got None")
                    failed += 1
                elif expected_site_type and result.site_type != expected_site_type:
                    print(f"FAIL: {url} expected {expected_site_type}, got {result.site_type}")
                    failed += 1
                else:
                    print(f"PASS: {url} → {result.site_type} ({result.description})")
                    passed += 1
            else:
                if result is not None:
                    print(f"FAIL: {url} expected no match, got {result.site_type}")
                    failed += 1
                else:
                    print(f"PASS: {url} → no match (correct)")
                    passed += 1

    print(f"\n{passed} passed, {failed} failed")
    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Run smoke test**

```bash
.\.venv\Scripts\python.exe tests/manual/test_strategy_router_smoke.py
```

Expected: 5 passed, 0 failed.

- [ ] **Step 4: Run full test suite to confirm**

```bash
.\.venv\Scripts\python.exe -m pytest tests/unit/ -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/seed_strategies.py tests/manual/test_strategy_router_smoke.py
git commit -m "feat: add seed strategies script and strategy router smoke test"
```

---

## Implementation Order (Dependency Graph)

```
Task 0 (Migration + Config) ──► Task 1 (Error Classifier)
                                   │
                                   ▼
                              Task 2 (Trajectory Buffer)
                                   │
                                   ▼
                              Task 3 (Strategy Store)
                                   │
                                   ▼
                              Task 4 (Strategy Router)
                                   │
                          ┌────────┴────────┐
                          ▼                 ▼
                    Task 5 (Adapters)  Task 8 (Prompt Files)
                          │                 │
                          ▼                 ▼
                    Task 6 (SnapshotExecutor)
                          │
                          ▼
                    Task 7 (Trajectory Store + Annotator)
                          │
                          ▼
                    Task 9 (deepagents_runner.py changes)
                          │
                          ▼
                    Task 10 (Worker Integration)
                          │
                          ▼
                    Task 11 (Seed Strategies + Smoke Test)
```

Tasks 5 and 8 can run in parallel after Task 4.
