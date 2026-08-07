"""Evidence-bound matching tool for the PEV ``job-matching`` Skill."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from backend.app.services.agent_runtime.tool_context import ToolContext


class MatchObservedJobsInput(BaseModel):
    """Confirmed user capabilities or preferences selected by the Executor."""

    profile_keywords: list[str] = Field(default_factory=list, max_length=30)
    preferred_locations: list[str] = Field(default_factory=list, max_length=20)
    ranking_criteria: list[
        Literal["skills", "location", "salary", "recency", "company_type"]
    ] = Field(default_factory=lambda: ["skills"])
    #: The cap mirrors the extraction cap (``_MAX_CANDIDATES_PER_PAGE``) so a
    #: full card-list extraction can be ranked in one call without dropping
    #: captured jobs.
    limit: int = Field(default=100, ge=1, le=100)

    @field_validator("profile_keywords")
    @classmethod
    def normalize_keywords(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip().lower() for value in values if value.strip()))

    @field_validator("preferred_locations")
    @classmethod
    def normalize_locations(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))

    @field_validator("ranking_criteria")
    @classmethod
    def deduplicate_ranking_criteria(
        cls, values: list[Literal["skills", "location", "salary", "recency", "company_type"]]
    ) -> list[Literal["skills", "location", "salary", "recency", "company_type"]]:
        return list(dict.fromkeys(values))


class ObservedJobMatch(BaseModel):
    """A transparent score over one immutable public JD artifact."""

    artifact_id: str
    source_url: str
    title: str | None
    score: int = Field(ge=0, le=100)
    matched_keywords: list[str]
    matched_locations: list[str] = Field(default_factory=list)
    compensation_text: str | None = None
    observed_company_types: list[str] = Field(default_factory=list)
    unverified_ranking_criteria: list[str] = Field(default_factory=list)
    evidence_excerpt: str


class MatchObservedJobsOutput(BaseModel):
    """Ranked output with no candidates outside the supplied evidence context."""

    matches: list[ObservedJobMatch]
    unresolved_ranking_criteria: list[str] = Field(default_factory=list)


_COMPENSATION_RE = re.compile(
    r"(?i)(?:[¥￥]|rmb)?\s*\d+(?:\.\d+)?\s*(?:k|千|万)"
    r"(?:\s*[-~至]\s*(?:[¥￥]|rmb)?\s*\d+(?:\.\d+)?\s*(?:k|千|万))?"
    r"(?:\s*(?:/\s*(?:月|年)|月薪|年薪))?"
)
_COMPANY_TYPE_LABELS = ("国企", "民营", "外企", "事业单位")


def match_observed_jobs(
    context: ToolContext, payload: MatchObservedJobsInput
) -> MatchObservedJobsOutput:
    """Rank only public evidence already observed in this authenticated PEV run.

    Structured candidates (``extract-observed-job-details`` output) take
    priority: each captured job is scored individually against the confirmed
    profile, so a card-list page produces per-job ranked matches instead of
    one aggregated entry. Raw page evidence remains the fallback for runs
    with no structured extraction (single-JD pages, evidence-only evals).
    """
    candidates = context.metadata.get("structured_job_candidates", [])
    matches: list[ObservedJobMatch] = []
    if isinstance(candidates, list) and candidates:
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            match = _match_candidate(candidate, payload)
            if match is not None:
                matches.append(match)
    else:
        matches = _match_raw_evidence(context, payload)
    matches.sort(key=lambda match: (-match.score, match.title or "", match.artifact_id))
    selected_matches = matches[: payload.limit]
    unresolved = [
        criterion
        for criterion in payload.ranking_criteria
        if any(criterion in match.unverified_ranking_criteria for match in selected_matches)
    ]
    return MatchObservedJobsOutput(
        matches=selected_matches,
        unresolved_ranking_criteria=unresolved,
    )


def _match_raw_evidence(
    context: ToolContext, payload: MatchObservedJobsInput
) -> list[ObservedJobMatch]:
    """Score whole-page evidence items as jobs (fallback path)."""
    raw_evidence = context.metadata.get("observed_public_evidence", [])
    if not isinstance(raw_evidence, list):
        return []
    matches: list[ObservedJobMatch] = []
    for item in raw_evidence:
        if not isinstance(item, dict):
            continue
        artifact_id = item.get("artifact_id")
        source_url = item.get("source_url")
        visible_text = item.get("visible_text")
        if not all(
            isinstance(value, str) and value
            for value in (artifact_id, source_url, visible_text)
        ):
            continue
        title = item.get("title")
        normalized_title = title if isinstance(title, str) else None
        searchable = f"{normalized_title or ''}\n{visible_text}".lower()
        matches.append(
            _score_job(
                artifact_id=artifact_id,
                source_url=source_url,
                title=normalized_title,
                searchable=searchable,
                excerpt=visible_text,
                payload=payload,
            )
        )
    return matches


def _match_candidate(
    item: dict[str, Any], payload: MatchObservedJobsInput
) -> ObservedJobMatch | None:
    """Score one structured job candidate against the confirmed profile."""
    artifact_id = item.get("artifact_id")
    source_url = item.get("source_url")
    if not (
        isinstance(artifact_id, str)
        and artifact_id
        and isinstance(source_url, str)
        and source_url
    ):
        return None
    title = item.get("title")
    normalized_title = title if isinstance(title, str) else None
    locations = item.get("locations") if isinstance(item.get("locations"), list) else []
    responsibilities = item.get("responsibilities")
    requirements = item.get("requirements")
    section_text = "\n".join(
        part for part in (responsibilities, requirements) if isinstance(part, str) and part
    )
    company_name = item.get("company_name")
    searchable = "\n".join(
        part
        for part in (
            normalized_title,
            " ".join(locations),
            company_name if isinstance(company_name, str) else None,
            section_text,
        )
        if part
    ).lower()
    excerpt = section_text or normalized_title or ""
    return _score_job(
        artifact_id=artifact_id,
        source_url=source_url,
        title=normalized_title,
        searchable=searchable,
        excerpt=excerpt,
        payload=payload,
    )


def _score_job(
    *,
    artifact_id: str,
    source_url: str,
    title: str | None,
    searchable: str,
    excerpt: str,
    payload: MatchObservedJobsInput,
) -> ObservedJobMatch:
    """Score one job unit from its searchable text, exposing unverified criteria."""
    matched = [keyword for keyword in payload.profile_keywords if keyword in searchable]
    matched_locations = [
        location for location in payload.preferred_locations if location.lower() in searchable
    ]
    compensation_match = _COMPENSATION_RE.search(searchable)
    compensation_text = compensation_match.group(0).strip() if compensation_match else None
    observed_company_types = [label for label in _COMPANY_TYPE_LABELS if label in searchable]
    unverified = _unverified_criteria(
        payload.ranking_criteria,
        matched_locations=matched_locations,
        compensation_text=compensation_text,
        observed_company_types=observed_company_types,
    )
    score = min(100, len(matched) * 34)
    return ObservedJobMatch(
        artifact_id=artifact_id,
        source_url=source_url,
        title=title,
        score=score,
        matched_keywords=matched,
        matched_locations=matched_locations,
        compensation_text=compensation_text,
        observed_company_types=observed_company_types,
        unverified_ranking_criteria=unverified,
        evidence_excerpt=excerpt[:500],
    )


def _unverified_criteria(
    ranking_criteria: list[str],
    *,
    matched_locations: list[str],
    compensation_text: str | None,
    observed_company_types: list[str],
) -> list[str]:
    """Surface omitted evidence rather than silently ranking from assumptions."""
    unverified: list[str] = []
    for criterion in ranking_criteria:
        if criterion == "location" and not matched_locations:
            unverified.append(criterion)
        elif criterion == "salary" and compensation_text is None:
            unverified.append(criterion)
        elif criterion == "company_type" and not observed_company_types:
            unverified.append(criterion)
        elif criterion == "recency":
            # Captured pages currently do not preserve an authoritative publish time.
            unverified.append(criterion)
    return unverified
