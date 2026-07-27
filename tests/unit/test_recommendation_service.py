"""Characterization tests for RecommendationService (Task 3).

``rank`` is pure (no truncation); ``filter_and_sort`` is what applies top_n.
The personalized service must call ``rank`` (or the ranker) directly, never
``filter_and_sort`` whose default is ``top_n=20``.
"""

from __future__ import annotations

from backend.app.services.recommendation_service import RecommendationService
from backend.app.services.relevance.relevance_ranker import RankedCandidate


class _FakeRanker:
    """Returns one RankedCandidate per input, score = index, no truncation."""

    def rank(self, candidates, *, profile_summary, preferences):  # noqa: ANN001
        return [
            RankedCandidate(index=i, title=_title(c)) for i, c in enumerate(candidates)
        ]


def _title(c: object) -> str | None:
    if isinstance(c, dict):
        return c.get("title")
    return getattr(c, "title", None)


def test_recommendation_rank_does_not_truncate_candidates() -> None:
    candidates = [{"title": f"role-{i}"} for i in range(21)]
    ranked = RecommendationService(_FakeRanker()).rank(
        candidates, profile_summary={}, preferences={}
    )
    assert len(ranked) == 21


def test_filter_and_sort_does_truncate_by_default() -> None:
    """Documents the trap the personalized service avoids."""
    from backend.app.services.recommendation_service import Recommendation

    recs = [Recommendation(job_id=str(i), score=float(i)) for i in range(25)]
    truncated = RecommendationService.filter_and_sort(recs)
    assert len(truncated) == 20
