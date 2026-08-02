"""Strict presentation DTOs for the personalized discovery v1 API.

Every request model uses ``extra="forbid"`` so crawler inputs (``url``,
``site``, ``adapter``, crawl-plan fields) are rejected with 422 rather than
silently ignored. Card responses leak no JD body, raw task, error detail, or
evidence excerpt - only identity, a safe apply URL, score metadata, fixed
label, and timestamps.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.app.domain.personalized_discovery import (
    RecommendationPresentationState,
    SourceStatusReason,
)

# Fixed, always-present label telling the user this is a pre-review,
# auto-discovered suggestion they should confirm themselves.
AUTO_DISCOVERY_LABEL = "自动发现，建议自行确认"

# Role list / term limits mirror the domain (Task 1) so a runaway DTO cannot
# fill the JSON column or the LLM prompt. Per-term content validation
# (non-blank, length cap) is enforced by the domain normalizer in the route.
_MAX_ROLE_TERMS = 100


class PreferencesUpdateRequest(BaseModel):
    """Extend the user's role preferences for personalized discovery.

    All fields are optional (PATCH semantics). Role terms are validated by the
    domain normalizer in the route (blank terms -> 422); the threshold is 0..100.
    """

    model_config = ConfigDict(extra="forbid")

    desired_roles: list[str] | None = Field(default=None, max_length=_MAX_ROLE_TERMS)
    role_synonyms: list[str] | None = Field(default=None, max_length=_MAX_ROLE_TERMS)
    excluded_roles: list[str] | None = Field(default=None, max_length=_MAX_ROLE_TERMS)
    personalized_discovery_min_score: float | None = Field(default=None, ge=0, le=100)


class PreferencesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    desired_roles: list[str] = Field(default_factory=list)
    role_synonyms: list[str] = Field(default_factory=list)
    excluded_roles: list[str] = Field(default_factory=list)
    personalized_discovery_min_score: float | None = None
    version: int = Field(ge=0)


class RunCreateRequest(BaseModel):
    """Empty by design: a run processes existing retained shared tasks only.

    Any body field (e.g. ``url``, ``site``) is rejected with 422.
    """

    model_config = ConfigDict(extra="forbid")


class RunSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_count: int = 0
    status_count: int = 0
    candidate_pool: int = 0
    recommendation_count: int = 0
    statuses: list[str] = Field(default_factory=list)


class RunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    status: str
    preference_version: int
    started_at: datetime
    finished_at: datetime | None = None
    summary: RunSummary | None = None


class EvidenceLinkResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str | None = None
    evidence_type: str | None = None


class RecommendationCardResponse(BaseModel):
    """A single pre-review recommendation card.

    Deliberately omits JD body, raw task id details, error text, and evidence
    excerpts. The apply URL has been re-validated against the source host.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str | None = None
    company: str | None = None
    locations: list[str] = Field(default_factory=list)
    apply_url: str | None = None
    score: float
    reason: str | None = None
    signals: list[str] = Field(default_factory=list)
    evidence_links: list[EvidenceLinkResponse] = Field(default_factory=list)
    label: str = AUTO_DISCOVERY_LABEL
    state: RecommendationPresentationState
    created_at: datetime
    updated_at: datetime


class RecommendationListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[RecommendationCardResponse]
    total: int = Field(ge=0)


class SourceStatusResponse(BaseModel):
    """A source that could not be recommended, with fixed display copy.

    Carries only source identity + a closed reason code + fixed display text -
    never cookies, auth headers, wall text, or anti-bot detail.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    run_id: str
    task_id: str
    source_key: str | None = None
    safe_source_url: str
    reason_code: SourceStatusReason
    display_text: str
    retry_guidance: str
    created_at: datetime


class SourceStatusListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[SourceStatusResponse]
    total: int = Field(ge=0)


# ``new`` is the initial state set by the service, not a user interaction.
InteractionState = Literal["viewed", "saved", "dismissed", "apply_clicked"]


class InteractionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: InteractionState
