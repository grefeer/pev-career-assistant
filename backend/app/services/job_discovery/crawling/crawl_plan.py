"""CrawlPlan schema -- structured instructions the deterministic CrawlExecutor replays.

Produced by PATH A (static templates) or PATH C (planning/repair agent). A
CrawlPlan never carries job data, only *how* to crawl: listing selectors,
pagination mechanism plus a positive terminal condition, detail selectors, and
completion rules. ``validate_security`` refuses plans that cannot *prove*
completion (e.g. infinite scroll with no terminal signal) -- the Agent cannot
declare "done" to compensate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import yaml

from backend.app.services.job_discovery.schemas import PaginationType


@dataclass
class ListingSchema:
    item_selector: str
    title_selector: str
    detail_link_selector: str | None = None
    location_selector: str | None = None
    job_code_selector: str | None = None


@dataclass
class PaginationSchema:
    type: PaginationType
    page_selector: str | None = None
    next_selector: str | None = None
    disabled_selector: str | None = None
    terminal_selector: str | None = None
    endpoint_pattern: str | None = None
    items_path: str | None = None
    total_count_path: str | None = None
    has_more_path: str | None = None
    next_cursor_path: str | None = None
    offset_path: str | None = None


@dataclass
class DetailSchema:
    title_selector: str | None = None
    body_selector: str | None = None
    responsibility_selector: str | None = None
    requirement_selector: str | None = None
    education_selector: str | None = None
    major_selector: str | None = None
    location_selector: str | None = None


@dataclass
class CompletionRules:
    require_all_pages: bool = True
    require_all_details: bool = True


@dataclass
class CrawlPlan:
    """Validated crawl instructions (version 1)."""

    version: int
    listing: ListingSchema
    pagination: PaginationSchema
    detail: DetailSchema
    completion: CompletionRules
    scope_actions: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_yaml(cls, plan_yaml: str) -> "CrawlPlan":
        """Parse and security-validate a ``plan_type: crawl_plan`` YAML string."""
        raw = yaml.safe_load(plan_yaml)
        if not isinstance(raw, dict):
            raise ValueError("crawl plan must be a YAML mapping")
        if raw.get("plan_type") != "crawl_plan":
            raise ValueError("plan_type must be crawl_plan")
        pagination = PaginationSchema(
            **{
                **raw["pagination"],
                "type": PaginationType(raw["pagination"]["type"]),
            }
        )
        plan = cls(
            version=int(raw["version"]),
            listing=ListingSchema(**raw["listing"]),
            pagination=pagination,
            detail=DetailSchema(**raw["detail"]),
            completion=CompletionRules(**raw["completion"]),
            scope_actions=raw.get("scope_actions", []),
        )
        plan.validate_security()
        return plan

    def validate_security(self) -> None:
        """Refuse plans that cannot provably terminate.

        Infinite scroll must declare at least one positive terminal signal
        (a terminal DOM marker, a ``hasMore``/``totalCount`` JSON path, or a
        ``nextCursor=null`` path). Without one the crawl cannot be proven
        complete and the Agent is not allowed to fill the gap.
        """
        if self.version != 1:
            raise ValueError("unsupported crawl plan version")
        if self.pagination.type == PaginationType.INFINITE_SCROLL:
            terminal_fields = (
                self.pagination.terminal_selector,
                self.pagination.has_more_path,
                self.pagination.total_count_path,
            )
            if not any(terminal_fields):
                raise ValueError(
                    "infinite_scroll requires a positive terminal signal"
                )
