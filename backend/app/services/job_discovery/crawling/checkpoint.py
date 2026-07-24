"""Serializable state for an interrupted deterministic crawl."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class CrawlCheckpoint:
    plan_version: int
    source_url: str
    pagination_cursor: dict[str, Any] | None = None
    visited_page_keys: list[str] = field(default_factory=list)
    visited_page_fingerprints: list[str] = field(default_factory=list)
    pending_detail_keys: list[str] = field(default_factory=list)
    completed_detail_keys: list[str] = field(default_factory=list)
    failed_detail_keys: list[str] = field(default_factory=list)
    collected_listings: list[dict[str, Any]] = field(default_factory=list)
    completed_details: list[dict[str, Any]] = field(default_factory=list)
    pagination_complete: bool = False
    completion_evidence: list[str] = field(default_factory=list)
    expected_page_count: int | None = None
    expected_listing_count: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CrawlCheckpoint":
        return cls(**data)

    def validate_for(self, *, plan_version: int, source_url: str) -> None:
        if self.plan_version != plan_version:
            raise ValueError("checkpoint plan_version mismatch")
        if self.source_url != source_url:
            raise ValueError("checkpoint source_url mismatch")
