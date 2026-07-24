from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal


@dataclass
class DiscoveryTaskInput:
    source_id: str
    raw_record_id: str
    external_record_id: str
    source_key: str
    source_url: str
    url_hash: str
    record_fields: list[dict]


@dataclass
class TriageResult:
    site_type: str  # official_site, career_site, job_detail, wechat_article, email_only, blocked, invalid
    confidence: float  # 0.0-1.0
    recommended_action: str  # run_web_navigation, parse_wechat_article, finish_manual_review, skip
    notes: str = ""


@dataclass
class PageEvidence:
    evidence_type: str  # page_text, screenshot, wechat_text, wechat_image, ocr_text, email_instruction, browser_trace
    url: str | None = None
    title: str | None = None
    content_hash: str = ""
    text_excerpt: str | None = None
    metadata: dict | None = None


@dataclass
class WechatArticleResult:
    title: str | None = None
    text_content: str = ""
    image_urls: list[str] = field(default_factory=list)
    email_delivery_instructions: str | None = None
    needs_manual_review: bool = False
    manual_review_reason: str = ""


@dataclass
class OcrResult:
    full_text: str = ""
    confidence: float = 0.0
    slice_count: int = 0
    warnings: list[str] = field(default_factory=list)
    needs_manual_review: bool = False


@dataclass
class NormalizedJobCandidate:
    title: str | None = None
    company_name: str | None = None
    department: str | None = None
    description_text: str = ""
    responsibilities: str = ""
    requirements: str = ""
    locations: list[str] = field(default_factory=list)
    recruitment_types: list[str] = field(default_factory=list)
    industries: list[str] = field(default_factory=list)
    apply_url: str | None = None
    application_channel_json: dict | None = None
    deadline_text: str | None = None
    referral_code: str | None = None
    confidence: float = 0.0
    evidence_refs: list[dict] = field(default_factory=list)
    normalization_warnings: list[str] = field(default_factory=list)


@dataclass
class DiscoveryRunResult:
    status: str  # succeeded, partial_success, needs_manual_review, failed
    block_reason: str | None = None
    evidence: list[PageEvidence] = field(default_factory=list)
    candidates: list[NormalizedJobCandidate] = field(default_factory=list)
    summary: str = ""
    # Full-crawl coverage proof (Planner-Executor-Verifier migration).
    # ``None`` = legacy PATH C path that carries no coverage; the global
    # ``enforce_result_invariants`` keeps its pre-migration
    # "succeeded requires candidates" behavior for that case (gray migration).
    # Only the post-crawl pipeline (run_post_crawl_pipeline) sets a real
    # coverage and relies on ``crawling.coverage.verify_coverage``.
    coverage: "CrawlCoverage | None" = None


@dataclass
class StrategyRecord:
    """In-memory representation of a matched strategy (decoupled from ORM)."""

    id: str
    url_pattern: str
    site_type: str
    description: str = ""
    priority: int = 0
    adapter: str | None = None
    plan_yaml: str = ""
    status: str = "active"
    success_count: int = 0

    @classmethod
    def from_orm(cls, orm_obj: Any) -> "StrategyRecord":
        """Build from a JobDiscoveryStrategy ORM instance."""
        return cls(
            id=orm_obj.id,
            url_pattern=orm_obj.url_pattern,
            site_type=orm_obj.site_type,
            description=orm_obj.description or "",
            priority=orm_obj.priority,
            adapter=orm_obj.adapter,
            plan_yaml=orm_obj.plan_yaml,
            status=orm_obj.status,
            success_count=orm_obj.success_count,
        )


# ---------------------------------------------------------------------------
# Full-crawl domain contracts (Planner-Executor-Verifier gray migration)
#
# These types are additive. Existing PATH C / SnapshotExecutor production
# paths do not set ``coverage`` and keep their pre-migration behavior. The
# deterministic CrawlExecutor, CoverageVerifier and post-crawl pipeline
# consume these. See:
# docs/superpowers/specs/2026-07-22-job-discovery-complete-crawl-refactor.md
# ---------------------------------------------------------------------------

RecruitmentType = Literal["campus", "internship", "social"]
DecisionStatus = Literal["PASS", "FAIL", "REVIEW"]


@dataclass
class RecruitmentScope:
    """Target recruitment scope for a single discovery task.

    One task targets exactly one ``recruitment_type``. ``social`` has no
    cohort; ``campus``/``internship`` require a ``graduation_year``.
    """

    recruitment_type: RecruitmentType = "campus"
    graduation_year: int | None = 2027

    def __post_init__(self) -> None:
        if self.recruitment_type == "social":
            self.graduation_year = None
            return
        if self.graduation_year is None:
            raise ValueError(
                "graduation_year is required for campus and internship"
            )


class PaginationType(str, Enum):
    PAGE_NUMBER = "page_number"
    NEXT_BUTTON = "next_button"
    LOAD_MORE = "load_more"
    INFINITE_SCROLL = "infinite_scroll"
    API_CURSOR = "api_cursor"
    API_OFFSET = "api_offset"
    SINGLE_PAGE = "single_page"
    UNKNOWN = "unknown"


@dataclass
class CrawlCoverage:
    """Deterministic proof of how completely a site was crawled.

    ``coverage_complete`` is advisory; the authoritative verdict comes from
    ``crawling.coverage.verify_coverage``. ``expected_*`` may be ``None`` when
    the site offers no upfront total -- API cursor / infinite scroll rely on a
    positive terminal signal instead.
    """

    pagination_type: PaginationType
    expected_page_count: int | None = None
    visited_page_count: int = 0
    visited_page_keys: list[str] = field(default_factory=list)
    expected_listing_count: int | None = None
    raw_listing_count: int = 0
    unique_listing_count: int = 0
    total_detail_count: int = 0
    fetched_detail_count: int = 0
    failed_detail_count: int = 0
    coverage_complete: bool = False
    completion_evidence: list[str] = field(default_factory=list)
    incomplete_reason: str | None = None
    resumable: bool = False
    resume_cursor: dict | None = None


@dataclass
class RawJobListing:
    """A job row extracted from a listing page, before detail fetch."""

    source_url: str
    detail_url: str | None
    company: str | None
    title: str
    locations: list[str] = field(default_factory=list)
    job_code: str | None = None
    recruitment_type_hint: str | None = None
    graduation_year_hints: list[int] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    source_record_key: str | None = None


@dataclass
class RawJobDetail:
    """The fetched full-text JD for one unique detail resource."""

    detail_url: str
    full_text: str
    title: str | None = None
    company: str | None = None
    locations: list[str] = field(default_factory=list)
    job_code: str | None = None
    structured_fields: dict = field(default_factory=dict)
    channel_text: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    detail_resource_key: str | None = None
