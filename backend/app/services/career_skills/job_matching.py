"""Evidence-bound matching tool for the PEV ``job-matching`` Skill."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from backend.app.services.agent_runtime.tool_context import ToolContext


class MatchObservedJobsInput(BaseModel):
    """Confirmed user capabilities or preferences selected by the Executor."""

    profile_keywords: list[str] = Field(default_factory=list, max_length=30)
    limit: int = Field(default=10, ge=1, le=20)

    @field_validator("profile_keywords")
    @classmethod
    def normalize_keywords(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip().lower() for value in values if value.strip()))


class ObservedJobMatch(BaseModel):
    """A transparent score over one immutable public JD artifact."""

    artifact_id: str
    source_url: str
    title: str | None
    score: int = Field(ge=0, le=100)
    matched_keywords: list[str]
    evidence_excerpt: str


class MatchObservedJobsOutput(BaseModel):
    """Ranked output with no candidates outside the supplied evidence context."""

    matches: list[ObservedJobMatch]


def match_observed_jobs(
    context: ToolContext, payload: MatchObservedJobsInput
) -> MatchObservedJobsOutput:
    """Rank only public evidence already observed in this authenticated PEV run."""
    raw_evidence = context.metadata.get("observed_public_evidence", [])
    if not isinstance(raw_evidence, list):
        return MatchObservedJobsOutput(matches=[])
    matches: list[ObservedJobMatch] = []
    for item in raw_evidence:
        if not isinstance(item, dict):
            continue
        artifact_id = item.get("artifact_id")
        source_url = item.get("source_url")
        visible_text = item.get("visible_text")
        if not all(isinstance(value, str) and value for value in (artifact_id, source_url, visible_text)):
            continue
        title = item.get("title")
        normalized_title = title if isinstance(title, str) else None
        searchable = f"{normalized_title or ''}\n{visible_text}".lower()
        matched = [keyword for keyword in payload.profile_keywords if keyword in searchable]
        score = min(100, len(matched) * 34)
        matches.append(
            ObservedJobMatch(
                artifact_id=artifact_id,
                source_url=source_url,
                title=normalized_title,
                score=score,
                matched_keywords=matched,
                evidence_excerpt=visible_text[:500],
            )
        )
    matches.sort(key=lambda match: (-match.score, match.title or "", match.artifact_id))
    return MatchObservedJobsOutput(matches=matches[: payload.limit])
