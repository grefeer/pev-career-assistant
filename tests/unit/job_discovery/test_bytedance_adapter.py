from __future__ import annotations

from backend.app.services.job_discovery.adapters.bytedance import (
    BYTEDANCE_CRAWL_PLAN,
    ByteDanceCrawlAdapter,
    _campus_subject_ids,
)
from backend.app.services.job_discovery.crawling.crawl_plan import CrawlPlan
from backend.app.services.job_discovery.schemas import PaginationType


def test_bytedance_uses_shared_total_driven_search_contract() -> None:
    plan = CrawlPlan.from_yaml(BYTEDANCE_CRAWL_PLAN)
    assert plan.pagination.type is PaginationType.PAGE_NUMBER
    assert plan.pagination.total_count_path == "$.data.count"


def test_bytedance_adapter_accepts_campus_url() -> None:
    assert ByteDanceCrawlAdapter().validate("https://jobs.bytedance.com/campus/position")


def test_bytedance_selects_only_current_campus_subjects() -> None:
    payload = {
        "data": {
            "job_subject_list": [
                {"id": "campus", "name": {"zh_cn": "2027届校招"}},
                {"id": "intern", "name": {"zh_cn": "日常实习"}},
                {"id": "campus", "name": {"zh_cn": "校招补录"}},
            ]
        }
    }

    assert _campus_subject_ids(payload) == ["campus"]
