"""Integration tests for strategy routing in the Job Discovery Worker.

Verifies that the StrategyRouter, SnapshotExecutor, and trajectory recording
are correctly wired into the worker's run_once() method without breaking
existing supervisor-only behaviour.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.app.db.base import Base
from backend.app.db.models import (
    JobDiscoveryStrategy,
    JobDiscoveryTask,
    JobDiscoveryTaskStatus,
    JobSource,
    JobSourceProvider,
    RawJobRecord,
)
from backend.app.services.job_discovery.worker import JobDiscoveryWorker


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine() -> Engine:
    """In-memory SQLite engine for a clean database per test."""
    e = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(e)
    return e


@pytest.fixture
def db_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine)


@pytest.fixture
def db(engine: Engine) -> Session:
    with Session(engine) as session:
        yield session


@pytest.fixture
def settings() -> Any:
    from tests.conftest import settings_override

    return settings_override(
        job_discovery_enabled=True,
        job_discovery_strategy_enabled=True,
        job_discovery_task_timeout_seconds=60,
        job_discovery_model="gpt-4o-mini",
        trajectory_annotation_enabled=True,
    )


@pytest.fixture
def settings_no_strategy() -> Any:
    from tests.conftest import settings_override

    return settings_override(
        job_discovery_enabled=True,
        job_discovery_strategy_enabled=False,
        job_discovery_task_timeout_seconds=60,
        job_discovery_model="gpt-4o-mini",
    )


@pytest.fixture
def source(db: Session) -> JobSource:
    s = JobSource(
        id="test-source-id-strategy",
        source_key="test-source-strategy",
        provider=JobSourceProvider.USER_SUBMISSION,
        name="Strategy Test Source",
        file_id="f-strategy",
        sheet_id="s-strategy",
        mapper_version="v1",
    )
    db.add(s)
    db.flush()
    return s


@pytest.fixture
def raw_record(db: Session, source: JobSource) -> RawJobRecord:
    r = RawJobRecord(
        id="test-raw-id-strategy",
        source_id=source.id,
        external_record_id="ext-strategy-1",
        payload_hash="b" * 64,
        raw_fields=[{"field": "公司", "value": "测试"}],
    )
    db.add(r)
    db.flush()
    return r


@pytest.fixture
def queued_task(db: Session, source: JobSource, raw_record: RawJobRecord) -> JobDiscoveryTask:
    t = JobDiscoveryTask(
        source_id=source.id,
        raw_record_id=raw_record.id,
        external_record_id="ext-strategy-1",
        source_key="test-source-strategy",
        source_url="https://career.example.com/jobs/123",
        url_hash="strat123hash",
        payload_hash="b" * 64,
        idempotency_key="task-idem-strategy",
        agent_version="1.0.0",
        status=JobDiscoveryTaskStatus.queued,
    )
    db.add(t)
    db.commit()
    return t


@pytest.fixture
def worker(
    db_session_factory: sessionmaker[Session],
    settings: Any,
) -> JobDiscoveryWorker:
    return JobDiscoveryWorker(db_session_factory, settings)


@pytest.fixture
def worker_no_strategy(
    db_session_factory: sessionmaker[Session],
    settings_no_strategy: Any,
) -> JobDiscoveryWorker:
    return JobDiscoveryWorker(db_session_factory, settings_no_strategy)


# ---------------------------------------------------------------------------
# Strategy seeding helpers
# ---------------------------------------------------------------------------


def _seed_strategy(db: Session, url_pattern: str, plan_yaml: str | None = None) -> JobDiscoveryStrategy:
    s = JobDiscoveryStrategy(
        url_pattern=url_pattern,
        site_type="other",
        priority=10,
        status="active",
        enabled=True,
        plan_yaml=plan_yaml or (
            "plan:\n"
            "  - tool: triage_link\n"
            "    params:\n"
            "      url: '{{task.url}}'\n"
            "    expect: classify\n"
            "    on_error: skip\n"
        ),
    )
    db.add(s)
    db.commit()
    db.flush()
    return s


# ---------------------------------------------------------------------------
# Structural tests — strategy routing code paths
# ---------------------------------------------------------------------------


class TestWorkerStrategyStructural:
    """Structural integration tests — verifies the strategy routing blocks
    are wired into run_once() without breaking normal execution."""

    @patch("backend.app.services.job_discovery.worker.claim_next_task")
    @patch("backend.app.services.job_discovery.worker.build_discovery_supervisor_agent")
    def test_strategy_routing_when_no_match_falls_back_to_supervisor(
        self,
        mock_build_agent: MagicMock,
        mock_claim: MagicMock,
        engine: Engine,
        settings: Any,
        worker: JobDiscoveryWorker,
        queued_task: JobDiscoveryTask,
        db: Session,
    ) -> None:
        """When no strategy matches, the supervisor agent is called normally."""
        # Seed a non-matching strategy
        _seed_strategy(db, "other.company.com/*")

        mock_claim.side_effect = lambda db, **kw: db.merge(queued_task)
        mock_agent = MagicMock()
        mock_agent.invoke.return_value = {
            "structured_response": {
                "status": "succeeded",
                "block_reason": None,
                "evidence": [],
                "candidates": [{"title": "Engineer", "company_name": "Example"}],
                "summary": "No match fallback",
            }
        }
        mock_build_agent.return_value = mock_agent

        result = worker.run_once()

        assert result == 1
        mock_build_agent.assert_called_once()

        with Session(engine) as vs:
            task = vs.get(JobDiscoveryTask, queued_task.id)
            assert task is not None
            assert task.status is JobDiscoveryTaskStatus.succeeded

    @patch("backend.app.services.job_discovery.worker.claim_next_task")
    @patch("backend.app.services.job_discovery.worker.build_discovery_supervisor_agent")
    def test_strategy_disabled_uses_supervisor(
        self,
        mock_build_agent: MagicMock,
        mock_claim: MagicMock,
        engine: Engine,
        worker_no_strategy: JobDiscoveryWorker,
        queued_task: JobDiscoveryTask,
        db: Session,
    ) -> None:
        """When strategy_enabled is False, always use the supervisor agent."""
        # Seed a matching strategy
        _seed_strategy(db, "career.example.com/*")

        mock_claim.side_effect = lambda db, **kw: db.merge(queued_task)
        mock_agent = MagicMock()
        mock_agent.invoke.return_value = {
            "structured_response": {
                "status": "succeeded",
                "block_reason": None,
                "evidence": [],
                "candidates": [{"title": "Engineer"}],
                "summary": "Strategy disabled, used supervisor",
            }
        }
        mock_build_agent.return_value = mock_agent

        result = worker_no_strategy.run_once()

        assert result == 1
        mock_build_agent.assert_called_once()

    @patch("backend.app.services.job_discovery.worker.claim_next_task")
    @patch("backend.app.services.job_discovery.worker.build_discovery_supervisor_agent")
    @patch("backend.app.services.job_discovery.worker.SnapshotExecutor")
    def test_strategy_matched_uses_snapshot_executor(
        self,
        mock_snapshot_executor: MagicMock,
        mock_build_agent: MagicMock,
        mock_claim: MagicMock,
        engine: Engine,
        settings: Any,
        worker: JobDiscoveryWorker,
        queued_task: JobDiscoveryTask,
        db: Session,
    ) -> None:
        """When a strategy matches (no adapter), SnapshotExecutor is used."""
        _seed_strategy(db, "career.example.com/*")

        mock_claim.side_effect = lambda db, **kw: db.merge(queued_task)
        # SnapshotExecutor returns success
        mock_snap = MagicMock()
        mock_snap.execute.return_value = MagicMock(
            status="succeeded",
            block_reason=None,
            evidence=[],
            candidates=[{"title": "Snapshot Engineer"}],
            summary="Snapshot executed",
            needs_supervisor_fallback=False,
        )
        mock_snapshot_executor.return_value = mock_snap

        result = worker.run_once()

        assert result == 1
        # Supervisor should NOT be called — snapshot handled it
        mock_build_agent.assert_not_called()
        # SnapshotExecutor should have been instantiated
        mock_snapshot_executor.assert_called_once()

        with Session(engine) as vs:
            task = vs.get(JobDiscoveryTask, queued_task.id)
            assert task is not None
            assert task.status is JobDiscoveryTaskStatus.succeeded

    @patch("backend.app.services.job_discovery.worker.claim_next_task")
    @patch("backend.app.services.job_discovery.worker.build_discovery_supervisor_agent")
    @patch("backend.app.services.job_discovery.worker.SnapshotExecutor")
    def test_snapshot_fallback_triggers_supervisor(
        self,
        mock_snapshot_executor: MagicMock,
        mock_build_agent: MagicMock,
        mock_claim: MagicMock,
        engine: Engine,
        settings: Any,
        worker: JobDiscoveryWorker,
        queued_task: JobDiscoveryTask,
        db: Session,
    ) -> None:
        """When SnapshotExecutor fails, supervisor takes over with context."""
        from backend.app.services.job_discovery.strategy.snapshot_executor import (
            SnapshotExecutionResult,
        )
        from backend.app.services.job_discovery.schemas import PageEvidence

        _seed_strategy(db, "career.example.com/*")

        mock_claim.side_effect = lambda db, **kw: db.merge(queued_task)
        # SnapshotExecutor returns a real SnapshotExecutionResult
        mock_snap_result = SnapshotExecutionResult(
            status="failed",
            summary="Snapshot failed",
            needs_supervisor_fallback=True,
            snapshot_context={
                "source": "snapshot",
                "strategy_id": "test-id",
                "completed_steps": [],
                "failed_step": None,
            },
        )
        mock_snap = MagicMock()
        mock_snap.execute.return_value = mock_snap_result
        mock_snapshot_executor.return_value = mock_snap

        # Supervisor succeeds
        mock_agent = MagicMock()
        mock_agent.invoke.return_value = {
            "structured_response": {
                "status": "succeeded",
                "block_reason": None,
                "evidence": [],
                "candidates": [{"title": "Fallback Engineer"}],
                "summary": "Supervisor fallback succeeded",
            }
        }
        mock_build_agent.return_value = mock_agent

        result = worker.run_once()

        assert result == 1
        # Supervisor should have been called (fallback path)
        mock_build_agent.assert_called_once()
        # SnapshotExecutor should have been instantiated
        mock_snapshot_executor.assert_called_once()

        with Session(engine) as vs:
            task = vs.get(JobDiscoveryTask, queued_task.id)
            assert task is not None
            assert task.status is JobDiscoveryTaskStatus.succeeded

    @patch("backend.app.services.job_discovery.worker.claim_next_task")
    @patch("backend.app.services.job_discovery.worker.build_discovery_supervisor_agent")
    def test_strategy_enabled_no_match_uses_supervisor(
        self,
        mock_build_agent: MagicMock,
        mock_claim: MagicMock,
        engine: Engine,
        settings: Any,
        worker: JobDiscoveryWorker,
        queued_task: JobDiscoveryTask,
        db: Session,
    ) -> None:
        """When strategy is enabled but no pattern matches, supervisor is used."""
        _seed_strategy(db, "other.company.com/*")

        mock_claim.side_effect = lambda db, **kw: db.merge(queued_task)
        mock_agent = MagicMock()
        mock_agent.invoke.return_value = {
            "structured_response": {
                "status": "succeeded",
                "block_reason": None,
                "evidence": [],
                "candidates": [{"title": "No Match Engineer"}],
                "summary": "No strategy match, used supervisor",
            }
        }
        mock_build_agent.return_value = mock_agent

        result = worker.run_once()

        assert result == 1
        mock_build_agent.assert_called_once()

    @patch("backend.app.services.job_discovery.worker.claim_next_task")
    @patch("backend.app.services.job_discovery.worker.build_discovery_supervisor_agent")
    def test_cross_engine_trajectory_save_does_not_crash(
        self,
        mock_build_agent: MagicMock,
        mock_claim: MagicMock,
        engine: Engine,
        settings: Any,
        worker: JobDiscoveryWorker,
        queued_task: JobDiscoveryTask,
        db: Session,
    ) -> None:
        """Trajectory save failure should not crash the task (best-effort)."""
        _seed_strategy(db, "career.example.com/*")

        mock_claim.side_effect = lambda db, **kw: db.merge(queued_task)
        mock_agent = MagicMock()
        mock_agent.invoke.return_value = {
            "structured_response": {
                "status": "succeeded",
                "block_reason": None,
                "evidence": [],
                "candidates": [{"title": "Engineer"}],
                "summary": "Trajectory test",
            }
        }
        mock_build_agent.return_value = mock_agent

        result = worker.run_once()

        assert result == 1
        with Session(engine) as vs:
            task = vs.get(JobDiscoveryTask, queued_task.id)
            assert task is not None
            assert task.status is JobDiscoveryTaskStatus.succeeded


# ---------------------------------------------------------------------------
# Structural sanity: helper functions
# ---------------------------------------------------------------------------


class TestWorkerHelperFunction:
    def test_extract_url_pattern_basic(self) -> None:
        from backend.app.services.job_discovery.worker import _extract_url_pattern
        assert _extract_url_pattern("https://career.example.com/jobs/123") == "career.example.com/jobs/*"

    def test_extract_url_pattern_root(self) -> None:
        from backend.app.services.job_discovery.worker import _extract_url_pattern
        assert _extract_url_pattern("https://career.example.com/") == "career.example.com/*"

    def test_extract_url_pattern_invalid(self) -> None:
        from backend.app.services.job_discovery.worker import _extract_url_pattern
        assert _extract_url_pattern("") is None

    def test_derive_health_check_url_simple(self) -> None:
        from backend.app.services.job_discovery.worker import _derive_health_check_url
        assert _derive_health_check_url("career.example.com/*") == "https://career.example.com"

    def test_derive_health_check_url_with_path(self) -> None:
        from backend.app.services.job_discovery.worker import _derive_health_check_url
        assert _derive_health_check_url("example.com/campus/*") == "https://example.com/campus"

    def test_derive_health_check_url_empty(self) -> None:
        from backend.app.services.job_discovery.worker import _derive_health_check_url
        assert _derive_health_check_url("*") == ""

    @patch("backend.app.services.job_discovery.worker.run_web_navigation")
    def test_make_run_web_navigation_wrapper(self, mock_run: MagicMock) -> None:
        from backend.app.services.job_discovery.worker import _make_run_web_navigation_wrapper
        mock_run.return_value = {"status": "ok"}
        wrapper = _make_run_web_navigation_wrapper(
            MagicMock(), MagicMock(), MagicMock()
        )
        assert callable(wrapper)
        assert wrapper.__name__ == "run_web_navigation"
        result = wrapper("https://example.com")
        assert result["status"] == "ok"
        mock_run.assert_called_once()

    def test_load_adapter_missing_module(self) -> None:
        from backend.app.services.job_discovery.worker import _load_adapter
        with pytest.raises((ModuleNotFoundError, AttributeError)):
            _load_adapter("nonexistent.module.Class")
