"""Shared fixture-replay harness for the certified complete-crawl adapters.

Each PEV gray-migration adapter exposes a ``XxxCrawlDriver.from_fixture``
replay path so the deterministic ``CrawlExecutor`` + ``CoverageVerifier`` can
prove coverage without a live browser. ``execute_fixture_crawl(site)`` wires
the captured ``tests/fixtures/job_discovery/<site>/contract.json`` through the
executor and returns the ``CrawlExecutionResult`` -- the single entry point
used by the per-site unit tests for the "follow total until last page" /
"emits every listing" assertions.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.app.services.job_discovery.adapters.feishu import (
    FEISHU_CRAWL_PLAN,
    FeishuCrawlDriver,
)
from backend.app.services.job_discovery.adapters.inovance import (
    INOVANCE_CRAWL_PLAN,
    InovanceCrawlDriver,
)
from backend.app.services.job_discovery.adapters.xiaohongshu import (
    XHS_CRAWL_PLAN,
    XiaohongshuCrawlDriver,
)
from backend.app.services.job_discovery.crawling.crawl_executor import CrawlExecutor
from backend.app.services.job_discovery.crawling.crawl_plan import CrawlPlan
from backend.app.services.job_discovery.schemas import DiscoveryTaskInput

FIXTURES = (
    Path(__file__).resolve().parents[2] / "fixtures" / "job_discovery"
)


#: Registry of fixture-replay sites. Each entry maps a site name to the
#: ``(driver_cls, plan_yaml)`` pair consumed by ``execute_fixture_crawl``.
_REGISTRY: dict[str, tuple[type, str]] = {
    "feishu": (FeishuCrawlDriver, FEISHU_CRAWL_PLAN),
    "inovance": (InovanceCrawlDriver, INOVANCE_CRAWL_PLAN),
    "xiaohongshu": (XiaohongshuCrawlDriver, XHS_CRAWL_PLAN),
}


def execute_fixture_crawl(site: str) -> Any:
    """Run a fixture-replay crawl for ``site`` and return the result.

    The driver is built from ``tests/fixtures/job_discovery/<site>``; the plan
    is the adapter's declared ``*_CRAWL_PLAN``; the task carries the fixture's
    ``page_url`` as ``source_url``. The ``CrawlExecutor`` merges source/detail
    records, fetches each unique detail once, and runs ``verify_coverage``.
    """
    driver_cls, plan_yaml = _REGISTRY[site]
    fixture = FIXTURES / site
    driver = driver_cls.from_fixture(fixture)
    plan = CrawlPlan.from_yaml(plan_yaml)
    task = DiscoveryTaskInput(
        source_id=site,
        raw_record_id=f"raw-{site}",
        external_record_id=f"{site}-1",
        source_key=site,
        source_url=driver.source_url,
        url_hash=f"{site}-hash",
        record_fields=[],
    )
    return CrawlExecutor(driver).execute(plan=plan, task=task)
