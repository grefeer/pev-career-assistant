"""Deterministic job-matching deliverable recovery for the PEV runtime."""

from __future__ import annotations

from typing import Any, Protocol

from backend.app.services.agent_runtime.schemas import ExecutorResult, ToolObservation
from backend.app.services.career_skills.tailoring_keywords import goal_role_keywords


class DeliverableRecoveryContext(Protocol):
    """Runtime-owned capabilities injected into deliverable recovery."""

    @property
    def user_id(self) -> str: ...

    @property
    def run_id(self) -> str: ...

    @property
    def task_goal(self) -> str: ...

    @property
    def task_context(self) -> dict[str, Any]: ...

    @property
    def task_private_context(self) -> dict[str, Any]: ...

    def structured_job_candidates(self) -> list[dict[str, Any]]: ...

    def list_evidence_artifacts(self) -> list[Any]: ...

    def invoke_tool(
        self,
        name: str,
        payload: dict[str, Any],
        *,
        metadata: dict[str, Any] | None = None,
    ) -> ToolObservation: ...

    def persist(
        self, execution: ExecutorResult, *, mark: str | None = None
    ) -> list[dict[str, str]]: ...

    def consume_tool_budget(self) -> bool: ...

    def append_event(self, event_type: str, payload: dict[str, Any]) -> None: ...


def build_matching_deliverable(
    ctx: DeliverableRecoveryContext,
    artifact_refs: list[dict[str, Any]],
) -> tuple[list[ToolObservation], list[dict[str, str]]]:
    """Finish a transparent match report from trusted candidates."""
    if any(ref.get("artifact_type") == "job_matching_report" for ref in artifact_refs):
        return [], []
    keywords = goal_role_keywords(ctx.task_goal)
    candidates = ctx.structured_job_candidates()
    usable_candidates = [
        candidate
        for candidate in candidates
        if candidate.get("source_quality") not in {"list_only", "js_shell", "empty"}
    ]
    # Matching consumes the full trusted candidate projection. Do not
    # pre-filter it by title: the deterministic matcher owns role, location,
    # and experience constraints and must explain exclusions.
    if not usable_candidates:
        # A chained matching step may inherit only public page refs (the
        # prior step did not persist structured extraction). The matching
        # tool has a bounded raw-page fallback, so one complete page is
        # enough to invoke it without fabricating a structured candidate.
        usable_candidates = [
            {"artifact_id": artifact.id}
            for artifact in ctx.list_evidence_artifacts()
            if artifact.artifact_type == "public_job_page"
            and artifact.content_json.get("quality") == "jd_complete"
        ]
    if not usable_candidates or not ctx.consume_tool_budget():
        return [], []
    profile_facts = ctx.task_private_context.get("confirmed_profile_facts", {})
    profile_keywords = list(keywords)
    if isinstance(profile_facts, dict):
        skills = profile_facts.get("skills")
        if isinstance(skills, list):
            profile_keywords.extend(
                skill for skill in skills if isinstance(skill, str)
            )
        for key, value in profile_facts.items():
            if "name" in str(key).lower() and isinstance(value, str):
                profile_keywords.extend(goal_role_keywords(value))
    profile_keywords.extend(
        value
        for value in (
            ctx.task_context.get("role_keywords", [])
            if isinstance(ctx.task_context.get("role_keywords", []), list)
            else []
        )
        if isinstance(value, str)
    )
    preferred_locations = ctx.task_context.get("location_keywords", [])
    if not isinstance(preferred_locations, list):
        preferred_locations = []
    payload = {
        "profile_keywords": list(dict.fromkeys(profile_keywords))[:30],
        "preferred_locations": [
            value for value in preferred_locations if isinstance(value, str)
        ][:20],
        "ranking_criteria": ["skills", "location", "recency"],
        "limit": 100,
    }
    observation = ctx.invoke_tool("match-observed-jobs", payload)
    if observation.status != "succeeded":
        return [observation], []
    execution = ExecutorResult(
        status="succeeded",
        observations=[observation],
        summary="已基于目标角色匹配的公开 JD 生成职业交付物。",
    )
    refs = ctx.persist(execution, mark="runtime_auto_deliverable")
    return [observation], refs

def matching_report_target_artifact_id(
    artifacts: list[Any], structured_candidates: list[dict[str, Any]]
) -> str | None:
    """Resolve the top match to the exact artifact expected by tailoring.

    A chained tailoring step may reference the matching report's top target;
    this deterministic lookup returns the exact persisted candidate/page id
    instead of asking the model to invent a cross-run identifier.
    """
    for artifact in artifacts:
        if artifact.artifact_type != "job_matching_report":
            continue
        matches = artifact.content_json.get("matches")
        if not isinstance(matches, list) or not matches:
            continue
        top = matches[0]
        if not isinstance(top, dict):
            continue
        candidate_ids = [
            top.get("candidate_id"),
            top.get("artifact_id"),
            top.get("source_artifact_id"),
        ]
        source_url = top.get("source_url")
        structured_candidate_ids = {
            candidate.get("candidate_id")
            for candidate in structured_candidates
            if isinstance(candidate.get("candidate_id"), str)
        }
        for candidate_id in candidate_ids:
            if not isinstance(candidate_id, str):
                continue
            if candidate_id in structured_candidate_ids:
                return candidate_id
            if any(artifact.id == candidate_id for artifact in artifacts):
                return candidate_id
        if isinstance(source_url, str) and source_url:
            for artifact in artifacts:
                if artifact.source_url == source_url and artifact.artifact_type in {
                    "public_job_page",
                    "structured_job_details",
                }:
                    return artifact.id
    return None

