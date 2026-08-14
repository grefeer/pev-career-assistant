"""Regression tests for business-neutral PEV prompt rules and handoff guards."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from backend.app.domain.agent_runtime import AgentRole, ComplexityLevel
from backend.app.services.agent_runtime.executor_agent import (
    ExecutorAgent,
    _EXECUTOR_INSTRUCTION,
)
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
    ExecutorResult,
    PlanStep,
)
from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.agent_runtime.tool_registry import ToolDefinition, ToolRegistry
from backend.app.services.agent_runtime.verifier_agent import (
    VerifierAgent,
    _VERIFIER_INSTRUCTION,
)


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
    for prompt in (_PLANNER_INSTRUCTION, _EXECUTOR_INSTRUCTION, _VERIFIER_INSTRUCTION):
        assert "通用运行时硬规则" in prompt
        assert "progress_ledger" in prompt
        assert "来源、搜索" not in prompt
        assert "岗位" not in prompt

    assert "只有在没有任何允许路径" in _PLANNER_INSTRUCTION
    assert "工具成功不等于步骤完成" in _EXECUTOR_INSTRUCTION
    assert "RETRY_EXECUTOR 的前提" in _VERIFIER_INSTRUCTION
    assert "存在性问题" in _VERIFIER_INSTRUCTION
    assert "真实负结论" in _VERIFIER_INSTRUCTION


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


def test_executor_corrects_premature_need_user_when_no_terminal_block_exists() -> None:
    task = _task()
    plan = _plan(task)
    gateway = ScriptedGateway(
        [
            {"action": "need_user", "user_question": "请提供信息"},
            {"action": "complete", "summary": "已完成"},
        ]
    )
    result = ExecutorAgent(gateway=gateway, tools=_registry(AgentRole.executor)).run(
        task=task,
        plan=plan,
        step=plan.steps[0],
        context=ToolContext(user_id="u", run_id="r"),
    )

    assert result.status == "succeeded"
    assert "runtime_feedback" in gateway.states[1]


def test_verifier_corrects_premature_need_user_when_verifier_tool_remains() -> None:
    task = _task()
    plan = _plan(task)
    gateway = ScriptedGateway(
        [
            {"action": "decide", "verification_decision": "NEED_USER", "feedback": "补充"},
            {"action": "decide", "verification_decision": "NEED_USER", "feedback": "仍缺失"},
        ]
    )
    result = VerifierAgent(gateway=gateway, tools=_registry(AgentRole.verifier)).run(
        task=task,
        plan=plan,
        step=plan.steps[0],
        execution=ExecutorResult(status="succeeded", summary="已完成"),
        context=ToolContext(user_id="u", run_id="r"),
    )

    assert result.decision.value == "NEED_USER"
    assert "runtime_feedback" in gateway.states[1]
