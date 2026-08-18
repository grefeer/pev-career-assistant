"""Regression tests for business-neutral PEV prompt rules and handoff guards."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from backend.app.domain.agent_runtime import AgentRole, ComplexityLevel
from backend.app.services.agent_runtime.executor_agent import (
    ExecutorAgent,
    _EXECUTOR_INSTRUCTION,
)
from backend.app.services.agent_runtime.skill_definition import SkillRegistry
from tests.unit.deepagents_testkit import DeepGateway, scripted_executor_model
from backend.app.services.agent_runtime.error_policy import (
    FailureClass,
    build_terminal_contract,
)
from backend.app.services.agent_runtime.planner_agent import (
    PlannerAgent,
    _PLANNER_INSTRUCTION,
)
from backend.app.services.agent_runtime.schemas import (
    AgentTaskRequest,
    ExecutionPlan,
    PlanStep,
)
from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.agent_runtime.tool_registry import ToolDefinition, ToolRegistry


class EmptyInput(BaseModel):
    pass


class EmptyOutput(BaseModel):
    value: str = "ok"


class ScriptedGateway:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.states: list[dict[str, Any]] = []

    def decide(self, *, role, instruction, state, response_model):
        self.states.append(state)
        assert instruction
        return response_model.model_validate(self.responses.pop(0))


def _registry(role: AgentRole) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name=f"tool-{role.value}",
            skill_name="generic-skill",
            input_model=EmptyInput,
            output_model=EmptyOutput,
            allowed_roles=frozenset({
                AgentRole.planner,
                AgentRole.executor,
                AgentRole.verifier,
            }),
            handler=lambda _context, _payload: {"value": "ok"},
        )
    )
    return registry


def _task() -> AgentTaskRequest:
    return AgentTaskRequest(goal="完成当前步骤", allowed_skills=["generic-skill"])


def _plan(task: AgentTaskRequest) -> ExecutionPlan:
    return ExecutionPlan(
        task=task,
        created_by=AgentRole.planner,
        complexity=ComplexityLevel.L1,
        success_criteria=["产生可验证产出"],
        steps=[
            PlanStep(
                step_id="step-1",
                objective="完成当前步骤",
                allowed_skills=["generic-skill"],
            )
        ],
    )


def test_runtime_prompts_contain_decision_table_without_domain_workflow() -> None:
    for prompt in (_PLANNER_INSTRUCTION, _EXECUTOR_INSTRUCTION):
        assert "通用运行时硬规则" in prompt
        assert "progress_ledger" in prompt
        assert "来源、搜索" not in prompt
        assert "岗位" not in prompt

    assert "只有在没有任何允许路径" in _PLANNER_INSTRUCTION
    assert "工具成功不等于步骤完成" in _EXECUTOR_INSTRUCTION


def test_planner_need_user_is_not_mislabeled_as_invalid_model_output() -> None:
    contract = build_terminal_contract(
        error_code="need_user",
        source_role="planner",
        phase="planning",
    )

    assert contract.failure_class is FailureClass.MODEL_DECISION
    assert contract.reason_code == "need_user"


def test_planner_corrects_premature_need_user_when_a_tool_is_available() -> None:
    gateway = ScriptedGateway(
        [
            {"action": "need_user", "user_question": "请提供信息"},
            {
                "action": "plan",
                "complexity": "L1",
                "success_criteria": ["产生可验证产出"],
                "steps": [
                    {
                        "step_id": "step-1",
                        "objective": "完成当前步骤",
                        "allowed_skills": ["generic-skill"],
                    }
                ],
            },
        ]
    )
    result = PlannerAgent(gateway=gateway, tools=_registry(AgentRole.planner)).run(
        task=_task(),
        context=ToolContext(user_id="u", run_id="r"),
    )

    assert result.status == "planned"
    assert "runtime_feedback" in gateway.states[1]
    assert gateway.states[0]["available_executor_tools"][0]["name"] == "tool-planner"


def test_executor_honors_a_model_need_user_terminal() -> None:
    """The Deep executor trusts the model's terminal decision: a scripted
    need_user hands the step to the human directly (the legacy loop's
    premature-need-user correction is not part of the Deep path)."""
    # The Deep executor requires an existing skill package directory, so the
    # generic-skill fixture is not usable here; job-discovery owns the tool.
    task = AgentTaskRequest(goal="完成当前步骤", allowed_skills=["job-discovery"])
    plan = ExecutionPlan(
        task=task,
        created_by=AgentRole.planner,
        complexity=ComplexityLevel.L1,
        success_criteria=["完成"],
        steps=[PlanStep(step_id="step-1", objective="完成", allowed_skills=["job-discovery"])],
    )
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="tool-executor",
            skill_name="job-discovery",
            input_model=EmptyInput,
            output_model=EmptyOutput,
            allowed_roles=frozenset({AgentRole.executor}),
            handler=lambda _context, _payload: {"value": "ok"},
        )
    )
    gateway = DeepGateway(
        scripted_executor_model(
            [
                {"action": "need_user", "user_question": "请提供信息"},
            ]
        )
    )
    result = ExecutorAgent(
        gateway=gateway, tools=registry, skills=SkillRegistry()
    ).run(
        task=task,
        plan=plan,
        step=plan.steps[0],
        context=ToolContext(user_id="u", run_id="r"),
    )

    assert result.status == "needs_user"
    assert result.user_question == "请提供信息"


