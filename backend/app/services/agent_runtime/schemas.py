"""Validated data exchanged between the three autonomous PEV roles.

These schemas intentionally describe decisions and observations rather than a
pre-scripted workflow.  The harness may enforce their bounds, but only the
Agents choose a plan, a permitted Skill, and a verifier outcome.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from backend.app.domain.agent_runtime import (
    AgentRole,
    ComplexityLevel,
    VerificationDecision,
)

_SKILL_NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


class AgentBudget(BaseModel):
    """Hard per-run ceilings enforced by the runtime, not a business decision."""

    model_config = {"extra": "forbid"}

    max_agent_turns: int = Field(default=12, ge=1, le=100)
    max_tool_calls: int = Field(default=24, ge=1, le=200)
    max_replans: int = Field(default=2, ge=0, le=10)


class AgentTaskRequest(BaseModel):
    """User-scoped objective and the explicit Skill authority for one run."""

    model_config = {"extra": "forbid"}

    goal: str = Field(min_length=1, max_length=8_000)
    allowed_skills: list[str] = Field(min_length=1, max_length=16)
    context: dict[str, Any] = Field(default_factory=dict)
    private_context: dict[str, Any] = Field(default_factory=dict, exclude=True)
    budget: AgentBudget = Field(default_factory=AgentBudget)

    @field_validator("goal")
    @classmethod
    def normalize_goal(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("goal must not be empty")
        return cleaned

    @field_validator("allowed_skills")
    @classmethod
    def validate_allowed_skills(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if not cleaned or any(not value for value in cleaned):
            raise ValueError("allowed_skills must contain non-empty Skill names")
        if any(not _SKILL_NAME.fullmatch(value) for value in cleaned):
            raise ValueError("allowed_skills contains an invalid Skill name")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("allowed_skills must be unique")
        return cleaned


class PlanStep(BaseModel):
    """One Planner-created outcome, not a harness-selected tool invocation."""

    model_config = {"extra": "forbid"}

    step_id: str = Field(min_length=1, max_length=80)
    objective: str = Field(min_length=1, max_length=2_000)
    allowed_skills: list[str] = Field(min_length=1, max_length=8)
    success_criteria: list[str] = Field(default_factory=list, max_length=12)
    requires_verification: bool = False

    @field_validator("step_id", "objective")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("plan step fields must not be empty")
        return cleaned

    @field_validator("allowed_skills")
    @classmethod
    def validate_step_skills(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if not cleaned or any(not _SKILL_NAME.fullmatch(value) for value in cleaned):
            raise ValueError("plan step contains an invalid Skill name")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("plan step Skill names must be unique")
        return cleaned

    @field_validator("success_criteria")
    @classmethod
    def normalize_success_criteria(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values if value.strip()]
        if len(cleaned) != len(values):
            raise ValueError("success criteria must not be empty")
        return cleaned


class ExecutionPlan(BaseModel):
    """Structured Planner result constrained by the run's original authority."""

    model_config = {"extra": "forbid"}

    task: AgentTaskRequest
    created_by: AgentRole
    complexity: ComplexityLevel
    success_criteria: list[str] = Field(min_length=1, max_length=20)
    steps: list[PlanStep] = Field(min_length=1, max_length=20)

    @field_validator("success_criteria")
    @classmethod
    def validate_plan_success_criteria(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned):
            raise ValueError("success criteria must not be empty")
        return cleaned

    @model_validator(mode="after")
    def validate_plan_authority(self) -> "ExecutionPlan":
        if self.created_by is not AgentRole.planner:
            raise ValueError("execution plans must be created by the planner")
        step_ids = [step.step_id for step in self.steps]
        if len(set(step_ids)) != len(step_ids):
            raise ValueError("plan step_id values must be unique")
        permitted_skills = set(self.task.allowed_skills)
        for step in self.steps:
            forbidden = set(step.allowed_skills) - permitted_skills
            if forbidden:
                names = ", ".join(sorted(forbidden))
                raise ValueError(f"plan Skill is not allowed for this task: {names}")
        return self


class ToolObservation(BaseModel):
    """A validated tool result visible to an Agent's next autonomous turn."""

    model_config = {"extra": "forbid"}

    tool_name: str = Field(min_length=1, max_length=64)
    status: str
    output: dict[str, Any] | None = None
    error_code: str | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> "ToolObservation":
        if self.status == "succeeded" and self.output is None:
            raise ValueError("succeeded tool observations require output")
        if self.status == "failed" and not self.error_code:
            raise ValueError("failed tool observations require an error_code")
        if self.status not in {"succeeded", "failed"}:
            raise ValueError("tool observation status must be succeeded or failed")
        return self


