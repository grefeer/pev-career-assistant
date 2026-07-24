from __future__ import annotations

import pytest

from backend.app.services.job_discovery.crawling.checkpoint import CrawlCheckpoint


def test_checkpoint_roundtrip_preserves_pending_details() -> None:
    checkpoint = CrawlCheckpoint(
        plan_version=1,
        source_url="https://jobs.example.com/campus",
        pagination_cursor={"page": 3},
        visited_page_keys=["p1", "p2"],
        visited_page_fingerprints=["fingerprint-p1", "fingerprint-p2"],
        pending_detail_keys=["d2"],
        completed_detail_keys=["d1"],
        failed_detail_keys=[],
        collected_listings=[{"source_url": "https://jobs.example.com/campus"}],
        completed_details=[{"detail_url": "https://jobs.example.com/job/1"}],
        pagination_complete=True,
        completion_evidence=["next_cursor_null"],
        expected_page_count=3,
        expected_listing_count=25,
    )

    assert CrawlCheckpoint.from_dict(checkpoint.to_dict()) == checkpoint


def test_checkpoint_loads_legacy_dict_with_new_resume_fields_defaulted() -> None:
    checkpoint = CrawlCheckpoint.from_dict(
        {
            "plan_version": 1,
            "source_url": "https://jobs.example.com/campus",
            "completed_detail_keys": ["d1"],
        }
    )

    assert checkpoint.collected_listings == []
    assert checkpoint.completed_details == []
    assert checkpoint.pagination_complete is False
    assert checkpoint.completion_evidence == []
    assert checkpoint.expected_page_count is None
    assert checkpoint.expected_listing_count is None


def test_checkpoint_rejects_other_source_url() -> None:
    checkpoint = CrawlCheckpoint(
        plan_version=1,
        source_url="https://a.example/jobs",
    )

    with pytest.raises(ValueError, match="source_url"):
        checkpoint.validate_for(
            plan_version=1,
            source_url="https://b.example/jobs",
        )


def test_checkpoint_rejects_other_plan_version() -> None:
    checkpoint = CrawlCheckpoint(
        plan_version=1,
        source_url="https://a.example/jobs",
    )

    with pytest.raises(ValueError, match="plan_version"):
        checkpoint.validate_for(
            plan_version=2,
            source_url="https://a.example/jobs",
        )
