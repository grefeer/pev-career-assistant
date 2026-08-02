"""RecommendationService - rank JD candidates against profile + preferences.

Two entry points:
- ``rank``: pure in-memory scoring (one-shot personal-assistant run).
- ``score_and_cache``: DB-backed lazy scoring with cache hit/miss on
  ``job_relevance_scores`` so repeated recommendation queries skip the LLM.

The JobPosting-backed ``get_recommendations`` (fetch verified postings, map to
candidates, delegate to ``score_and_cache``) and auto-promotion of discovered
candidates to verified JobPostings in personal_mode are app-integration
concerns wired at the worker/route layer; this service stays decoupled from
JobPosting persistence so the rank+cache loop is independently testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from backend.app.repositories import relevance_scores as relevance_repo
from backend.app.services.relevance.relevance_ranker import (
    RankedCandidate,
    RelevanceRanker,
)


@dataclass
class Recommendation:
    """A scored, presentation-ready job recommendation."""

    job_id: str
    title: str | None = None
    company_name: str | None = None
    department: str | None = None
    locations: list[str] = field(default_factory=list)
    apply_url: str | None = None
    score: float = 0.0
    reason: str = ""
    matched_signals: list[str] = field(default_factory=list)


def _field(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


class RecommendationService:
    """Orchestrates the cheap ranker + relevance-score cache."""

    def __init__(self, ranker: RelevanceRanker) -> None:
        self.ranker = ranker

    def rank(
        self,
        candidates: list[Any],
        *,
        profile_summary: dict[str, Any],
        preferences: dict[str, Any],
    ) -> list[RankedCandidate]:
        """Pure in-memory scoring (no DB)."""
        return self.ranker.rank(
            candidates, profile_summary=profile_summary, preferences=preferences
        )

    def score_and_cache(
        self,
        db: Session,
        user_id: str,
        items: list[tuple[str, Any]],
        *,
        profile_summary: dict[str, Any],
        preferences: dict[str, Any],
        profile_version_id: str | None = None,
    ) -> list[Recommendation]:
        """Lazy-score ``items`` (``(job_id, candidate)``) and cache results.

        Cache hits skip the LLM; only job_ids lacking a cached score for the
        ``(profile_version_id, preferences_version)`` key are batched through
        the ranker. Returns one ``Recommendation`` per item, read from cache.
        """
        if not items:
            return []

        prefs_version = int(preferences.get("version") or 0)
        job_ids = [jid for jid, _ in items]
        unscored_ids = set(
            relevance_repo.list_unscored_job_ids(
                db,
                user_id,
                job_ids,
                profile_version_id=profile_version_id,
                preferences_version=prefs_version,
            )
        )

        if unscored_ids:
            to_score = [(jid, c) for jid, c in items if jid in unscored_ids]
            candidates = [c for _, c in to_score]
            ranked = self.ranker.rank(
                candidates,
                profile_summary=profile_summary,
                preferences=preferences,
            )
            for (_jid, _cand), scored in zip(to_score, ranked, strict=True):
                relevance_repo.upsert(
                    db,
                    user_id=user_id,
                    job_id=_jid,
                    profile_version_id=profile_version_id,
                    preferences_version=prefs_version,
                    score=scored.score,
                    reason=scored.reason,
                    matched_signals=scored.matched_signals,
                )
            db.flush()

        recs: list[Recommendation] = []
        for jid, cand in items:
            row = relevance_repo.get(
                db,
                jid,
                profile_version_id=profile_version_id,
                preferences_version=prefs_version,
            )
            if row is None:
                continue
            recs.append(
                Recommendation(
                    job_id=jid,
                    title=_field(cand, "title"),
                    company_name=_field(cand, "company_name"),
                    department=_field(cand, "department"),
                    locations=list(_field(cand, "locations") or []),
                    apply_url=_field(cand, "apply_url"),
                    score=row.score,
                    reason=row.reason or "",
                    matched_signals=row.matched_signals_json or [],
                )
            )
        return recs

    @staticmethod
    def filter_and_sort(
        recs: list[Recommendation],
        *,
        top_n: int = 20,
        min_score: float = 0.0,
    ) -> list[Recommendation]:
        filtered = [r for r in recs if r.score >= min_score]
        filtered.sort(key=lambda r: r.score, reverse=True)
        return filtered[:top_n]
