"""Thin Executor dispatcher for the adaptive PEV runtime.

Stage 1.2: the legacy non-Deep loop and its module-level helpers were
removed. The production Executor is ``agent_runtime.executor.deep_executor``
(DeepAgents tool-calling loop); this module keeps the historical
``ExecutorAgent`` surface used by the runtime, ``main``, and the
integration/eval suites, plus the shared executor prompt contract.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.app.domain.agent_runtime import AgentRole
from backend.app.services.agent_runtime.model_budget import ModelCallBudget
from backend.app.services.agent_runtime.model_gateway import AgentModelGateway
from backend.app.services.agent_runtime.prompt_rules import (
    COMMON_RUNTIME_RULES,
    EXECUTOR_RUNTIME_RULES,
)
from backend.app.services.agent_runtime.schemas import (
    AgentTaskRequest,
    ExecutionPlan,
    ExecutorResult,
    PlanStep,
    ToolObservation,
)
from backend.app.services.agent_runtime.skill_definition import SkillRegistry
from backend.app.services.agent_runtime.tool_budget import ToolCallBudget
from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.agent_runtime.tool_registry import ToolRegistry
from backend.app.services.agent_runtime.tracing import DecisionTrace
from backend.app.services.agent_runtime.turn_budget import AgentTurnBudget

#: Generic runtime prompt. Career-specific capture, matching, tailoring, and
#: planning rules live in canonical ``skill/*/SKILL.md`` packages.
_EXECUTOR_INSTRUCTION = (
    "## 角色\n"
    "You are the Executor role in a generic Planner-Executor-Verifier runtime.\n"
    "## 行为规则\n"
    "Work only toward the current step, use only advertised tools and Skill "
    "authority, inspect every observation, reuse successful calls, do not repeat a "
    "doomed call, honor typed step inputs and prior artifacts, and use confirmed "
    "private context when the activated Skill exposes it instead of asking the user "
    "to repeat server-held facts.\n"
    "## 流程\n"
    "Never claim an artifact absent from tool-backed observations. If evidence is "
    "blocked or a required input is unavailable, return a precise needs_user handoff.\n"
    "## 输出契约\n"
    "Choose complete only when the activated Skill contract and the step success "
    "criteria are satisfied.\n"
    "## 禁止项\n"
    "Do not invent evidence, bypass access controls, or perform an irreversible "
    "external action."
    "\n\n"
    + COMMON_RUNTIME_RULES
    + EXECUTOR_RUNTIME_RULES
)


class ExecutorAgent:
    """Bounded perceive–decide–act–observe adapter for a single plan step."""

    def __init__(
        self,
        *,
        gateway: AgentModelGateway,
        tools: ToolRegistry,
        skills: SkillRegistry | None = None,
    ) -> None:
        self._gateway = gateway
        self._tools = tools
        self._skills = skills

    def invoke_registered_tool(
        self, *, name: str, context: ToolContext, payload: dict[str, Any]
    ) -> ToolObservation:
        """Invoke one deterministic Executor tool for runtime normalization."""
        return self._tools.invoke(
            role=AgentRole.executor,
            name=name,
            context=context,
            payload=payload,
        )

    def has_registered_tool(self, name: str) -> bool:
        """Report whether a runtime normalization tool is actually available."""
        return any(
            definition.name == name and AgentRole.executor in definition.allowed_roles
            for definition in self._tools.definitions
        )

    def run(
        self,
        *,
        task: AgentTaskRequest,
        plan: ExecutionPlan,
        step: PlanStep,
        context: ToolContext,
        trace: DecisionTrace | None = None,
        tool_budget: ToolCallBudget | None = None,
        turn_budget: AgentTurnBudget | None = None,
        model_budget: ModelCallBudget | None = None,
        deadline: float | None = None,
        prior_observations: list[ToolObservation] | None = None,
    ) -> ExecutorResult:
        """Execute a step through the production DeepAgents executor."""
        from backend.app.services.agent_runtime.executor.deep_executor import (
            DeepExecutorAgent,
        )

        return DeepExecutorAgent(
            gateway=self._gateway,
            tools=self._tools,
            skills=self._skills,
            skill_root=Path(__file__).resolve().parents[4] / "skill",
        ).run(
            task=task,
            plan=plan,
            step=step,
            context=context,
            trace=trace,
            tool_budget=tool_budget,
            turn_budget=turn_budget,
            model_budget=model_budget,
            deadline=deadline,
            prior_observations=prior_observations,
        )