class PlannerDecision(BaseModel):
    """One autonomous Planner turn: inspect context, plan, or ask the user."""

    model_config = {"extra": "forbid"}

    action: str
    tool_name: str | None = None
    tool_input: dict[str, Any] = Field(default_factory=dict)
    complexity: ComplexityLevel | None = None
    success_criteria: list[str] = Field(default_factory=list)
    steps: list[PlanStep] = Field(default_factory=list)
    user_question: str | None = None

    @model_validator(mode="after")
    def validate_action_shape(self) -> "PlannerDecision":
        if self.action == "call_tool":
            if not self.tool_name:
                raise ValueError("call_tool requires tool_name")
        elif self.action == "plan":
            if self.complexity is None or not self.success_criteria or not self.steps:
                raise ValueError("plan requires complexity, success_criteria and steps")
        elif self.action == "need_user":
            if not self.user_question or not self.user_question.strip():
                raise ValueError("need_user requires user_question")
        else:
            raise ValueError("unknown planner action")
        return self


class PlannerResult(BaseModel):
    """Planner outcome consumed by the runtime; a plan is mandatory on success."""

    model_config = {"extra": "forbid"}

    status: str
    plan: ExecutionPlan | None = None
    observations: list[ToolObservation] = Field(default_factory=list)
    user_question: str | None = None

    @model_validator(mode="after")
    def validate_result_shape(self) -> "PlannerResult":
        if self.status == "planned" and self.plan is None:
            raise ValueError("planned result requires a plan")
        if self.status == "needs_user" and not self.user_question:
            raise ValueError("needs_user result requires user_question")
        if self.status not in {"planned", "needs_user", "failed"}:
            raise ValueError("unknown planner result status")
        return self


class ExecutorDecision(BaseModel):
    """One Executor turn: choose a permitted tool, finish, or request input."""

    model_config = {"extra": "forbid"}

    action: str
    tool_name: str | None = None
    tool_input: dict[str, Any] = Field(default_factory=dict)
    summary: str | None = None
    artifact_refs: list[dict[str, Any]] = Field(default_factory=list)
    user_question: str | None = None

    @model_validator(mode="after")
    def validate_action_shape(self) -> "ExecutorDecision":
        if self.action == "call_tool" and not self.tool_name:
            raise ValueError("call_tool requires tool_name")
        if self.action == "complete" and not self.summary:
            raise ValueError("complete requires summary")
        if self.action == "need_user" and not self.user_question:
            raise ValueError("need_user requires user_question")
        if self.action not in {"call_tool", "complete", "need_user"}:
            raise ValueError("unknown executor action")
        return self


class ExecutorResult(BaseModel):
    """Step-level execution outcome and the observations that support it."""

    model_config = {"extra": "forbid"}

    status: str
    summary: str | None = None
    artifact_refs: list[dict[str, Any]] = Field(default_factory=list)
    observations: list[ToolObservation] = Field(default_factory=list)
    user_question: str | None = None

    @model_validator(mode="after")
    def validate_result_shape(self) -> "ExecutorResult":
        if self.status == "succeeded" and not self.summary:
            raise ValueError("succeeded executor result requires summary")
        if self.status == "needs_user" and not self.user_question:
            raise ValueError("needs_user executor result requires user_question")
        if self.status not in {"succeeded", "needs_user", "failed"}:
            raise ValueError("unknown executor result status")
        return self


class VerifierDecision(BaseModel):
    """One Verifier turn: inspect evidence or route a machine-actionable next step."""

    model_config = {"extra": "forbid"}

    action: str
    tool_name: str | None = None
    tool_input: dict[str, Any] = Field(default_factory=dict)
    verification_decision: VerificationDecision | None = None
    feedback: str | None = None

    @model_validator(mode="after")
    def validate_action_shape(self) -> "VerifierDecision":
        if self.action == "call_tool" and not self.tool_name:
            raise ValueError("call_tool requires tool_name")
        if self.action == "decide":
            if self.verification_decision is None:
                raise ValueError("decide requires verification_decision")
            if (
                self.verification_decision is not VerificationDecision.PASS
                and not self.feedback
            ):
                raise ValueError("non-PASS verifier decisions require feedback")
        if self.action not in {"call_tool", "decide"}:
            raise ValueError("unknown verifier action")
        return self


class VerifierResult(BaseModel):
    """Verified decision plus independent evidence observations."""

    model_config = {"extra": "forbid"}

    decision: VerificationDecision
    feedback: str | None = None
    observations: list[ToolObservation] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_result_shape(self) -> "VerifierResult":
        if self.decision is not VerificationDecision.PASS and not self.feedback:
            raise ValueError("non-PASS verifier result requires feedback")
        return self
