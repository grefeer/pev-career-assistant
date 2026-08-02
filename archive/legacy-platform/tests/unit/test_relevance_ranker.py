"""Characterization tests for RelevanceRanker existing behavior (Task 3).

These guard the personalized service from accidentally relying on truncation or
non-zero failure scores. They pass against the current, unchanged ranker.
"""

from __future__ import annotations

from backend.app.services.job_discovery.schemas import NormalizedJobCandidate
from backend.app.services.relevance.relevance_ranker import RelevanceRanker


class _FailingLLM:
    """LLM stub whose invoke always raises - models a backend outage."""

    def invoke(self, _messages):  # noqa: ANN001, ANN202
        raise RuntimeError("simulated LLM outage")


def test_ranker_failure_returns_zero_score_for_every_candidate() -> None:
    candidate = NormalizedJobCandidate(title="AI应用开发工程师", company_name="某公司")
    ranked = RelevanceRanker(_FailingLLM()).rank(
        [candidate], profile_summary={}, preferences={}
    )
    assert len(ranked) == 1
    assert ranked[0].score == 0.0
    assert ranked[0].title == "AI应用开发工程师"


def test_ranker_preserves_input_order_and_count() -> None:
    candidates = [
        NormalizedJobCandidate(title=f"role-{i}") for i in range(5)
    ]
    ranked = RelevanceRanker(_FailingLLM()).rank(
        candidates, profile_summary={}, preferences={}
    )
    assert [r.index for r in ranked] == [0, 1, 2, 3, 4]
