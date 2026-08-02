from __future__ import annotations

from dataclasses import asdict
from types import SimpleNamespace

import pytest

from backend.app.services.job_discovery.planning.crawl_plan_agent import (
    PlanningBudgetExceeded,
    PlanningContractError,
    generate_crawl_plan,
)
from backend.app.services.job_discovery.schemas import DiscoveryTaskInput


CRAWL_PLAN_YAML = """
plan_type: crawl_plan
version: 1
listing:
  item_selector: .job
  title_selector: .title
pagination:
  type: single_page
detail:
  body_selector: .detail
completion: {}
"""


class _PlanningAgent:
    def __init__(self, response: object) -> None:
        self.response = response
        self.inputs: list[dict[str, object]] = []

    def invoke(self, agent_input: dict[str, object], **_: object) -> object:
        self.inputs.append(agent_input)
        return self.response


def _task() -> DiscoveryTaskInput:
    return DiscoveryTaskInput(
        source_id="source",
        raw_record_id="raw",
        external_record_id="external",
        source_key="source",
        source_url="https://jobs.example.test/list",
        url_hash="hash",
        record_fields=[],
    )


def test_generate_crawl_plan_returns_validated_plan_without_candidates() -> None:
    agent = _PlanningAgent({"structured_response": {"plan_yaml": CRAWL_PLAN_YAML}})

    plan = generate_crawl_plan(_task(), agent)

    assert plan.listing.item_selector == ".job"
    assert "candidates" not in asdict(plan)
    assert len(agent.inputs) == 1


def test_generate_crawl_plan_rejects_candidate_payload_before_parsing_plan() -> None:
    agent = _PlanningAgent(
        {
            "structured_response": {
                "plan_yaml": CRAWL_PLAN_YAML + "candidates:\n  - forbidden\n",
            }
        }
    )

    with pytest.raises(PlanningContractError, match="candidates"):
        generate_crawl_plan(_task(), agent)


def test_generate_crawl_plan_rejects_sibling_candidate_payload_before_extracting_plan() -> None:
    agent = _PlanningAgent(
        {
            "structured_response": {
                "plan_yaml": CRAWL_PLAN_YAML,
                "candidates": [{"title": "forbidden"}],
            }
        }
    )

    with pytest.raises(PlanningContractError, match="candidates"):
        generate_crawl_plan(_task(), agent)


def test_generate_crawl_plan_rejects_inspection_budget_above_configured_limit() -> None:
    settings = SimpleNamespace(job_discovery_planner_max_inspection_pages=3)
    agent = _PlanningAgent({"structured_response": {"plan_yaml": CRAWL_PLAN_YAML}})

    with pytest.raises(PlanningBudgetExceeded, match="inspection"):
        generate_crawl_plan(_task(), agent, max_inspection_pages=4, settings=settings)


def test_generate_crawl_plan_enforces_default_inspection_budget_without_settings() -> None:
    agent = _PlanningAgent({"structured_response": {"plan_yaml": CRAWL_PLAN_YAML}})

    with pytest.raises(PlanningBudgetExceeded, match="inspection"):
        generate_crawl_plan(_task(), agent, max_inspection_pages=999)


def test_generate_crawl_plan_converts_structured_manual_review_to_contract_error() -> None:
    agent = _PlanningAgent(
        {
            "structured_response": {
                "status": "needs_manual_review",
                "block_reason": "captcha",
            }
        }
    )

    with pytest.raises(PlanningContractError, match="captcha"):
        generate_crawl_plan(_task(), agent)


def test_planner_builder_uses_only_planning_tools_and_required_prompt(monkeypatch) -> None:
    import backend.app.services.job_discovery.deepagents_runner as runner

    captured: dict[str, object] = {}

    def _fake_create_deep_agent(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(runner, "create_deep_agent", _fake_create_deep_agent)

    runner.build_crawl_plan_agent(
        settings=SimpleNamespace(job_discovery_planner_max_inspection_pages=3),
        model=object(),
    )

    assert {tool.__name__ for tool in captured["tools"]} == {
        "open_rendered_url",
        "read_dom",
        "extract_links",
        "inspect_network_schema",
        "finish_with_manual_review",
    }
    prompt = captured["system_prompt"]
    assert "Your only deliverable is a valid CrawlPlan." in prompt
    assert "Inspect at most 3 pages." in prompt


def test_planner_response_schema_allows_manual_review_outcome(monkeypatch) -> None:
    import backend.app.services.job_discovery.deepagents_runner as runner

    captured: dict[str, object] = {}

    monkeypatch.setattr(
        runner,
        "create_deep_agent",
        lambda **kwargs: captured.update(kwargs) or object(),
    )

    runner.build_crawl_plan_agent(
        settings=SimpleNamespace(job_discovery_planner_max_inspection_pages=3),
        model=object(),
    )

    response_format = captured["response_format"]
    assert response_format.model_fields["plan_yaml"].is_required() is False
    assert response_format.model_fields["status"].default == "crawl_plan"
