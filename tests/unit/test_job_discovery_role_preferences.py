from __future__ import annotations

from backend.app.services.job_discovery.role_preferences import (
    DEFAULT_ROLE_PREFERENCES,
    filter_candidates_for_preferences,
)
from backend.app.services.job_discovery.schemas import NormalizedJobCandidate


def test_default_preferences_target_ai_application_and_agent_development() -> None:
    candidates = [
        NormalizedJobCandidate(title="AI应用开发工程师", responsibilities="开发 AI 应用"),
        NormalizedJobCandidate(title="Agent 开发工程师", responsibilities="构建智能体平台"),
        NormalizedJobCandidate(title="财务管培生", responsibilities="财务分析"),
    ]

    matched = filter_candidates_for_preferences(candidates, DEFAULT_ROLE_PREFERENCES)

    assert [candidate.title for candidate in matched] == ["AI应用开发工程师", "Agent 开发工程师"]


def test_preference_does_not_match_an_unrelated_title_only_because_jd_mentions_agent() -> None:
    matched = filter_candidates_for_preferences(
        [NormalizedJobCandidate(
            title="系统评测工程师", responsibilities="参与 Agent 平台的系统评测",
        )],
        DEFAULT_ROLE_PREFERENCES,
    )

    assert matched == []


def test_ai_product_manager_preference_inverts_against_dev() -> None:
    """AI产品经理 must KEEP a product role and FILTER a dev role.

    This is the genericity proof: the same filter that keeps dev roles for an
    ``AI应用开发`` preference keeps PRODUCT roles for an ``AI产品经理``
    preference and filters dev roles - because markers are derived from the
    preference, not hardcoded toward development.
    """
    candidates = [
        NormalizedJobCandidate(title="AI产品经理", responsibilities="负责 AI 产品的规划与落地"),
        NormalizedJobCandidate(title="AI应用开发工程师", responsibilities="开发 AI 应用"),
        NormalizedJobCandidate(title="后端产品经理", responsibilities="后端业务产品规划"),
    ]

    matched = filter_candidates_for_preferences(candidates, ["AI产品经理"])

    assert [candidate.title for candidate in matched] == ["AI产品经理"]


def test_preference_works_outside_ai_dev_without_hardcoded_tokens() -> None:
    """A non-AI-dev preference (芯片设计工程师) keeps a chip-design role and
    filters an unrelated dev role - proving no AI-dev keep-list is baked in."""
    candidates = [
        NormalizedJobCandidate(title="芯片设计工程师", responsibilities="负责芯片前端设计"),
        NormalizedJobCandidate(title="AI应用开发工程师", responsibilities="开发 AI 应用"),
    ]

    matched = filter_candidates_for_preferences(candidates, ["芯片设计工程师"])

    assert [candidate.title for candidate in matched] == ["芯片设计工程师"]
