"""Regression tests for typed decisions, replans, dependencies, and model budgets."""

from __future__ import annotations

import pytest

from backend.app.services.agent_runtime.model_budget import ModelCallBudget
from backend.app.services.agent_runtime.runtime import (
    AgentRuntime,
    StepDependencyError,
    _task_input_value,
)
from backend.app.services.agent_runtime.schemas import (
    AgentTaskRequest,
    ExecutorDecision,
    PlanStep,
    PlannerDecision,
    ReplanReason,
    StepInputRef,
    StepOutputRef,
    VerifierDecision,
)
from backend.app.services.agent_runtime.tool_context import ToolContext


def test_role_decisions_publish_real_discriminators() -> None:
    for model in (PlannerDecision, ExecutorDecision, VerifierDecision):
        schema = model.model_json_schema()
        assert schema["discriminator"]["propertyName"] == "action"
        assert len(schema["oneOf"]) >= 2

    assert PlannerDecision.model_validate({"action": "need_user", "user_question": "确认"}).action == "need_user"
    with pytest.raises(Exception):
        PlannerDecision.model_validate({"action": "plan"})


def test_replan_state_is_typed_and_separate_from_feedback() -> None:
    task = AgentTaskRequest(
        goal="workflow",
        allowed_skills=["one"],
        context={
            "verifier_feedback": ["human-readable feedback"],
            "replan_state": {
                "count": 1,
                "conversion_reasons": ["need_user_contract"],
            },
        },
    )
    assert task.replan_state.count == 1
    assert task.replan_state.conversion_used(ReplanReason.NEED_USER_CONTRACT)
    assert not task.replan_state.conversion_used(ReplanReason.RETRY_CONTRACT_EXHAUSTED)


def test_structured_step_inputs_resolve_context_and_prior_artifacts() -> None:
    task = AgentTaskRequest(
        goal="workflow",
        allowed_skills=["one", "two"],
        context={"candidate_urls": ["https://example.test/job"]},
    )
    step = PlanStep(
        step_id="two",
        objective="consume",
        allowed_skills=["two"],
        depends_on=["one"],
        inputs=[
            StepInputRef(kind="context", name="candidate_urls"),
            StepInputRef(
                kind="artifact",
                name="jd",
                from_step="one",
                artifact_type="public_job_page",
            ),
        ],
        outputs=[StepOutputRef(name="report")],
    )
    context = ToolContext(user_id="u", run_id="r")
    step_task, step_context = AgentRuntime._prepare_step_inputs(
        task=task,
        context=context,
        plan_step=step,
        step_outputs={
            "one": [
                {
                    "artifact_id": "a1",
                    "artifact_type": "public_job_page",
                    "source_url": "https://example.test/job",
                    "content_hash": "h1",
                    "output_name": "jd",
                }
            ]
        },
    )
    assert step_task.context["resolved_step_inputs"]["context"]["candidate_urls"]
    assert step_context.metadata["resolved_step_inputs"]["artifacts"]["jd"][0]["artifact_id"] == "a1"

    with pytest.raises(StepDependencyError):
        AgentRuntime._prepare_step_inputs(
            task=task,
            context=context,
            plan_step=step,
            step_outputs={},
        )


def test_artifact_input_rejects_a_same_named_output_with_the_wrong_type() -> None:
    task = AgentTaskRequest(goal="match", allowed_skills=["job-matching"])
    step = PlanStep(
        step_id="match",
        objective="match",
        allowed_skills=["job-matching"],
        depends_on=["discover"],
        inputs=[
            StepInputRef(
                kind="artifact",
                name="jd",
                from_step="discover",
                artifact_type="structured_job_details",
            )
        ],
    )

    with pytest.raises(StepDependencyError, match="cannot resolve artifact"):
        AgentRuntime._prepare_step_inputs(
            task=task,
            context=ToolContext(user_id="u", run_id="r"),
            plan_step=step,
            step_outputs={
                "discover": [
                    {
                        "artifact_id": "page-1",
                        "artifact_type": "public_job_page",
                        "output_name": "jd",
                    }
                ]
            },
        )


def test_model_budget_is_a_hard_physical_ceiling() -> None:
    budget = ModelCallBudget(2, 100, 40)
    assert budget.try_reserve(60)
    assert budget.record({"input_tokens": 60, "output_tokens": 20})
    assert budget.try_reserve(40)
    assert not budget.record({"input_tokens": 40, "output_tokens": 21})
    assert budget.requests_used == 2
    assert budget.output_tokens_used == 41
    assert not budget.try_reserve(1)


def test_typed_context_input_can_reference_task_goal() -> None:
    task = AgentTaskRequest(goal="find a role", allowed_skills=["job-discovery"])

    assert _task_input_value(task, "goal") == "find a role"
    assert _task_input_value(task, "context.goal") is None


def test_typed_inputs_resolve_derived_and_private_profile_context_without_leaking_it() -> None:
    task = AgentTaskRequest(
        goal="tailor",
        allowed_skills=["resume-tailoring"],
        private_context={"confirmed_profile_facts": {"skills": ["Python"]}},
    )
    step = PlanStep(
        step_id="tailor",
        objective="tailor",
        allowed_skills=["resume-tailoring"],
        inputs=[
            StepInputRef(kind="context", name="confirmed_profile_fact_fields"),
            StepInputRef(kind="context", name="confirmed_profile_facts"),
        ],
    )

    step_task, _ = AgentRuntime._prepare_step_inputs(
        task=task,
        context=ToolContext(user_id="u", run_id="r"),
        plan_step=step,
        step_outputs={},
    )

    resolved = step_task.context["resolved_step_inputs"]
    assert resolved["context"]["confirmed_profile_fact_fields"] == ["skills"]
    assert "confirmed_profile_facts" in resolved["private_inputs"]
    assert "confirmed_profile_facts" not in resolved["context"]
