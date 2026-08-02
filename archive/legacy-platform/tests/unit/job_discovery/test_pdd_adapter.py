from __future__ import annotations

from backend.app.services.job_discovery.adapters.pdd import PDD_CRAWL_PLAN, PddCrawlDriver
from backend.app.services.job_discovery.crawling.crawl_plan import CrawlPlan
from backend.app.services.job_discovery.schemas import DiscoveryTaskInput


def _task() -> DiscoveryTaskInput:
    return DiscoveryTaskInput(
        source_id="source", raw_record_id="raw", external_record_id="external",
        source_key="source", source_url="https://careers.pddglobalhr.com/campus/grad?t=token",
        url_hash="hash", record_fields=[],
    )


def test_pdd_api_driver_proves_total_and_reuses_inline_jd() -> None:
    payload = {
        "success": True,
        "result": {"total": "2", "list": [
            {"id": "one", "name": "AI Agent研发工程师", "code": "X1", "jobDuty": "职责一", "workLocationName": "上海"},
            {"id": "two", "name": "算法工程师", "code": "X2", "jobDuty": "职责二", "workLocationName": "杭州"},
        ]},
    }
    driver = PddCrawlDriver(source_url=_task().source_url, api_fetcher=lambda *_: payload)
    plan = CrawlPlan.from_yaml(PDD_CRAWL_PLAN)

    page = driver.fetch_listing_page(plan=plan, task=_task(), cursor=None)

    assert page.expected_listing_count == 2
    assert page.terminal_evidence == "pdd_api_total_reached"
    assert page.next_cursor is None
    assert page.listings[0].apply_url and "positionId=one" in page.listings[0].apply_url
    detail = driver.fetch_detail(plan=plan, listing=page.listings[0], resource_key="one")
    assert detail.full_text == "岗位职责\n职责一"
