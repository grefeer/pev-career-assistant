from __future__ import annotations

from backend.app.services.job_discovery.adapters.mioffice import (
    MIOFFICE_CRAWL_PLAN,
    MiofficeCrawlAdapter,
    _parse_card_text,
)
from backend.app.services.job_discovery.crawling.crawl_plan import CrawlPlan
from backend.app.services.job_discovery.schemas import PaginationType


def test_mioffice_reuses_total_driven_shared_api_contract() -> None:
    plan = CrawlPlan.from_yaml(MIOFFICE_CRAWL_PLAN)

    assert plan.pagination.type is PaginationType.PAGE_NUMBER
    assert plan.listing.item_selector == "a[href*='/toptalent/position/'][href*='/detail']"
    assert plan.completion.require_all_details is True


def test_mioffice_adapter_accepts_xiaomi_public_share_url() -> None:
    adapter = MiofficeCrawlAdapter()

    assert adapter.validate("https://xiaomi.jobs.f.mioffice.cn/s/kJVnd58xtWY")


def test_mioffice_card_body_is_labelled_for_deterministic_normalization() -> None:
    title, body, locations = _parse_card_text(
        "AI Compiler 开发工程师-芯片\n北京\n校招\n正式\n软件研发类\n1、研发职责"
    )

    assert title == "AI Compiler 开发工程师-芯片"
    assert body == "岗位职责\n1、研发职责"
    assert locations == ["北京"]
