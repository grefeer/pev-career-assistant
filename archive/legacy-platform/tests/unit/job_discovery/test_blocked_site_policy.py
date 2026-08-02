"""Task 7: authenticated career-site walls must not bypass to partial success.

A Beisen-style detail API that returns 401 and demands SPA session
authentication, or an iFlytek authenticated detail endpoint, must classify as
``blocked`` so the worker forwards the task to ``needs_manual_review`` instead
of entering the Planner repair loop or escalating to the legacy Supervisor.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.app.db.base import Base
from backend.app.db.models import (
    DiscoveryBlockReason,
    JobDiscoveryTask,
    JobDiscoveryTaskStatus,
    JobSource,
    JobSourceProvider,
    RawJobRecord,
)
from backend.app.services.job_discovery.crawling.crawl_plan import CrawlPlan
from backend.app.services.job_discovery.strategy.snapshot_executor import (
    SnapshotExecutionResult,
)
from backend.app.services.job_discovery.strategy.error_classifier import (
    classify_execution_error,
)
from backend.app.services.job_discovery.worker import JobDiscoveryWorker


IFLYTEK_URL = "https://campus.iflytek.com/jobs"
# Mirrors a Beisen/iFlytek authenticated detail API: a 401 that demands SPA
# session authentication. Used as the raw executor failure text.
AUTH_WALL_ERROR = "detail API returned 401 and requires SPA session authentication"


def test_beisen_authenticated_api_is_blocked_not_structure_error() -> None:
    error = classify_execution_error(AUTH_WALL_ERROR)
    assert error.error_type == "blocked"
    assert error.reason == "authentication_required"


def test_iflytek_session_auth_wall_is_blocked() -> None:
    error = classify_execution_error(
        "detail endpoint requires authenticated session access (401)"
    )
    assert error.error_type == "blocked"
    assert error.reason == "authentication_required"


def test_explicit_auth_wall_phrase_is_blocked() -> None:
    error = classify_execution_error("hit authentication wall on detail page")
    assert error.error_type == "blocked"
    assert error.reason == "authentication_required"


def test_bare_403_does_not_auto_bypass_to_partial_success() -> None:
    """A public-API 403 with no transient override must never silently bypass."""
    error = classify_execution_error("detail API returned 403 forbidden")
    assert error.error_type == "blocked"
    assert error.reason == "authentication_required"


def test_transient_403_still_retries_instead_of_becoming_a_wall() -> None:
    """A transient public-API 403 (rate-limited) retries, not manual review."""
    error = classify_execution_error("detail API returned 403 rate_limited")
    assert error.error_type == "transient"


def test_structure_error_keeps_planner_repair_routing() -> None:
    """Regression: a real structure error must still route to Planner repair."""
    error = classify_execution_error("selector_not_found on listing page")
    assert error.error_type == "structure_error"


# ---------------------------------------------------------------------------
# Worker routing: an authenticated wall must reach needs_manual_review without
# entering the Planner repair loop or escalating to the legacy Supervisor.
# ---------------------------------------------------------------------------


@pytest.fixture
def engine() -> Engine:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def db_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def queued_task_id(engine: Engine) -> str:
    with Session(engine) as db:
        source = JobSource(
            id="auth-source",
            source_key="auth-source",
            provider=JobSourceProvider.USER_SUBMISSION,
            name="Auth source",
            file_id="file",
            sheet_id="sheet",
            mapper_version="v1",
        )
        raw = RawJobRecord(
            id="auth-raw",
            source_id=source.id,
            external_record_id="external",
            payload_hash="a" * 64,
            raw_fields=[],
        )
        task = JobDiscoveryTask(
            source_id=source.id,
            raw_record_id=raw.id,
            external_record_id="external",
            source_key=source.source_key,
            source_url=IFLYTEK_URL,
            url_hash="hash",
            payload_hash="a" * 64,
            idempotency_key="auth-task",
            agent_version="1.0.0",
            status=JobDiscoveryTaskStatus.queued,
        )
        db.add_all([source, raw, task])
        db.commit()
        return task.id


@pytest.fixture
def settings() -> Any:
    from tests.conftest import settings_override

    return settings_override(
        job_discovery_enabled=True,
        job_discovery_strategy_enabled=False,
        job_discovery_pev_enabled=True,
        job_discovery_planner_enabled=True,
        job_discovery_legacy_path_c_enabled=True,
        job_discovery_planner_max_inspection_pages=3,
        job_discovery_task_timeout_seconds=60,
    )


_CRAWL_PLAN_YAML = """
plan_type: crawl_plan
version: 1
listing:
  item_selector: .job
  title_selector: .title
pagination:
  type: single_page
detail:
  body_selector: .detail
completion: {}
"""


@patch("backend.app.services.job_discovery.worker.claim_next_task")
@patch("backend.app.services.job_discovery.worker.repair_crawl_plan")
@patch("backend.app.services.job_discovery.worker.build_discovery_supervisor_agent")
@patch("backend.app.services.job_discovery.worker.SnapshotExecutor")
@patch("backend.app.services.job_discovery.worker.generate_crawl_plan")
@patch("backend.app.services.job_discovery.worker.build_crawl_plan_agent")
def test_blocked_result_does_not_enter_planner_repair_loop(
    mock_build_planner: MagicMock,
    mock_generate_plan: MagicMock,
    mock_snapshot_executor: MagicMock,
    mock_supervisor: MagicMock,
    mock_repair_plan: MagicMock,
    mock_claim: MagicMock,
    engine: Engine,
    db_session_factory: sessionmaker[Session],
    settings: Any,
    queued_task_id: str,
) -> None:
    """A Beisen/iFlytek auth wall routes to manual review, never Planner repair
    or the legacy Supervisor. The raw auth-wall text overrides any declared
    structure error type (mirrors the captcha override contract)."""
    mock_claim.side_effect = lambda db, **_: db.get(JobDiscoveryTask, queued_task_id)
    mock_generate_plan.return_value = CrawlPlan.from_yaml(_CRAWL_PLAN_YAML)
    # FakeDriver.authentication_required(): the executor surfaces an auth wall
    # whose raw error text must drive classification, even if the declared
    # failed-step error_type is structure_error.
    mock_snapshot_executor.return_value.execute.return_value = SnapshotExecutionResult(
        status="failed",
        needs_supervisor_fallback=True,
        snapshot_context={
            "failed_step": {
                "error_type": "structure_error",
                "error": AUTH_WALL_ERROR,
            }
        },
    )

    assert JobDiscoveryWorker(db_session_factory, settings).run_once() == 1

    mock_repair_plan.assert_not_called()
    mock_supervisor.assert_not_called()
    with Session(engine) as db:
        task = db.get(JobDiscoveryTask, queued_task_id)
        assert task is not None
        assert task.status is JobDiscoveryTaskStatus.needs_manual_review
        # The auth-wall reason is persisted as a recognized blocked reason
        # (permission_denied), not silently "unknown".
        assert task.block_reason is DiscoveryBlockReason.permission_denied

