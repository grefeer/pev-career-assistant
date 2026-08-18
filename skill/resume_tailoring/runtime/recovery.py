"""Deterministic resume-tailoring deliverable recovery for the PEV runtime."""

from __future__ import annotations

from typing import Any, Protocol

from backend.app.services.agent_runtime.schemas import ExecutorResult, ToolObservation
from backend.app.services.career_skills.tailoring_keywords import (
    goal_role_keywords,
    tailoring_keywords,
)


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

    def matching_report_target_artifact_id(self) -> str | None: ...

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


def build_tailoring_deliverable(
    ctx: DeliverableRecoveryContext,
    artifact_refs: list[dict[str, Any]],
) -> tuple[list[ToolObservation], list[dict[str, str]]]:
    """Finish a fact-grounded tailoring brief from trusted candidates."""
    if any(ref.get("artifact_type") == "resume_tailoring_brief" for ref in artifact_refs):
        return [], []
    keywords = goal_role_keywords(ctx.task_goal)
    candidates = ctx.structured_job_candidates()
    usable_candidates = [
        candidate
        for candidate in candidates
        if candidate.get("source_quality") not in {"list_only", "js_shell", "empty"}
    ]
    if not usable_candidates:
        # A chained link can inherit complete public pages without their
        # prior run's structured-candidate rows. If matching legitimately
        # selected one of those raw pages, rehydrate that exact persisted
        # page as the deterministic tailoring candidate instead of asking
        # the model to invent a cross-run candidate id.
        raw_report_target_id = ctx.matching_report_target_artifact_id()
        if raw_report_target_id:
            for artifact in ctx.list_evidence_artifacts():
                if (
                    artifact.id != raw_report_target_id
                    or artifact.artifact_type != "public_job_page"
                    or artifact.content_json.get("quality") != "jd_complete"
                ):
                    continue
                visible_text = artifact.content_json.get("visible_text")
                if not isinstance(visible_text, str) or not visible_text.strip():
                    continue
                raw_title = artifact.content_json.get("title")
                usable_candidates = [
                    {
                        "artifact_id": artifact.id,
                        "candidate_id": None,
                        "source_artifact_id": artifact.id,
                        "source_url": artifact.source_url,
                        "page_source_url": artifact.source_url,
                        "source_quality": "jd_complete",
                        "title": raw_title if isinstance(raw_title, str) else None,
                        "responsibilities": visible_text,
                        "requirements": "",
                        "full_text": visible_text,
                        "locations": [],
                        "recruitment_types": [],
                        "skills": [],
                    }
                ]
                break
    # Tailoring must use the same deterministic role, city, experience and
    # graduate-scope filter as matching. Otherwise the first body-backed JD
    # can silently become a wrong-target brief.
    from skill.job_matching.runtime.job_matching import (
        _candidate_meets_goal_constraints,
        _source_allowed_for_goal,
    )

    usable_candidates = [
        candidate
        for candidate in usable_candidates
        if _candidate_meets_goal_constraints(
            candidate,
            ctx.task_goal,
            ctx.task_private_context.get("confirmed_profile_facts"),
        )
        and _source_allowed_for_goal(
            candidate.get("page_source_url") or candidate.get("source_url"),
            ctx.task_goal,
            evidence_text="\n".join(
                str(candidate.get(key) or "")
                for key in (
                    "title",
                    "responsibilities",
                    "requirements",
                    "full_text",
                    "page_text_prefix",
                )
            ),
        )
    ]
    ranked: list[tuple[int, dict[str, Any]]] = []
    for candidate in usable_candidates:
        searchable = "\n".join(
            str(candidate.get(key) or "")
            for key in ("title", "company_name", "responsibilities", "requirements")
        ).lower()
        score = sum(1 for keyword in keywords if keyword.lower() in searchable)
        if score:
            ranked.append((score, candidate))
    if not ranked:
        # A chain step may describe only "the selected job" and therefore
        # contain no role keyword. Prefer a real, body-backed JD over a
        # recommendation card so the tailoring tool receives resolvable
        # target evidence even before the matching-report projection is
        # available.
        ranked = [
            (1, candidate)
            for candidate in usable_candidates
            if (
                isinstance(candidate.get("title"), str)
                and (
                    isinstance(candidate.get("responsibilities"), str)
                    and candidate.get("responsibilities").strip()
                    or isinstance(candidate.get("requirements"), str)
                    and candidate.get("requirements").strip()
                )
            )
        ]
    if not ranked or not ctx.consume_tool_budget():
        return [], []
    ranked.sort(
        key=lambda item: (
            -item[0],
            str(item[1].get("title") or ""),
            str(item[1].get("artifact_id") or ""),
        )
    )
    selected = ranked[0][1]
    artifact_id = selected.get("artifact_id")
    if not isinstance(artifact_id, str) or not artifact_id:
        return [], []
    target_keywords = [
        keyword
        for keyword in keywords
        if keyword.lower()
        in "\n".join(
            str(selected.get(key) or "")
            for key in ("title", "responsibilities", "requirements")
        ).lower()
    ] or ["岗位"]
    tailoring_target_id: str | None = None
    report_target_id = ctx.matching_report_target_artifact_id()
    if report_target_id:
        tailoring_target_id = report_target_id
        report_candidate = next(
            (
                candidate
                for candidate in usable_candidates
                if report_target_id
                in {
                    candidate.get("candidate_id"),
                    candidate.get("artifact_id"),
                    candidate.get("source_artifact_id"),
                }
            ),
            None,
        )
        if report_candidate is not None:
            selected = report_candidate
            artifact_id = report_target_id
            target_keywords = tailoring_keywords(
                ctx.task_goal,
                ctx.task_private_context.get("confirmed_profile_facts", {}),
                report_candidate,
            )
        else:
            report_facts = ctx.task_private_context.get(
                "confirmed_profile_facts", {}
            )
            if isinstance(report_facts, dict):
                for key, value in report_facts.items():
                    if "name" in str(key).lower() and isinstance(value, str):
                        target_keywords = goal_role_keywords(value)
                        break
    payload: dict[str, Any] = {
        "target_artifact_id": tailoring_target_id
        or selected.get("candidate_id")
        or selected.get("artifact_id"),
    }
    payload["target_keywords"] = target_keywords
    observation = ctx.invoke_tool("build-resume-tailoring-brief", payload)
    if observation.status != "succeeded":
        return [observation], []
    execution = ExecutorResult(
        status="succeeded",
        observations=[observation],
        summary="已基于目标角色匹配的公开 JD 生成职业交付物。",
    )
    refs = ctx.persist(execution, mark="runtime_auto_deliverable")
    return [observation], refs
