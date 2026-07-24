"""Shared adapter base for certified complete-crawl implementations."""

from __future__ import annotations

from abc import abstractmethod

from backend.app.services.job_discovery.adapters.base import DomainAdapter
from backend.app.services.job_discovery.crawling.checkpoint import CrawlCheckpoint
from backend.app.services.job_discovery.crawling.crawl_executor import CrawlExecutor
from backend.app.services.job_discovery.crawling.crawl_plan import CrawlPlan
from backend.app.services.job_discovery.crawling.driver import CrawlDriver
from backend.app.services.job_discovery.post_crawl_pipeline import run_post_crawl_pipeline
from backend.app.services.job_discovery.schemas import (
    DiscoveryRunResult,
    DiscoveryTaskInput,
    StrategyRecord,
)
from backend.app.services.job_discovery.strategy.trajectory_buffer import TrajectoryBuffer


class CompleteCrawlAdapter(DomainAdapter):
    """Adapter contract that shares the deterministic executor and pipeline."""

    @abstractmethod
    def build_driver(
        self,
        plan: CrawlPlan,
        task: DiscoveryTaskInput,
        trajectory: TrajectoryBuffer,
    ) -> CrawlDriver:
        """Return the site-specific driver; do not implement crawl loops here."""

    def execute(
        self,
        task: DiscoveryTaskInput,
        strategy: StrategyRecord,
        trajectory: TrajectoryBuffer,
    ) -> DiscoveryRunResult:
        return self.execute_crawl(CrawlPlan.from_yaml(strategy.plan_yaml), task, trajectory)

    def execute_crawl(
        self,
        plan: CrawlPlan,
        task: DiscoveryTaskInput,
        trajectory: TrajectoryBuffer,
        checkpoint: CrawlCheckpoint | None = None,
    ) -> DiscoveryRunResult:
        driver = self.build_driver(plan, task, trajectory)
        try:
            crawl_result = CrawlExecutor(driver, trajectory).execute(
                plan=plan,
                task=task,
                checkpoint=checkpoint,
            )
            return run_post_crawl_pipeline(task, crawl_result)
        finally:
            close = getattr(driver, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    # Cleanup must never hide a crawl or pipeline failure.
                    pass
