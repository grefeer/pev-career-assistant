from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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
