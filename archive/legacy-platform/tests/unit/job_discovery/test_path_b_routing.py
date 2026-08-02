from __future__ import annotations

import pytest

from backend.app.services.job_discovery.adapters.complete_crawl_base import (
    CompleteCrawlAdapter,
)
from backend.app.services.job_discovery.crawling.crawl_executor import CrawlExecutor
from backend.app.services.job_discovery.crawling.driver import ListingPage
from backend.app.services.job_discovery.schemas import (
    DiscoveryTaskInput,
    PaginationType,
    RawJobDetail,
    RawJobListing,
    StrategyRecord,
)
from backend.app.services.job_discovery.strategy.snapshot_executor import SnapshotExecutor
from backend.app.services.job_discovery.strategy.strategy_store import validate_plan_yaml
from backend.app.services.job_discovery.strategy.trajectory_buffer import TrajectoryBuffer
from backend.app.services.job_discovery.strategy.error_classifier import (
    classify_execution_error,
    classify_next_action,
)


CRAWL_PLAN_YAML = """
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


class _Driver:
    def __init__(self) -> None:
        self.closed = False

    def fetch_listing_page(self, *, plan, task, cursor):
        return ListingPage(
            page_key="only",
            listings=[RawJobListing(
                source_url=task.source_url,
                detail_url="https://jobs.example.test/jobs/1",
                company="Example",
                title="Engineer",
            )],
            next_cursor=None,
            terminal_evidence="single_page_terminal",
        )

    def fetch_detail(self, *, plan, listing, resource_key):
        return RawJobDetail(
            detail_url=listing.detail_url or "",
            full_text="岗位职责：实现服务。任职要求：熟悉 Python 和测试。",
        )

    def close(self) -> None:
        self.closed = True


class _CompleteAdapter(CompleteCrawlAdapter):
    def __init__(self) -> None:
        self.driver = _Driver()

    def build_driver(self, plan, task, trajectory):
        return self.driver

    def validate(self, url: str) -> bool:
        return True


def _task() -> DiscoveryTaskInput:
    return DiscoveryTaskInput(
        source_id="source", raw_record_id="raw", external_record_id="external",
        source_key="source", source_url="https://jobs.example.test/list",
        url_hash="hash", record_fields=[],
    )


def _strategy(plan_yaml: str = CRAWL_PLAN_YAML) -> StrategyRecord:
    return StrategyRecord(
        id="strategy",
        url_pattern="jobs.example.test/*",
        site_type="career_site",
        plan_yaml=plan_yaml,
    )


def test_validate_plan_yaml_accepts_explicit_and_legacy_snapshot_variants() -> None:
    assert validate_plan_yaml(CRAWL_PLAN_YAML) == []
    assert validate_plan_yaml("plan_type: snapshot_plan\nplan: []") == []
    assert validate_plan_yaml("plan: []") == []
    assert validate_plan_yaml("plan_type: unsupported\nplan: []") == ["unsupported plan_type"]


def test_crawl_plan_dispatches_to_common_crawl_executor() -> None:
    trajectory = TrajectoryBuffer("task", "strategy", "snapshot")
    executor = SnapshotExecutor(
        _strategy(), _task(), trajectory, crawl_driver_factory=lambda plan, task: _Driver()
    )

    result = executor.execute()

    assert result.status == "succeeded"
    assert result.coverage is not None
    assert result.coverage.pagination_type is PaginationType.SINGLE_PAGE


def test_legacy_snapshot_plan_keeps_tool_replay_path() -> None:
    executor = SnapshotExecutor(
        _strategy("plan: []"), _task(), TrajectoryBuffer("task", "strategy", "snapshot")
    )

    result = executor.execute()

    assert result.status == "succeeded"
    assert result.coverage is None


def test_crawl_plan_without_driver_returns_structural_repair_context() -> None:
    executor = SnapshotExecutor(
        _strategy(), _task(), TrajectoryBuffer("task", "strategy", "snapshot")
    )

    result = executor.execute()

    assert result.needs_supervisor_fallback is True
    assert result.snapshot_context is not None
    assert result.snapshot_context["checkpoint"] is not None
    assert result.snapshot_context["failed_step"]["error_type"] == "structure_error"


def test_malformed_declared_crawl_plan_returns_structural_repair_context() -> None:
    executor = SnapshotExecutor(
        _strategy("""
plan_type: crawl_plan
version: 1
listing:
  item_selector: .job
  title_selector: .title
pagination:
  type: unsupported
detail:
  body_selector: .detail
completion: {}
"""),
        _task(),
        TrajectoryBuffer("task", "strategy", "snapshot"),
    )

    result = executor.execute()

    assert result.needs_supervisor_fallback is True
    assert result.snapshot_context is not None
    assert result.snapshot_context["checkpoint"]["plan_version"] == 1
    assert result.snapshot_context["failed_step"]["error_type"] == "structure_error"


def test_complete_crawl_adapter_delegates_to_shared_crawl_executor(
    monkeypatch,
) -> None:
    adapter = _CompleteAdapter()
    trajectory = TrajectoryBuffer("task", "strategy", "adapter")
    called = False

    class _RecordingCrawlExecutor:
        def __init__(self, driver, trajectory):
            self._delegate = CrawlExecutor(driver, trajectory)

        def execute(self, **kwargs):
            nonlocal called
            called = True
            return self._delegate.execute(**kwargs)

    monkeypatch.setattr(
        "backend.app.services.job_discovery.adapters.complete_crawl_base.CrawlExecutor",
        _RecordingCrawlExecutor,
    )
    result = adapter.execute(_task(), _strategy(), trajectory)

    assert result.status == "succeeded"
    assert called is True
    assert adapter.driver.closed is True


def test_complete_crawl_adapter_closes_driver_when_executor_raises(monkeypatch) -> None:
    adapter = _CompleteAdapter()
    trajectory = TrajectoryBuffer("task", "strategy", "adapter")

    def _raise_execute(self, **kwargs):
        raise RuntimeError("executor failed")

    monkeypatch.setattr(CrawlExecutor, "execute", _raise_execute)

    with pytest.raises(RuntimeError, match="executor failed"):
        adapter.execute(_task(), _strategy(), trajectory)

    assert adapter.driver.closed is True


@pytest.mark.parametrize(
    ("message", "error_type", "next_action"),
    [
        ("selector_not_found", "structure_error", "planner_repair_then_path_b"),
        ("request timeout", "transient", "resume_path_b"),
        ("captcha encountered", "blocked", "needs_manual_review"),
        ("completion_unverified", "completion_unverified", "needs_manual_review"),
        ("malformed candidate payload", "data_error", "partial_success"),
    ],
)
def test_execution_error_classifier_routes_path_b_recovery(
    message: str,
    error_type: str,
    next_action: str,
) -> None:
    assert classify_execution_error(message).error_type == error_type
    assert classify_next_action(error_type) == next_action
