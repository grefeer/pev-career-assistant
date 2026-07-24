"""Validated PATH C CrawlPlan generation and repair helpers."""

from __future__ import annotations

import json
from dataclasses import asdict
from enum import Enum
from typing import Any

import yaml
from langchain_core.messages import HumanMessage

from backend.app.config import Settings
from backend.app.services.job_discovery.crawling.crawl_plan import CrawlPlan
from backend.app.services.job_discovery.schemas import DiscoveryTaskInput


class PlanningContractError(ValueError):
    """The planning agent returned something other than a safe CrawlPlan."""


class PlanningBudgetExceeded(ValueError):
    """The requested planning inspection budget exceeds the configured limit."""


_FORBIDDEN_FINAL_RESULT_KEYS = frozenset(
    {
        "candidates",
        "candidate",
        "discovered_jobs",
        "evidence",
        "coverage",
        "coverage_complete",
        "jobs",
    }
)
DEFAULT_MAX_INSPECTION_PAGES = 3


def generate_crawl_plan(
    task: DiscoveryTaskInput,
    agent: Any,
    max_inspection_pages: int = DEFAULT_MAX_INSPECTION_PAGES,
    *,
    settings: Settings | None = None,
) -> CrawlPlan:
    """Ask PATH C for a validated plan without allowing final job output."""
    return _generate_plan(
        task,
        agent,
        mode="generate",
        max_inspection_pages=max_inspection_pages,
        settings=settings,
    )


def repair_crawl_plan(
    task: DiscoveryTaskInput,
    failed_plan: CrawlPlan,
    snapshot_context: dict[str, Any],
    agent: Any,
    max_inspection_pages: int = DEFAULT_MAX_INSPECTION_PAGES,
    *,
    settings: Settings | None = None,
) -> CrawlPlan:
    """Ask PATH C to repair a failed plan using only execution context."""
    return _generate_plan(
        task,
        agent,
        mode="repair",
        max_inspection_pages=max_inspection_pages,
        settings=settings,
        failed_plan=failed_plan,
        snapshot_context=snapshot_context,
    )


def _generate_plan(
    task: DiscoveryTaskInput,
    agent: Any,
    *,
    mode: str,
    max_inspection_pages: int,
    settings: Settings | None,
    failed_plan: CrawlPlan | None = None,
    snapshot_context: dict[str, Any] | None = None,
) -> CrawlPlan:
    _validate_budget(max_inspection_pages, settings)

    # Import lazily so the planner contract does not create an import cycle
    # with the DeepAgent builder that supplies the inspection tools.
    from backend.app.services.job_discovery.deepagents_runner import _reset_nav_state

    _reset_nav_state(max_pages=max_inspection_pages)
    payload: dict[str, Any] = {
        "mode": mode,
        "task": asdict(task),
        "max_inspection_pages": max_inspection_pages,
    }
    if failed_plan is not None:
        payload["failed_plan"] = _crawl_plan_payload(failed_plan)
    if snapshot_context is not None:
        payload["snapshot_context"] = snapshot_context

    result = agent.invoke(
        {"messages": [HumanMessage(content=json.dumps(payload, ensure_ascii=False))]},
        config={"recursion_limit": 20},
    )
    plan_yaml = _extract_plan_yaml(result)
    _reject_final_result_payload(plan_yaml)
    try:
        return CrawlPlan.from_yaml(plan_yaml)
    except (KeyError, TypeError, ValueError, yaml.YAMLError) as exc:
        raise PlanningContractError(f"Invalid CrawlPlan: {exc}") from exc


def _validate_budget(max_inspection_pages: int, settings: Settings | None) -> None:
    if max_inspection_pages < 1:
        raise PlanningBudgetExceeded("inspection-page budget must be positive")
    configured_limit = (
        settings.job_discovery_planner_max_inspection_pages
        if settings is not None
        else DEFAULT_MAX_INSPECTION_PAGES
    )
    if max_inspection_pages > configured_limit:
        raise PlanningBudgetExceeded(
            "inspection-page budget exceeds configured planner limit"
        )


def _extract_plan_yaml(result: Any) -> str:
    payload = result.get("structured_response") if isinstance(result, dict) else result
    if hasattr(payload, "model_dump"):
        payload = payload.model_dump()
    elif hasattr(payload, "dict"):
        payload = payload.dict()

    if isinstance(payload, dict):
        forbidden = _find_forbidden_key(payload)
        if forbidden is not None:
            raise PlanningContractError(
                f"Planner output must not contain final-result key: {forbidden}"
            )
        if "plan_yaml" in payload:
            plan_yaml = payload["plan_yaml"]
        elif "crawl_plan_yaml" in payload:
            plan_yaml = payload["crawl_plan_yaml"]
        elif payload.get("status") == "needs_manual_review":
            raise PlanningContractError(
                f"Planner requested manual review: {payload.get('block_reason', 'unknown')}"
            )
        else:
            raise PlanningContractError("Planner response must include plan_yaml")
    elif isinstance(payload, str):
        plan_yaml = payload
    else:
        raise PlanningContractError("Planner response must be YAML or a plan_yaml mapping")

    if not isinstance(plan_yaml, str):
        raise PlanningContractError("Planner plan_yaml must be a string")
    return plan_yaml


def _reject_final_result_payload(plan_yaml: str) -> None:
    try:
        raw = yaml.safe_load(plan_yaml)
    except yaml.YAMLError as exc:
        raise PlanningContractError(f"Invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise PlanningContractError("CrawlPlan must be a YAML mapping")

    forbidden = _find_forbidden_key(raw)
    if forbidden is not None:
        raise PlanningContractError(
            f"Planner output must not contain final-result key: {forbidden}"
        )


def _find_forbidden_key(value: Any) -> str | None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in _FORBIDDEN_FINAL_RESULT_KEYS:
                return str(key)
            forbidden = _find_forbidden_key(child)
            if forbidden is not None:
                return forbidden
    elif isinstance(value, list):
        for child in value:
            forbidden = _find_forbidden_key(child)
            if forbidden is not None:
                return forbidden
    return None


def _crawl_plan_payload(plan: CrawlPlan) -> dict[str, Any]:
    def _normalize(value: Any) -> Any:
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, dict):
            return {key: _normalize(child) for key, child in value.items()}
        if isinstance(value, list):
            return [_normalize(child) for child in value]
        return value

    return _normalize(asdict(plan))
