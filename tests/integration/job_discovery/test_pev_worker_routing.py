from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.app.db.base import Base
from backend.app.db.models import (
    JobDiscoveryTask,
    JobDiscoveryTaskStatus,
    JobSource,
    JobSourceProvider,
    RawJobRecord,
)
from backend.app.services.job_discovery.planning.crawl_plan_agent import (
    PlanningContractError,
)
from backend.app.services.job_discovery.crawling.crawl_plan import CrawlPlan
from backend.app.services.job_discovery.crawling.crawl_executor import CrawlExecutionResult
from backend.app.services.job_discovery.post_crawl_pipeline import run_post_crawl_pipeline
from backend.app.services.job_discovery.schemas import (
    CrawlCoverage,
    DiscoveryRunResult,
    DiscoveryTaskInput,
    PaginationType,
)
from backend.app.services.job_discovery.strategy.snapshot_executor import (
    SnapshotExecutionResult,
)
from backend.app.services.job_discovery.worker import JobDiscoveryWorker


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
            id="pev-source",
            source_key="pev-source",
            provider=JobSourceProvider.USER_SUBMISSION,
            name="PEV source",
            file_id="file",
            sheet_id="sheet",
            mapper_version="v1",
        )
        raw = RawJobRecord(
            id="pev-raw",
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
            source_url="https://unknown.example.test/jobs",
            url_hash="hash",
            payload_hash="a" * 64,
            idempotency_key="pev-task",
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


@patch("backend.app.services.job_discovery.worker.claim_next_task")
@patch("backend.app.services.job_discovery.worker.build_discovery_supervisor_agent")
@patch("backend.app.services.job_discovery.worker.SnapshotExecutor")
@patch("backend.app.services.job_discovery.worker.generate_crawl_plan")
@patch("backend.app.services.job_discovery.worker.build_crawl_plan_agent")
def test_unknown_site_generates_plan_then_executes_path_b_with_coverage(
    mock_build_planner: MagicMock,
    mock_generate_plan: MagicMock,
    mock_snapshot_executor: MagicMock,
    mock_supervisor: MagicMock,
    mock_claim: MagicMock,
    engine: Engine,
    db_session_factory: sessionmaker[Session],
    settings: Any,
    queued_task_id: str,
) -> None:
    mock_claim.side_effect = lambda db, **_: db.get(JobDiscoveryTask, queued_task_id)
    coverage = CrawlCoverage(
        pagination_type=PaginationType.SINGLE_PAGE,
        visited_page_count=1,
        completion_evidence=["single_page_terminal"],
    )
    mock_snapshot_executor.return_value.execute.return_value = DiscoveryRunResult(
        status="failed", coverage=coverage, summary="verified crawl"
    )
    mock_generate_plan.return_value = CrawlPlan.from_yaml(
        """
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
    )

    assert JobDiscoveryWorker(db_session_factory, settings).run_once() == 1

    mock_build_planner.assert_called_once()
    mock_generate_plan.assert_called_once()
    mock_snapshot_executor.assert_called_once()
    mock_supervisor.assert_not_called()
    with Session(engine) as db:
        task = db.get(JobDiscoveryTask, queued_task_id)
        assert task is not None
        assert task.status is JobDiscoveryTaskStatus.succeeded
        assert task.result_summary_json["coverage_verified"] is True


@patch("backend.app.services.job_discovery.worker.claim_next_task")
@patch("backend.app.services.job_discovery.worker.build_discovery_supervisor_agent")
@patch("backend.app.services.job_discovery.worker.SnapshotExecutor")
@patch("backend.app.services.job_discovery.worker.generate_crawl_plan")
@patch("backend.app.services.job_discovery.worker.build_crawl_plan_agent")
def test_blocked_planner_outcome_never_falls_back_to_legacy_supervisor(
    mock_build_planner: MagicMock,
    mock_generate_plan: MagicMock,
    mock_snapshot_executor: MagicMock,
    mock_supervisor: MagicMock,
    mock_claim: MagicMock,
    engine: Engine,
    db_session_factory: sessionmaker[Session],
    settings: Any,
    queued_task_id: str,
) -> None:
    mock_claim.side_effect = lambda db, **_: db.get(JobDiscoveryTask, queued_task_id)
    mock_generate_plan.side_effect = PlanningContractError(
        "Planner requested manual review: captcha"
    )

    assert JobDiscoveryWorker(db_session_factory, settings).run_once() == 1

    mock_build_planner.assert_called_once()
    mock_snapshot_executor.assert_not_called()
    mock_supervisor.assert_not_called()
    with Session(engine) as db:
        task = db.get(JobDiscoveryTask, queued_task_id)
        assert task is not None
        assert task.status is JobDiscoveryTaskStatus.needs_manual_review
        assert task.result_summary_json["coverage_verified"] is False


@pytest.mark.parametrize(
    ("block_reason", "expected_status", "expected_calls"),
    [
        ("captcha", JobDiscoveryTaskStatus.needs_manual_review, 1),
        ("malformed candidate payload", JobDiscoveryTaskStatus.partial_success, 1),
        ("network_timeout", JobDiscoveryTaskStatus.succeeded, 2),
    ],
)
@patch("backend.app.services.job_discovery.worker.claim_next_task")
@patch("backend.app.services.job_discovery.worker.build_discovery_supervisor_agent")
@patch("backend.app.services.job_discovery.worker.SnapshotExecutor")
@patch("backend.app.services.job_discovery.worker.generate_crawl_plan")
@patch("backend.app.services.job_discovery.worker.build_crawl_plan_agent")
def test_coverage_bearing_path_b_error_uses_execution_recovery_route(
    mock_build_planner: MagicMock,
    mock_generate_plan: MagicMock,
    mock_snapshot_executor: MagicMock,
    mock_supervisor: MagicMock,
    mock_claim: MagicMock,
    engine: Engine,
    db_session_factory: sessionmaker[Session],
    settings: Any,
    queued_task_id: str,
    block_reason: str,
    expected_status: JobDiscoveryTaskStatus,
    expected_calls: int,
) -> None:
    mock_claim.side_effect = lambda db, **_: db.get(JobDiscoveryTask, queued_task_id)
    mock_generate_plan.return_value = CrawlPlan.from_yaml(
        """
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
    )
    coverage = CrawlCoverage(
        pagination_type=PaginationType.SINGLE_PAGE,
        visited_page_count=1,
        completion_evidence=["single_page_terminal"],
    )
    first_result = DiscoveryRunResult(
        status="failed", block_reason=block_reason, coverage=coverage
    )
    mock_snapshot_executor.return_value.execute.side_effect = [
        first_result,
        DiscoveryRunResult(status="failed", coverage=coverage),
    ]

    assert JobDiscoveryWorker(db_session_factory, settings).run_once() == 1

    assert mock_snapshot_executor.call_count == expected_calls
    mock_supervisor.assert_not_called()
    with Session(engine) as db:
        task = db.get(JobDiscoveryTask, queued_task_id)
        assert task is not None
        assert task.status is expected_status


@patch("backend.app.services.job_discovery.worker.claim_next_task")
@patch("backend.app.services.job_discovery.worker.repair_crawl_plan")
@patch("backend.app.services.job_discovery.worker.build_discovery_supervisor_agent")
@patch("backend.app.services.job_discovery.worker.SnapshotExecutor")
@patch("backend.app.services.job_discovery.worker.generate_crawl_plan")
@patch("backend.app.services.job_discovery.worker.build_crawl_plan_agent")
def test_raw_captcha_overrides_declared_structure_error_and_never_falls_back(
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
    mock_claim.side_effect = lambda db, **_: db.get(JobDiscoveryTask, queued_task_id)
    mock_generate_plan.return_value = CrawlPlan.from_yaml(
        """
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
    )
    mock_snapshot_executor.return_value.execute.return_value = SnapshotExecutionResult(
        status="failed",
        needs_supervisor_fallback=True,
        snapshot_context={
            "failed_step": {
                "error_type": "structure_error",
                "error": "captcha encountered",
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


def _pipeline_result(error: str | None) -> DiscoveryRunResult:
    task = DiscoveryTaskInput(
        source_id="pev-source",
        raw_record_id="pev-raw",
        external_record_id="external",
        source_key="pev-source",
        source_url="https://unknown.example.test/jobs",
        url_hash="hash",
        record_fields=[],
    )
    coverage = CrawlCoverage(
        pagination_type=PaginationType.SINGLE_PAGE,
        visited_page_count=1,
        completion_evidence=["single_page_terminal"],
    )
    return run_post_crawl_pipeline(
        task,
        CrawlExecutionResult([], [], coverage, None, error=error),
    )


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_calls"),
    [
        ("captcha", JobDiscoveryTaskStatus.needs_manual_review, 1),
        ("network_timeout", JobDiscoveryTaskStatus.succeeded, 2),
    ],
)
@patch("backend.app.services.job_discovery.worker.claim_next_task")
@patch("backend.app.services.job_discovery.worker.build_discovery_supervisor_agent")
@patch("backend.app.services.job_discovery.worker.SnapshotExecutor")
@patch("backend.app.services.job_discovery.worker.generate_crawl_plan")
@patch("backend.app.services.job_discovery.worker.build_crawl_plan_agent")
def test_real_crawl_execution_error_survives_pipeline_and_routes_worker(
    mock_build_planner: MagicMock,
    mock_generate_plan: MagicMock,
    mock_snapshot_executor: MagicMock,
    mock_supervisor: MagicMock,
    mock_claim: MagicMock,
    engine: Engine,
    db_session_factory: sessionmaker[Session],
    settings: Any,
    queued_task_id: str,
    error: str,
    expected_status: JobDiscoveryTaskStatus,
    expected_calls: int,
) -> None:
    mock_claim.side_effect = lambda db, **_: db.get(JobDiscoveryTask, queued_task_id)
    mock_generate_plan.return_value = CrawlPlan.from_yaml(
        """
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
    )
    mock_snapshot_executor.return_value.execute.side_effect = [
        _pipeline_result(error),
        _pipeline_result(None),
    ]

    assert JobDiscoveryWorker(db_session_factory, settings).run_once() == 1

    assert mock_snapshot_executor.call_count == expected_calls
    mock_supervisor.assert_not_called()
    with Session(engine) as db:
        task = db.get(JobDiscoveryTask, queued_task_id)
        assert task is not None
        assert task.status is expected_status


@patch("backend.app.services.job_discovery.worker.claim_next_task")
@patch("backend.app.services.job_discovery.worker.run_web_navigation")
@patch("backend.app.services.job_discovery.worker.build_discovery_supervisor_agent")
@patch("backend.app.services.job_discovery.worker.SnapshotExecutor")
@patch("backend.app.services.job_discovery.worker.generate_crawl_plan")
@patch("backend.app.services.job_discovery.worker.build_crawl_plan_agent")
def test_tencent_captcha_result_never_invokes_legacy_record_field_navigation(
    mock_build_planner: MagicMock,
    mock_generate_plan: MagicMock,
    mock_snapshot_executor: MagicMock,
    mock_supervisor: MagicMock,
    mock_navigation: MagicMock,
    mock_claim: MagicMock,
    engine: Engine,
    db_session_factory: sessionmaker[Session],
    settings: Any,
    queued_task_id: str,
) -> None:
    with Session(engine) as db:
        task = db.get(JobDiscoveryTask, queued_task_id)
        assert task is not None
        task.source_key = "tencent-blocked-referrals"
        db.commit()

    mock_claim.side_effect = lambda db, **_: db.get(JobDiscoveryTask, queued_task_id)
    mock_generate_plan.return_value = CrawlPlan.from_yaml(
        """
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
    )
    mock_snapshot_executor.return_value.execute.return_value = _pipeline_result("captcha")
    mock_navigation.return_value = {"evidence_pages": []}

    assert JobDiscoveryWorker(db_session_factory, settings).run_once() == 1

    mock_navigation.assert_not_called()
    mock_supervisor.assert_not_called()
    with Session(engine) as db:
        task = db.get(JobDiscoveryTask, queued_task_id)
        assert task is not None
        assert task.status is JobDiscoveryTaskStatus.needs_manual_review
